#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A1 逐年概率统计报告：臂库 vs 基线库，同年并排。

用于「跑完一年报一次」的节奏。只读，可在回测运行中调用：
有效边界 = 六任务全 ok 的最大日期（LastCompleteDate 等价），只统计
opened_date ≤ 边界 且 status='closed' 的笔。

用法：
  python scripts/a1_year_report.py --year 2005
  python scripts/a1_year_report.py --all          # 所有已完成年份逐年
"""
import argparse
import math
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARM_DB = os.path.join(ROOT, "var", "bt", "arms", "bt_a1_t4off.db")
BASE_DB = os.path.join(ROOT, "var", "bt", "base", "bt_baseA_cont_adj.db")
UNIT_PCT = 0.02


def last_complete(db):
    r = db.execute("""
        SELECT MAX(date) FROM (
          SELECT date FROM daily_run
          GROUP BY date
          HAVING COUNT(*)=6 AND SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)=6
        )""").fetchone()
    return r[0] if r else None


def load(db, upto):
    eq = {r[0]: r[1] for r in db.execute("SELECT date,total FROM equity_snapshot").fetchall()}
    leg = {r[0]: r[1] for r in db.execute(
        "SELECT position_id, qty*price FROM position_leg WHERE leg_no=1").fetchall()}
    rows = []
    for pid, code, od, cd, pnl, reason in db.execute("""
            SELECT id, code, cast(opened_date as text), cast(closed_date as text),
                   coalesce(realized_pnl,0), coalesce(close_reason,'')
            FROM position WHERE status='closed' ORDER BY opened_date, id"""):
        if upto and od[:10] > upto:
            continue
        n = leg.get(pid, 0.0)
        e = eq.get(od[:10], 0.0)
        rows.append(dict(pid=pid, code=code, od=od[:10], cd=(cd or "")[:10], pnl=float(pnl),
                         reason=reason, units=(n / (e * UNIT_PCT) if e > 0 else 0.0),
                         notional=n, year=od[:4]))
    return rows


def metrics(rows):
    n = len(rows)
    if n == 0:
        return None
    pnls = [r["pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), -sum(loss)
    units = sum(r["units"] for r in rows)
    ps = sorted(pnls)
    ex3 = sum(ps[:-3]) if n > 3 else 0.0
    wr = len(wins) / n
    z = 1.96
    d = 1 + z * z / n
    c = wr + z * z / (2 * n)
    rad = z * math.sqrt(wr * (1 - wr) / n + z * z / (4 * n * n))
    lo, hi = (c - rad) / d, (c + rad) / d
    mw = gw / len(wins) if wins else 0.0
    ml = gl / len(loss) if loss else 0.0
    odds = mw / ml if ml else float("inf")
    return dict(n=n, w=len(wins), l=len(loss), wr=wr, lo=lo, hi=hi,
                gw=gw, gl=gl, pnl=sum(pnls), avg=sum(pnls) / n,
                mw=mw, ml=ml, odds=odds, be=(1 / (1 + odds) if odds else 0),
                pf=(gw / gl if gl else float("inf")), unit=(sum(pnls) / units if units else 0),
                ex3=ex3, fees=0)


def fmt_metrics(tag, m):
    if not m:
        return f"{tag:<10} 无数据"
    return (f"{tag:<10} {m['n']:>4}笔 {m['w']:>3}胜 {m['wr']*100:>5.1f}% "
            f"[{m['lo']*100:>4.1f},{m['hi']*100:>4.1f}] 总账 {m['pnl']:>+10,.0f} "
            f"均笔 {m['avg']:>+8,.0f} 均盈 {m['mw']:>+8,.0f} 均亏 {-m['ml']:>8,.0f} "
            f"赔率 {m['odds']:>5.2f} 保本 {m['be']*100:>4.1f}% PF {m['pf']:>5.2f} "
            f"每单位 {m['unit']:>+7,.0f} 剔前3 {m['ex3']:>+10,.0f}")


def year_rows(rows, y):
    return [r for r in rows if r["year"] == str(y)]


def open_cnt(db, y):
    """该年建仓但仍未平仓的笔数（>0 表示该年统计不完整，只能当临时值看）。"""
    r = db.execute("SELECT COUNT(*) FROM position WHERE status<>'closed' "
                   "AND substr(cast(opened_date as text),1,4)=?", (str(y),)).fetchone()
    return r[0] if r else 0


def band_by_units(rows):
    """t1/t2/t3/t4 分档（首腿单位数 ±0.35 容忍，同 base_ladder_band）。"""
    ref = [("t1(3)", 3.0), ("t2(2)", 2.0), ("t3(1)", 1.0), ("t4(0.5)", 0.5)]
    out = {k: [] for k, _ in ref}
    out["tx"] = []
    for r in rows:
        u = r["units"]
        hit = "tx"
        for name, v in ref:
            if abs(u - v) <= 0.35:
                hit = name
                break
        out[hit].append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default=ARM_DB)
    ap.add_argument("--base", default=BASE_DB)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    arm = sqlite3.connect(f"file:{a.arm}?mode=ro", uri=True)
    base = sqlite3.connect(f"file:{a.base}?mode=ro", uri=True)
    cut = last_complete(arm)
    arows = load(arm, cut)
    # 基线一律截到臂的同一天：年内未跑完时（如 2005 只到 8 月），
    # 拿基线整年比臂部分年是错的口径，必须同窗口。
    brows = load(base, cut)
    print(f"# A1 逐年概率报告（臂=units.t4=0）｜有效边界 {cut}")
    print(f"臂库 {os.path.basename(a.arm)}｜基线 {os.path.basename(a.base)}\n")

    years = sorted({r["year"] for r in arows})
    if a.year:
        years = [str(a.year)]
    elif not a.all and years:
        years = [years[-1]]  # 默认只报最新完成年

    print(f"{'':<10} {'':>4}   {'':>5} {'':>6} {'':>4}  {'':>4}{'':>4} {'':>11} {'':>9} {'':>9} {'':>8} {'':>5} {'':>5} {'':>5} {'':>8} {'':>11}")
    for y in years:
        ar = year_rows(arows, y)
        br = year_rows(brows, y)
        am, bm = metrics(ar), metrics(br)
        op = open_cnt(arm, y)
        flag = "" if op == 0 else f"  ⚠ 未结算：该年建仓仍有 {op} 笔未平仓，本行只是临时值"
        print(f"\n=== {y} ==={flag}")
        print(fmt_metrics("臂(A1)", am))
        print(fmt_metrics("基线", bm))
        if am and bm:
            print(f"{'差值':<10} {am['n']-bm['n']:>+4}笔 {(am['wr']-bm['wr'])*100:>+5.1f}pp "
                  f"总账 {am['pnl']-bm['pnl']:>+10,.0f} 均笔 {am['avg']-bm['avg']:>+8,.0f} "
                  f"赔率 {am['odds']-bm['odds']:>+5.2f} PF {am['pf']-bm['pf']:>+5.2f}")
        # 分档（确认 t4 = 0 笔）
        bands = band_by_units(ar)
        print("  分档: " + " ｜ ".join(
            f"{k} {len(v)}笔" + (f" {sum(1 for x in v if x['pnl']>0)/len(v)*100:.0f}%胜" if v else "")
            for k, v in bands.items() if v))
        # 持有天数带
        def hold_days(r):
            try:
                from datetime import date
                y1, m1, d1 = map(int, r["od"].split("-"))
                y2, m2, d2 = map(int, r["cd"].split("-"))
                return (date(y2, m2, d2) - date(y1, m1, d1)).days
            except Exception:
                return -1
        hb = {"≤2": 0, "3-5": 0, "6-10": 0, "11-20": 0, ">20": 0}
        for r in ar:
            h = hold_days(r)
            k = ("≤2" if h <= 2 else "3-5" if h <= 5 else "6-10" if h <= 10
                 else "11-20" if h <= 20 else ">20")
            if h >= 0:
                hb[k] += 1
        print("  持有: " + " ｜ ".join(f"{k} {v}笔" for k, v in hb.items()))
        # 出口
        ex = {}
        for r in ar:
            ex.setdefault(r["reason"] or "(空)", []).append(r["pnl"])
        print("  出口: " + " ｜ ".join(
            f"{k} {len(v)}笔 {sum(v):+,.0f}" for k, v in
            sorted(ex.items(), key=lambda kv: -len(kv[1]))[:6]))

    # 累计到边界
    print("\n=== 累计（截至 %s，按建仓日归年，同期已全部平仓）===" % cut)
    print(fmt_metrics("臂(A1)", metrics(arows)))
    print(fmt_metrics("基线同期", metrics(brows)))

    # 判据口径：按平仓日归年（std 里「剔最好年」就是这个口径）
    def by_close_year(rows):
        d = {}
        for r in rows:
            d.setdefault(r["cd"][:4], []).append(r)
        return d
    ac, bc = by_close_year(arows), by_close_year(brows)
    print("\n=== 按平仓日归年（判据口径：剔最好年用这个）===")
    for y in sorted(set(ac) | set(bc)):
        am = metrics(ac.get(y, []))
        bm = metrics(bc.get(y, []))
        if am:
            print(fmt_metrics("  A1 " + y, am))
        if bm:
            print(fmt_metrics("  基线 " + y, bm))
    if ac:
        tot = sum(r["pnl"] for v in ac.values() for r in v)
        best = max(((sum(r["pnl"] for r in v), y) for y, v in ac.items()))
        print(f"  臂(A1) 合计 {tot:+,.0f}｜最好年 {best[1]} {best[0]:+,.0f}｜剔最好年 {tot-best[0]:+,.0f}")


if __name__ == "__main__":
    main()
