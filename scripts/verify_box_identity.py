#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""箱体身份链验收（docs/49 §五之二 特征一；migration 0028 + 0029）。

用法：
    python scripts/verify_box_identity.py var/bt/verify_0029_2005.db

背景：0028 加 dead_date，0029 把 broken_date 收紧为「只表向上突破」并把 UpsertBoxes 的
身份键由 (top,bottom) 升级为 (top,bottom,bottom_confirmed)。本脚本在真实回测库上逐条断言，
不靠"代码看起来对"来判定。

判据（全绿 = PASS）：
  V1 broken_date 只出现在 broken_up
  V2 broken_down / invalid 必须有 dead_date
  V3 broken_up 不得有 dead_date
  V4 dead_date 不得早于 bottom_confirmed，也不得早于 broken_date
  V5 position.entry_box_id 100% 命中存在的箱体、且同一只股票
  V6 买入箱体的 status 必须是 broken_up
  V7 position.opened_date 不得早于箱体 broken_date
  V8 同 (code, top, bottom) 且下轨确认日也相同，却出现多行（身份键未生效）
      注：invalid 箱体的身份键另含 dead_date（作废箱下轨未确认时身份会退化到只剩 top），
      故同 top 不同作废日的作废箱各占一行属正常，不算 V8 违规。

