#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LB-2 · Box 阶梯速度梯度实验 · 专项报告生成器。

实验：docs/29 §五之二 队列第 9 项 LB-2（十一维 #11「Box 生命体征」之阶梯速度），A 类零成本后验，不重跑回测、不改回测器。
框架：docs/experiments/e17_lb2_ladderspeed_framework.md（零结果纯规则，含 H1/H2 判据与分带规则）

核心口径（框架 §4）：
  ladder_gap = 该 code 的 daily_bar 在 [前驱 broken_date, 当前 broken_date] 的行数（含两端）
  pu         = realized_pnl / units

关联链（框架 §3.2）：position → leg1 → plan_order → trade_plan.execute_date + reason_json(box_top, box_bottom)
  → box 表反向精确匹配（entry_box_id 在新基线库全为 NULL）→ prev_box_id → 前驱 box 的 broken_date。

输出（docs/experiments/，实验报告唯一沉淀处）：
  e17_lb2_ladderspeed_report.html / .md / _data.json

用法：
  python scripts/e17_lb2_ladderspeed.py               # 正式生成（HARD 失败即中止）
  python scripts/e17_lb2_ladderspeed.py --check-only  # 只跑校准，不写文件
  python scripts/e17_lb2_ladderspeed.py --allow-drift # HARD 降级为 WARN
