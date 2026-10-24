#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E17 新高距离仓位阶梯 · 基线 vs 回测 逐年对照。

用法：
  python scripts/e17_compare.py <baseline_db> <e17_db> <year>

输出一段 Markdown 到 stdout（由调用方拼装 newhigh_ladder_backtest.md）：
  ① 核心指标对照（收益/回撤/夏普/胜率/盈亏比/净利/最大仓位暴露）
  ② 阶梯档位真实表现（按 position_leg.plan_order_id -> reason_json 精确归属）
  ③ 买单意图分布（含未成交，观察 0.5 单位与拒买占比）
"""
import json
import sqlite3
import sys


def connect_ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def metrics(con):
    eq = con.execute(
        "SELECT date, total, market_value, drawdown FROM equity_snapshot ORDER BY date"
    ).fetchall()
    if not eq:
        raise SystemExit("equity_snapshot 为空")
    days = len(eq)
    eq_start, eq_end = eq[0]["total"], eq[-1]["total"]
    ret = (eq_end / eq_start - 1) if eq_start else 0.0
    maxdd = min((r["drawdown"] for r in eq), default=0.0)
    max_expo = max(
        (r["market_value"] / r["total"] for r in eq if r["total"] > 0), default=0.0
    )
    rets = [
        eq[i]["total"] / eq[i - 1]["total"] - 1
        for i in range(1, len(eq))
        if eq[i - 1]["total"] > 0
    ]
    sharpe = 0.0
    if len(rets) > 1:
        n = len(rets)
        mean = sum(rets) / n
        var = sum((x - mean) ** 2 for x in rets) / (n - 1)
        std = var ** 0.5
        if std > 0:
            sharpe = mean / std * (252 ** 0.5)

    row = con.execute(
        """SELECT COUNT(*) n,
                  SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                  SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END) losses,
                  SUM(CASE WHEN realized_pnl>0 THEN realized_pnl ELSE 0 END) gw,
                  SUM(CASE WHEN realized_pnl<0 THEN realized_pnl ELSE 0 END) gl,
                  COALESCE(SUM(realized_pnl),0) pnl
           FROM position WHERE status='closed'"""
    ).fetchone()
    n = row["n"] or 0
    gw, gl = row["gw"] or 0.0, row["gl"] or 0.0
    pf = (gw / abs(gl)) if gl != 0 else (float("inf") if gw > 0 else 0.0)
    open_n = con.execute(
        "SELECT COUNT(*) FROM position WHERE status!='closed'"
    ).fetchone()[0]
    return {
        "days": days,
        "eq_start": eq_start,
        "eq_end": eq_end,
        "ret": ret,
        "maxdd": maxdd,
        "max_expo": max_expo,
        "sharpe": sharpe,
        "n": n,
        "wins": row["wins"] or 0,
        "losses": row["losses"] or 0,
        "wr": (row["wins"] or 0) / n if n else 0.0,
        "pf": pf,
        "pnl": row["pnl"],
        "open_n": open_n,
    }


def tiers(con):
    """按成交首腿精确归属档位（reason_json 的 sizing_basis/units）。"""
    rows = con.execute(
        """SELECT p.id, p.code, p.realized_pnl, p.status, o.reason_json
           FROM position_leg l
           JOIN position p ON p.id = l.position_id
           LEFT JOIN plan_order o ON o.id = l.plan_order_id
           WHERE l.leg_no = 1"""
    ).fetchall()
    out = {}
    for r in rows:
        try:
            rj = json.loads(r["reason_json"] or "{}")
        except ValueError:
            rj = {}
        basis = rj.get("sizing_basis") or "unknown"
        units = rj.get("units")
        if units is None:
            units = rj.get("unit_value")
        try:
            units = float(units)
        except (TypeError, ValueError):
            units = 1.0
        d = out.setdefault(
            basis,
            {"n": 0, "closed": 0, "wins": 0, "pnl": 0.0, "units": 0.0, "dist": []},
        )
        d["n"] += 1
        d["units"] += units
        dist = rj.get("newhigh_distance_pct")
        if dist is not None:
            d["dist"].append(float(dist))
        if r["status"] == "closed" and r["realized_pnl"] is not None:
            d["closed"] += 1
            d["pnl"] += r["realized_pnl"]
            if r["realized_pnl"] > 0:
                d["wins"] += 1
    return out


def buy_intents(con):
    """买单意图（含未成交），按 sizing_basis 聚合。"""
    rows = con.execute(
        "SELECT reason_json, qty FROM plan_order WHERE side='buy'"
    ).fetchall()
    out = {}
    for r in rows:
        try:
            rj = json.loads(r["reason_json"] or "{}")
        except ValueError:
            rj = {}
        basis = rj.get("sizing_basis") or "unknown"
        d = out.setdefault(basis, {"orders": 0, "qty": 0})
        d["orders"] += 1
        d["qty"] += r["qty"] or 0
    return out


def fmt_pf(x):
    return "∞" if x == float("inf") else f"{x:.2f}"


def section(year, base, e17):
    bm, em = metrics(base), metrics(e17)
    bt, et = tiers(base), tiers(e17)
    bi, ei = buy_intents(base), buy_intents(e17)

    L = []
    L.append(f"## {year} 年")
    L.append("")
    L.append("### ① 核心指标对照")
    L.append("")
    L.append("| 指标 | 基线 A M1 | E17 阶梯 | Δ |")
    L.append("|------|-----------|----------|---|")
    L.append(f"| 净利 | {bm['pnl']:,.0f} | {em['pnl']:,.0f} | {em['pnl']-bm['pnl']:+,.0f} |")
    L.append(f"| 平仓笔数 | {bm['n']} | {em['n']} | {em['n']-bm['n']:+d} |")
    L.append(f"| 胜率 | {bm['wr']*100:.1f}% | {em['wr']*100:.1f}% | {(em['wr']-bm['wr'])*100:+.1f}pp |")
    L.append(f"| 盈亏比 | {fmt_pf(bm['pf'])} | {fmt_pf(em['pf'])} | — |")
    L.append(f"| 收益率 | {bm['ret']*100:.2f}% | {em['ret']*100:.2f}% | {(em['ret']-bm['ret'])*100:+.2f}pp |")
    L.append(f"| 最大回撤 | {bm['maxdd']*100:.2f}% | {em['maxdd']*100:.2f}% | {(em['maxdd']-bm['maxdd'])*100:+.2f}pp |")
    L.append(f"| 夏普率 | {bm['sharpe']:.3f} | {em['sharpe']:.3f} | {em['sharpe']-bm['sharpe']:+.3f} |")
    L.append(f"| 最大仓位暴露 | {bm['max_expo']*100:.1f}% | {em['max_expo']*100:.1f}% | {(em['max_expo']-bm['max_expo'])*100:+.1f}pp |")
    L.append(f"| 年末未平仓 | {bm['open_n']} | {em['open_n']} | {em['open_n']-bm['open_n']:+d} |")
    L.append("")

    L.append("### ② E17 档位真实表现（按成交首腿归属）")
    L.append("")
    L.append("| 档位 | 单位 | 建仓数 | 平仓数 | 胜率 | 净利 | 每单位净利 | 平均距新高 |")
    L.append("|------|------|--------|--------|------|------|------------|------------|")
    order = ["newhigh_t1", "newhigh_t2", "newhigh_t3", "newhigh_t4", "A_regime_up", "default", "newhigh_unavailable"]
    keys = [k for k in order if k in et] + [k for k in sorted(et) if k not in order]
    for k in keys:
        d = et[k]
        avg_u = d["units"] / d["n"] if d["n"] else 0
        wr = d["wins"] / d["closed"] * 100 if d["closed"] else 0
        per_u = d["pnl"] / d["units"] if d["units"] else 0
        dist = f"{sum(d['dist'])/len(d['dist']):.1f}%" if d["dist"] else "—"
        L.append(
            f"| {k} | {avg_u:.2f} | {d['n']} | {d['closed']} | {wr:.1f}% | "
            f"{d['pnl']:,.0f} | {per_u:,.0f} | {dist} |"
        )
    L.append("")

    L.append("### ③ 买单意图分布（含未成交）")
    L.append("")
    L.append("| 档位 | 基线单数 | 基线股数 | E17 单数 | E17 股数 |")
    L.append("|------|----------|----------|----------|----------|")
    keys = [k for k in order if k in bi or k in ei] + [
        k for k in sorted(set(bi) | set(ei)) if k not in order
    ]
    for k in keys:
        b = bi.get(k, {"orders": 0, "qty": 0})
        e = ei.get(k, {"orders": 0, "qty": 0})
        L.append(f"| {k} | {b['orders']} | {b['qty']:,} | {e['orders']} | {e['qty']:,} |")
    L.append("")
    return "\n".join(L), bm, em


def main():
    if len(sys.argv) < 4:
        raise SystemExit("usage: e17_compare.py <baseline_db> <e17_db> <year>")
    year, bp, ep = sys.argv[3], sys.argv[1], sys.argv[2]
    base, e17 = connect_ro(bp), connect_ro(ep)
    text, bm, em = section(year, base, e17)
    print(text)
    base.close()
    e17.close()


if __name__ == "__main__":
    main()
