#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B-4 HARD① 独立校验器：变体① 是否复现历史基线库。

用法：
  python scripts/p4_verify.py --year 2007                 # 默认比对 v1
  python scripts/p4_verify.py --year 2007 --variant 2

输出三类判定（框架 §6.2）：
  A 完全复现        —— 逐笔全等
  B 不完全复现      —— 差异存在，但可归因到已登记 commit（挂单预占/预算/0018）
  C 差异无法归因    —— 出现与上述无关的差异

本脚本只读两个库，不写任何东西；判定依赖的是**结构性证据**，不是主观解释：
  · 参数表逐项差异（语义层）
  · 笔数/金额/逐笔三元组（账层）
  · close_reason 分布与持有天数分布（行为层，用于归因）
"""
import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
P4 = BT / "p4"

# 允许归因到已登记 commit 的参数键（框架 §6.2 B 类判据）
ATTRIBUTABLE_KEYS = {
    "pool.max_hold",          # caac49f / 0018 废止只数上限
    "pos.equity_gate",        # 0018 新增
    "pos.equity_limit",       # 0018 新增
    "pos.single_limit",       # 0018 新增
}


def q(db, sql, args=()):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def load(db):
    """取出对账所需的全部结构。"""
    rows = q(db, """
        SELECT p.code, p.opened_date, p.closed_date, p.realized_pnl,
               p.close_reason, p.avg_cost
        FROM position p WHERE p.status='closed' ORDER BY p.code, p.opened_date
    """)
    params = dict(q(db, "SELECT key, value FROM param"))
    return rows, params


def hold_bars(db, rows):
    """按库内行情算持有交易日数（BarCount 口径：不含开仓日）。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = []
        for code, od, cd, pnl, cr, cost in rows:
            n = con.execute(
                "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>? AND date<=?",
                (code, od, cd)).fetchone()[0]
            out.append(n)
        return out
    finally:
        con.close()


def cmp_params(a, b):
    keys = sorted(set(a) | set(b))
    diff = []
    for k in keys:
        if a.get(k) != b.get(k):
            diff.append((k, a.get(k), b.get(k),
                         "可归因" if k in ATTRIBUTABLE_KEYS else "**不可归因**"))
    return diff


def compare(ref_rows, cur_rows, tol=0.01):
    """逐笔三元组对账。返回 (n_ref, n_cur, 差异明细)。"""
    key = lambda r: (r[0], r[1], r[2])
    dr = {key(r): r for r in ref_rows}
    dc = {key(r): r for r in cur_rows}
    only_ref = [k for k in dr if k not in dc]
    only_cur = [k for k in dc if k not in dr]
    pnl_diff = [(k, dr[k][3], dc[k][3]) for k in dr
                if k in dc and abs((dr[k][3] or 0) - (dc[k][3] or 0)) > tol]
    return only_ref, only_cur, pnl_diff


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2007)
    ap.add_argument("--variant", type=int, default=1)
    ap.add_argument("--show", type=int, default=8, help="每类差异最多展示条数")
    a = ap.parse_args()

    ref_db = BT / f"bt_e17_{a.year}.db"
    cur_db = P4 / f"p4_v{a.variant}_{a.year}.db"
    for p in (ref_db, cur_db):
        if not p.exists():
            raise SystemExit(f"缺库：{p}")

    ref, ref_p = load(ref_db)
    cur, cur_p = load(cur_db)
    ref_sum = sum(r[3] or 0 for r in ref)
    cur_sum = sum(r[3] or 0 for r in cur)

    print("=" * 78)
    print(f"HARD① 对账：p4_v{a.variant}_{a.year}.db  vs  bt_e17_{a.year}.db")
    print("=" * 78)
    print(f"{'':22}{'历史基线':>18}{'变体①':>18}")
    print(f"{'已平仓笔数':<22}{len(ref):>18}{len(cur):>18}")
    print(f"{'Σrealized_pnl':<22}{ref_sum:>18,.2f}{cur_sum:>18,.2f}")
    print(f"{'差额':<22}{'':>18}{cur_sum - ref_sum:>18,.2f}")

    print("\n--- 参数差异（语义层）---")
    pdiff = cmp_params(ref_p, cur_p)
    if not pdiff:
        print("  无差异")
    hard = []
    for k, va, vb, tag in pdiff:
        print(f"  {k:<24} 历史={va!s:<10} 变体={vb!s:<10} {tag}")
        if "不可归因" in tag:
            hard.append(k)

    only_ref, only_cur, pnl_diff = compare(ref, cur)
    print("\n--- 逐笔对账（账层）---")
    print(f"  仅历史有：{len(only_ref)} 笔    仅变体有：{len(only_cur)} 笔    "
          f"同键但金额不同：{len(pnl_diff)} 笔")
    for tag, lst in (("仅历史有", only_ref), ("仅变体有", only_cur)):
        for k in lst[:a.show]:
            print(f"    {tag}: {k}")
        if len(lst) > a.show:
            print(f"    … 另 {len(lst) - a.show} 笔")
    for k, va, vb in pnl_diff[:a.show]:
        print(f"    金额差: {k}  历史={va:,.2f} 变体={vb:,.2f} 差={vb - va:+,.2f}")
    if len(pnl_diff) > a.show:
        print(f"    … 另 {len(pnl_diff) - a.show} 笔")

    print("\n--- 行为层（归因用）---")
    rc_ref = Counter((r[4] or "?") for r in ref)
    rc_cur = Counter((r[4] or "?") for r in cur)
    print(f"  {'close_reason':<24}{'历史':>10}{'变体':>10}{'差':>10}")
    for k in sorted(set(rc_ref) | set(rc_cur)):
        print(f"  {k:<24}{rc_ref.get(k, 0):>10}{rc_cur.get(k, 0):>10}"
              f"{rc_cur.get(k, 0) - rc_ref.get(k, 0):>+10}")

    h_ref = hold_bars(ref_db, ref)
    h_cur = hold_bars(cur_db, cur)
    if len(h_ref) == len(h_cur):
        same = sum(1 for x, y in zip(sorted(h_ref), sorted(h_cur)) if x == y)
        print(f"\n  持有天数分布一致率（排序后逐位）：{same}/{len(h_ref)} "
              f"= {same / len(h_ref) * 100:.2f}%")
        print(f"  历史中位 {sorted(h_ref)[len(h_ref) // 2]}  变体中位 "
              f"{sorted(h_cur)[len(h_cur) // 2]}")

    exact = (len(ref) == len(cur) and not only_ref and not only_cur and not pnl_diff)
    print("\n" + "=" * 78)
    if exact and not hard:
        print("判定：A —— 逐笔完全复现。① 可作为对照锚，且可与 14 年历史库直接横比。")
        rc = 0
    elif hard:
        print(f"判定：C —— 出现**不可归因**的参数差异：{hard}")
        print("      整个实验作废，先查二进制/参数/迁移，不得继续。")
        rc = 2
    else:
        print("判定：B —— 不完全复现，但差异限于已登记 commit（挂单预占 / 预算 / 0018）。")
        print("      ① 降级为「新二进制下的旧语义锚」：①②③ 之间仍完全可比，")
        print("      但 ① 的绝对值**不得与 14 年历史库横比**，报告须显著标注。")
        rc = 1
    print("=" * 78)
    return rc


if __name__ == "__main__":
    sys.exit(main())
