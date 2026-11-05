#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""base_ladder_band.py —— 用新基线已跑数据重算 §11 关键分带

零回测（只读库）。产出：
  1. 阶梯四档每单位净利 + 出口×档位交叉表（§11.1 / §11.8 核心）
  2. 首次转正日分带（§11.2 悬崖表，adj 口径）
  3. 利润集中度（§11.3）
  4. 持仓并发分桶（§11.4）

用法：python scripts/base_ladder_band.py [--db ...]
"""
from __future__ import annotations
import argparse, sqlite3, statistics as st
from datetime import date
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "var" / "bt" / "base" / "bt_baseA_cont_adj.db"


def band_unit(u: float) -> str:
    """反算单位数 → 档位标签。

    ⚠️ 2026-11-05 修正（docs/55 §16.2）：原实现用硬边界 `u>=0.5 → t3`，
    而 units 是连续量（p25=0.49/p50=0.98/p75=2.50），硬边界会把
    u∈[0.5,0.65) 的 0.5 档错划进 t3（实测 101 笔，足以让 t3/t4 结论翻符号）。
    现改为「最近档 ±0.35 容差 + 未归类桶 tx」，与 base_loss_source.py 完全一致。
    """
    for name, v in (("t1(3)", 3.0), ("t2(2)", 2.0), ("t3(1)", 1.0), ("t4(0.5)", 0.5)):
        if abs(u - v) <= 0.35:
            return name
    return "tx"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT))
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    c = con.cursor()

    # 1. 平仓交易
    pos = c.execute(
        "select id, code, cast(opened_date as text), cast(closed_date as text), "
        "coalesce(close_reason,''), coalesce(realized_pnl,0) from position where status='closed'"
    ).fetchall()

    # entry 日权益
    eq_map = {r[0]: r[1] for r in c.execute("select date, total from equity_snapshot").fetchall()}

    # 首腿 notional
    leg_notional = {}
    for pid, qty, price in c.execute("select position_id, qty, price from position_leg where leg_no=1").fetchall():
        leg_notional[pid] = qty * price

    rows = []
    for pid, code, od, cd, reason, pnl in pos:
        notional = leg_notional.get(pid, 0)
        eq = eq_map.get(od[:10], 0)
        units = notional / (eq * 0.02) if (eq and eq > 0) else 0
        rows.append({"pid": pid, "code": code, "open": od, "close": cd,
                     "reason": reason, "pnl": float(pnl), "units": units,
                     "band": band_unit(units)})

    # 2. 阶梯四档
    print(f"\n=== 阶梯四档（n={len(rows)}，按首腿反算单位数归档）===")
    bands = ["t1(3)", "t2(2)", "t3(1)", "t4(0.5)"]
    print(f"{'档':<10}{'n':>5}{'占比':>7}{'胜率':>7}{'合计':>12}{'均笔':>9}{'每单位净利':>12}{'剔前3':>11}{'PF':>7}")
    band_data = {}
    for b in bands:
        sub = [r for r in rows if r["band"] == b]
        if not sub: continue
        pnls = sorted(r["pnl"] for r in sub)
        n = len(pnls); w = len([p for p in pnls if p > 0])
        gw = sum(p for p in pnls if p > 0); gl = -sum(p for p in pnls if p <= 0)
        u = float(b.split("(")[1].rstrip(")"))
        excl3 = sum(pnls[:-3]) if n > 3 else 0.0
        band_data[b] = {"n": n, "rows": sub, "unit": u}
        print(f"{b:<10}{n:>5}{100*n/len(rows):>6.1f}%{100*w/n:>6.1f}%{sum(pnls):>12,.0f}"
              f"{sum(pnls)/n:>9,.0f}{sum(pnls)/n/u:>12,.0f}{excl3:>11,.0f}{(gw/gl if gl else float('inf')):>7.2f}")

    # 3. 出口×档位交叉表
    print(f"\n=== 出口 × 档位交叉表（合计盈亏，括号内笔数）===")
    reasons = sorted({r["reason"] for r in rows})
    hdr = "出口\\档".ljust(18) + "".join(b.rjust(18) for b in bands) + "合计".rjust(20)
    print(hdr)
    for rs in reasons:
        line = rs.ljust(18)
        tot = 0; totn = 0
        for b in bands:
            sub = [r for r in rows if r["reason"] == rs and r["band"] == b]
            s = sum(r["pnl"] for r in sub)
            line += f"{s:>12,.0f}({len(sub):>3})".rjust(18)
            tot += s; totn += len(sub)
        line += f"{tot:>14,.0f}({totn:>4})".rjust(20)
        print(line)

    # 4. 首次转正日分带（§11.2 悬崖）
    print(f"\n=== 首次转正日分带（adj 口径，前瞻用 daily_bar_adj）===")
    # 入场价 = 首腿 price；后续每日 close 与之比
    entry_price = {pid: price for pid, price in
                   c.execute("select position_id, price from position_leg where leg_no=1").fetchall()}
    # daily_bar_adj: code, date, adj_close
    code_dates = {}
    for code, d, close in c.execute("select code, date, adj_close from daily_bar_adj").fetchall():
        code_dates.setdefault(code, {})[d] = close

    def first_positive_day(code, open_d, entry_p):
        cds = code_dates.get(code, {})
        d0 = date.fromisoformat(open_d[:10])
        # 找入场后 1..20 交易日
        seq = sorted([d for d in cds if d > open_d[:10]])
        for i, d in enumerate(seq[:20], 1):
            cp = cds.get(d)
            if cp and cp > entry_p:
                return i
        return 99  # 20 日内未转正

    buckets = {"1": [], "2": [], "3": [], "4-5": [], "6-10": [], "never": []}
    for r in rows:
        ep = entry_price.get(r["pid"])
        if not ep: continue
        d = first_positive_day(r["code"], r["open"], ep)
        if d == 1: buckets["1"].append(r["pnl"])
        elif d == 2: buckets["2"].append(r["pnl"])
        elif d == 3: buckets["3"].append(r["pnl"])
        elif d in (4, 5): buckets["4-5"].append(r["pnl"])
        elif 6 <= d <= 10: buckets["6-10"].append(r["pnl"])
        else: buckets["never"].append(r["pnl"])

    print(f"{'首次转正日':<14}{'n':>5}{'占比':>7}{'胜率':>7}{'均笔':>9}{'合计':>12}{'剔前3':>11}")
    for k in ["1", "2", "3", "4-5", "6-10", "never"]:
        ps = sorted(buckets[k])
        if not ps:
            print(f"{k:<14}{0:>5}"); continue
        n = len(ps); w = len([p for p in ps if p > 0])
        excl3 = sum(ps[:-3]) if n > 3 else 0.0
        print(f"{'第'+k+'日' if k not in ('never',) else '20日内未转正':<14}{n:>5}{100*n/len(rows):>6.1f}%"
              f"{100*w/n:>6.1f}%{sum(ps)/n:>9,.0f}{sum(ps):>12,.0f}{excl3:>11,.0f}")
    early = sum(buckets["1"]) + sum(buckets["2"])
    late = sum(buckets["3"]) + sum(buckets["4-5"]) + sum(buckets["6-10"]) + sum(buckets["never"])
    print(f"\n第1-2日转正: {early:,.0f}   第3日及以后: {late:,.0f}   差额 {early-late:,.0f}")

    # 5. 利润集中度
    print(f"\n=== 利润集中度 ===")
    allp = sorted(r["pnl"] for r in rows)
    n = len(allp); tot = sum(allp)
    for k in [1, 3, 5, 10, 50]:
        s = sum(allp[-k:])
        print(f"前 {k:>2} 笔: {s:>12,.0f}  占总账 {100*s/tot:>6.1f}%")
    print(f"最差 10 笔: {sum(allp[:10]):>12,.0f}")

    con.close()


if __name__ == "__main__":
    main()
