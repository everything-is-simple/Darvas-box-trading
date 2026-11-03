#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""攻防转换 A 类零成本后验 —— 市场状态是否决定「每一单位」的赚钱效率？

问题（用户 2026-11-03 提出）：
    「牛市加满、熊市撤退」的前提是——**同样的 1 单位钱，在牛市状态下比熊市状态下更赚钱**。
    若这个前提不成立，加满只是放大同一质量的赌注，等于放大亏损（P0-3：买错才是出血口）。

做法（只读、不跑回测）：
    1. 对 baseA 14 年库的每一笔已平仓交易，取建仓日之前的指数状态（严格 T-1，无前视）：
         ma60      = 上证收盘 vs 60 日均线（上/下）
         dd52      = 上证距 252 日最高点的回撤（）
    2. 每笔折成「单位」：units = Σ(position_leg.qty×price) / (建仓日权益 × 2%)
    3. 报每单位净利（= realized_pnl / units），按状态分带。
    4. 纪律：n 与剔除前三赢家并列；改善年数 ≥3 才算方向一致。

用法：
    python scripts/e17_regime_scale_a.py
    python scripts/e17_regime_scale_a.py --dir var/bt/base --json out.json
"""
import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "var" / "bt" / "base"
YEARS = list(range(2005, 2019))
INDEX = "sh000001"
MA_N = 60
HIGH_N = 252


def load_index(con):
    """整条指数序列（该库含全历史），返回 [(date, close)] 升序。"""
    rows = con.execute(
        "SELECT date, close FROM daily_bar WHERE code=? ORDER BY date", (INDEX,)
    ).fetchall()
    return [(r[0][:10], float(r[1])) for r in rows if r[1]]


def state_series(series):
    """逐日的 (ma60, dd52) 状态，只用当日及之前的数据（T 日收盘可算 T 日状态）。"""
    closes = [c for _, c in series]
    dates = [d for d, _ in series]
    out = {}
    for i, d in enumerate(dates):
        if i + 1 < MA_N:
            continue
        ma = sum(closes[i + 1 - MA_N : i + 1]) / MA_N
        lo = max(0, i + 1 - HIGH_N)
        hi = max(closes[lo : i + 1])
        out[d] = (closes[i] / ma - 1.0, closes[i] / hi - 1.0)
    return out


def prev_state(state, date):
    """取严格早于 date 的最近一个状态（防前视）。"""
    best = None
    for d in state:  # state 是有序的字典（插入序=日期序）
        if d < date:
            best = state[d]
        else:
            break
    return best


def load_trades(db):
    """每笔已平仓交易：建仓日、已实现净利、成本（Σ leg qty×price）、建仓日权益。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        eq = {r[0][:10]: r[1] for r in con.execute("SELECT date, total FROM equity_snapshot")}
        first_eq = min(eq.values()) if eq else 0.0
        rows = con.execute(
            """
            SELECT p.opened_date, p.closed_date, p.realized_pnl, p.close_reason,
                   COALESCE(SUM(l.qty * l.price), 0) AS cost
            FROM position p
            LEFT JOIN position_leg l ON l.position_id = p.id
            WHERE p.status='closed' AND p.realized_pnl IS NOT NULL
            GROUP BY p.id
            ORDER BY p.opened_date
            """
        ).fetchall()
        out = []
        for od, cd, pnl, reason, cost in rows:
            od = (od or "")[:10]
            if not od:
                continue
            base_eq = eq.get(od) or first_eq
            unit_val = base_eq * 0.02
            units = (cost / unit_val) if unit_val > 0 and cost else 0.0
            out.append(
                dict(opened=od, closed=(cd or "")[:10], pnl=float(pnl or 0.0),
                     reason=reason, cost=float(cost or 0.0), units=units)
            )
        return out
    finally:
        con.close()


def summarize(trades):
    """按带汇总：n / 每单位净利 / 胜率 / 剔除前三后的每单位净利。"""
    if not trades:
        return None
    n = len(trades)
    units = sum(t["units"] for t in trades)
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    top3 = sum(sorted((t["pnl"] for t in trades), reverse=True)[:3])
    return dict(
        n=n,
        units=round(units, 2),
        pnl=round(pnl, 2),
        per_unit=round(pnl / units, 2) if units > 0 else 0.0,
        per_unit_ex3=round((pnl - top3) / units, 2) if units > 0 else 0.0,
        win=round(wins / n * 100, 1) if n else 0.0,
    )


