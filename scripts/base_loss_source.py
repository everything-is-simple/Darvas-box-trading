#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""亏损来源全景分解（只读回测库）。

回答四个问题：
  1. 大亏损由哪些单笔构成？（最差 20 笔明细 + 剔除后的账）
  2. 亏损在"出口 × 档位 × 年"三维上如何分布？哪个格子最大？
  3. 每个档位每年到底亏多少（t4 是否年年亏）？
  4. 单笔亏损的尾部有多长（是否"千刀万剐"型）？

用法：python scripts/base_loss_source.py [--db PATH] [--json OUT]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import date

DEFAULT_DB = "var/bt/base/bt_baseA_cont_adj.db"


def tier_of(units: float) -> str:
    for name, v in (("t1", 3.0), ("t2", 2.0), ("t3", 1.0), ("t4", 0.5)):
        if abs(units - v) <= 0.35:
            return name
    return "tx"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    c = con.cursor()

    eq = c.execute("select date, total, market_value, cash from equity_snapshot order by date").fetchall()
    ds = [r[0] for r in eq]

    import bisect

    def eq_at(d: str) -> float:
        i = bisect.bisect_right(ds, d) - 1
        return eq[i][1] if i >= 0 else 1_000_000.0

    legs = dict(c.execute(
        "select position_id, sum(qty*price) from position_leg group by position_id").fetchall())

    rows = c.execute(
        "select id, code, cast(opened_date as text), cast(closed_date as text), "
        "coalesce(realized_pnl,0), coalesce(close_reason,'') "
        "from position where status='closed'").fetchall()

    T = []
    for pid, code, o, cl, pnl, reason in rows:
        notional = legs.get(pid, 0.0)
        base = eq_at(o)
        units = notional / (base * 0.02) if base > 0 else 0.0
        hold = (date.fromisoformat(cl[:10]) - date.fromisoformat(o[:10])).days
        T.append({
            "code": code, "open": o[:10], "close": cl[:10], "pnl": pnl,
            "reason": reason, "tier": tier_of(units), "units": round(units, 2),
            "hold": hold, "year": int(cl[:4]), "equity": round(base, 0),
            "ret_pct": round(100 * pnl / notional, 2) if notional > 0 else None,
        })

    n = len(T)
    total = sum(t["pnl"] for t in T)
    print(f"=== 样本 {n} 笔  总账 {total:,.0f} ===")

    # ---------- 1. 最差 20 笔 ----------
    worst = sorted(T, key=lambda t: t["pnl"])[:20]
    print(f"\n=== 最差 20 笔（合计 {sum(t['pnl'] for t in worst):,.0f}）===")
    print(f"{'代码':<9}{'入场':<12}{'出场':<12}{'持天':>4}{'档':>4}{'单位':>6}"
          f"{'亏损额':>11}{'占入场权益%':>11}{'回报%':>8}  出口")
    for t in worst:
        print(f"{t['code']:<9}{t['open']:<12}{t['close']:<12}{t['hold']:>4}{t['tier']:>4}"
              f"{t['units']:>6.1f}{t['pnl']:>11,.0f}{100*t['pnl']/t['equity']:>11.2f}"
              f"{(t['ret_pct'] or 0):>8.1f}  {t['reason']}")

    # ---------- 2. 尾部形状 ----------
    losses = sorted([t["pnl"] for t in T if t["pnl"] < 0])
    wins = sorted([t["pnl"] for t in T if t["pnl"] > 0])
    print(f"\n=== 尾部形状 ===")
    print(f"亏损单 {len(losses)} 笔（{100*len(losses)/n:.1f}%），合计 {sum(losses):,.0f}，"
          f"均 {sum(losses)/len(losses):,.0f}，中位 {losses[len(losses)//2]:,.0f}")
    print(f"盈利单 {len(wins)} 笔（{100*len(wins)/n:.1f}%），合计 {sum(wins):,.0f}，"
          f"均 {sum(wins)/len(wins):,.0f}，中位 {wins[len(wins)//2]:,.0f}")
    for k in (1, 5, 10, 20, 50):
        s = sum(losses[:k])
        print(f"  最差 {k:>2} 笔合计 {s:>12,.0f}  = 总亏损的 {100*s/sum(losses):>5.1f}%")
    # 单笔亏损占入场权益的比例分布
    ratios = sorted(100 * t["pnl"] / t["equity"] for t in T if t["equity"] > 0)
    print(f"\n单笔盈亏/入场权益（%）：p1 {ratios[len(ratios)//100]:.2f}  "
          f"p5 {ratios[len(ratios)//20]:.2f}  中位 {ratios[len(ratios)//2]:.2f}  "
          f"p95 {ratios[19*len(ratios)//20]:.2f}  max {ratios[-1]:.2f}")

    # ---------- 3. 三维交叉：出口 × 档位 × 年 ----------
    print(f"\n=== 亏损格子排序（出口 × 档位，只有负值）===")
    cell = defaultdict(lambda: [0, 0.0])
    for t in T:
        cell[(t["reason"], t["tier"])][0] += 1
        cell[(t["reason"], t["tier"])][1] += t["pnl"]
    neg = sorted([(k, v) for k, v in cell.items() if v[1] < 0], key=lambda x: x[1][1])
    tot_loss = -sum(v[1] for v in cell.values() if v[1] < 0)
    print(f"{'格':<30}{'笔数':>6}{'合计':>13}{'均笔':>10}{'占总亏损':>10}")
    for (r, ti), (cnt, v) in neg:
        print(f"{r + ' × ' + ti:<30}{cnt:>6}{v:>13,.0f}{v/cnt:>10,.0f}{100*v/-tot_loss:>9.1f}%")

    # ---------- 4. 档位 × 年（t4 是否年年亏）----------
    print(f"\n=== 档位 × 年（已实现，括号笔数）===")
    years = sorted({t["year"] for t in T})
    print(f"{'档':<5}" + "".join(f"{y:>12}" for y in years))
    gy = defaultdict(lambda: [0, 0.0])
    for t in T:
        gy[(t["tier"], t["year"])][0] += 1
        gy[(t["tier"], t["year"])][1] += t["pnl"]
    for ti in ("t1", "t2", "t3", "t4"):
        line = f"{ti:<5}"
        for y in years:
            cnt, v = gy.get((ti, y), (0, 0.0))
            line += f"{v:>9,.0f}({cnt:>2})" if cnt else f"{'—':>12}"
        print(line)

    # ---------- 5. time_stop 按年 ----------
    print(f"\n=== time_stop 按年（失血主渠道）===")
    ts = defaultdict(lambda: [0, 0.0])
    for t in T:
        if t["reason"] == "time_stop":
            ts[t["year"]][0] += 1
            ts[t["year"]][1] += t["pnl"]
    for y in years:
        cnt, v = ts.get(y, (0, 0.0))
        if cnt:
            print(f"  {y}  {cnt:>4} 笔  {v:>12,.0f}  均 {v/cnt:>8,.0f}")

    # ---------- 6. stop 按年（盈利主渠道的稳定性）----------
    print(f"\n=== stop 按年（盈利主渠道）===")
    st = defaultdict(lambda: [0, 0.0])
    for t in T:
        if t["reason"] == "stop":
            st[t["year"]][0] += 1
            st[t["year"]][1] += t["pnl"]
    vals = []
    for y in years:
        cnt, v = st.get(y, (0, 0.0))
        if cnt:
            print(f"  {y}  {cnt:>4} 笔  {v:>12,.0f}  均 {v/cnt:>9,.0f}")
            vals.append(v / cnt)
    if vals:
        print(f"  → stop 均笔：最好 {max(vals):,.0f} / 最差 {min(vals):,.0f} / "
              f"倍数 {max(vals)/abs(min(vals)):.1f}x（最差为负则取绝对值）")

    # ---------- 7. 持有期 × 档位 ----------
    print(f"\n=== 持有期 × 档位 ===")
    bands = [("<=2", 0, 2), ("3-10", 3, 10), ("11-30", 11, 30), (">30", 31, 10**6)]
    print(f"{'持有':<8}" + "".join(f"{t:>14}" for t in ("t1", "t2", "t3", "t4")) + f"{'合计':>14}")
    for name, lo, hi in bands:
        sub = [t for t in T if lo <= t["hold"] <= hi]
        line = f"{name:<8}"
        for ti in ("t1", "t2", "t3", "t4"):
            s = [t for t in sub if t["tier"] == ti]
            line += f"{sum(t['pnl'] for t in s):>10,.0f}({len(s):>2})" if s else f"{'—':>14}"
        line += f"{sum(t['pnl'] for t in sub):>10,.0f}({len(sub):>3})"
        print(line)

    # ---------- 8. 如果删掉某一格，账怎么变 ----------
    print(f"\n=== 反事实：删掉某格后的总账（仅算术，不含槽位再利用）===")
    base_total = total
    for (r, ti), (cnt, v) in sorted(neg, key=lambda x: x[1][1])[:8]:
        print(f"  删 {r} × {ti:<3} → 总账 {base_total - v:>12,.0f}  (+{-v:,.0f}，删 {cnt} 笔)")
    for ti in ("t1", "t2", "t3", "t4"):
        v = sum(t["pnl"] for t in T if t["tier"] == ti)
        cnt = len([t for t in T if t["tier"] == ti])
        print(f"  删整档 {ti:<3} → 总账 {base_total - v:>12,.0f}  (+{-v:,.0f}，删 {cnt} 笔，"
              f"释放 {100*cnt/n:.1f}% 槽位)")

    if args.json:
        out = {
            "n": n, "total": total, "worst20": worst,
            "cells_negative": [{"reason": k[0], "tier": k[1], "n": v[0], "pnl": round(v[1], 0)}
                               for k, v in neg],
            "tier_year": {f"{k[0]}|{k[1]}": {"n": v[0], "pnl": round(v[1], 0)} for k, v in gy.items()},
            "stop_by_year": {str(y): {"n": v[0], "pnl": round(v[1], 0)} for y, v in st.items()},
            "timestop_by_year": {str(y): {"n": v[0], "pnl": round(v[1], 0)} for y, v in ts.items()},
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\nJSON → {args.json}")


if __name__ == "__main__":
    main()
