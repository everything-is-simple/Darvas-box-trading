#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E2 执行层审计（docs/19 E2 + docs/18 附录A 口径）——P0-5 每轮必跑。

用法：python scripts/audit_e2.py <db_path> [<db_path> ...]

检查项：
  1. 跳空纪律：所有买腿成交价 <= 计划买点 x match.max_entry_gap（读库内参数，默认 1.05）。
     R0 病灶：71 笔买腿 63.4% 偏离 >5%、中位 +7.35%（docs/18）。
  2. 卖出无持仓：卖腿成交必须挂在有效持仓上（回放级审计由 rc=0 证明，此处再做 SQL 独立复核）。
  3. 盈利集中度：top2 平仓盈利占净利比例（R0 = 171%，全部利润来自 2 笔跳空追高）。
     S1 若成立，此比例必须显著回落——否则「正期望」仍是执行缺陷的副产品。

输出：每个库一段人类可读报告 + 汇总行；任一检查项失败时进程退出码为 1。
"""
import sqlite3
import statistics as st
import sys


def one(con, sql, *args):
    r = con.execute(sql, args).fetchone()
    return r[0] if r else None


def audit(path):
    print(f"=== {path} ===")
    ok = True
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        gate = float(one(con, "SELECT value FROM param WHERE key='match.max_entry_gap'") or 1.05)

        # 1) buy-leg gap discipline
        legs = con.execute(
            "SELECT l.date, l.price / o.trigger_price - 1 "
            "FROM position_leg l JOIN plan_order o ON o.id = l.plan_order_id "
            "WHERE o.side = 'buy'").fetchall()
        v = [x[1] for x in legs]
        over = [x for x in v if x > gate - 1 + 1e-9]
        if v:
            print(f"[1] buy legs: n={len(v)} median={st.median(v)*100:+.2f}% "
                  f"max={max(v)*100:+.2f}% over_gate={len(over)}")
        else:
            print("[1] buy legs: n=0")
        if over:
            ok = False
            for d, dev in [(x[0], x[1]) for x in legs if x[1] > gate - 1 + 1e-9][:10]:
                print(f"    VIOLATION {d} dev={dev*100:+.2f}%")

        # 2) sell legs must sit on a real position
        orphans = one(con,
                      "SELECT COUNT(*) FROM position_leg l JOIN plan_order o ON o.id = l.plan_order_id "
                      "WHERE o.side = 'sell' AND l.position_id NOT IN (SELECT id FROM position)")
        print(f"[2] sell legs without position: {orphans}")
        if orphans:
            ok = False

        # 3) profit concentration
        pnls = con.execute(
            "SELECT code, opened_date, closed_date, realized_pnl FROM position "
            "WHERE status = 'closed' ORDER BY realized_pnl DESC").fetchall()
        net = sum(p[3] for p in pnls)
        top2 = sum(p[3] for p in pnls[:2])
        share = (top2 / net * 100) if net else 0.0
        print(f"[3] closed={len(pnls)} net_pnl={net:.0f} top2={top2:.0f} share={share:.1f}%")
        for p in pnls[:3]:
            print(f"    top: {p[0]} {p[1]}->{p[2]} pnl={p[3]:.0f}")
        print(f"=== verdict: {'PASS' if ok else 'FAIL'} ===")
        return ok
    finally:
        con.close()


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: audit_e2.py <db_path> [<db_path> ...]")
    all_ok = all(audit(p) for p in sys.argv[1:])
    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