退出码：0 = 全部通过；1 = 有判据失败。
"""
import sqlite3
import sys
from pathlib import Path

FAIL = []


def check(name, got, want, detail=""):
    ok = got == want
    if not ok:
        FAIL.append(name)
    print(f"  {'✓' if ok else '✗'} {name}: {got}" + (f"  (期望 {want})" if not ok else "") + (f"  {detail}" if detail else ""))
    return ok


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/verify_box_identity.py <回测库路径>")
        return 2
    db_path = Path(sys.argv[1])
    if not db_path.exists():
        print(f"库不存在：{db_path}")
        return 2

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c = con.cursor()

    cols = [r[1] for r in c.execute("PRAGMA table_info(box)")]
    if "dead_date" not in cols:
        print("✗ box 表无 dead_date 列 —— schema 未到 0028")
        return 1

    total = c.execute("SELECT COUNT(*) FROM box").fetchone()[0]
    pos_total = c.execute("SELECT COUNT(*) FROM position").fetchone()[0]
    print(f"库: {db_path.name}   箱体 {total:,}   持仓 {pos_total:,}")
    print()

    print("[语义分离]")
    check("V1 broken_date 非空且 status≠broken_up 的箱体",
          c.execute("SELECT COUNT(*) FROM box WHERE broken_date IS NOT NULL AND status<>'broken_up'").fetchone()[0], 0)
    check("V2 broken_down 缺 dead_date",
          c.execute("SELECT COUNT(*) FROM box WHERE status='broken_down' AND dead_date IS NULL").fetchone()[0], 0)
    check("V2b invalid 缺 dead_date",
          c.execute("SELECT COUNT(*) FROM box WHERE status='invalid' AND dead_date IS NULL").fetchone()[0], 0)
    check("V3 broken_up 却有 dead_date",
          c.execute("SELECT COUNT(*) FROM box WHERE status='broken_up' AND dead_date IS NOT NULL").fetchone()[0], 0)
    check("V4a dead_date < bottom_confirmed",
          c.execute("SELECT COUNT(*) FROM box WHERE dead_date IS NOT NULL AND bottom_confirmed IS NOT NULL "
                    "AND dead_date < bottom_confirmed").fetchone()[0], 0)
    check("V4b dead_date < broken_date",
          c.execute("SELECT COUNT(*) FROM box WHERE dead_date IS NOT NULL AND broken_date IS NOT NULL "
                    "AND dead_date < broken_date").fetchone()[0], 0)

    print()
    print("[持仓 ↔ 箱体]")
    check("V5a entry_box_id 指向不存在的箱体",
          c.execute("SELECT COUNT(*) FROM position p LEFT JOIN box b ON b.id=p.entry_box_id "
                    "WHERE p.entry_box_id IS NOT NULL AND b.id IS NULL").fetchone()[0], 0)
    check("V5b entry_box 与持仓跨股票错配",
          c.execute("SELECT COUNT(*) FROM position p JOIN box b ON b.id=p.entry_box_id "
                    "WHERE p.code<>b.code").fetchone()[0], 0)
    check("V6 买入箱体 status≠broken_up",
          c.execute("SELECT COUNT(*) FROM position p JOIN box b ON b.id=p.entry_box_id "
                    "WHERE b.status<>'broken_up'").fetchone()[0], 0)
    check("V7 opened_date 早于买入箱体 broken_date",
          c.execute("SELECT COUNT(*) FROM position p JOIN box b ON b.id=p.entry_box_id "
                    "WHERE b.broken_date IS NOT NULL AND p.opened_date < b.broken_date").fetchone()[0], 0)
    check("V7b 买入箱体 broken_date 为空",
          c.execute("SELECT COUNT(*) FROM position p JOIN box b ON b.id=p.entry_box_id "
                    "WHERE b.broken_date IS NULL").fetchone()[0], 0)

    print()
    print("[身份稳定]")
    # 身份键：top|bottom|bottom_confirmed|（invalid 追加 dead_date），与 repo_box.boxKey 一致。
    dup_key = c.execute("""SELECT COUNT(*) FROM (
        SELECT code, ROUND(top,6), ROUND(bottom,6),
               COALESCE(CAST(bottom_confirmed AS TEXT),'') bc,
               CASE WHEN status='invalid' THEN COALESCE(CAST(dead_date AS TEXT),'') ELSE '' END dd
        FROM box
        GROUP BY code, ROUND(top,6), ROUND(bottom,6),
                 COALESCE(CAST(bottom_confirmed AS TEXT),''),
                 CASE WHEN status='invalid' THEN COALESCE(CAST(dead_date AS TEXT),'') ELSE '' END
        HAVING COUNT(*) > 1)""").fetchone()[0]
    # 同价多命：同一只股票 top/bottom 相同但下轨确认日不同 → 不同生命周期，应各占一行
    multi_life = c.execute("""SELECT COUNT(*) FROM (
        SELECT code, ROUND(top,6), ROUND(bottom,6)
        FROM box GROUP BY code, ROUND(top,6), ROUND(bottom,6)
        HAVING COUNT(DISTINCT COALESCE(bottom_confirmed,'')) > 1)""").fetchone()[0]
    # 作废箱同 top 不同作废日：合法多行（身份键含 dead_date），仅作信息展示
    invalid_multi = c.execute("""SELECT COUNT(*) FROM (
        SELECT code, ROUND(top,6), ROUND(bottom,6)
        FROM box WHERE status='invalid'
        GROUP BY code, ROUND(top,6), ROUND(bottom,6)
        HAVING COUNT(*) > 1)""").fetchone()[0]
    print(f"  · 同价多命（top/bottom 同、下轨确认日不同）的组数: {multi_life}")
    print(f"  · 作废箱同 top 多行（作废日不同，合法）: {invalid_multi}")
    check("V8 真实身份键重复（同 code+top+bottom+下轨确认日+作废日）", dup_key, 0)

    print()
    print("[统计]")
    for r in c.execute("""SELECT status, COUNT(*) n, SUM(dead_date IS NOT NULL) dead,
                          SUM(broken_date IS NOT NULL) brk FROM box GROUP BY status ORDER BY n DESC"""):
        print(f"  {r[0]:<13} 共{r[1]:>7}  dead_date{r[2]:>7}  broken_date{r[3]:>7}")
    if pos_total:
        n = c.execute("SELECT COUNT(*) FROM position WHERE entry_box_id IS NOT NULL").fetchone()[0]
        print(f"  entry_box_id 非空: {n}/{pos_total} ({n/pos_total*100:.1f}%)")

    print()
    if FAIL:
        print(f"结果: FAIL（{len(FAIL)} 项未通过：{', '.join(FAIL)}）")
        return 1
    print("结果: 全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