"""
import hashlib
import json
import os
import sqlite3
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
EXP = ROOT / "docs" / "experiments"
OUT_HTML = EXP / "e17_lb2_ladderspeed_report.html"
OUT_MD = EXP / "e17_lb2_ladderspeed_report.md"
OUT_JSON = EXP / "e17_lb2_ladderspeed_data.json"

# ---------------------------------------------------------------- 分带定义
# 阶梯速度四带（框架 §5.1，左闭右开，预设固定值 15/30/60）
BANDS = [(1, 15), (15, 30), (30, 60), (60, None)]

# ---------------------------------------------------------------- 校准基线（框架 §7.6，结构性锚点）
BASELINE = {
    "closed": 1481,
    "pnl": 1461228.2,
    "pnl_tol": 1.0,
    "first_level": 9,     # 首级箱体（prev_box_id 为 NULL）笔数，容差 ±5
    "first_level_tol": 5,
}

TIER_NOTE = "1 单位 = 2% 权益；E17 阶梯 t1=3 / t2=2 / t3=1 / t4=0.5"


# ---------------------------------------------------------------- 基础工具
def years_list():
    return sorted(int(os.path.basename(p).split("_")[2].split(".")[0])
                  for p in BT.glob("base/bt_baseA_*.db"))


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


def day_label(lo, hi):
    if hi is None:
        return f"{lo} 日+"
    return f"{lo}–{hi-1} 日"


def share_pct(x):
    if x is None:
        return "—"
    if abs(x) < 5e-5:
        return "0.0%"
    return f"{x * 100:.1f}%"


# ---------------------------------------------------------------- 数据加载
def load_trades():
    """14 库逐笔加载：交易样本 + 反向匹配 box 算阶梯速度。返回 (trades, funnel, meta)。"""
    trades, funnel = [], {"dbs": 0, "closed": 0, "no_box_fields": 0,
                          "match_fail": 0, "matched": 0, "neg_gap": 0,
                          "multi_match": 0, "first_level": 0, "chain_broken": 0}
    meta = {"db_mtimes": {}, "close_reasons": {}}
    for y in years_list():
        db = BT / "base" / f"bt_baseA_{y}.db"
        meta["db_mtimes"][f"bt_baseA_{y}.db"] = datetime.fromtimestamp(
            db.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sql = """
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.realized_pnl,
               p.close_reason, p.avg_cost,
               tp.execute_date AS ex_date, o.reason_json,
               (SELECT SUM(qty)       FROM position_leg WHERE position_id=p.id) AS qty,
               (SELECT SUM(qty*price) FROM position_leg WHERE position_id=p.id) AS cost_basis,
               (SELECT COUNT(*)       FROM position_leg WHERE position_id=p.id) AS n_legs
        FROM position p
        JOIN position_leg l ON l.position_id=p.id AND l.leg_no=1
        JOIN plan_order o ON o.id=l.plan_order_id
        JOIN trade_plan tp ON tp.id=o.trade_plan_id
        WHERE p.status='closed'
        """
        rows = con.execute(sql).fetchall()
        funnel["closed"] += len(rows)
        for r in rows:
            try:
                rj = json.loads(r["reason_json"] or "{}")
            except (TypeError, ValueError):
                rj = {}
            box_top = rj.get("box_top")
            box_bottom = rj.get("box_bottom")
            pnl = r["realized_pnl"]
            qty = r["qty"] or 0
            cost = r["cost_basis"] or 0.0
            units = float(rj.get("units", 1.0) or 1.0)

            # 非 breakout 或缺 box 字段 → 不可复算
            if box_top is None or box_bottom is None:
                funnel["no_box_fields"] += 1
                trades.append({
                    "year": y, "code": r["code"], "pnl": round(pnl, 2),
                    "units": units, "qty": qty, "cost_basis": round(cost, 2),
                    "ladder_gap": None, "recomputable": False, "first_level": False,
                    "reason": "缺 box_top/box_bottom",
                })
                continue

            # 反向匹配 box（含 prev_box_id）
            bx = con.execute(
                """SELECT id, prev_box_id, bottom_confirmed, broken_date, top, bottom, status
                   FROM box WHERE code=? AND ABS(top-?)<1e-6 AND ABS(bottom-?)<1e-6
                   AND status='broken_up'""",
                (r["code"], box_top, box_bottom)).fetchall()
            if len(bx) == 0:
                funnel["match_fail"] += 1
                trades.append({
                    "year": y, "code": r["code"], "pnl": round(pnl, 2),
                    "units": units, "qty": qty, "cost_basis": round(cost, 2),
                    "ladder_gap": None, "recomputable": False, "first_level": False,
                    "reason": "box 匹配失败",
                })
                continue
            # 多命中：同票相近价位反复成箱，取 broken_date 最新
            if len(bx) > 1:
                funnel["multi_match"] += 1
                bx = sorted(bx, key=lambda b: b["broken_date"] or "", reverse=True)
            b = bx[0]

            broken = b["broken_date"]
            if broken is None:
                funnel["match_fail"] += 1
                trades.append({
                    "year": y, "code": r["code"], "pnl": round(pnl, 2),
                    "units": units, "qty": qty, "cost_basis": round(cost, 2),
                    "ladder_gap": None, "recomputable": False, "first_level": False,
                    "reason": "box 缺 broken_date",
                })
                continue

            # 首级箱体（prev_box_id 为 NULL）→ 无前驱，阶梯速度不可算
            prev_id = b["prev_box_id"]
            if prev_id is None:
                funnel["first_level"] += 1
                funnel["matched"] += 1
                cr = (r["close_reason"] or "?").strip() or "?"
                meta["close_reasons"][cr] = meta["close_reasons"].get(cr, 0) + 1
                trades.append({
                    "year": y, "code": r["code"], "opened": r["opened_date"],
                    "closed": r["closed_date"], "close_reason": cr,
                    "pnl": round(pnl, 2), "qty": qty, "cost_basis": round(cost, 2),
                    "units": units,
                    "ndist": rj.get("newhigh_distance_pct"),
                    "box_top": box_top, "box_bottom": box_bottom,
                    "broken_date": broken,
                    "ladder_gap": None, "recomputable": False, "first_level": True,
                    "reason": "首级箱体（无前驱）",
                    "n_legs": r["n_legs"],
                })
                continue

            # 前驱 box 的 broken_date（沿 prev_box_id）
            prev = con.execute("SELECT broken_date FROM box WHERE id=?",
                               (prev_id,)).fetchone()
            if prev is None or prev["broken_date"] is None:
                funnel["chain_broken"] += 1
                trades.append({
                    "year": y, "code": r["code"], "pnl": round(pnl, 2),
                    "units": units, "qty": qty, "cost_basis": round(cost, 2),
                    "ladder_gap": None, "recomputable": False, "first_level": False,
                    "reason": "前驱断链（前驱不存在或缺 broken_date）",
                })
                continue

            # 阶梯速度交易日计数（含两端）
            gap = con.execute(
                "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
                (r["code"], prev["broken_date"], broken)).fetchone()[0]
            if gap < 1:
                funnel["neg_gap"] += 1
                trades.append({
                    "year": y, "code": r["code"], "pnl": round(pnl, 2),
                    "units": units, "qty": qty, "cost_basis": round(cost, 2),
                    "ladder_gap": None, "recomputable": False, "first_level": False,
                    "reason": "阶梯速度<1（前驱 broken_date 晚于当前 broken_date）",
                })
                continue

            funnel["matched"] += 1
            cr = (r["close_reason"] or "?").strip() or "?"
            meta["close_reasons"][cr] = meta["close_reasons"].get(cr, 0) + 1
            trades.append({
                "year": y, "code": r["code"], "opened": r["opened_date"],
                "closed": r["closed_date"], "close_reason": cr,
                "pnl": round(pnl, 2), "qty": qty, "cost_basis": round(cost, 2),
                "units": units,
                "ndist": rj.get("newhigh_distance_pct"),
                "box_top": box_top, "box_bottom": box_bottom,
                "prev_box_id": prev_id, "prev_broken_date": prev["broken_date"],
                "broken_date": broken,
                "ladder_gap": gap, "recomputable": True, "first_level": False,
                "reason": None,
                "n_legs": r["n_legs"],
            })
        con.close()
        funnel["dbs"] += 1
    return trades, funnel, meta


def band_of(d):
    for lo, hi in BANDS:
        if (hi is None and d >= lo) or (hi is not None and lo <= d < hi):
            return (lo, hi)
    return None


# ---------------------------------------------------------------- 聚合
def agg(rows):
    n = len(rows)
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
    wr = len(wins) / n if n else None
    ranked = sorted((r["pnl"] for r in rows), reverse=True)
    top3 = sum(ranked[:3])
    ex_rows = sorted(rows, key=lambda x: -x["pnl"])[3:] if len(rows) > 3 else []
    ex_pnl = sum(r["pnl"] for r in ex_rows)
    ex_units = sum(r["units"] for r in ex_rows)
    gaps = sorted(r["ladder_gap"] for r in rows if r["ladder_gap"] is not None)
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "pnl": pnl,
        "units": units, "wr": wr, "pu": (pnl / units if units else None),
        "pf": (gw / abs(gl) if gl else None),
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "bewr": bewr,
        "gap": ((wr - bewr) * 100 if (wr is not None and bewr is not None) else None),
        "top3": top3,
        "conc": (top3 / gw * 100 if (gw > 0 and top3 > 0) else None),
        "pnl_ex_top3": ex_pnl,
        "pu_ex_top3": (ex_pnl / ex_units if ex_units else None),
        "gap_med": pct_of(gaps, 0.5),
        "gap_mean": (sum(gaps) / len(gaps)) if gaps else None,
        "gap_p25": pct_of(gaps, 0.25),
        "gap_p75": pct_of(gaps, 0.75),
    }


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


def band_rows(trades, lo, hi):
    rc = [t for t in trades if t["recomputable"]]
    if hi is None:
        return [t for t in rc if t["ladder_gap"] >= lo]
    return [t for t in rc if lo <= t["ladder_gap"] < hi]


def band_stat(trades, bands):
    out = []
    for lo, hi in bands:
        out.append({"label": day_label(lo, hi), "lo": lo, "hi": hi,
                    **agg(band_rows(trades, lo, hi))})
    return out


def consistency(trades, lo, hi):
    years = sorted({t["year"] for t in trades})
    per_year = []
    for y in years:
        s = agg(band_rows([t for t in trades if t["year"] == y], lo, hi))
        per_year.append({"year": y, "n": s["n"], "pnl": s["pnl"], "pu": s["pu"]})
    ws = [p for p in per_year if p["n"] > 0]
    M = len(ws)
    P = sum(1 for p in ws if p["pnl"] > 0)
    filt = [p for p in ws if p["n"] >= 3]
    Mf = len(filt)
    Pf = sum(1 for p in filt if p["pnl"] > 0)
    longest, cur, last = 0, 0, 0
    for p in filt:
        sgn = 1 if p["pnl"] > 0 else -1
        cur = cur + 1 if sgn == last else 1
        last = sgn
        longest = max(longest, cur)
    tot = sum(p["pnl"] for p in ws)
    pos_sum = sum(p["pnl"] for p in ws if p["pnl"] > 0)
    return {"M": M, "P": P, "ratio": (P / M if M else None),
            "Mf": Mf, "Pf": Pf, "ratio_f": (Pf / Mf if Mf else None),
            "longest_run": longest,
            "pos_share": (pos_sum / tot if tot else None),
            "per_year": per_year}


def verdict(s, c):
    if s["n"] < 30:
        return "样本不足"
    if c["Mf"] < 8:
        return "出现年数不足"
    ratio, gap = c["ratio_f"], s["gap"]
    pu, pu_ex = s["pu"], s["pu_ex_top3"]
    gapok = gap is not None and gap >= 2
    gapneg = gap is not None and gap <= -2
    if pu is not None and pu > 0 and pu_ex is not None and pu_ex <= 0:
        return "假 edge（前三撑起）"
    if ratio is not None and ratio >= 0.70:
        return "edge" if gapok else "稳但缺口不足"
    if ratio is not None and ratio <= 0.30:
        return "负 edge" if gapneg else "跨年偏负（缺口未达 −2pp）"
    return "不稳定（正但非跨年）" if (pu is not None and pu > 0) else "不稳定（负但非跨年）"


# ---------------------------------------------------------------- 判定
def judge_h1(bands):
    # H1 方向 = 阶梯间隔越短质量越高 → 每单位净利随间隔递减
    pus = [b["pu"] for b in bands]
    downs = sum(1 for i in range(len(pus) - 1)
                if pus[i] is not None and pus[i + 1] is not None and pus[i + 1] < pus[i])
    return {"ok": downs >= 3, "downs": downs, "pairs": len(pus) - 1, "pus": pus}


def judge_h2(bands):
    b0 = bands[0]  # <15 日带
    return {"ok": (b0["pu"] is not None and b0["pu"] < 0
                   and b0["n"] >= 30
                   and b0["pnl_ex_top3"] is not None and b0["pnl_ex_top3"] < 0),
            "pu": b0["pu"], "n": b0["n"], "pnl_ex_top3": b0["pnl_ex_top3"]}


# ---------------------------------------------------------------- 校准
def calibrate(trades, funnel, data):
    res = []

    def add(level, name, expected, actual, ok, note=""):
        res.append({"level": level, "name": name, "expected": expected,
                    "actual": actual, "ok": bool(ok), "note": note})

    ys = years_list()
    add("HARD", "年份连续 2005–2018", "14 年连续",
        f"{len(ys)} 年 {ys[0]}–{ys[-1]}" if ys else "无库",
        ys == list(range(2005, 2019)), "缺年或乱序")

    add("HARD", "①样本总对账 vs 基线 1481", f"{BASELINE['closed']}",
        f"{funnel['closed']}", funnel["closed"] == BASELINE["closed"], "")

    n_bands = sum(s["n"] for s in data["bands"])
    n_fl = data["first_level"]["n"]
    n_un = data["unrecomputable"]["n"]
    add("HARD", "②加总恒等式 Σ四带 + 首级 + 不可复算 = 1481", f"{BASELINE['closed']}",
        f"{n_bands + n_fl + n_un}", n_bands + n_fl + n_un == BASELINE["closed"],
        "不静默丢弃")

    add("HARD", "④反向匹配唯一性（多命中已按规则消解，失败=0）",
        "失败 0", f"匹配 {funnel['matched']} / 失败 {funnel['match_fail']} / 多命中 {funnel['multi_match']}",
        funnel["match_fail"] == 0, "多命中按 broken_date 最新取一")

    add("HARD", "⑤前驱链完整（断链=0）", "0 笔",
        f"{funnel['chain_broken']} 笔", funnel["chain_broken"] == 0, "")

    add("HARD", "⑥阶梯速度非负（无 前驱晚于当前）", "0 笔",
        f"{funnel['neg_gap']} 笔", funnel["neg_gap"] == 0, "")

    # 成本口径齐备：可复算样本（recomputable=True，即 ladder_gap 非空）的 cost_basis>0 且 qty>0
    n_rc = sum(1 for t in trades if t["recomputable"])
    ok_cost = sum(1 for t in trades if t["recomputable"] and t["qty"] > 0 and t["cost_basis"] > 0)
    add("HARD", "⑦成本口径齐备（可复算样本 cost_basis>0 且 qty>0）",
        f"{n_rc}/{n_rc}", f"{ok_cost}/{n_rc}",
        ok_cost == n_rc, "成本来自 position_leg 求和（仅可复算样本）")

    tot = sum(t["pnl"] for t in trades)
    add("HARD", "⑧逐年净利对账 Σrealized_pnl ≈ +1,461,228.2", f"{BASELINE['pnl']:,.1f}",
        f"{tot:,.1f}", abs(tot - BASELINE["pnl"]) <= BASELINE["pnl_tol"], "新基线口径")

    ym = data["year_matrix"]
    add("HARD", "③逐年符号表齐备 2005–2018", "14 行",
        f"{len(ym)} 行", len(ym) == 14 and all(str(y) in ym for y in range(2005, 2019)),
        "缺年须登记原因")

    add("WARN", "首级箱体笔数（结构性锚点 ≈9）",
        f"{BASELINE['first_level']}（±{BASELINE['first_level_tol']}）",
        f"{funnel['first_level']} 笔",
        abs(funnel["first_level"] - BASELINE["first_level"]) <= BASELINE["first_level_tol"],
        "prev_box_id 为 NULL 的入场 box")

    add("WARN", "不可复算样本占比", "登记",
        f"{funnel['no_box_fields'] + funnel['match_fail'] + funnel['chain_broken'] + funnel['neg_gap']} 笔",
        True, "缺 box 字段 / 匹配失败 / 前驱断链 / 阶梯异常")
    add("WARN", "多命中 box 笔数", "登记", f"{funnel['multi_match']} 笔",
        True, "同票相近价位反复成箱")
    return res


# ---------------------------------------------------------------- 构建
def build():
    trades, funnel, meta = load_trades()

    bands = band_stat(trades, BANDS)
    for b in bands:
        b["cons"] = consistency(trades, b["lo"], b["hi"])
        b["verdict"] = verdict(b, b["cons"])

    fl_rows = [t for t in trades if t["first_level"]]
    fl = agg(fl_rows)
    fl["label"] = "首级箱体（无前驱，不可算阶梯速度）"

    un_rows = [t for t in trades if (not t["recomputable"]) and (not t["first_level"])]
    un = agg(un_rows)
    un["label"] = "不可复算（缺 box 字段 / 匹配失败 / 前驱断链）"

    # 逐年矩阵
    years = sorted({t["year"] for t in trades})
    ym = OrderedDict()
    for y in years:
        yt = [t for t in trades if t["year"] == y]
        row = {"overall": agg(yt)}
        row["bands"] = {}
        for lo, hi in BANDS:
            s = agg(band_rows(yt, lo, hi))
            row["bands"][day_label(lo, hi)] = {"n": s["n"], "pnl": s["pnl"], "pu": s["pu"]}
        ym[str(y)] = row

    # 附录切片：newhigh_distance 交叉
    t1 = [t for t in trades if t["recomputable"] and t["ndist"] is not None and t["ndist"] <= 15]
    slices = {
        "t1": {"label": "距 52 周新高 ≤15%（甜区）", "n_rows": len(t1), **agg(t1)},
        "rest": {"label": "距 52 周新高 >15%",
                 "n_rows": len([t for t in trades if t["recomputable"] and t["ndist"] is not None and t["ndist"] > 15]),
                 **agg([t for t in trades if t["recomputable"] and t["ndist"] is not None and t["ndist"] > 15])},
    }

    # 阶梯速度直方图
    rc = [t for t in trades if t["recomputable"]]
    hist = []
    for lo, hi in BANDS:
        rows = band_rows(trades, lo, hi)
        hist.append({"label": day_label(lo, hi), "lo": lo, "hi": hi,
                     "n": len(rows),
                     "share": (len(rows) / len(rc)) if rc else None,
                     "pnl": sum(r["pnl"] for r in rows)})

    h1 = judge_h1(bands)
    h2 = judge_h2(bands)

    data = {
        "meta": {
            "experiment": "LB-2 Box 阶梯速度梯度",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generator": "scripts/e17_lb2_ladderspeed.py",
            "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
            "framework": "docs/experiments/e17_lb2_ladderspeed_framework.md",
            "db_mtimes": meta["db_mtimes"],
            "close_reasons": meta["close_reasons"],
            "unit_note": TIER_NOTE,
        },
        "funnel": funnel,
        "trades": trades,
        "bands": bands,
        "first_level": fl,
        "unrecomputable": un,
        "year_matrix": ym,
        "hist": hist,
        "slices": slices,
        "h1": h1, "h2": h2,
        "totals": agg(trades),
    }
    data["calibration"] = calibrate(trades, funnel, data)

    # 结论文本
    t = data["totals"]
    rc_n = funnel["matched"]
    concl = []
    concl.append(f"2005–2018 共 <b>{funnel['closed']}</b> 笔已平仓交易，其中可复算阶梯速度 <b>{rc_n}</b> 笔、"
                 f"首级箱体 <b>{funnel['first_level']}</b> 笔、"
                 f"不可复算 <b>{funnel['closed'] - rc_n - funnel['first_level']}</b> 笔。")
    concl.append(f"阶梯速度四带每单位净利：" + " / ".join(
        f"<b>{fsign(b['pu'])}</b>（{b['label']}）" for b in bands) + "。")
    concl.append(f"阶梯速度四带胜率：" + " / ".join(
        f"<b>{fpct(b['wr'])}</b>" for b in bands) + "。")
    concl.append(f"H1（阶梯速度与突破质量单调相关）<b>{'成立' if h1['ok'] else '不成立'}</b>"
                 f"（递减相邻对 {h1['downs']}/{h1['pairs']}）；"
                 f"H2（快梯突破质量差）<b>{'成立' if h2['ok'] else '不成立'}</b>。")
    data["conclusion"] = concl

    data["hyp_text"] = {
        "h1": f"H1 阶梯速度单调相关 —— {'成立' if h1['ok'] else '不成立'}",
        "h2": f"H2 快梯 box 质量差 —— {'成立' if h2['ok'] else '不成立'}",
    }
    return data


# ---------------------------------------------------------------- 渲染
CSS = """
:root{--bg:#f5f6f8;--surface:#fff;--ink:#1e2430;--muted:#697386;--line:#e3e6eb;
--soft:#f0f2f5;--red:#c0392b;--red2:#faece9;--green:#1b7a4b;--green2:#e9f5ef;
--gold:#a66a12;--gold2:#fbf3e4;--blue:#2d5b8e;--blue2:#ecf2f9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif;font-size:15px;line-height:1.7}
.page{max-width:1220px;margin:0 auto;padding:42px 28px 64px}
header{border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:26px}
.eyebrow{font-size:12px;letter-spacing:.16em;color:var(--blue);font-weight:700;margin-bottom:10px}
h1{font-size:32px;line-height:1.28;margin:0 0 10px;letter-spacing:-.02em}
h2{font-size:23px;margin:38px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:17px;margin:20px 0 9px}
p{margin:9px 0}.sub{color:var(--muted);max-width:960px;margin:0}
.muted{color:var(--muted)}.small{font-size:12.5px}
.up{color:var(--red)}.down{color:var(--green)}.neutral{color:var(--gold)}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin:22px 0}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:17px 18px}
.kpi .label{font-size:12px;color:var(--muted)}
.kpi .value{font-size:25px;font-weight:700;line-height:1.25;margin-top:4px}
.kpi .note{font-size:12px;color:var(--muted);margin-top:3px}
.summary{background:var(--surface);border-left:4px solid var(--blue);border-radius:8px;padding:18px 22px;margin:22px 0}
.summary h2{border:0;margin:0 0 8px;padding:0;font-size:20px}
.summary ul{margin:8px 0 0 19px;padding:0}.summary li{margin:6px 0}
.callout{border-radius:8px;padding:14px 17px;margin:15px 0;font-size:14px}
.callout.gold{background:var(--gold2);border:1px solid #ead9b8}
.callout.red{background:var(--red2);border:1px solid #efd0ca}
.callout.blue{background:var(--blue2);border:1px solid #d4e0ee}
.table-wrap{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow-x:auto;margin:13px 0}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:760px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
th{background:#fafbfc;color:#4b5565;font-size:12px;font-weight:600}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fcfcfd}
tr.empty td{background:#f4f5f7;color:#9aa1ad;font-style:italic}
tr.subset td{background:#f8f9fb;font-style:italic;color:#5a6472}
.vd{font-size:11.5px;padding:1px 7px;border-radius:999px;white-space:nowrap}
.vd-edge{background:var(--red2);color:var(--red);font-weight:700}
.vd-fake{background:var(--gold2);color:var(--gold);font-weight:700}
.vd-neg{background:var(--green2);color:var(--green);font-weight:700}
.vd-unstable{background:var(--soft);color:#5a6472}
.vd-small{background:#f1f3f6;color:#8b93a0}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:17px 19px;margin:14px 0}
.hist-row{display:grid;grid-template-columns:118px 1fr 132px;align-items:center;gap:10px;margin:7px 0}
.hist-label{font-size:12.5px}
.hist-track{position:relative;height:20px;background:var(--soft);border-radius:4px;overflow:hidden}
.hist-track b{position:absolute;left:0;top:3px;bottom:3px;border-radius:2px}
.hist-val{text-align:right;font-size:12.5px;font-variant-numeric:tabular-nums}
.matrix td{font-size:12px}
.hyp{display:grid;grid-template-columns:1fr;gap:10px}
.hyp-card{background:var(--surface);border:1px solid var(--line);border-left-width:4px;border-radius:8px;padding:14px 17px}
.hyp-card.ok{border-left-color:var(--red)}
.hyp-card.no{border-left-color:var(--green)}
.hyp-card .t{font-weight:700;font-size:15.5px}
.hyp-card .d{font-size:13px;color:var(--muted);margin-top:4px}
.foot{margin-top:38px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
code{background:var(--soft);border-radius:3px;padding:1px 5px;font-size:12.5px}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}.page{padding:28px 16px 48px}h1{font-size:26px}}
"""


def vd_html(v):
    if v == "edge":
        k = "vd-edge"
    elif v.startswith("假 edge"):
        k = "vd-fake"
    elif v.startswith("负 edge"):
        k = "vd-neg"
    elif v.startswith(("不稳定", "稳但", "跨年偏负")):
        k = "vd-unstable"
    else:
        k = "vd-small"
    return f'<span class="vd {k}">{v}</span>'


def main_table(rows):
    head = ("<thead><tr><th>阶梯速度（交易日）</th><th>笔数</th><th>胜率</th>"
            "<th>每单位净利</th><th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th>"
            "<th>剔除前三净利</th><th>间隔中位</th><th>净利</th><th>裁定</th></tr></thead><tbody>")
    parts = ['<div class="table-wrap"><table>', head]
    for b in rows:
        if b["n"] == 0:
            parts.append(f'<tr class="empty"><td><strong>{b["label"]}</strong></td><td>0</td>'
                         f'<td colspan="10">无样本</td>'
                         f'<td>{vd_html(b.get("verdict", "样本不足"))}</td></tr>')
            continue
        conc = f'{b["conc"]:.1f}%' if b["conc"] is not None else "—"
        parts.append(
            f'<tr><td><strong>{b["label"]}</strong></td>'
            f'<td>{b["n"]}{" ⚠" if b["n"] < 30 else ""}</td>'
            f'<td>{fpct(b["wr"])}</td>'
            f'<td class="{cls(b["pu"])}">{fsign(b["pu"])}</td>'
            f'<td>{fpct(b["bewr"])}</td>'
            f'<td class="{cls(b["gap"])}">{fpp(b["gap"])}</td>'
            f'<td>{fnum(b["pf"], 2) if b["pf"] is not None else "—"}</td>'
            f'<td>{conc}</td>'
            f'<td class="{cls(b["pnl_ex_top3"])}">{fsign(b["pnl_ex_top3"])}</td>'
            f'<td class="muted">{fnum(b["gap_med"], 1) if b["gap_med"] is not None else "—"}</td>'
            f'<td class="{cls(b["pnl"])}">{fsign(b["pnl"])}</td>'
            f'<td>{vd_html(b.get("verdict", "—"))}</td></tr>')
    parts.append("</tbody></table></div>")
    return "".join(parts)


def hist_html(hist):
    mx = max((h["share"] or 0) for h in hist) or 1
    parts = ['<div class="panel"><h3>阶梯速度分布（可复算样本）</h3>']
    for h in hist:
        sh = h["share"] or 0
        parts.append(
            f'<div class="hist-row"><div class="hist-label">{h["label"]}</div>'
            f'<div class="hist-track"><b style="width:{max(sh / mx * 100, 0.4):.2f}%;background:var(--blue)"></b></div>'
            f'<div class="hist-val">{h["n"]} 笔 · {share_pct(h["share"])}</div></div>')
    parts.append('<p class="small muted" style="margin-bottom:0">'
                 '800 根窗口左截断会低估阶梯链长度、高估首级箱体（框架 §3.5）。</p></div>')
    return "".join(parts)


def year_matrix_html(ym):
    labels = [day_label(lo, hi) for lo, hi in BANDS]
    parts = ['<div class="table-wrap"><table class="matrix"><thead><tr><th>年份</th><th>全年净利</th>']
    for lb in labels:
        parts.append(f"<th>{lb}</th>")
    parts.append("</tr></thead><tbody>")
    for y, d in ym.items():
        parts.append(f'<tr><td><strong>{y}</strong></td>'
                     f'<td class="{cls(d["overall"]["pnl"])}">{fsign(d["overall"]["pnl"])}</td>')
        for lb in labels:
            b = d["bands"][lb]
            if b["n"] == 0:
                parts.append('<td class="muted">—</td>')
            else:
                parts.append(f'<td class="{cls(b["pnl"])}">{fsign(b["pnl"])}'
                             f'<span class="muted small">({b["n"]})</span></td>')
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def calib_html(cal):
    parts = ['<div class="table-wrap"><table><thead><tr><th>级别</th><th>校验项</th>'
             "<th>期望</th><th>实际</th><th>结果</th><th>备注</th></tr></thead><tbody>"]
    for c in cal:
        ok = "✓" if c["ok"] else "✗"
        k = "up" if c["ok"] else "down"
        parts.append(f'<tr><td>{c["level"]}</td><td style="text-align:left">{c["name"]}</td>'
                     f'<td class="muted">{c["expected"]}</td><td>{c["actual"]}</td>'
                     f'<td class="{k}"><strong>{ok}</strong></td>'
                     f'<td class="muted small" style="text-align:left">{c["note"]}</td></tr>')
    parts.append("</tbody></table></div>")
    return "".join(parts)


def hyp_card(title, ok, detail):
    return (f'<div class="hyp-card {"ok" if ok else "no"}">'
            f'<div class="t">{title} <span class="vd {"vd-edge" if ok else "vd-neg"}">'
            f'{"成立" if ok else "不成立"}</span></div>'
            f'<div class="d">{detail}</div></div>')


def render_html(data):
    t = data["totals"]
    f = data["funnel"]
    h1, h2 = data["h1"], data["h2"]
    bands = data["bands"]
    rc_n = f["matched"]

    kpis = [
        ("已平仓样本", f"{f['closed']}", "2005–2018 · 新基线单镜上证"),
        ("可复算阶梯速度", f"{rc_n}", f"首级 {f['first_level']} / 不可复算 {f['closed'] - rc_n - f['first_level']}"),
        ("阶梯间隔中位数", f"{fnum(t['gap_med'],1) if t['gap_med'] is not None else '—'} 日",
         f"P25 {fnum(t['gap_p25'],1) if t['gap_p25'] is not None else '—'} / P75 {fnum(t['gap_p75'],1) if t['gap_p75'] is not None else '—'}"),
        ("总净利", f"¥{t['pnl']:,.0f}", "已实现口径"),
        ("总每单位净利", fsign(t["pu"]), f"{t['units']:.0f} 单位"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="label">{a}</div><div class="value">{b}</div>'
        f'<div class="note">{c}</div></div>' for a, b, c in kpis)

    concl = "".join(f"<li>{x}</li>" for x in data["conclusion"])

    h1_detail = f"四带每单位净利：" + "｜".join(
        f"{bands[i]['label']} {fsign(h1['pus'][i])}" for i in range(4)) + \
        f"｜递减相邻对 <b>{h1['downs']}/{h1['pairs']}</b>（需≥3）"
    h2_detail = (f"&lt;15 日带每单位净利 <b>{fsign(h2['pu'])}</b>（需&lt;0）、"
                 f"n=<b>{h2['n']}</b>（需≥30）、剔除前三 <b>{fsign(h2['pnl_ex_top3'])}</b>（需&lt;0）")

    hyp_html = (hyp_card(data["hyp_text"]["h1"], h1["ok"], h1_detail)
                + hyp_card(data["hyp_text"]["h2"], h2["ok"], h2_detail))

    sl = data["slices"]
    sl_rows = []
    for k in ["t1", "rest"]:
        s = sl[k]
        conc = f'{s["conc"]:.1f}%' if s["conc"] is not None else "—"
        sl_rows.append(
            f'<tr><td><strong>{s["label"]}</strong></td><td>{s["n"]}</td>'
            f'<td>{fpct(s["wr"])}</td><td class="{cls(s["pu"])}">{fsign(s["pu"])}</td>'
            f'<td>{conc}</td>'
            f'<td class="{cls(s["pnl_ex_top3"])}">{fsign(s["pnl_ex_top3"])}</td>'
            f'<td class="muted">{fnum(s["gap_med"],1) if s["gap_med"] is not None else "—"}</td>'
            f'<td class="{cls(s["pnl"])}">{fsign(s["pnl"])}</td></tr>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LB-2 Box 阶梯速度梯度 · 2005–2018</title>
<style>{CSS}</style></head><body><div class="page">
<header>
<div class="eyebrow">DARVAS · E17 · 实验报告 · 十一维 #11 Box 生命体征 · LB-2 阶梯速度</div>
<h1>LB-2 Box 阶梯速度梯度</h1>
<p class="sub">2005–2018 全 14 年已平仓交易的「入场 Box 相对前驱 Box 的台阶时间间隔（阶梯速度）」与突破后交易质量的关系。A 类后验、零回测，
只读 <code>var/bt/base/bt_baseA_&lt;year&gt;.db</code>。框架先于跑数定稿：<code>docs/experiments/e17_lb2_ladderspeed_framework.md</code>。</p>
</header>

<div class="kpis">{kpi_html}</div>

<div class="summary" id="s0"><h2>核心结论</h2>
<ul>{concl}</ul>
<p class="small muted" style="margin-top:10px">读法：阶梯速度是「从上一级箱体突破到本级箱体突破隔了多少交易日」，<b>不是</b>寿命（box 自身存续）、
也<b>不是</b>持仓持有天数。本实验只报相关性分布，不下因果结论（框架 §1.2）。</p>
</div>

<h2 id="s1">一、口径与样本</h2>
<div class="table-wrap"><table><thead><tr><th>口径</th><th style="text-align:left">定义</th></tr></thead><tbody>
<tr><td><code>ladder_gap</code></td><td style="text-align:left">该 code 的 daily_bar 在 [前驱 broken_date, 当前 broken_date] 的行数（含两端）——前驱 = 沿 prev_box_id 的上一级箱体</td></tr>
<tr><td><code>关联链</code></td><td style="text-align:left">position → leg1 → plan_order → reason_json(box_top, box_bottom) → box 反向匹配 → prev_box_id → 前驱 broken_date（entry_box_id 全 NULL）</td></tr>
<tr><td><code>每单位净利</code></td><td style="text-align:left">realized_pnl / units（units 取自 reason_json.units）</td></tr>
<tr><td>分带</td><td style="text-align:left">&lt;15 / 15–30 / 30–60 / &gt;60 交易日（预设固定值，未按结果调整）</td></tr>
</tbody></table></div>

<div class="callout blue"><b>样本漏斗</b>：14 库 closed <b>{f['closed']}</b> 笔 →
反向匹配 box 成功 <b>{f['matched']}</b> 笔（其中首级箱体 {f['first_level']} 笔）→ 缺 box 字段 {f['no_box_fields']} /
匹配失败 {f['match_fail']} / 前驱断链 {f['chain_broken']} / 阶梯异常 {f['neg_gap']} → <b>可复算阶梯速度 {rc_n} 笔</b>
（多命中 {f['multi_match']} 笔按 broken_date 最新取一）。</div>

<div class="callout gold"><b>已知数据偏差（结论必须携带）</b>：① <b>800 根窗口左截断</b>（docs/36 缺口 L4）——
前驱落在窗口左边界外 → 被误判「首级」或断链，阶梯链长度被低估；② 未复权（adj_factor≡1.0）；
③ 幸存者偏差。</div>

<h2 id="s2">二、阶梯速度分布</h2>
{hist_html(data['hist'])}

<h2 id="s3">三、阶梯速度主表（四带）</h2>
<p>四项固定口径 + PF + 缺口 + 剔除前三。Σ 四带 = {sum(b['n'] for b in bands)} 笔；加「首级箱体」「不可复算」子集行（<b>非加总行</b>）
= {f['closed']} 笔。</p>
{main_table(bands)}
<div class="table-wrap"><table><thead><tr><th>阶梯速度</th><th>笔数</th><th>胜率</th>
<th>每单位净利</th><th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th><th>剔除前三净利</th><th>净利</th></tr></thead><tbody>
<tr class="subset"><td><strong>{data['first_level']['label']}</strong>
<br><span class="small">（子集标记，非加总行）</span></td>
<td>{data['first_level']['n']}</td><td>{fpct(data['first_level']['wr'])}</td>
<td class="{cls(data['first_level']['pu'])}">{fsign(data['first_level']['pu'])}</td>
<td>{fpct(data['first_level']['bewr'])}</td>
<td class="{cls(data['first_level']['gap'])}">{fpp(data['first_level']['gap'])}</td>
<td>{fnum(data['first_level']['pf'],2) if data['first_level']['pf'] is not None else '—'}</td>
<td>—</td>
<td class="{cls(data['first_level']['pnl_ex_top3'])}">{fsign(data['first_level']['pnl_ex_top3'])}</td>
<td class="{cls(data['first_level']['pnl'])}">{fsign(data['first_level']['pnl'])}</td></tr>
<tr class="subset"><td><strong>{data['unrecomputable']['label']}</strong>
<br><span class="small">（子集标记，非加总行）</span></td>
<td>{data['unrecomputable']['n']}</td><td>{fpct(data['unrecomputable']['wr'])}</td>
<td class="{cls(data['unrecomputable']['pu'])}">{fsign(data['unrecomputable']['pu'])}</td>
<td>{fpct(data['unrecomputable']['bewr'])}</td>
<td class="{cls(data['unrecomputable']['gap'])}">{fpp(data['unrecomputable']['gap'])}</td>
<td>{fnum(data['unrecomputable']['pf'],2) if data['unrecomputable']['pf'] is not None else '—'}</td>
<td>—</td>
<td class="{cls(data['unrecomputable']['pnl_ex_top3'])}">{fsign(data['unrecomputable']['pnl_ex_top3'])}</td>
<td class="{cls(data['unrecomputable']['pnl'])}">{fsign(data['unrecomputable']['pnl'])}</td></tr>
</tbody></table></div>

<h2 id="s4">四、逐年矩阵</h2>
<p>逐格「净利（笔数）」，用于区分「某一年全市场普遍现象」与「长期结构」。</p>
{year_matrix_html(data['year_matrix'])}

<h2 id="s5">五、与第 5 维（距 52 周新高）交叉切片</h2>
<div class="table-wrap"><table><thead><tr><th>切片</th><th>笔数</th><th>胜率</th>
<th>每单位净利</th><th>集中度</th><th>剔除前三净利</th><th>间隔中位</th><th>净利</th></tr></thead><tbody>
{''.join(sl_rows)}
</tbody></table></div>
<p class="small muted">阶梯速度（第 11 维）与距新高（第 5 维）是两个维度，交叉只作附录观察，<b>不得据此下因果结论</b>。</p>

<h2 id="s6">六、判定</h2>
<div class="hyp">{hyp_html}</div>
<h3>6.1 逐带裁定</h3>
<div class="table-wrap"><table><thead><tr><th>阶梯速度</th><th>n</th><th>Mf</th><th>P′/M′</th>
<th>最长连续同号年</th><th>正号年净利占比</th><th>裁定</th></tr></thead><tbody>
{''.join(f'<tr><td><strong>{b["label"]}</strong></td><td>{b["n"]}</td>'
         f'<td>{b["cons"]["Mf"]}</td>'
         f'<td>{b["cons"]["Pf"]}/{b["cons"]["Mf"]}'
         f'<span class="muted small">（{fpct(b["cons"]["ratio_f"])}）</span></td>'
         f'<td>{b["cons"]["longest_run"]}</td>'
         f'<td>{share_pct(b["cons"]["pos_share"])}</td>'
         f'<td>{vd_html(b["verdict"])}</td></tr>' for b in bands)}
</tbody></table></div>

<h2 id="s7">七、附录</h2>
<h3>7.1 HARD 校验</h3>
{calib_html(data['calibration'])}
<h3>7.2 复现</h3>
<div class="panel"><p class="small">生成器：<code>python scripts/e17_lb2_ladderspeed.py</code>（先 <code>--check-only</code> 校准）<br>
独立校验：<code>python scripts/e17_lb2_ladderspeed_verify.py</code><br>
sha256（前 16 位）：<code>{data['meta']['generator_sha256']}</code><br>
数据源 mtime：{', '.join(f'{k} {v}' for k, v in list(data['meta']['db_mtimes'].items())[:3])} …</p></div>

<div class="foot">
<p><b>结论边界</b>：只量化「阶梯速度与突破质量的相关性分布」，不预测「阶梯速度该定多少阈值」。任何阈值规则须走 B 类回测。</p>
<p>单因子轮换：本实验只动「阶梯速度」一个分组变量，其余十维固定（docs/29 §2.3）。样本外（2019+）未参与。</p>
<p>生成于 {data['meta']['generated_at']} · {data['meta']['unit_note']}</p>
</div>
</div></body></html>"""


def render_md(data):
    t = data["totals"]
    f = data["funnel"]
    h1, h2 = data["h1"], data["h2"]
    bands = data["bands"]
    rc_n = f["matched"]
    L = []
    A = L.append
    A("# LB-2 Box 阶梯速度梯度 · 2005–2018")
    A("")
    A("> A 类后验、零回测。框架：`docs/experiments/e17_lb2_ladderspeed_framework.md`（零结果纯规则）。")
    A("")
    A(f"生成时间：{data['meta']['generated_at']} · 生成器 `{data['meta']['generator']}` "
      f"sha256 `{data['meta']['generator_sha256']}`")
    A("")
    A("## 核心结论")
    A("")
    for x in data["conclusion"]:
        A(f"- {x.replace('<b>', '**').replace('</b>', '**')}")
    A("")
    A(f"阶梯速度是「从上一级箱体突破到本级箱体突破隔了多少交易日」，不是寿命、不是持仓天数。只报相关性，不下因果。")
    A("")
    A("## 一、口径与样本")
    A("")
    A("| 口径 | 定义 |")
    A("|---|---|")
    A("| ladder_gap | 该 code daily_bar 在 [前驱 broken_date, 当前 broken_date] 的行数（含两端） |")
    A("| 关联链 | position → leg1 → plan_order → reason_json(box_top, box_bottom) → box 反向匹配 → prev_box_id → 前驱 broken_date |")
    A("| 每单位净利 | realized_pnl / units |")
    A("| 分带 | <15 / 15–30 / 30–60 / >60 交易日 |")
    A("")
    A(f"样本漏斗：closed {f['closed']} → 匹配 {f['matched']}（首级 {f['first_level']}）→ 可复算 {rc_n}。")
    A("")
    A(f"数据偏差：800 根窗口左截断（低估阶梯链长度）；未复权；幸存者偏差。")
    A("")
    A("## 二、阶梯速度分布")
    A("")
    A("| 阶梯速度 | 笔数 | 占比 | 净利 |")
    A("|---|---:|---:|---:|")
    for h in data["hist"]:
        A(f"| {h['label']} | {h['n']} | {share_pct(h['share'])} | {fsign(h['pnl'])} |")
    A("")
    A("## 三、阶梯速度主表（四带）")
    A("")
    A("| 阶梯速度 | 笔数 | 胜率 | 每单位净利 | 保本胜率 | 缺口 | PF | 集中度 | 剔除前三 | 间隔中位 | 净利 | 裁定 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for b in bands:
        conc = f"{b['conc']:.1f}%" if b["conc"] is not None else "—"
        A(f"| {b['label']} | {b['n']} | {fpct(b['wr'])} | {fsign(b['pu'])} | {fpct(b['bewr'])} | "
          f"{fpp(b['gap'])} | {fnum(b['pf'],2)} | {conc} | {fsign(b['pnl_ex_top3'])} | "
          f"{fnum(b['gap_med'],1) if b['gap_med'] is not None else '—'} | {fsign(b['pnl'])} | {b['verdict']} |")
    fl = data["first_level"]
    A(f"| *{fl['label']}（子集，非加总行）* | {fl['n']} | {fpct(fl['wr'])} | {fsign(fl['pu'])} | "
      f"{fpct(fl['bewr'])} | {fpp(fl['gap'])} | {fnum(fl['pf'],2)} | — | {fsign(fl['pnl_ex_top3'])} | — | {fsign(fl['pnl'])} | 首级 |")
    un = data["unrecomputable"]
    A(f"| *{un['label']}（子集，非加总行）* | {un['n']} | {fpct(un['wr'])} | {fsign(un['pu'])} | "
      f"{fpct(un['bewr'])} | {fpp(un['gap'])} | {fnum(un['pf'],2)} | — | {fsign(un['pnl_ex_top3'])} | — | {fsign(un['pnl'])} | 不可复算 |")
    A("")
    A(f"加总恒等式：Σ 四带 = {sum(b['n'] for b in bands)}；+ 首级 {fl['n']} + 不可复算 {un['n']} = {f['closed']}。")
    A("")
    A("## 四、逐年矩阵")
    A("")
    labels = [day_label(lo, hi) for lo, hi in BANDS]
    A("| 年份 | 全年 | " + " | ".join(labels) + " |")
    A("|---|---:|" + "---:|" * len(labels))
    for y, d in data["year_matrix"].items():
        cells = []
        for lb in labels:
            b = d["bands"][lb]
            cells.append(f"{fsign(b['pnl'])}（{b['n']}）" if b["n"] else "—")
        A(f"| {y} | {fsign(d['overall']['pnl'])} | " + " | ".join(cells) + " |")
    A("")
    A("## 五、与第 5 维交叉切片")
    A("")
    A("| 切片 | 笔数 | 胜率 | 每单位净利 | 集中度 | 剔除前三 | 间隔中位 | 净利 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|")
    for k in ["t1", "rest"]:
        s = data["slices"][k]
        A(f"| {s['label']} | {s['n']} | {fpct(s['wr'])} | {fsign(s['pu'])} | "
          f"{f'{s['conc']:.1f}%' if s['conc'] is not None else '—'} | {fsign(s['pnl_ex_top3'])} | "
          f"{fnum(s['gap_med'],1) if s['gap_med'] is not None else '—'} | {fsign(s['pnl'])} |")
    A("")
    A("## 六、判定")
    A("")
    A(f"### {data['hyp_text']['h1']}")
    A("")
    A("四带每单位净利：" + "｜".join(
        f"{bands[i]['label']} {fsign(h1['pus'][i])}" for i in range(4)))
    A("")
    A(f"递减相邻对 **{h1['downs']}/{h1['pairs']}**（需 ≥3）")
    A("")
    A(f"### {data['hyp_text']['h2']}")
    A("")
    A(f"- <15 日带每单位净利：**{fsign(h2['pu'])}**（需 <0）")
    A(f"- n = **{h2['n']}**（需 ≥30）")
    A(f"- 剔除前三：**{fsign(h2['pnl_ex_top3'])}**（需 <0）")
    A("")
    A("### 逐带裁定")
    A("")
    A("| 阶梯速度 | n | Mf | P′/M′ | 最长同号 | 正号年净利占比 | 裁定 |")
    A("|---|---:|---:|---:|---:|---:|---|")
    for b in bands:
        A(f"| {b['label']} | {b['n']} | {b['cons']['Mf']} | "
          f"{b['cons']['Pf']}/{b['cons']['Mf']} | {b['cons']['longest_run']} | "
          f"{share_pct(b['cons']['pos_share'])} | {b['verdict']} |")
    A("")
    A("## 七、附录")
    A("")
    A("| 级别 | 校验项 | 期望 | 实际 | 结果 |")
    A("|---|---|---|---|---|")
    for c in data["calibration"]:
        A(f"| {c['level']} | {c['name']} | {c['expected']} | {c['actual']} | "
          f"{'✓' if c['ok'] else '✗'} |")
    A("")
    A("## 结论边界")
    A("")
    A("- 只量化「阶梯速度与突破质量的相关性分布」，不预测阈值；阈值须走 B 类。")
    A("- 单因子轮换：只动「阶梯速度」，其余十维固定；样本外（2019+）未参与。")
    A("- 800 根窗口左截断 + 未复权，偏差已登记。")
    return "\n".join(L) + "\n"


def main():
    check_only = "--check-only" in sys.argv
    allow_drift = "--allow-drift" in sys.argv

    data = build()
    cal = data["calibration"]
    hard_fail = [c for c in cal if c["level"] == "HARD" and not c["ok"]]

    if check_only:
        print(f"{'级别':<6}{'结果':<6}校验项")
        for c in cal:
            print(f"{c['level']:<6}{'PASS' if c['ok'] else 'FAIL':<6}{c['name']}  "
                  f"[期望 {c['expected']} / 实际 {c['actual']}]")
        print(f"\nHARD {sum(1 for c in cal if c['level'] == 'HARD' and c['ok'])}"
              f"/{sum(1 for c in cal if c['level'] == 'HARD')} 通过；"
              f"HARD 失败 {len(hard_fail)} 项")
        if hard_fail and not allow_drift:
            return 1
        return 0

    if hard_fail and not allow_drift:
        print("HARD 校验失败，已中止写入：")
        for c in hard_fail:
            print(f"  ✗ {c['name']}  期望 {c['expected']} / 实际 {c['actual']}")
        return 1

    EXP.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(render_html(data), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    stats = {k: v for k, v in data.items() if k not in ("meta", "trades")}
    OUT_JSON.write_text(json.dumps({"meta": data["meta"], "stats": stats,
                                    "trades": data["trades"]},
                                   ensure_ascii=False, indent=1), encoding="utf-8")

    t = data["totals"]
    funnel = data["funnel"]
    n_rc = sum(1 for x in data["trades"] if x["recomputable"])
    print(f"样本 {t['n']}（可复算 {n_rc} / 首级 {funnel['first_level']} / "
          f"不可复算 {funnel['closed'] - n_rc - funnel['first_level']}）")
    for b in data["bands"]:
        print(f"  {b['label']:<12} n={b['n']:<4} 每单位 {fsign(b['pu']):>9}  裁定 {b['verdict']}")
    print(f"H1 {data['h1']['ok']} / H2 {data['h2']['ok']}")
    print(f"已写入 {OUT_HTML.name} / {OUT_MD.name} / {OUT_JSON.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
