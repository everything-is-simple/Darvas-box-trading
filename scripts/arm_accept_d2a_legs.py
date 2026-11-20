#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2a 腿数机制（`reason["leg"]` 按持仓腿数 +1）结构／时序只读验收 CLI。

定位：`67 §4.1` S3 阶段 3 装油门的第二个子步，判据与验收物见 `67 §8.7`
起跑前预注册验收 CLI 清单（D2a 行）、`65 §5` 施工表第 4 行
（`internal/planner/planner.go:789` `reason["leg"]` 恒 1 → 按持仓腿数 +1；
`MaxLegs` 只立机制）。适用对象默认为冻结的段 A 对照底座
`var/bt/arms/bt_clean_default_0033.db`，可用 `--arm` 指到任一 scratch 臂库。

判据（结构／时序，不含数值尺度）：
  C1 `reason["leg"]` 不再恒 1       —— buy 单 `plan_order.reason_json.leg` 取值含 ≥2
  C2 按持仓腿数 +1                  —— 每个多腿持仓内腿号序列 1..n 连续，
                                     且 `reason_json.leg == plan_order.leg_no`
  C3 逐腿对账真实账本               —— filled 买单必有 `trade_fill`；
                                     `position_leg` 必有对应 `plan_order` 且腿号一致

**时点归因铁律**：只用逐日快照 `plan_order.reason_json` 与账本表 `trade_fill`／
`position_leg`／`plan_order`，**不读最终态 `box` 表反查**。
库内事实：`trade_fill` 无 leg 列，腿号一律经 `plan_order.leg_no` 取得。

账本恒等式（`doc_anchors.compute()["values"]["identity_gap"]`）只作**账本体检**输出，
不列为本 CLI 判据（D2a 行判据文字不含恒等式；恒等式是 D1 判据）。

本 CLI 只逐条记录事实，不作完成线／臂验收裁定，不冻结账本、不回填看板、不改文档。
默认退出码 0 表示 CLI 跑通；`--strict` 时任一结构判据未过返回 2。
对 0033 冻结对照底座跑 = 结构基线冒烟（`reason["leg"]` 恒 1，C1／C2 必未过），**非臂验收**。

用法：
  python scripts/arm_accept_d2a_legs.py
  python scripts/arm_accept_d2a_legs.py --arm var/bt/arms/bt_clean_default_0033.db
  python scripts/arm_accept_d2a_legs.py --out var/bt/arms/accept_0033_d2a_legs.md
  python scripts/arm_accept_d2a_legs.py --strict
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from doc_anchors import compute  # noqa: E402

ARM = "var/bt/arms/bt_clean_default_0033.db"


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).resolve().as_posix(),
                           uri=True, timeout=30)