DD_BUCKETS = [
    ("距高 ≥ -2%（最强）", lambda d: d >= -0.02),
    ("-2% ~ -5%", lambda d: -0.05 <= d < -0.02),
    ("-5% ~ -10%", lambda d: -0.10 <= d < -0.05),
    ("-10% ~ -20%", lambda d: -0.20 <= d < -0.10),
    ("< -20%（深跌）", lambda d: d < -0.20),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--breadth", default=str(ROOT / "var" / "tmp" / "breadth50.csv"),
                    help="市场宽度序列（scripts/e17_breadth_build.py 产出）；缺失则跳过宽度分析")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    d = Path(a.dir)

    all_trades = []
    per_year = {}
    state_cache = None
    for y in YEARS:
        db = d / f"bt_baseA_{y}.db"
        if not db.exists():
            print(f"[skip] {db.name} 不存在")
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        series = load_index(con)
        con.close()
        state = state_series(series)
        trades = load_trades(db)
        for t in trades:
            st = prev_state(state, t["opened"])
            if st is None:
                continue
            t["ma60_gap"], t["dd52"] = st
            t["year"] = y
        trades = [t for t in trades if "dd52" in t]
        per_year[y] = trades
        all_trades.extend(trades)
        print(f"[{y}] 交易 {len(trades):3d} 笔")

    if not all_trades:
        print("没有可用交易")
        return

    print("\n" + "=" * 78)
    print("一、按「指数距 252 日高点回撤」分带（T-1 状态，无前视）")
    print("=" * 78)
    print(f"{'带':<20}{'n':>5}{'单位':>9}{'已实现':>12}{'每单位':>10}{'剔前三':>10}{'胜率':>7}")
    dd_rows = []
    for label, fn in DD_BUCKETS:
        sub = [t for t in all_trades if fn(t["dd52"])]
        s = summarize(sub)
        if not s:
            print(f"{label:<20}{0:>5}")
            continue
        dd_rows.append((label, s))
        print(f"{label:<20}{s['n']:>5}{s['units']:>9.1f}{s['pnl']:>12,.0f}"
              f"{s['per_unit']:>10,.0f}{s['per_unit_ex3']:>10,.0f}{s['win']:>6.1f}%")

    print("\n" + "=" * 78)
    print("二、按 MA60 上下（闸门口径）")
    print("=" * 78)
    ma_rows = []
    for label, fn in [("指数在 MA60 上方", lambda g: g > 0), ("指数在 MA60 下方", lambda g: g <= 0)]:
        sub = [t for t in all_trades if fn(t["ma60_gap"])]
        s = summarize(sub)
        if not s:
            continue
        ma_rows.append((label, s))
        print(f"{label:<20}{s['n']:>5}{s['units']:>9.1f}{s['pnl']:>12,.0f}"
              f"{s['per_unit']:>10,.0f}{s['per_unit_ex3']:>10,.0f}{s['win']:>6.1f}%")

    print("\n" + "=" * 78)
    print("三、二维交叉（进攻态 = MA60 上方 且 距高 ≥ -5%）")
    print("=" * 78)
    cells = [
        ("进攻：上 & 距高≥-5%", lambda t: t["ma60_gap"] > 0 and t["dd52"] >= -0.05),
        ("中性：上 & -5~-20%", lambda t: t["ma60_gap"] > 0 and -0.20 <= t["dd52"] < -0.05),
        ("弱势：上 & <-20%", lambda t: t["ma60_gap"] > 0 and t["dd52"] < -0.20),
        ("防御：下 & 距高≥-20%", lambda t: t["ma60_gap"] <= 0 and t["dd52"] >= -0.20),
        ("崩盘：下 & <-20%", lambda t: t["ma60_gap"] <= 0 and t["dd52"] < -0.20),
    ]
    cross_rows = []
    for label, fn in cells:
        sub = [t for t in all_trades if fn(t)]
        s = summarize(sub)
        if not s:
            print(f"{label:<24}{0:>5}")
            continue
        cross_rows.append((label, s))
        print(f"{label:<24}{s['n']:>5}{s['units']:>9.1f}{s['pnl']:>12,.0f}"
              f"{s['per_unit']:>10,.0f}{s['per_unit_ex3']:>10,.0f}{s['win']:>6.1f}%")

    print("\n" + "=" * 78)
    print("四、逐年 × MA60 上方/下方的每单位净利（看方向一致性）")
    print("=" * 78)
    print(f"{'年':<7}{'上方 n':>7}{'上方/单位':>12}{'下方 n':>7}{'下方/单位':>12}{'差':>10}")
    better = 0
    valid = 0
    for y in YEARS:
        ts = per_year.get(y, [])
        up = [t for t in ts if t["ma60_gap"] > 0]
        dn = [t for t in ts if t["ma60_gap"] <= 0]
        su, sd = summarize(up), summarize(dn)
        if not su or not sd:
            print(f"{y:<7}{len(up):>7}{'—':>12}{len(dn):>7}{'—':>12}")
            continue
        diff = su["per_unit"] - sd["per_unit"]
        valid += 1
        if diff > 0:
            better += 1
        print(f"{y:<7}{su['n']:>7}{su['per_unit']:>12,.0f}{sd['n']:>7}{sd['per_unit']:>12,.0f}{diff:>10,.0f}")
    print(f"\n方向一致：上方更赚的年数 {better}/{valid}")

    if a.json:
        Path(a.json).write_text(json.dumps(dict(
            dd={k: v for k, v in dd_rows},
            ma={k: v for k, v in ma_rows},
            cross={k: v for k, v in cross_rows},
            consistency=f"{better}/{valid}",
        ), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写 {a.json}")

    # ---- 市场宽度与仓位乘数模拟（可选，依赖 breadth CSV）----
    bp = Path(a.breadth)
    if not bp.exists():
        print(f"\n[skip] 宽度序列缺失：{bp}（先跑 scripts/e17_breadth_build.py）")
        return
    breadth = {}
    for r in csv.DictReader(bp.open(encoding="utf-8")):
        breadth[r["date"]] = float(r["breadth50"])
    bdates = sorted(breadth)

    def prev_breadth(date):
        lo, hi, best = 0, len(bdates) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if bdates[mid] < date:
                best = bdates[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return breadth[best] if best else None

    got = 0
    for t in all_trades:
        bb = prev_breadth(t["opened"])
        if bb is not None:
            t["b50"] = bb
            got += 1
    if not got:
        print("\n[skip] 宽度与交易日期无交集")
        return

    print("\n" + "=" * 78)
    print("五、按市场宽度（MA50 上方占比，T-1）分带")
    print("=" * 78)
    print(f"{'带':<20}{'n':>5}{'单位':>9}{'已实现':>12}{'每单位':>10}{'剔前三':>10}{'胜率':>7}")
    for label, fn in [
        ("宽度 ≥ 70%", lambda x: x >= 0.70),
        ("50% ~ 70%", lambda x: 0.50 <= x < 0.70),
        ("30% ~ 50%", lambda x: 0.30 <= x < 0.50),
        ("< 30%", lambda x: x < 0.30),
    ]:
        s = summarize([t for t in all_trades if "b50" in t and fn(t["b50"])])
        if not s:
            continue
        print(f"{label:<20}{s['n']:>5}{s['units']:>9.1f}{s['pnl']:>12,.0f}"
              f"{s['per_unit']:>10,.0f}{s['per_unit_ex3']:>10,.0f}{s['win']:>6.1f}%")

    print("\n" + "=" * 78)
    print("六、仓位乘数模拟（线性近似：pnl×m，不含复利/只数交互，仅供方向判定）")
    print("=" * 78)
    base = sum(t["pnl"] for t in all_trades)
    print(f"基线已实现 {base:,.0f}（{len(all_trades)} 笔）")

    def mult_b(t):
        if t.get("b50", 0) >= 0.70 and t["dd52"] >= -0.05:
            return 2.0
        if t.get("b50", 1) < 0.50 or t["dd52"] < -0.20:
            return 0.5
        return 1.0

    print(f"\n{'年':<7}{'基线':>12}{'方案B（宽度×距高双确认）':>22}{'差':>11}")
    tb = ts = 0
    better_b = 0
    for y in YEARS:
        rs = [t for t in all_trades if t.get("year") == y]
        if not rs:
            continue
        b0 = sum(t["pnl"] for t in rs)
        b1 = sum(t["pnl"] * mult_b(t) for t in rs)
        tb += b0
        ts += b1
        better_b += 1 if b1 > b0 else 0
        print(f"{y:<7}{b0:>12,.0f}{b1:>22,.0f}{b1-b0:>11,.0f}")
    print(f"{'合计':<7}{tb:>12,.0f}{ts:>22,.0f}{ts-tb:>11,.0f}")
    print(f"改善年数 {better_b}/{len([y for y in YEARS if any(t.get('year')==y for t in all_trades)])}")

    ids = sorted(all_trades, key=lambda t: -t["pnl"])[:3]
    ex0 = tb - sum(t["pnl"] for t in ids)
    ex1 = ts - sum(t["pnl"] * mult_b(t) for t in ids)
    print(f"剔除前三赢家：基线 {ex0:,.0f} → 方案B {ex1:,.0f}（{ex1-ex0:+,.0f}）")

    if a.json:
        p = Path(a.json)
        obj = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        obj["breadth"] = {k: v for k, v in dd_rows}  # 占位，见下方 sim
        obj["sim_B"] = dict(base=round(tb, 2), scaled=round(ts, 2),
                            delta=round(ts - tb, 2), better_years=better_b,
                            ex3_base=round(ex0, 2), ex3_scaled=round(ex1, 2))
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已更新 {p}")


if __name__ == "__main__":
    main()
