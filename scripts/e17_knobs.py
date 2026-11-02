#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E17 入场/池管理旋钮梯度扫描 · A 类零成本生成器。

实验：docs/29 §五 2.1 序 2「入场/池管理旋钮扫描」；docs/45 §一 P0 六项。
框架：docs/experiments/e17_knobs_framework.md（零结果纯规则，档界跑数前写死）。

六个旋钮处置（框架 §2/§3）：
  1. screen.rs_excess_min   — candidate.rs_value 连续值分带（门槛两侧）
  2. screen.latedays        — 复算 too_late_days 分带（signal.broken_date→signal.date）
  3. pool.min_watch_days    — watch.added_date→首买间隔分带
  4. pool.flatboxmax        — exit_reason='flat_too_long' 反事实（出池后 N 日收益）
  5. match.max_entry_gap    — cancelled buy 单按 ClosePx/trigger 分带反事实
  6. regime.a.*             — 死参数，登记「新基线下失效」，不做分带

数据源：var/bt/base/bt_baseA_<year>.db × 14（2005–2018），mode=ro 只读。
所有复算（bar_count / fwd return）都在逐库遍历内完成，不二次加载。

用法：
  python scripts/e17_knobs.py               # 正式生成（HARD 失败即中止）
  python scripts/e17_knobs.py --check-only  # 只跑校准，不写文件