def main():
    ap = argparse.ArgumentParser(description="D2a 腿数机制只读验收 CLI")
    ap.add_argument("--arm", default=ARM, help="臂库路径（默认冻结的 0033 对照底座）")
    ap.add_argument("--out", default="", help="可选：同时把报告写入 md/txt 文件")
    ap.add_argument("--strict", action="store_true",
                    help="任一结构判据未过时返回退出码 2（默认只报告、总返回 0）")
    a = ap.parse_args()

    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    checks = []

    def check(name, ok, detail):
        checks.append((name, bool(ok), detail))
        emit("  %s %-34s %s" % ("✅" if ok else "❌", name, detail))

    c = ro(a.arm)
    sha = c.execute("select engine_sha256 from baseline limit 1").fetchone()
    sha = sha[0] if sha else "(缺)"
    non_ok = c.execute("select count(*) from daily_run where status <> 'ok'").fetchone()[0]

    emit("=" * 100)
    emit("D2a 腿数机制 · 只读验收（`67 §4.1` S3 / `65 §5` 施工表第 4 行 / `67 §8.7` D2a 行）")
    emit("时间 %s | 臂库 %s | 引擎 sha %s" %
         (datetime.now().isoformat(timespec="seconds"), a.arm, sha[:16]))
    emit("daily_run 非 ok = %d | 只读 mode=ro | 不读最终态 box 表" % non_ok)
    emit("=" * 100)

    orders = c.execute("""
        select id, code, leg_no, status, reason_json
        from plan_order where side = 'buy' order by id
    """).fetchall()
    leg_rows = c.execute("""
        select position_id, leg_no, plan_order_id, date, price
        from position_leg order by position_id, leg_no
    """).fetchall()

    dist = Counter()
    rj_bad = 0
    for oid, _code, leg_no, _status, rj in orders:
        try:
            raw = json.loads(rj or "{}").get("leg")
            val = int(raw) if raw is not None else None
        except Exception:
            val = None
        dist[val] += 1
        if val != leg_no:
            rj_bad += 1

    emit("\n## 账本规模")
    emit("  buy 单 %d 笔｜`reason_json.leg` 分布 %s" %
         (len(orders), dict(sorted(dist.items(), key=lambda kv: (kv[0] is None, kv[0])))))

    # ---------- C1 ----------
    emit("\n## C1 `reason[\"leg\"]` 不再恒 1")
    max_leg = max((v for v in dist if v is not None), default=0)
    check("C1 出现 leg≥2", max_leg >= 2,
          "最大腿号 = %s；取值种类 = %d（0033 为装油门前底座，leg 恒 1；装油门后按持仓腿数 +1）"
          % (max_leg if max_leg else "(无)", len(dist)))

    # ---------- C2 ----------
    groups = {}
    for pid, leg_no, poid, _date, _price in leg_rows:
        groups.setdefault(pid, []).append((leg_no, poid))
    multi = bad_seq = 0
    for _pid, seq in groups.items():
        if len(seq) < 2:
            continue
        multi += 1
        nums = sorted(x[0] for x in seq)
        if nums != list(range(1, len(nums) + 1)):
            bad_seq += 1
    emit("\n## C2 按持仓腿数 +1")
    check("C2 多腿持仓序列连续", multi > 0 and bad_seq == 0 and rj_bad == 0,
          "多腿持仓 %d 个；序列非 1..n 连续 %d 个；`reason.leg` 与 `plan_order.leg_no`"
          " 不一致 %d 笔" % (multi, bad_seq, rj_bad))
    emit("     「按持仓腿数 +1」= 新腿号取当前 `position_leg` 条数 +1；`MaxLegs` 只立机制、")
    emit("     不在此判数值上限（`65 §5` 施工表第 4 行）。")

    # ---------- C3 ----------
    emit("\n## C3 逐腿对账真实账本")
    filled = [r for r in orders if r[3] == "filled"]
    filled_without_fill = c.execute("""
        select count(*) from plan_order p
        where p.side = 'buy' and p.status = 'filled'
          and not exists (select 1 from trade_fill f where f.plan_order_id = p.id)
    """).fetchone()[0]
    order_by_id = {r[0]: r for r in orders}
    orphan = mismatch = 0
    for _pid, leg_no, poid, _date, _price in leg_rows:
        o = order_by_id.get(poid)
        if o is None:
            orphan += 1
        elif o[2] != leg_no:
            mismatch += 1
    check("C3 逐腿账本自洽", len(filled) > 0 and filled_without_fill == 0
          and orphan == 0 and mismatch == 0,
          "filled 买单 %d 笔；无 `trade_fill` %d 笔；`position_leg` 无对应订单 %d 行；"
          "腿号不一致 %d 行" % (len(filled), filled_without_fill, orphan, mismatch))
    emit("     ⚠ C3 只验账本自洽，不证明「已按腿数 +1」；后者由 C1／C2 判定。")

    # ---------- 账本体检（非判据） ----------
    emit("\n## 账本体检（非 D2a 判据）")
    try:
        gap = float(compute(a.arm)["values"]["identity_gap"])
        emit("  恒等式 `identity_gap` = %.2f（`doc_anchors.py:205` 口径；D1 判据另验）" % gap)
    except Exception as e:
        emit("  恒等式重算失败：%s" % e)

    # ---------- 判定 ----------
    ok_all = all(ok for _n, ok, _d in checks)
    emit("\n## 判定")
    emit("  D2a 结构判据：%s" % ("✅ 全过" if ok_all else "❌ 有未过项"))
    emit("  ⚠ 本 CLI 不裁定臂验收。对冻结的 0033 对照底座跑时，`reason[\"leg\"]` 恒 1，")
    emit("     C1／C2 未过属**预期**，只证明脚本能读真实账本、正确输出判据。")
    emit("     `67 §8.7` 契约：阶段 3 起跑前预注册；未跑通真实臂库前不得宣布 D2a 已验收。")
    emit("\n  只读（mode=ro）、不冻结账本、不回填 `67 §8.7` 看板、不改文档、不替裁。")
    c.close()

    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n报告已写入 %s" % out.as_posix())

    return 2 if a.strict and not ok_all else 0


if __name__ == "__main__":
    raise SystemExit(main())
