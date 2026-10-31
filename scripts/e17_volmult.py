#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-4 B 类量比扫描生成器（docs/29 §七 P1-4 B 类，2026-10-30 立项）。

读 var/bt/volmult/ 下 3 档(box.volmult=1.0/1.5/2.0) × 4 年(2012/2015/2017/2018) 共 12 库，
按「突破当日量比」分 4 带（0-1×/1-2×/2-5×/5×+），报四项固定口径 + PF + 剔除前三 + 次新拆分。

口径与 A 类（scripts/e17_breakout.py）完全一致，不重写：
  volratio = volume(plan_date) / avg(volume, 前 50 交易日，不含当日)
  cost     = Σ(position_leg.qty×price)
  每单位净利 = Σrealized_pnl / Σunits（已实现口径）
  胜率/保本胜率/缺口/利润集中度/PF/剔除前三 = 同 A 类

用法：
  python scripts/e17_volmult.py --diag      # 诊断：档位×年份 n 分布、量比分布、次新占比
  python scripts/e17_volmult.py             # 全量产出 report.html/md + data.json
"""
import argparse
import json
import sqlite3
import statistics
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOLMULT_DIR = ROOT / "var" / "bt" / "volmult"
OUT_DIR = ROOT / "docs" / "experiments"

VOL_WINDOW = 50
SLIPPAGE = 0.002

# 观测分带（带界固定，禁止挪）
BANDS = [
    ("0–1×", 0.0, 1.0),
    ("1–2×", 1.0, 2.0),
    ("2–5×", 2.0, 5.0),
    ("5×+", 5.0, float("inf")),
]

# 次新判定：list_date 到 plan_date 交易日 < 60
NEW_DAYS = 60


def connect(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def pct(x, d=1):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def money(x, d=0):
    return "—" if x is None else f"{x:,.{d}f}"


def fnum(x, d=2):
    return "—" if x is None else f"{x:.{d}f}"


def median(v):
    return statistics.median(v) if v else None


def band_of(v, bands):
    for label, lo, hi in bands:
        if lo <= v < hi:
            return label
    return None


def parse_dbname(name):
    # bt_volmult_1.0_2012.db -> (volmult, year)
    stem = Path(name).stem  # bt_volmult_1.0_2012
    parts = stem.split("_")
    volmult = parts[2]
    year = int(parts[3])
    return volmult, year


def load_db(db: Path):
    """读一个库 → trades 列表（一笔 = 一个 filled 买入单）。"""
    volmult, year = parse_dbname(db.name)
    c = connect(db)
    try:
        eq = dict(c.execute("SELECT date, total FROM equity_snapshot").fetchall())
        first_eq = c.execute("SELECT total FROM equity_snapshot ORDER BY date LIMIT 1").fetchone()
        first_eq = first_eq[0] if first_eq else 0.0

        # 上市日期（次新判定）
        list_dates = dict(c.execute("SELECT code, list_date FROM security").fetchall())

        q = """
        SELECT t.plan_date, o.code, o.reason_json,
               b.close, b.volume,
               p.id, p.realized_pnl, p.status, p.close_reason, p.opened_date, p.closed_date,
               COALESCE(SUM(l.qty * l.price), 0) AS cost
        FROM plan_order o
        JOIN trade_plan t ON t.id = o.trade_plan_id
        JOIN daily_bar b ON b.code = o.code AND b.date = t.plan_date
        LEFT JOIN position_leg l ON l.plan_order_id = o.id
        LEFT JOIN position p ON p.id = l.position_id
        WHERE o.side = 'buy' AND o.status = 'filled'
        GROUP BY o.id
        ORDER BY t.plan_date, o.code
        """
        rows = c.execute(q).fetchall()
        trades = []
        for (plan_date, code, rj, close, vol, pid, pnl, pst, creason, od, cd, cost) in rows:
            rj = json.loads(rj) if rj else {}
            top = rj.get("box_top")
            if not top or close is None:
                continue
            vs = c.execute(
                "SELECT volume FROM daily_bar WHERE code=? AND date<? ORDER BY date DESC LIMIT ?",
                (code, plan_date, VOL_WINDOW)).fetchall()
            if not vs:
                continue
            avg = sum(v[0] for v in vs) / len(vs)
            vr = (vol / avg) if avg > 0 else None

            hold_td = None
            if od and cd:
                hold_td = c.execute(
                    "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
                    (code, od, cd)).fetchone()[0]

            base_eq = eq.get(od) or first_eq or 0.0
            unit_val = base_eq * 0.02
            units = (cost / unit_val) if unit_val > 0 else 0.0

            # 次新判定：list_date 到 plan_date 的交易日数
            list_date = list_dates.get(code)
            list_td = None
            if list_date:
                list_td = c.execute(
                    "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
                    (code, list_date, plan_date)).fetchone()[0]
            is_new = (list_td is not None and list_td < NEW_DAYS)

            trades.append(dict(
                volmult=volmult, year=year, code=code, plan_date=plan_date,
                volratio=vr, pnl=pnl, status=pst, close_reason=creason,
                opened=od, closed=cd, cost=cost, units=units, hold_td=hold_td,
                is_new=is_new, list_td=list_td, reason=rj,
            ))
        return trades
    finally:
        c.close()


def load_all():
    trades = []
    by_db = OrderedDict()
    for db in sorted(VOLMULT_DIR.glob("bt_volmult_*.db")):
        if "-wal" in db.name or "-shm" in db.name:
            continue
        t = load_db(db)
        by_db[db.name] = t
        trades.extend(t)
    return trades, by_db


def agg(rows):
    """rows = 一笔一 dict。返回四项口径 + 附加指标。"""
    closed = [r for r in rows if r["pnl"] is not None]
    n_signal = len(rows)
    n_closed = len(closed)
    if n_closed == 0:
        return dict(n_signal=n_signal, n_closed=0)
    wins = [r["pnl"] for r in closed if r["pnl"] > 0]
    losses = [r["pnl"] for r in closed if r["pnl"] <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    tot = sum(r["pnl"] for r in closed)
    units = sum(r["units"] for r in closed)
    per_unit = tot / units if units > 0 else None
    win_rate = len(wins) / n_closed
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    breakeven = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else None
    gap = (win_rate - breakeven) if breakeven is not None else None
    top3 = sum(sorted([r["pnl"] for r in closed], reverse=True)[:3])
    share = top3 / gross_win if gross_win > 0 else None
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else None)
    ex_top3 = tot - top3
    hold = median([r["hold_td"] for r in closed if r["hold_td"] is not None])
    # 次新拆分
    new_rows = [r for r in closed if r["is_new"]]
    non_new = [r for r in closed if not r["is_new"]]
    new_pct = len(new_rows) / n_closed if n_closed else None
    return dict(
        n_signal=n_signal, n_closed=n_closed,
        win_rate=win_rate, per_unit=per_unit, breakeven=breakeven, gap=gap,
        pf=pf, share=share, ex_top3=ex_top3, hold_med=hold,
        new_pct=new_pct, new_n=len(new_rows),
        new_per_unit=(sum(r["pnl"] for r in new_rows) / sum(r["units"] for r in new_rows)) if new_rows and sum(r["units"] for r in new_rows) > 0 else None,
        new_win=(len([r for r in new_rows if r["pnl"] > 0]) / len(new_rows)) if new_rows else None,
        nonnew_per_unit=(sum(r["pnl"] for r in non_new) / sum(r["units"] for r in non_new)) if non_new and sum(r["units"] for r in non_new) > 0 else None,
    )


def band_table(trades, key="volratio"):
    out = []
    for label, lo, hi in BANDS:
        sub = [r for r in trades if r[key] is not None and lo <= r[key] < hi]
        m = agg(sub)
        m["band"] = label
        m["lo"], m["hi"] = lo, hi
        out.append(m)
    return out


def per_year_table(trades):
    """每带 × 每年 的每单位净利与笔数（跨年一致性用）。"""
    out = defaultdict(dict)
    for label, lo, hi in BANDS:
        for y in (2012, 2015, 2017, 2018):
            sub = [r for r in trades if r["volratio"] is not None and lo <= r["volratio"] < hi and r["year"] == y]
            m = agg(sub)
            out[label][y] = m
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", action="store_true", help="只出诊断，不写产物")
    a = ap.parse_args()

    trades, by_db = load_all()
    if not trades:
        print("未找到任何库（var/bt/volmult/ 为空）。先跑 e17_volmult_run.py。")
        return 1

    # 诊断
    print(f"库数 {len(by_db)}，总信号 {len(trades)}")
    print("\n=== 档位 × 年份 n ===")
    for dbname, t in by_db.items():
        volmult, year = parse_dbname(dbname)
        n_closed = sum(1 for r in t if r["pnl"] is not None)
        print(f"  volmult={volmult} {year}: n={len(t)} closed={n_closed}")

    print("\n=== 量比分布（合并 12 库）===")
    v = sorted([r["volratio"] for r in trades if r["volratio"] is not None])
    if v:
        print(f"  min={v[0]:.2f} p25={v[len(v)//4]:.2f} p50={v[len(v)//2]:.2f} p75={v[len(v)*3//4]:.2f} max={v[-1]:.2f}")
        for label, lo, hi in BANDS:
            n = sum(1 for x in v if lo <= x < hi)
            print(f"  {label}: {n}")

    if a.diag:
        return 0

    # 全量：分带统计
    arm = band_table(trades)
    pyt = per_year_table(trades)

    # 次新占比诊断
    print("\n=== 各带次新占比（n_closed）===")
    for b in arm:
        if b["n_closed"] == 0:
            print(f"  {b['band']}: 无样本")
            continue
        print(f"  {b['band']}: {pct(b['new_pct'],1)}（{b['new_n']}/{b['n_closed']}）")

    # 组数据并写 json
    data = dict(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        bands=[{k: b[k] for k in b if k not in ("lo", "hi")} for b in arm],
        per_year={lab: {str(y): {k: v.get(k) for k in ("n_signal", "n_closed", "per_unit", "win_rate", "ex_top3")} for y, v in d.items()} for lab, d in pyt.items()},
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "e17_volmult_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== 分带总表（每单位净利 / 胜率 / PF / 剔除前三 / 次新占比）===")
    hdr = f"{'带':<7}{'n信号':>6}{'n平仓':>6}{'胜率':>7}{'每单位':>9}{'PF':>6}{'集中度':>7}{'剔前三':>11}{'次新%':>6}"
    print(hdr)
    for b in arm:
        if b["n_closed"] == 0:
            print(f"{b['band']:<7}{b['n_signal']:>6}{0:>6}   (无样本)")
            continue
        pf = "inf" if b["pf"] == float("inf") else (f"{b['pf']:.2f}" if b["pf"] is not None else "—")
        print(f"{b['band']:<7}{b['n_signal']:>6}{b['n_closed']:>6}"
              f"{pct(b['win_rate'],1) if b['win_rate'] is not None else '—':>7}"
              f"{money(b['per_unit']):>9}{pf:>6}"
              f"{pct(b['share'],0) if b['share'] is not None else '—':>7}"
              f"{money(b['ex_top3']):>11}"
              f"{pct(b['new_pct'],0) if b['new_pct'] is not None else '—':>6}")
    print("\ndata.json 已写。HTML/MD 报告待生成器后续补（本脚本先出数）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
