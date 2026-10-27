#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-5 独立校验器：抽查 3 笔，用**与生成器完全不同的代码路径**手算 MFE 与回吐率。

纪律（框架 §6 步 4 / HARD ③）：不复用 `e17_giveback.py` 的任何函数，独立写 SQL 与算式，
逐项比对 `docs/experiments/e17_giveback_data.json` 里的值，相对误差须 < 0.01%。

抽查规则（跑前写死，不看结果挑）：
  ① 短持亏损：hold_bars ≤ 10 且 realized_pnl < 0 中 hold 最接近 6 的第一笔（按年份升序）
  ② 长持大赢家：mfe > 50% 中 mfe 最大的那笔
  ③ 中庸：hold_bars 落在 11–20 且 realized_pnl > 0 中 hold 最接近 15 的第一笔

用法：python scripts/e17_giveback_verify.py [--json docs/experiments/e17_giveback_data.json]
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
DEF_JSON = ROOT / "docs" / "experiments" / "e17_giveback_data.json"
TOL = 1e-4  # 0.01% 相对误差


def pick(trades):
    short = sorted([t for t in trades if t["hold"] <= 10 and t["pnl"] < 0],
                   key=lambda t: (abs(t["hold"] - 6), t["year"]))
    big = sorted([t for t in trades if t["testable"] and t["mfe"] > 0.5],
                 key=lambda t: -t["mfe"])
    mid = sorted([t for t in trades if 11 <= t["hold"] <= 20 and t["pnl"] > 0],
                 key=lambda t: (abs(t["hold"] - 15), t["year"]))
    out = []
    if short:
        out.append(("①短持亏损", short[0]))
    if big:
        out.append(("②长持大赢家", big[0]))
    if mid:
        out.append(("③中庸（11–20日盈利）", mid[0]))
    return out


def recompute(t):
    """独立重算：成本走 position_leg，最高价走 ORDER BY high DESC LIMIT 1。"""
    db = BT / f"bt_e17_{t['year']}.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    legs = con.execute(
        "SELECT l.qty, l.price FROM position_leg l "
        "JOIN position p ON p.id=l.position_id "
        "WHERE p.code=? AND p.opened_date=? AND p.closed_date=?",
        (t["code"], t["opened"], t["closed"])).fetchall()
    qty = sum(q for q, _ in legs)
    cost = sum(q * px for q, px in legs)

    hi = con.execute(
        "SELECT high, date FROM daily_bar WHERE code=? AND date>=? AND date<=? "
        "ORDER BY high DESC LIMIT 1",
        (t["code"], t["opened"], t["closed"])).fetchone()
    bars = con.execute(
        "SELECT COUNT(DISTINCT date) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
        (t["code"], t["opened"], t["closed"])).fetchone()[0]
    con.close()

    avg = cost / qty
    mfe = hi[0] / avg - 1.0
    ret = t["pnl"] / cost
    gb = (mfe - ret) / mfe if mfe > 0 else None
    return {"qty": qty, "cost": cost, "avg": avg, "max_high": hi[0], "high_date": hi[1],
            "hold": bars, "mfe": mfe, "ret": ret, "gb": gb}


def rel(a, b):
    if a is None or b is None:
        return None
    d = abs(a - b)
    return d / max(abs(a), abs(b), 1e-12)


def main():
    p = Path(sys.argv[sys.argv.index("--json") + 1]) if "--json" in sys.argv else DEF_JSON
    data = json.loads(p.read_text(encoding="utf-8"))
    trades = data["trades"]
    picks = pick(trades)

    print(f"{'抽查':<20}{'年份':<6}{'代码':<11}{'项目':<11}{'生成器':>15}{'独立重算':>15}{'相对差':>13}")
    bad = 0
    for tag, t in picks:
        r = recompute(t)
        pairs = [("cost_basis", t["cost_basis"], r["cost"]),
                 ("avg_cost", t["avg_cost"], r["avg"]),
                 ("max_high", t["max_high"], r["max_high"]),
                 ("hold_bars", t["hold"], r["hold"]),
                 ("mfe", t["mfe"], r["mfe"]),
                 ("ret", t["ret"], r["ret"]),
                 ("giveback", t["gb"], r["gb"])]
        for i, (name, a, b) in enumerate(pairs):
            d = rel(a, b)
            ok = (d is not None and d < TOL) or (a is None and b is None)
            if not ok:
                bad += 1
            av = f"{a:.8f}" if isinstance(a, float) else str(a)
            bv = f"{b:.8f}" if isinstance(b, float) else str(b)
            dv = f"{d * 100:.6f}%" if d is not None else "—"
            prefix = (f"{tag:<20}{t['year']:<6}{t['code']:<11}") if i == 0 else " " * 37
            print(f"{prefix}{name:<11}{av:>15}{bv:>15}{dv:>13}{'' if ok else '  ✗'}")
        print(f"{'':<20}{'':<6}{'window':<11}{t['opened']} → {t['closed']}"
              f"   hold={r['hold']}  最高价日={r['high_date']}")
        print()

    print(f"抽查 {len(picks)} 笔 × 7 项，容差 {TOL * 100:.2f}%；不通过 {bad} 项")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