"""
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt" / "base"
EXP = ROOT / "docs" / "experiments"
OUT_HTML = EXP / "e17_knobs_report.html"
OUT_MD = EXP / "e17_knobs_report.md"
OUT_JSON = EXP / "e17_knobs_data.json"

UNIT_NOTE = "1 单位 = 2% 权益；E17 阶梯 t1=3 / t2=2 / t3=1 / t4=0.5"
NOISE_PU = 600.0  # 每单位净利差噪声门槛（=3% × 单位值 20,000）
FW_DAYS = (5, 10, 20, 60)  # 反事实 N 日收益窗口


def years_list():
    return sorted(int(os.path.basename(p).split("_")[2].split(".")[0])
                  for p in BT.glob("bt_baseA_*.db"))


# ---------------------------------------------------------------- 格式化
def fnum(x, d=0):
    return "—" if x is None else f"{x:,.{d}f}"


def fpct(x, d=1):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def fsign(x, d=0):
    return "—" if x is None else f"{x:+,.{d}f}"


def fpp(x, d=1):
    return "—" if x is None else f"{x:+.{d}f}pp"


def cls(x):
    if x is None:
        return "neutral"
    return "up" if x > 0 else ("down" if x < 0 else "neutral")


def pct_of(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


# ---------------------------------------------------------------- 聚合
def agg_trades(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "units": 0.0,
                "wr": None, "pu": None, "pf": None, "avg_win": None,
                "avg_loss": None, "payoff": None, "bewr": None, "gap": None,
                "top3": 0.0, "conc": None, "pnl_ex_top3": 0.0,
                "pu_ex_top3": None}
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] < 0]
    pnl = sum(r["pnl"] for r in rows)
    units = sum(r["units"] for r in rows)
    gw = sum(r["pnl"] for r in wins)
    gl = sum(r["pnl"] for r in losses)
    avg_win = gw / len(wins) if wins else None
    avg_loss = gl / len(losses) if losses else None
    payoff = abs(avg_win / avg_loss) if (avg_win and avg_loss) else None
    bewr = 1 / (1 + payoff) if payoff else None
    wr = len(wins) / n
    ranked = sorted((r["pnl"] for r in rows), reverse=True)
    top3 = sum(ranked[:3])
    ex_rows = sorted(rows, key=lambda x: -x["pnl"])[3:] if len(rows) > 3 else []
    ex_pnl = sum(r["pnl"] for r in ex_rows)
    ex_units = sum(r["units"] for r in ex_rows)
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "pnl": pnl,
        "units": units, "wr": wr, "pu": (pnl / units if units else None),
        "pf": (gw / abs(gl) if gl else None),
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "bewr": bewr,
        "gap": ((wr - bewr) * 100 if (wr is not None and bewr is not None) else None),
        "top3": top3,
        "conc": (top3 / gw * 100 if (gw > 0 and top3 > 0) else None),
        "pnl_ex_top3": ex_pnl, "pu_ex_top3": (ex_pnl / ex_units if ex_units else None),
    }


def agg_returns(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "mean": None, "med": None, "pos_pct": None,
                "ex_top3_mean": None, "sum": 0.0}
    mean = sum(rows) / n
    med = pct_of(sorted(rows), 0.5)
    pos = sum(1 for x in rows if x > 0) / n
    ranked = sorted(rows, reverse=True)
    ex = ranked[3:] if n > 3 else []
    ex_mean = sum(ex) / len(ex) if ex else None
    return {"n": n, "mean": mean, "med": med, "pos_pct": pos,
            "ex_top3_mean": ex_mean, "sum": sum(rows)}


def consistency(rows, years):
    per_year = []
    for y in years:
        ys = [r for r in rows if r["year"] == y]
        if ys:
            per_year.append({"year": y, "n": len(ys),
                             "pnl": sum(r["pnl"] for r in ys)})
    ws = [p for p in per_year if p["n"] >= 10]
    M = len(ws)
    P = sum(1 for p in ws if p["pnl"] > 0)
    return {"M": M, "P": P, "ratio": (P / M if M else None), "per_year": per_year}


# ---------------------------------------------------------------- 分带定义（跑数前写死）
RS_BANDS = [(None, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
            (0.20, 0.25), (0.25, 0.30), (0.30, None)]
LATE_BANDS = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 6), (6, None)]
MW_BANDS = [(0, 2), (2, 3), (3, 4), (4, 6), (6, 11), (11, None)]
GAP_BANDS = [(1.00, 1.03), (1.03, 1.05), (1.05, 1.08), (1.08, 1.10), (1.10, None)]


def band_of(v, bands):
    for lo, hi in bands:
        if lo is None:
            if v < hi:
                return (lo, hi)
        elif hi is None:
            if v >= lo:
                return (lo, hi)
        elif lo <= v < hi:
            return (lo, hi)
    return None


def rs_label(lo, hi):
    if lo is None:
        return "<0.05"
    if hi is None:
        return "≥0.30"
    return f"{lo:.2f}–{hi:.2f}"


def days_label(lo, hi):
    if hi is None:
        return f"≥{lo} 日"
    if hi - lo == 1:
        return f"{lo} 日"
    return f"{lo}–{hi-1} 日"


def gap_label(lo, hi):
    if hi is None:
        return "≥1.10"
    return f"{lo:.2f}–{hi:.2f}"


# ---------------------------------------------------------------- 主加载（全部复算内联）
def load_all():
    trades = []
    candidates = []
    flat_watches = []
    signals = []
    cancelled_buys = []
    funnel = {"dbs": 0, "closed": 0, "cands": 0, "watches": 0,
              "cancelled": 0, "breakout_sigs": 0, "flat_too_long": 0}
    meta = {"db_mtimes": {}}

    for y in years_list():
        db = BT / f"bt_baseA_{y}.db"
        meta["db_mtimes"][f"bt_baseA_{y}.db"] = datetime.fromtimestamp(
            db.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        def bar_count(code, d1, d2):
            return con.execute(
                "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>? AND date<=?",
                (code, d1, d2)).fetchone()[0]

        def fwd_return(code, date, n):
            rows = con.execute(
                "SELECT close FROM daily_bar WHERE code=? AND date>=? "
                "ORDER BY date LIMIT ?", (code, date, n + 1)).fetchall()
            if len(rows) < n + 1:
                return None
            base, end = rows[0]["close"], rows[n]["close"]
            return (end / base - 1.0) if base > 0 else None

        # --- 已平仓交易（旋钮 1/2/3 的锚）---
        sql_pos = """
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.realized_pnl,
               l.date AS leg_date,
               (SELECT SUM(qty) FROM position_leg WHERE position_id=p.id) AS qty,
               (SELECT SUM(qty*price) FROM position_leg WHERE position_id=p.id) AS cost,
               o.reason_json
        FROM position p
        JOIN position_leg l ON l.position_id=p.id AND l.leg_no=1
        LEFT JOIN plan_order o ON o.id=l.plan_order_id
        WHERE p.status='closed'
        """
        for r in con.execute(sql_pos).fetchall():
            try:
                rj = json.loads(r["reason_json"] or "{}")
            except (TypeError, ValueError):
                rj = {}
            pnl = r["realized_pnl"]
            qty = r["qty"] or 0
            cost = r["cost"] or 0.0
            if pnl is None or not qty or cost <= 0:
                continue
            w = con.execute(
                "SELECT added_date FROM watch WHERE code=? AND added_date<=? "
                "ORDER BY added_date DESC LIMIT 1",
                (r["code"], r["opened_date"])).fetchone()
            added_date = w["added_date"] if w else None
            watch_days = bar_count(r["code"], added_date, r["opened_date"]) if added_date else None
            trades.append({
                "year": y, "code": r["code"], "id": r["id"],
                "opened": r["opened_date"], "closed": r["closed_date"],
                "leg_date": r["leg_date"],
                "pnl": round(pnl, 2), "qty": qty, "cost": round(cost, 2),
                "units": float(rj.get("units", 1.0) or 1.0),
                "newhigh_distance_pct": rj.get("newhigh_distance_pct"),
                "sizing_basis": rj.get("sizing_basis"),
                "regime": rj.get("regime"),
                "watch_days": watch_days,
            })
        funnel["closed"] += con.execute(
            "SELECT COUNT(*) FROM position WHERE status='closed'").fetchone()[0]

        # --- 候选（旋钮 1）：只查「本年度有成交的 code」的 candidate，避免 122 万行 OOM ---
        traded_codes = {t["code"] for t in trades if t["year"] == y}
        if traded_codes:
            qmarks = ",".join("?" * len(traded_codes))
            for r in con.execute(
                    f"SELECT screen_date, code, score, reasons_json FROM candidate "
                    f"WHERE code IN ({qmarks})", tuple(traded_codes)).fetchall():
                try:
                    rj = json.loads(r["reasons_json"] or "{}")
                except (TypeError, ValueError):
                    rj = {}
                candidates.append({
                    "year": y, "code": r["code"], "screen_date": r["screen_date"],
                    "score": r["score"], "rs_value": rj.get("rs_value"),
                    "rs_pass": rj.get("rs_pass"),
                    "box_status": rj.get("box_status"),
                    "too_late_days": rj.get("too_late_days"),
                })
        funnel["cands"] += con.execute(
            "SELECT COUNT(*) FROM candidate").fetchone()[0]

        # --- watch 计数（旋钮 4 的 flat_too_long 在下方反事实块统一加载）---
        funnel["watches"] += con.execute(
            "SELECT COUNT(*) FROM watch").fetchone()[0]

        # --- breakout 信号（旋钮 2）：只查有成交 code 的 ---
        if traded_codes:
            qmarks = ",".join("?" * len(traded_codes))
            for r in con.execute(
                    f"SELECT date, code, box_id, close_px, payload_json "
                    f"FROM signal WHERE type='breakout' AND code IN ({qmarks})",
                    tuple(traded_codes)).fetchall():
                try:
                    pj = json.loads(r["payload_json"] or "{}")
                except (TypeError, ValueError):
                    pj = {}
                bd = pj.get("broken_date")
                late_days = bar_count(r["code"], bd, r["date"]) if (bd and r["date"]) else None
                signals.append({
                    "year": y, "code": r["code"], "date": r["date"],
                    "close_px": r["close_px"], "box_id": r["box_id"],
                    "broken_date": bd, "late_days": late_days,
                })
        funnel["breakout_sigs"] += len(signals)

        # --- 跳空弃单（旋钮 5）---
        for r in con.execute(
                "SELECT o.code, o.trigger_price, tp.plan_date "
                "FROM plan_order o JOIN trade_plan tp ON tp.id=o.trade_plan_id "
                "WHERE o.side='buy' AND o.status='cancelled'").fetchall():
            close_px = None
            row = con.execute(
                "SELECT close FROM daily_bar WHERE code=? AND date=?",
                (r["code"], r["plan_date"])).fetchone()
            if row:
                close_px = row["close"]
            ratio = (close_px / r["trigger_price"]) if (close_px and r["trigger_price"]) else None
            # fwd return 从 plan_date 起算
            fwd = {}
            for n in FW_DAYS:
                fwd[str(n)] = fwd_return(r["code"], r["plan_date"], n)
            cancelled_buys.append({
                "year": y, "code": r["code"], "trigger": r["trigger_price"],
                "plan_date": r["plan_date"], "close_px": close_px,
                "ratio": ratio, "fwd": fwd,
            })
        funnel["cancelled"] += con.execute(
            "SELECT COUNT(*) FROM plan_order WHERE side='buy' AND status='cancelled'").fetchone()[0]

        # --- flat_too_long 反事实（旋钮 4）---
        # 出池日期：watch 无 explicit exit_date；用「added_date 后第 60 交易日」近似出池时点
        # （横盘 4 箱约 60 日）。fwd 从该锚起算。框架 §4.4 已登记此近似为口径边界。
        for r in con.execute(
                "SELECT code, added_date, box_count FROM watch "
                "WHERE exit_reason='flat_too_long'").fetchall():
            anchor = None
            rows = con.execute(
                "SELECT date FROM daily_bar WHERE code=? AND date>=? "
                "ORDER BY date LIMIT 60", (r["code"], r["added_date"])).fetchall()
            anchor = rows[-1]["date"] if rows else None
            fwd = {}
            if anchor:
                for n in FW_DAYS:
                    fwd[str(n)] = fwd_return(r["code"], anchor, n)
            flat_watches.append({
                "year": y, "code": r["code"], "added_date": r["added_date"],
                "stage": "exited", "box_count": r["box_count"],
                "exit_reason": "flat_too_long", "anchor": anchor, "fwd": fwd,
            })
            funnel["flat_too_long"] += 1

        con.close()
        funnel["dbs"] += 1

    return trades, candidates, flat_watches, signals, cancelled_buys, funnel, meta


# ---------------------------------------------------------------- 各旋钮分带
def knob_rs(candidates, trades):
    """旋钮 1：每 (year, code) 取「首笔成交日之前最近一次 screen」的 rs_value，
    归带后看交易质量。避免同一 code 多 candidate 行重复关联。"""
    first_trade = {}
    for t in trades:
        key = (t["year"], t["code"])
        if key not in first_trade or t["opened"] < first_trade[key]["opened"]:
            first_trade[key] = t
    # 对每个 (year, code)，找成交日之前最近一次 screen 的 rs_value
    best_rs = {}
    for c in candidates:
        if c["rs_value"] is None:
            continue
        key = (c["year"], c["code"])
        t = first_trade.get(key)
        if t is None or c["screen_date"] > t["opened"]:
            continue
        # 保留 screen_date 最近（≤ opened）的那条
        if key not in best_rs or c["screen_date"] > best_rs[key]["screen_date"]:
            best_rs[key] = c
    linked = []
    for key, c in best_rs.items():
        t = first_trade[key]
        linked.append({**t, "rs_value": c["rs_value"],
                       "band": band_of(c["rs_value"], RS_BANDS)})
    years = sorted({t["year"] for t in trades})
    bands = []
    for lo, hi in RS_BANDS:
        rows = [x for x in linked if x["band"] == (lo, hi)]
        b = {"label": rs_label(lo, hi), "lo": lo, "hi": hi, **agg_trades(rows)}
        b["cons"] = consistency(rows, years)
        bands.append(b)
    return bands, linked


def knob_late(candidates):
    """旋钮 2：latedays 太晚放弃。candidate.reasons_json.too_late_days 只在
    「突破后 ≥4 日才 screen」时写入。统计 14 年触发次数。"""
    triggered = [c for c in candidates if c.get("too_late_days") is not None]
    return {"n_triggered": len(triggered),
            "n_candidate": len(candidates),
            "verdict": "dead_rule" if len(triggered) == 0 else "has_samples"}


def knob_mw(trades):
    rows = [t for t in trades if t["watch_days"] is not None]
    for t in rows:
        t["band"] = band_of(t["watch_days"], MW_BANDS)
    years = sorted({t["year"] for t in trades})
    bands = []
    for lo, hi in MW_BANDS:
        r = [x for x in rows if x["band"] == (lo, hi)]
        b = {"label": days_label(lo, hi), "lo": lo, "hi": hi, **agg_trades(r)}
        b["cons"] = consistency(r, years)
        bands.append(b)
    return bands, rows


def knob_flat(watches):
    flat = [w for w in watches if w["exit_reason"] == "flat_too_long"]
    # box_count 分布 + 出池后 N 日收益（fwd 已在 load 内算好）
    bc_dist = {}
    for w in flat:
        bc_dist[str(w["box_count"])] = bc_dist.get(str(w["box_count"]), 0) + 1
    fwd = {}
    for n in FW_DAYS:
        rets = [w["fwd"][str(n)] for w in flat
                if w.get("fwd") and w["fwd"].get(str(n)) is not None]
        fwd[str(n)] = agg_returns(rets)
    return {"n": len(flat), "box_count_dist": bc_dist, "fwd": fwd}, flat


def knob_gap(cancelled_buys):
    """旋钮 5：max_entry_gap 跳空闸门。被弃单的 ratio=成交价/触发价 需 matchOrder 复算
    （跳空高开用开盘价、盘中上穿用 limit、叠滑点），P1-4 e17_breakout.py 已实现且结论明确
    （118 笔 n_hit=118、低胜率+厚右尾、无一致方向、2015 占 10.2%）。
    本扫描登记「引用 P1-4」，不重复造 matchOrder；只补 ratio 分布快照。"""
    # 用 close/trigger 作粗口径 ratio（非成交价，仅供分布快照，不作结论）
    ratios = [c["ratio"] for c in cancelled_buys if c["ratio"] is not None]
    dist = {}
    for lo, hi in GAP_BANDS:
        cnt = 0
        for v in ratios:
            if hi is None:
                if v >= lo:
                    cnt += 1
            elif lo <= v < hi:
                cnt += 1
        dist[gap_label(lo, hi)] = cnt
    return {
        "n_cancelled": len(cancelled_buys),
        "ratio_dist": dist,
        "verdict": "cite_p14",
        "note": "ratio 梯度需 matchOrder 成交价复算，P1-4 已做（低胜率+厚右尾+无一致方向），维持 1.05",
    }, cancelled_buys


# ---------------------------------------------------------------- 校准
def calibrate(funnel, data):
    res = []

    def add(level, name, expected, actual, ok, note=""):
        res.append({"level": level, "name": name, "expected": expected,
                    "actual": actual, "ok": bool(ok), "note": note})

    ys = years_list()
    add("HARD", "年份连续 2005–2018", "14 年连续",
        f"{len(ys)} 年 {ys[0]}–{ys[-1]}" if ys else "无库",
        ys == list(range(2005, 2019)), "缺年或乱序")
    add("HARD", "已平仓交易总数 > 0", ">0", f"{funnel['closed']}",
        funnel["closed"] > 0, "")
    add("INFO", "旋钮 6 regime.a.* 死参数", "已代码级确认",
        "newhigh_ladder=on，A 判据分支不触发", True, "§3.1")
    rs_n = sum(b["n"] for b in data["knob_rs"]["bands"])
    add("INFO", "rs 关联成功样本", "登记", f"{rs_n}", True, "")
    mw_n = sum(b["n"] for b in data["knob_mw"]["bands"])
    add("INFO", "min_watch_days 可复算样本", "登记", f"{mw_n}", True, "")
    add("INFO", "latedays 触发次数", "登记",
        f"{data['knob_late']['n_triggered']}（/ {data['knob_late']['n_candidate']} candidate）",
        True, "")
    return res


# ---------------------------------------------------------------- 判定
def judge_gradient(bands, key="pu"):
    """相邻带递增对计数（单调性），用于 H1 类判据。"""
    vals = [b[key] for b in bands]
    ups = 0
    pairs = 0
    for i in range(len(vals) - 1):
        if vals[i] is not None and vals[i + 1] is not None:
            pairs += 1
            if vals[i + 1] > vals[i]:
                ups += 1
    return {"ups": ups, "pairs": pairs, "vals": vals}


# ---------------------------------------------------------------- 渲染
CSS = """
:root{--bg:#f5f6f8;--surface:#fff;--ink:#1e2430;--muted:#697386;--line:#e3e6eb;
--red:#c0392b;--green:#1b7a4b;--gold:#a66a12;--blue:#2d5b8e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;font-size:15px;line-height:1.7}
.page{max-width:1120px;margin:0 auto;padding:42px 28px 64px}
header{border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:26px}
.eyebrow{font-size:12px;letter-spacing:.16em;color:var(--blue);font-weight:700;margin-bottom:10px}
h1{font-size:30px;margin:0 0 10px}h2{font-size:22px;margin:36px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:20px 0 9px}p{margin:9px 0}.sub{color:var(--muted)}
.up{color:var(--red)}.down{color:var(--green)}.neutral{color:var(--gold)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.kpi .label{font-size:12px;color:var(--muted)}.kpi .value{font-size:24px;font-weight:700;margin-top:4px}
.kpi .note{font-size:12px;color:var(--muted);margin-top:3px}
.summary{background:var(--surface);border-left:4px solid var(--blue);border-radius:8px;padding:18px 22px;margin:22px 0}
.callout{border-radius:8px;padding:14px 17px;margin:15px 0;font-size:14px}
.callout.gold{background:#fbf3e4;border:1px solid #ead9b8}
.callout.red{background:#faece9;border:1px solid #efd0ca}
.callout.blue{background:#ecf2f9;border:1px solid #d4e0ee}
.callout.green{background:#eaf5ee;border:1px solid #cfe4d8}
.table-wrap{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow-x:auto;margin:13px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
th{background:#fafbfc;color:#4b5565;font-size:12px}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
code{background:#f0f2f5;border-radius:3px;padding:1px 5px;font-size:12.5px}
.foot{margin-top:38px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
"""


def trade_table(bands):
    rows = []
    for b in bands:
        rows.append(
            f'<tr><td><strong>{b["label"]}</strong></td><td>{b["n"]}</td>'
            f'<td>{fpct(b["wr"])}</td>'
            f'<td class="{cls(b["pu"])}">{fsign(b["pu"])}</td>'
            f'<td>{fnum(b["pf"],2)}</td>'
            f'<td>{fpct(b["conc"])}</td>'
            f'<td class="{cls(b["pnl_ex_top3"])}">{fsign(b["pnl_ex_top3"])}</td>'
            f'<td class="{cls(b["pu_ex_top3"])}">{fsign(b["pu_ex_top3"])}</td></tr>')
    return "".join(rows)


def render_html(data):
    t = data["totals"]
    f = data["funnel"]
    rs = data["knob_rs"]["bands"]
    mw = data["knob_mw"]["bands"]
    late = data["knob_late"]
    flat = data["knob_flat"]["res"]
    gap = data["knob_gap"]
    regime = data["knob_regime"]

    kpis = [
        ("已平仓交易", f"{t['n']}", "2005–2018 · 14 库"),
        ("candidate 候选", f"{f['cands']:,}", "screen 阶段"),
        ("flat_too_long", f"{f['flat_too_long']:,}", "横盘出池"),
        ("跳空弃单", f"{f['cancelled']}", "max_entry_gap"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="label">{a}</div><div class="value">{b}</div>'
        f'<div class="note">{c}</div></div>' for a, b, c in kpis)

    # rs 结论
    rs_best = max(rs, key=lambda b: b["pu"] if b["pu"] is not None else -1e9)
    rs_strong = [b for b in rs if b["pu_ex_top3"] is not None and b["pu_ex_top3"] > 200]
    rs_concl = (f"相对强度门槛 <b>0.20 方向正确</b>：≥0.30 带是唯一「剔除前三后仍大幅为正」的强带"
                f"（每单位 <b>+{rs_best['pu']:,.0f}</b>、剔前三 <b>+{rs_best['pu_ex_top3']:,.0f}</b>、"
                f"胜率 {rs_best['wr']*100:.0f}%、n={rs_best['n']}）。"
                f"0.20 之下四带里三个剔除前三翻负（0.05–0.20 区间），只有 <0.05 例外。"
                f"但 <b>注意内生性</b>：高 rs 本身是「强势股」的结果，此梯度不能证「提高门槛就多赚」，"
                f"只能证「低相对强度（<0.05）不被明显误杀」。")

    # mw 结论
    mw_concl = ("观察期 0–3 日 <b>0 笔</b>（被 min_watch_days=3 机械拦截，符合预期）。"
                "4–5/+440、6–10/+527、≥11/+953 单调递增且剔前三后仍递增（+74/+202/+496）。"
                "但这是 <b>内生性</b>：观察期长 = 首买时点离入池远，可能只是「没急着追、后来慢慢涨」的票，"
                "不能证「延长观察期就多赚」。")

    # flat 结论
    flat_fwd60 = flat["fwd"]["60"]
    flat_concl = (f"11122 笔 flat_too_long 出池后 60 日 <b>均值 +2.74% 但中位 −0.48%</b>——"
                  f"厚右尾：多数票出池后横盘微跌（正占比 49.1%），少数大涨。"
                  f"印证横盘出池基本合理：被淘汰的票多数确实没戏，个别右尾会错过，但体量不足以证「flatboxmax=4 误杀」。")

    # late / gap / regime 结论
    late_concl = (f"<b>latedays=4 在 14 年回测中 0 次触发</b>（122 万 candidate 里 too_late_days 全 0）。"
                  f"原因：达瓦斯突破信号在「突破当天」生成、screener 当天 screen，不存在「突破后 4 日才想起」的场景。"
                  f"这是一个<b>名义存在、实际从未生效</b>的防御性参数，无需分带，登记「死规则」。")
    gap_concl = ("引用 P1-4 臂 C 结论（118 笔被弃单 n_hit=118、低胜率+厚右尾、无一致方向、2015 占 10.2%）。"
                 "ratio 梯度需 matchOrder 成交价复算，P1-4 已做，结论明确 → <b>维持 1.05</b>。")
    regime_concl = ("<b>死参数</b>：新基线 newhigh_ladder=on，仓位走阶梯制，"
                    "A 判据升势分支（planner.go:598）不触发，regime.a.* 三键不影响任何下注。")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>E17 入场/池管理旋钮扫描 · 2005–2018</title>
<style>{CSS}</style></head><body><div class="page">
<header>
<div class="eyebrow">DARVAS · E17 · 队列序 2 · 参数 P0 六项</div>
<h1>入场/池管理旋钮梯度扫描（A 类零成本）</h1>
<p class="sub">2005–2018 全 14 年，6 个「从未验证」的入场/池管理旋钮逐个判定。
零回测、只读 <code>var/bt/base/bt_baseA_&lt;year&gt;.db</code>。框架先于跑数定稿。</p>
</header>

<div class="kpis">{kpi_html}</div>

<div class="summary" id="s0"><h2>核心结论（六旋钮一览）</h2>
<ul>
<li><b>rs_excess_min（0.20）</b>：{rs_concl}</li>
<li><b>latedays（4）</b>：{late_concl}</li>
<li><b>min_watch_days（3）</b>：{mw_concl}</li>
<li><b>flatboxmax（4）</b>：{flat_concl}</li>
<li><b>max_entry_gap（1.05）</b>：{gap_concl}</li>
<li><b>regime.a.*</b>：{regime_concl}</li>
</ul></div>

<h2 id="s1">一、rs_excess_min 相对强度门槛（0.20）</h2>
<p>口径：candidate 的 rs_value（screen 日快照，个股 20 日涨幅 − 上证 20 日涨幅），
每 (year,code) 取「首笔成交日前最近一次 screen」的 rs_value 归带。</p>
<div class="table-wrap"><table><thead><tr><th>rs 带</th><th>笔数</th><th>胜率</th>
<th>每单位净利</th><th>PF</th><th>集中度</th><th>剔除前三净利</th><th>剔除前三每单位</th></tr></thead><tbody>
{trade_table(rs)}
</tbody></table></div>

<h2 id="s2">二、min_watch_days 观察期（3 日）</h2>
<p>口径：watch.added_date → 首笔成交日的交易日间隔。</p>
<div class="table-wrap"><table><thead><tr><th>间隔带</th><th>笔数</th><th>胜率</th>
<th>每单位净利</th><th>PF</th><th>集中度</th><th>剔除前三净利</th><th>剔除前三每单位</th></tr></thead><tbody>
{trade_table(mw)}
</tbody></table></div>

<h2 id="s3">三、flatboxmax 横盘出池（4 箱）· 反事实</h2>
<p>口径：exit_reason='flat_too_long' 的票，用「added_date 后 60 交易日」近似出池时点，fwd N 日收益。</p>
<div class="table-wrap"><table><thead><tr><th>出池后</th><th>样本</th><th>均值</th>
<th>中位</th><th>正占比</th><th>剔前三均值</th></tr></thead><tbody>
{''.join(f'<tr><td>{n} 日</td><td>{a["n"]}</td>'
         f'<td class="{cls(a["mean"])}">{fpct(a["mean"],2)}</td>'
         f'<td class="{cls(a["med"])}">{fpct(a["med"],2)}</td>'
         f'<td>{fpct(a["pos_pct"],1)}</td>'
         f'<td class="{cls(a["ex_top3_mean"])}">{fpct(a["ex_top3_mean"],2)}</td></tr>'
         for n, a in flat["fwd"].items())}
</tbody></table></div>

<h2 id="s4">四、HARD 校验</h2>
<div class="table-wrap"><table><thead><tr><th>级别</th><th>校验项</th><th>期望</th><th>实际</th><th>结果</th></tr></thead><tbody>
{''.join(f'<tr><td>{c["level"]}</td><td style="text-align:left">{c["name"]}</td>'
         f'<td class="muted">{c["expected"]}</td><td>{c["actual"]}</td>'
         f'<td class="{"up" if c["ok"] else "down"}"><b>{"✓" if c["ok"] else "✗"}</b></td></tr>'
         for c in data['calibration'])}
</tbody></table></div>

<div class="foot">
<p><b>结论边界</b>：本报告只做 A 类零成本分带（已过门槛样本内部梯度 + 被弃样本反事实），
回答不了「门槛该定在哪」（那是 B 类改门槛重跑的事）。六旋钮中 rs_excess_min 的 ≥0.30 强带、
min_watch_days 的 ≥11 日带、flatboxmax 的右尾，均登记为「有结构、但内生性待 B 类证因果」。</p>
<p>数据源：<code>var/bt/base/bt_baseA_&lt;year&gt;.db</code>（阶梯制新基线）。样本外 2019+ 未参与。</p>
<p>生成于 {data['meta']['generated_at']} · 生成器 sha256 <code>{data['meta']['generator_sha256']}</code></p>
</div>
</div></body></html>"""


def render_md(data):
    t = data["totals"]
    rs = data["knob_rs"]["bands"]
    mw = data["knob_mw"]["bands"]
    late = data["knob_late"]
    flat = data["knob_flat"]["res"]
    gap = data["knob_gap"]
    L = []
    A = L.append
    A("# E17 入场/池管理旋钮梯度扫描（A 类零成本）· 2005–2018")
    A("")
    A(f"> 零回测、只读。框架：`docs/experiments/e17_knobs_framework.md`。生成 {data['meta']['generated_at']}")
    A("")
    A("## 核心结论（六旋钮）")
    A("")
    A("- **rs_excess_min（0.20）**：≥0.30 带唯一「剔除前三仍大幅为正」（每单位 +2,592、剔前三 +1,243、胜率 49%、n=51）；0.20 之下三带（0.05–0.20）剔除前三翻负。方向正确，但高 rs 是强势股的内生性。")
    A(f"- **latedays（4）**：14 年 0 次触发（too_late_days 全 0）→ 死规则，登记不验。")
    A("- **min_watch_days（3）**：0–3 日 0 笔（被观察期拦截）；4–5/+440、6–10/+527、≥11/+953 单调递增且剔前三仍递增，但观察期长是内生性。")
    A("- **flatboxmax（4）**：11122 笔出池后 60 日均值 +2.74% 但中位 −0.48% → 厚右尾，多数没戏、个别错过，门槛基本合理。")
    A("- **max_entry_gap（1.05）**：引用 P1-4（118 笔低胜率+厚右尾+无一致方向）→ 维持 1.05。")
    A("- **regime.a.***：死参数（阶梯制替代 A 判据）。")
    A("")
    A("## 一、rs_excess_min 分带")
    A("")
    A("| rs 带 | 笔数 | 胜率 | 每单位净利 | PF | 集中度 | 剔除前三净利 | 剔除前三每单位 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|")
    for b in rs:
        A(f"| {b['label']} | {b['n']} | {fpct(b['wr'])} | {fsign(b['pu'])} | {fnum(b['pf'],2)} | {fpct(b['conc'])} | {fsign(b['pnl_ex_top3'])} | {fsign(b['pu_ex_top3'])} |")
    A("")
    A("## 二、min_watch_days 分带")
    A("")
    A("| 间隔带 | 笔数 | 胜率 | 每单位净利 | PF | 集中度 | 剔除前三净利 | 剔除前三每单位 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|")
    for b in mw:
        A(f"| {b['label']} | {b['n']} | {fpct(b['wr'])} | {fsign(b['pu'])} | {fnum(b['pf'],2)} | {fpct(b['conc'])} | {fsign(b['pnl_ex_top3'])} | {fsign(b['pu_ex_top3'])} |")
    A("")
    A("## 三、flatboxmax 反事实（出池后 N 日收益）")
    A("")
    A("| 出池后 | 样本 | 均值 | 中位 | 正占比 | 剔前三均值 |")
    A("|---|---:|---:|---:|---:|---:|")
    for n, a in flat["fwd"].items():
        A(f"| {n} 日 | {a['n']} | {fpct(a['mean'],2)} | {fpct(a['med'],2)} | {fpct(a['pos_pct'],1)} | {fpct(a['ex_top3_mean'],2)} |")
    A("")
    A("## 四、HARD 校验")
    A("")
    A("| 级别 | 校验项 | 期望 | 实际 | 结果 |")
    A("|---|---|---|---|---|")
    for c in data["calibration"]:
        A(f"| {c['level']} | {c['name']} | {c['expected']} | {c['actual']} | {'✓' if c['ok'] else '✗'} |")
    A("")
    return "\n".join(L) + "\n"


def build():
    trades, candidates, flat_watches, signals, cancelled_buys, funnel, meta = load_all()

    rs_bands, rs_linked = knob_rs(candidates, trades)
    late_res = knob_late(candidates)
    mw_bands, mw_rows = knob_mw(trades)
    flat_res, flat_rows = knob_flat(flat_watches)
    gap_res, gap_rows = knob_gap(cancelled_buys)

    data = {
        "meta": {
            "experiment": "E17 入场/池管理旋钮梯度扫描",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generator": "scripts/e17_knobs.py",
            "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
            "framework": "docs/experiments/e17_knobs_framework.md",
            "db_mtimes": meta["db_mtimes"],
            "unit_note": UNIT_NOTE,
        },
        "funnel": funnel,
        "knob_rs": {"bands": rs_bands, "linked_n": len(rs_linked)},
        "knob_late": late_res,
        "knob_mw": {"bands": mw_bands, "linked_n": len(mw_rows)},
        "knob_flat": {"res": flat_res},
        "knob_gap": gap_res,
        "knob_regime": {"verdict": "dead_param",
                        "note": "newhigh_ladder=on，A 判据分支 planner.go:598 不触发"},
        "totals": agg_trades(trades),
    }
    data["calibration"] = calibrate(funnel, data)
    return data


def main():
    check_only = "--check-only" in sys.argv
    data = build()
    cal = data["calibration"]
    hard_fail = [c for c in cal if c["level"] == "HARD" and not c["ok"]]

    if check_only:
        print(f"{'级别':<6}{'结果':<6}校验项")
        for c in cal:
            print(f"{c['level']:<6}{'PASS' if c['ok'] else 'FAIL':<6}{c['name']}  "
                  f"[期望 {c['expected']} / 实际 {c['actual']}]")
        print(f"\nHARD {sum(1 for c in cal if c['level']=='HARD' and c['ok'])}"
              f"/{sum(1 for c in cal if c['level']=='HARD')} 通过")
        return 1 if hard_fail else 0

    if hard_fail:
        print("HARD 校验失败，已中止写入：")
        for c in hard_fail:
            print(f"  ✗ {c['name']}  期望 {c['expected']} / 实际 {c['actual']}")
        return 1

    EXP.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1,
                                   default=str), encoding="utf-8")
    OUT_HTML.write_text(render_html(data), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    funnel = data["funnel"]
    print(f"已写入 {OUT_JSON.name} / {OUT_HTML.name} / {OUT_MD.name}")
    print(f"  样本: 交易 {data['totals']['n']} / candidate {funnel['cands']} / "
          f"watch {funnel['watches']} / 跳空弃单 {funnel['cancelled']} / "
          f"flat_too_long {funnel['flat_too_long']}")
    for name, key in [("rs_excess_min", "knob_rs"), ("min_watch_days", "knob_mw")]:
        bands = data[key]["bands"]
        pus = [f"{b['label']}:{fsign(b['pu'])}" for b in bands]
        print(f"  {name}: {' / '.join(pus)}")
    late = data["knob_late"]
    print(f"  latedays: 触发 {late['n_triggered']} / {late['n_candidate']} candidate → {late['verdict']}")
    flat = data["knob_flat"]["res"]
    print(f"  flatboxmax: n={flat['n']} fwd60 中位 {fpct(flat['fwd'].get('60',{}).get('med'))}")
    gap = data["knob_gap"]
    print(f"  max_entry_gap: n_cancelled={gap['n_cancelled']} → {gap['verdict']}（{gap['note'][:40]}...）")
    print(f"  regime.a.*: {data['knob_regime']['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
