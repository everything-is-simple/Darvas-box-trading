#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-1 · box 高度分带实验 · 专项报告生成器。

实验：docs/29 §五 P0-1（十维 #2 box 高度），A 类零成本后验，不重跑回测、不改回测器。
框架：docs/experiments/e17_box_height_framework.md（零结果纯规则，含三级加密与判定矩阵）

双口径（框架 §4）：
  主口径 /top    = (top-bottom)/top    × 100   ← 与 box.min_width 闸门同尺（engine.go:127）
  对照   /bottom = (top-bottom)/bottom × 100   ← docs/29:102 与历史 E8 口径

三级加密（框架 §5）：
  L1  5% 等距全切
  L2  触发条件命中后降 2%（n>=30 且 符号反转 / 集中度>50% / 剔除前三后符号翻转）
  L3  按需降 1%（默认不启用）

输出（docs/experiments/，实验报告唯一沉淀处）：
  e17_box_height_report.html / .md / _data.json

用法：
  python scripts/e17_box_height_report.py              # 正式生成（HARD 失败即中止）
  python scripts/e17_box_height_report.py --check-only # 只跑校准，不写文件
  python scripts/e17_box_height_report.py --allow-drift# HARD 降级为 WARN（需明确知道为何漂移）
"""
import csv
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
OUT_HTML = EXP / "e17_box_height_report.html"
OUT_MD = EXP / "e17_box_height_report.md"
OUT_JSON = EXP / "e17_box_height_data.json"
METRICS = BT / "e17_metrics.csv"

# ---------------------------------------------------------------- 分带定义
# L1：5% 等距全切（框架 §5.2）。末带 (x, None) 表示开口尾部，按规则合并。
BANDS_TOP_L1 = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 25),
                (25, 30), (30, 35), (35, 40), (40, None)]
BANDS_BOTTOM_L1 = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 25),
                   (25, 30), (30, 35), (35, 40), (40, 45), (45, None)]

# L2：2% 加密（框架 §5.3 触发区间）
BANDS_TOP_L2 = [(v, v + 2) for v in range(10, 30, 2)]          # 10–30
BANDS_BOTTOM_L2 = [(v, v + 2) for v in range(15, 39, 2)] + [(39, 40)]  # 15–40

# L3：1% 加密（框架 §5.3 —— L2 中仍出现「n≥30 且集中度>50%」的段，降 1% 复核峰是否成片）
# 触发记录：/top 20–22(81.4%)、24–26(85.0%)、26–28(88.6%)；/bottom 17–19(78.9%)、21–23(67.1%)、
#          23–25(53.4%)、25–27(92.2%)、27–29(90.0%) → 分别覆盖 20–28、17–29。
BANDS_TOP_L3 = [(v, v + 1) for v in range(20, 28)]             # 20–28
BANDS_BOTTOM_L3 = [(v, v + 1) for v in range(17, 29)]          # 17–29

# 汇总行（框架 §5.5：合并行必须另起一行并显式标注）
MERGE_TOP = [("5–25%（主区汇总）", 5, 25),
             ("10–25%（中箱，H2 裁定行）", 10, 25),
             ("25%+（高箱区汇总，H1 裁定行）", 25, None)]
MERGE_BOTTOM = [("40%+（尾部合并）", 40, None)]

# ---------------------------------------------------------------- 校准基线
# 基线锁定：2026-10-26 首次基线跑（14 库 2005-2018）。改基线必须走一次 diff 评审。
BASELINE = {
    "leg1": 1405,
    "closed": 1328,
    "pnl": 842799.7,
    "pnl_tol": 0.5,
    # 按 (lo, hi, 期望剔除前三净利) 锁定，避免中英文破折号不一致导致匹配失败
    "top_band_ex_top3": [(5, 10, -8451.0), (10, 15, 209687.0)],
    "zero_band": (0, 5),
}

TIER_NOTE = "1 单位 = 2% 权益；E17 阶梯 t1=3 / t2=2 / t3=1 / t4=0.5"


# ---------------------------------------------------------------- 基础工具
def years_list():
    return sorted(int(os.path.basename(p).split("_")[2].split(".")[0])
                  for p in glob_db())


def glob_db():
    return [str(p) for p in BT.glob("bt_e17_*.db")]


def h_top(t, b):
    return (t - b) / t * 100.0 if t else None


def h_bottom(t, b):
    return (t - b) / b * 100.0 if b else None


def fnum(x, d=0):
    return "—" if x is None else f"{x:,.{d}f}"


def fpct(x, d=1):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def fsign(x, d=0):
    return "—" if x is None else f"{x:+,.{d}f}"


def fpp(x, d=1):
    """百分点显示（输入已是 pp 数值）。"""
    return "—" if x is None else f"{x:+.{d}f}pp"


def cls(x):
    if x is None:
        return "neutral"
    return "up" if x > 0 else ("down" if x < 0 else "neutral")


def band_label(lo, hi):
    return f"{lo}–{hi}%" if hi is not None else f"{lo}%+"


# ---------------------------------------------------------------- 数据加载
def load_trades():
    """加载 14 库 leg_no=1 建仓记录 + box 几何。返回 (trades, funnel)。"""
    trades, funnel = [], {"dbs": 0, "leg1": 0, "closed": 0,
                          "miss_box": 0, "bad_geom": 0}
    for y in years_list():
        db = BT / f"bt_e17_{y}.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sql = ("SELECT p.opened_date, p.closed_date, p.status, p.realized_pnl, "
               "o.reason_json FROM position_leg l "
               "JOIN position p ON p.id=l.position_id "
               "LEFT JOIN plan_order o ON o.id=l.plan_order_id WHERE l.leg_no=1")
        for r in con.execute(sql):
            funnel["leg1"] += 1
            try:
                rj = json.loads(r["reason_json"] or "{}")
            except (TypeError, ValueError):
                rj = {}
            top, bot = rj.get("box_top"), rj.get("box_bottom")
            if top is None or bot is None:
                funnel["miss_box"] += 1
                continue
            try:
                top, bot = float(top), float(bot)
            except (TypeError, ValueError):
                funnel["miss_box"] += 1
                continue
            if top <= bot or bot <= 0:
                funnel["bad_geom"] += 1
                continue
            units = rj.get("units", 1.0)
            try:
                units = float(units)
            except (TypeError, ValueError):
                units = 1.0
            if r["status"] == "closed" and r["realized_pnl"] is not None:
                funnel["closed"] += 1
            trades.append({
                "year": y, "top": top, "bottom": bot,
                "ht": h_top(top, bot), "hb": h_bottom(top, bot),
                "units": units, "pnl": r["realized_pnl"],
                "status": r["status"], "opened": r["opened_date"],
                "closed": r["closed_date"], "basis": rj.get("sizing_basis"),
            })
        con.close()
        funnel["dbs"] += 1
    return trades, funnel


def load_metrics():
    """读 e17_metrics.csv 做跨源对账。返回 {year: (closed_count, pnl)}。"""
    out = {}
    if not METRICS.exists():
        return out
    with METRICS.open(encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip().isdigit():
                continue
            try:
                out[int(row[0])] = (int(float(row[8])), float(row[10]))
            except (IndexError, ValueError):
                continue
    return out


# ---------------------------------------------------------------- 聚合
def agg(rows):
    """四项固定口径 + PF + 缺口 + 剔除前三（框架 §4.5）。"""
    rows = [r for r in rows if r["pnl"] is not None]
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
    ex_rows = rows[:]
    # 剔除前三赢家：按 pnl 降序去掉前三笔（仅当确实存在交易时）
    if len(ranked) >= 3:
        cut = ranked[2]
        removed = 0
        ex_rows = []
        for r in sorted(rows, key=lambda x: -x["pnl"]):
            if removed < 3 and (r["pnl"] > cut or (r["pnl"] == cut and removed < 3)):
                removed += 1
                continue
            ex_rows.append(r)
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
        # 前三赢家合计为负（该带全亏、最大三笔也是亏）时集中度无意义，记 None
        "conc": (top3 / gw * 100 if (gw > 0 and top3 > 0) else None),
        "pnl_ex_top3": ex_pnl,
        "pu_ex_top3": (ex_pnl / ex_units if ex_units else None),
    }


def band_rows(trades, key, lo, hi):
    if hi is None:
        return [t for t in trades if t[key] >= lo]
    return [t for t in trades if lo <= t[key] < hi]


def band_stat(trades, key, bands):
    out = []
    for lo, hi in bands:
        out.append({"label": band_label(lo, hi), "lo": lo, "hi": hi,
                    **agg(band_rows(trades, key, lo, hi))})
    return out


def merge_stat(trades, key, merges):
    out = []
    for label, lo, hi in merges:
        out.append({"label": label, "lo": lo, "hi": hi,
                    **agg(band_rows(trades, key, lo, hi))})
    return out


# ---------------------------------------------------------------- 逐月/逐年一致性
def year_matrix(trades, key, bands):
    years = sorted({t["year"] for t in trades})
    m = OrderedDict()
    for y in years:
        yc = [t for t in trades if t["year"] == y]
        m[str(y)] = {"overall": agg(yc),
                     "bands": {band_label(lo, hi): agg(band_rows(yc, key, lo, hi))
                               for lo, hi in bands}}
    return m


def consistency(trades, key, lo, hi):
    """跨年一致性记分卡（框架 §7.2）：M / P / P' / M' / longest_run / pos_share。"""
    years = sorted({t["year"] for t in trades})
    per_year = []
    for y in years:
        b = band_rows([t for t in trades if t["year"] == y], key, lo, hi)
        s = agg(b)
        per_year.append({"year": y, "n": s["n"], "pnl": s["pnl"]})
    with_sample = [p for p in per_year if p["n"] > 0]
    M = len(with_sample)
    P = sum(1 for p in with_sample if p["pnl"] > 0)
    filt = [p for p in with_sample if p["n"] >= 3]
    Mf = len(filt)
    Pf = sum(1 for p in filt if p["pnl"] > 0)
    longest, cur, last = 0, 0, 0
    for p in filt:
        sgn = 1 if p["pnl"] > 0 else -1
        cur = cur + 1 if sgn == last else 1
        last = sgn
        longest = max(longest, cur)
    tot = sum(p["pnl"] for p in with_sample)
    pos_sum = sum(p["pnl"] for p in with_sample if p["pnl"] > 0)
    return {
        "M": M, "P": P, "ratio": (P / M if M else None),
        "Mf": Mf, "Pf": Pf, "ratio_f": (Pf / Mf if Mf else None),
        "longest_run": longest,
        "pos_share": (pos_sum / tot if tot else None),
        "per_year": per_year,
    }


def verdict(s, c, is_zero_band=False):
    """裁定标签（框架 §7.3）。

    判定顺序刻意先查「剔除前三是否翻负」（假 edge），再查跨年稳定性——
    因为本实验最反直觉的发现是「某带总账为正但全靠两三笔」，若先按 ratio 判
    「不稳定」，会把「假 edge」这一关键性质掩盖掉。
    """
    if is_zero_band:
        return "不可检验（结构性无样本）"
    if s["n"] < 30:
        return "样本不足"
    if c["Mf"] < 8:
        return "出现年数不足"

    ratio, gap = c["ratio_f"], s["gap"]
    pu, pu_ex = s["pu"], s["pu_ex_top3"]
    gapok = gap is not None and gap >= 2
    gapneg = gap is not None and gap <= -2

    # 1) 剔除前三后翻负 → 假 edge（无论跨年是否稳定）
    if pu is not None and pu > 0 and pu_ex is not None and pu_ex <= 0:
        return "假 edge（前三撑起）"
    # 2) 跨年稳定为正且缺口达标
    if ratio is not None and ratio >= 0.70:
        return "edge" if gapok else "稳但缺口不足"
    # 3) 跨年稳定为负
    if ratio is not None and ratio <= 0.30:
        return "负 edge" if gapneg else "跨年偏负（缺口未达 −2pp）"
    # 4) 中间区：不稳定，区分正负方向
    if pu is not None and pu > 0:
        return "不稳定（正但非跨年）"
    return "不稳定（负但非跨年）"


# ---------------------------------------------------------------- 校准
def calibrate(trades, funnel, metrics, data):
    """返回断言列表 [{level,name,expected,actual,ok,note}]。"""
    res = []

    def add(level, name, expected, actual, ok, note=""):
        res.append({"level": level, "name": name, "expected": expected,
                    "actual": actual, "ok": bool(ok), "note": note})

    # HARD 1 年份连续
    ys = years_list()
    add("HARD", "年份连续 2005–2018", "14 年连续",
        f"{len(ys)} 年 {ys[0]}–{ys[-1]}",
        ys == list(range(2005, 2019)), "缺年或乱序")

    # HARD 2/3 跨源对账（每年平仓笔数与净利）
    closed = [t for t in trades if t["status"] == "closed" and t["pnl"] is not None]
    bad_n, bad_p = [], []
    for y in ys:
        yc = [t for t in closed if t["year"] == y]
        if y not in metrics:
            bad_n.append(f"{y}:metrics缺失")
            continue
        mn, mp = metrics[y]
        if len(yc) != mn:
            bad_n.append(f"{y}:{len(yc)}≠{mn}")
        if abs(sum(t['pnl'] for t in yc) - mp) > 1.0:
            bad_p.append(f"{y}:{sum(t['pnl'] for t in yc):.0f}≠{mp:.0f}")
    add("HARD", "每年平仓笔数与 e17_metrics.csv 一致", "全部一致",
        "一致" if not bad_n else "; ".join(bad_n), not bad_n)
    add("HARD", "每年净利与 e17_metrics.csv 差 ≤1.0", "全部一致",
        "一致" if not bad_p else "; ".join(bad_p), not bad_p)

    # HARD 4 分带完备
    tot_top = sum(b["n"] for b in data["top_l1"])
    tot_bot = sum(b["n"] for b in data["bottom_l1"])
    add("HARD", "两表分带笔数合计 = 已平仓样本", f"{len(closed)}",
        f"/top {tot_top} / /bottom {tot_bot}",
        tot_top == len(closed) and tot_bot == len(closed), "带边界漏算或重叠")

    # HARD 5 hb > ht
    ok5 = all(t["hb"] > t["ht"] for t in closed)
    add("HARD", "每笔 /bottom > /top（数学恒等）", "全部成立",
        "成立" if ok5 else "存在反例", ok5)

    # HARD 6 units 集合
    units_set = sorted({t["units"] for t in closed})
    add("HARD", "units ⊆ {0.5,1,2,3}", "{0.5,1,2,3}", str(units_set),
        set(units_set) <= {0.5, 1.0, 2.0, 3.0})

    # HARD 7 字段缺失/脏几何
    add("HARD", "box 字段缺失 = 0", "0", str(funnel["miss_box"]),
        funnel["miss_box"] == 0)
    add("HARD", "top<=bottom 脏数据 = 0", "0", str(funnel["bad_geom"]),
        funnel["bad_geom"] == 0)

    # HARD 8 空带
    zlo, zhi = BASELINE["zero_band"]
    z_top = next((b for b in data["top_l1"] if b["lo"] == zlo and b["hi"] == zhi), None)
    z_bot = next((b for b in data["bottom_l1"] if b["lo"] == zlo and b["hi"] == zhi), None)
    add("HARD", "0–5% 带两口径均 0 笔", "/top 0, /bottom 0",
        f"/top {z_top['n'] if z_top else 'NA'}, /bottom {z_bot['n'] if z_bot else 'NA'}",
        (z_top is not None and z_top["n"] == 0) and (z_bot is not None and z_bot["n"] == 0),
        "若非 0 说明闸门被绕过")

    # HARD 9 min ht >= 5
    mh = min(t["ht"] for t in closed)
    add("HARD", "min(/top) ≥ 5.0（闸门生效）", "≥5.0", f"{mh:.6f}",
        mh >= 5.0 - 1e-9)

    # WARN 10 样本量快照
    add("WARN", "leg1 建仓数（基线快照）", str(BASELINE["leg1"]),
        str(funnel["leg1"]), funnel["leg1"] == BASELINE["leg1"])
    add("WARN", "已平仓数（基线快照）", str(BASELINE["closed"]),
        str(len(closed)), len(closed) == BASELINE["closed"])

    # WARN 11 净利快照
    sp = sum(t["pnl"] for t in closed)
    add("WARN", "Σrealized_pnl（基线快照，非 842,801）",
        f"≈{BASELINE['pnl']:,.1f}", f"{sp:,.1f}",
        abs(sp - BASELINE["pnl"]) <= BASELINE["pnl_tol"],
        "842,801 是 metrics 按年取整之和，差 1.3")

    # WARN 12/13 关键带剔除前三快照
    for lo, hi, exp in BASELINE["top_band_ex_top3"]:
        b = next((x for x in data["top_l1"] if x["lo"] == lo and x["hi"] == hi), None)
        act = b["pnl_ex_top3"] if b else None
        add("WARN", f"/top {lo}–{hi}% 剔除前三净利（基线快照）",
            f"{exp:+,.0f}", f"{act:+,.0f}" if act is not None else "NA",
            act is not None and abs(act - exp) <= 1.0,
            "该带的真/假 edge 判断依赖此项")

    # INFO 自洽
    add("INFO", "Σband.pnl = 总净利", f"{sp:,.1f}",
        f"{sum(b['pnl'] for b in data['top_l1']):,.1f}",
        abs(sum(b["pnl"] for b in data["top_l1"]) - sp) <= 1.0)
    return res


# ---------------------------------------------------------------- 构建
def build():
    trades_all, funnel = load_trades()
    metrics = load_metrics()
    closed = [t for t in trades_all if t["status"] == "closed" and t["pnl"] is not None]

    top_l1 = band_stat(closed, "ht", BANDS_TOP_L1)
    bottom_l1 = band_stat(closed, "hb", BANDS_BOTTOM_L1)
    top_l2 = band_stat(closed, "ht", BANDS_TOP_L2)
    bottom_l2 = band_stat(closed, "hb", BANDS_BOTTOM_L2)
    top_l3 = band_stat(closed, "ht", BANDS_TOP_L3)
    bottom_l3 = band_stat(closed, "hb", BANDS_BOTTOM_L3)
    top_merge = merge_stat(closed, "ht", MERGE_TOP)
    bottom_merge = merge_stat(closed, "hb", MERGE_BOTTOM)

    cons_top = {}
    for b in top_l1:
        cons_top[b["label"]] = consistency(closed, "ht", b["lo"], b["hi"])
    cons_top_merge = {m["label"]: consistency(closed, "ht", m["lo"], m["hi"])
                      for m in top_merge}
    cons_bottom = {}
    for b in bottom_l1:
        cons_bottom[b["label"]] = consistency(closed, "hb", b["lo"], b["hi"])

    # 裁定
    for b in top_l1:
        b["verdict"] = verdict(b, cons_top[b["label"]], b["lo"] == 0 and b["hi"] == 5)
    for m in top_merge:
        m["verdict"] = verdict(m, cons_top_merge[m["label"]])
    for b in bottom_l1:
        b["verdict"] = verdict(b, cons_bottom[b["label"]], b["lo"] == 0 and b["hi"] == 5)

    # L2 加密层同样给裁定（探索性，不据此提新阈值）
    cons_top_l2, cons_bottom_l2 = {}, {}
    for b in top_l2:
        cons_top_l2[b["label"]] = consistency(closed, "ht", b["lo"], b["hi"])
        b["verdict"] = verdict(b, cons_top_l2[b["label"]])
    for b in bottom_l2:
        cons_bottom_l2[b["label"]] = consistency(closed, "hb", b["lo"], b["hi"])
        b["verdict"] = verdict(b, cons_bottom_l2[b["label"]])

    # L3 加密层（1%，探索性复核）
    cons_top_l3, cons_bottom_l3 = {}, {}
    for b in top_l3:
        cons_top_l3[b["label"]] = consistency(closed, "ht", b["lo"], b["hi"])
        b["verdict"] = verdict(b, cons_top_l3[b["label"]])
    for b in bottom_l3:
        cons_bottom_l3[b["label"]] = consistency(closed, "hb", b["lo"], b["hi"])
        b["verdict"] = verdict(b, cons_bottom_l3[b["label"]])

    # 假设裁定（框架 §2 的 H1–H4，逐条给证据与裁定）
    b510 = next(x for x in top_l1 if x["lo"] == 5 and x["hi"] == 10)
    mid = next(m for m in top_merge if m["lo"] == 10)
    m25p = next(m for m in top_merge if m["lo"] == 25)
    c_mid = cons_top_merge[mid["label"]]
    c_25p = cons_top_merge[m25p["label"]]
    bm40 = bottom_merge[0]
    b40 = next(x for x in top_l1 if x["lo"] == 40)

    h1_ok = (m25p["pu"] is not None and m25p["pu"] < 0
             and c_25p["ratio_f"] is not None and c_25p["ratio_f"] <= 0.30)
    h2_ok = (mid["pu"] is not None and b510["pu"] is not None and mid["pu"] > b510["pu"])
    h2_strong = h2_ok and mid["pu_ex_top3"] is not None and mid["pu_ex_top3"] > 0
    h4_ok = (abs(bm40["n"] - b40["n"]) >= 20 or abs(bm40["pnl"] - b40["pnl"]) >= 20000)
    hyps = [
        {"id": "H1", "claim": "高箱有害：/top ≥25% 每单位为负，且跨年符号一致为负",
         "evidence": f'{m25p["n"]} 笔，每单位 {fsign(m25p["pu"])}，'
                     f'跨年正号 {c_25p["Pf"]}/{c_25p["Mf"]}（{fpct(c_25p["ratio_f"])}），'
                     f'剔除前三 {fsign(m25p["pnl_ex_top3"])}',
         "verdict": "成立" if h1_ok else "不成立"},
        {"id": "H2", "claim": "中箱优于窄箱：/top 10–25% 每单位高于 5–10%",
         "evidence": f'10–25% 每单位 {fsign(mid["pu"])} vs 5–10% {fsign(b510["pu"])}；'
                     f'剔除前三后 10–25% {fsign(mid["pu_ex_top3"])} vs '
                     f'5–10% {fsign(b510["pu_ex_top3"])}',
         "verdict": ("成立（且剔除前三后仍优）" if h2_strong else
                     ("成立（但剔除前三后不优）" if h2_ok else "不成立"))},
        {"id": "H3", "claim": "「<5% 更优」不可检验",
         "evidence": "0–5% 带在两口径下均为 0 笔，无窄箱对照组",
         "verdict": "登记为不可检验（不作结论）"},
        {"id": "H4", "claim": "分母敏感：高箱端的符号/幅度对分母选择敏感",
         "evidence": f'/top 40%+ {b40["n"]} 笔 {fsign(b40["pnl"])} vs '
                     f'/bottom 40%+ {bm40["n"]} 笔 {fsign(bm40["pnl"])}；'
                     f'换算是 /bottom 40% ⟺ /top 28.6%',
         "verdict": "成立" if h4_ok else "不成立"},
    ]

    data = {
        "meta": {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "years": years_list(), "funnel": funnel,
            "total_closed": len(closed),
            "total_pnl": sum(t["pnl"] for t in closed),
            "generator_sha256": hashlib.sha256(
                Path(__file__).read_bytes()).hexdigest()[:16],
            "db_mtimes": {str(y): datetime.fromtimestamp(
                (BT / f"bt_e17_{y}.db").stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                for y in years_list()},
            "overall": agg(closed),
            "layers": "L1 5% + L2 2%（10–30% / 15–40%）+ L3 1%（20–28% / 17–29%）",
        },
        "top_l1": top_l1, "top_l2": top_l2, "top_merge": top_merge,
        "top_l3": top_l3, "bottom_l3": bottom_l3,
        "bottom_l1": bottom_l1, "bottom_l2": bottom_l2, "bottom_merge": bottom_merge,
        "cons_top": cons_top, "cons_top_merge": cons_top_merge,
        "cons_bottom": cons_bottom,
        "cons_top_l2": cons_top_l2, "cons_bottom_l2": cons_bottom_l2,
        "cons_top_l3": cons_top_l3, "cons_bottom_l3": cons_bottom_l3,
        "hypotheses": hyps,
        "ym_top": year_matrix(closed, "ht", BANDS_TOP_L1),
        "ym_bottom": year_matrix(closed, "hb", BANDS_BOTTOM_L1),
    }
    data["calibration"] = calibrate(trades_all, funnel, metrics, data)
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
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:17px 18px}
.kpi .label{font-size:12px;color:var(--muted)}
.kpi .value{font-size:26px;font-weight:700;line-height:1.25;margin-top:4px}
.kpi .note{font-size:12px;color:var(--muted);margin-top:3px}
.summary{background:var(--surface);border-left:4px solid var(--blue);border-radius:8px;padding:18px 22px;margin:22px 0}
.summary h2{border:0;margin:0 0 8px;padding:0;font-size:20px}
.summary ul{margin:8px 0 0 19px;padding:0}.summary li{margin:6px 0}
.callout{border-radius:8px;padding:14px 17px;margin:15px 0;font-size:14px}
.callout.gold{background:var(--gold2);border:1px solid #ead9b8}
.callout.red{background:var(--red2);border:1px solid #efd0ca}
.callout.blue{background:var(--blue2);border:1px solid #d4e0ee}
.table-wrap{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow-x:auto;margin:13px 0}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:900px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
th{background:#fafbfc;color:#4b5565;font-size:12px;font-weight:600}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fcfcfd}
tr.empty td{background:#f4f5f7;color:#9aa1ad;font-style:italic}
tr.merge td{background:#f8f9fb;font-weight:600}
tr.sum td{background:#fafbfc;font-weight:700;border-top:2px solid #cfd4dc}
.vd{font-size:11.5px;padding:1px 7px;border-radius:999px;white-space:nowrap}
.vd-edge{background:var(--red2);color:var(--red);font-weight:700}
.vd-fake{background:var(--gold2);color:var(--gold);font-weight:700}
.vd-neg{background:var(--green2);color:var(--green);font-weight:700}
.vd-unstable{background:var(--soft);color:#5a6472}
.vd-small{background:#f1f3f6;color:#8b93a0}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:17px 19px;margin:14px 0}
.diverging{margin:10px 0 4px}
.dv-row{display:grid;grid-template-columns:96px 1fr 108px;align-items:center;gap:9px;margin:6px 0}
.dv-label{font-size:12.5px}
.dv-track{position:relative;height:18px;background:var(--soft);border-radius:4px;overflow:hidden}
.dv-zero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#aab1bc;z-index:2}
.dv-track b{position:absolute;top:3px;bottom:3px;border-radius:2px;z-index:1}
.dv-val{text-align:right;font-size:12.5px;font-variant-numeric:tabular-nums}
.matrix td{font-size:12px}
.signbar{display:flex;gap:2px}
.sb{width:9px;height:14px;border-radius:2px;display:inline-block}
.sb.pos{background:var(--red)}.sb.neg{background:var(--green)}.sb.none{background:#d8dbe1}
.toc{display:flex;gap:9px;flex-wrap:wrap;margin:18px 0}
.toc a{text-decoration:none;color:var(--blue);background:var(--blue2);border:1px solid #d4e0ee;border-radius:999px;padding:3px 12px;font-size:12.5px}
.foot{margin-top:38px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
.foot p{margin:5px 0}
code{background:var(--soft);border-radius:3px;padding:1px 5px;font-size:12.5px}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}.page{padding:28px 16px 48px}h1{font-size:26px}}
@media print{body{background:#fff;font-size:11px}.page{max-width:none;padding:0}
h2{break-after:avoid}.table-wrap,.panel{break-inside:avoid}}
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


def hyp_html(v):
    if v.startswith("成立"):
        k = "vd-edge"
    elif v == "不成立":
        k = "vd-neg"
    else:
        k = "vd-fake"
    return f'<span class="vd {k}">{v}</span>'


def maintable(rows, show_equiv=None):
    """主表/对照表通用渲染。show_equiv=(key,conv) 时加换算列。"""
    head = ("<thead><tr><th>分带</th><th>笔数</th><th>胜率</th><th>每单位净利</th>"
            "<th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th>"
            "<th>前三赢家</th><th>剔除前三净利</th><th>剔除前三每单位</th><th>净利</th><th>裁定</th>"
            + ("<th>≈/top 等价区间</th>" if show_equiv else "") + "</tr></thead><tbody>")
    parts = ['<div class="table-wrap"><table>', head]
    for b in rows:
        zero = b["n"] == 0
        trc = "empty" if zero else ("merge" if "汇总" in b["label"] or "合并" in b["label"] else "")
        if zero:
            parts.append(
                f'<tr class="{trc}"><td><strong>{b["label"]}</strong></td><td>0</td>'
                f'<td colspan="10">结构性无样本（闸门所致：box.min_width ≥ 5%，样本不含窄箱）</td>'
                f'<td>{vd_html(b["verdict"])}</td>'
                + ("<td>—</td>" if show_equiv else "") + '</tr>')
            continue
        conc = f'{b["conc"]:.1f}%' if b["conc"] is not None else "—"
        eq = ""
        if show_equiv:
            conv = show_equiv
            lo = conv(b["lo"])
            hi = "" if b["hi"] is None else f"–{conv(b['hi']):.1f}%"
            eq = f'<td class="muted">{lo:.1f}%{hi}</td>'
        parts.append(
            f'<tr class="{trc}"><td><strong>{b["label"]}</strong></td>'
            f'<td>{b["n"]}{" ⚠" if b["n"] < 30 else ""}</td>'
            f'<td>{fpct(b["wr"])}</td>'
            f'<td class="{cls(b["pu"])}">{fsign(b["pu"])}</td>'
            f'<td>{fpct(b["bewr"])}</td>'
            f'<td class="{cls(b["gap"])}">{fpp(b["gap"])}</td>'
            f'<td>{fnum(b["pf"],2) if b["pf"] is not None else "—"}</td>'
            f'<td>{conc}</td>'
            f'<td class="{cls(b["top3"])}">{fsign(b["top3"])}</td>'
            f'<td class="{cls(b["pnl_ex_top3"])}"><strong>{fsign(b["pnl_ex_top3"])}</strong></td>'
            f'<td class="{cls(b["pu_ex_top3"])}">{fsign(b["pu_ex_top3"])}</td>'
            f'<td class="{cls(b["pnl"])}">{fsign(b["pnl"])}</td>'
            f'<td>{vd_html(b["verdict"])}</td>{eq}</tr>')
    parts.append("</tbody></table></div>")
    return "".join(parts)


def diverging(rows, title):
    vals = [r["pnl"] for r in rows if r["n"] > 0]
    mx = max((abs(v) for v in vals), default=1) or 1
    parts = [f'<div class="panel"><h3>{title}</h3><div class="diverging">']
    for r in rows:
        if r["n"] == 0:
            parts.append(
                f'<div class="dv-row"><div class="dv-label">{r["label"]}</div>'
                f'<div class="dv-track"><i class="dv-zero"></i></div>'
                f'<div class="dv-val neutral">无样本</div></div>')
            continue
        w = abs(r["pnl"]) / mx * 48
        if r["pnl"] >= 0:
            style = f"left:50%;width:{max(w,0.3):.2f}%;background:var(--red)"
        else:
            style = f"left:{max(50-w,0.3):.2f}%;width:{max(w,0.3):.2f}%;background:var(--green)"
        parts.append(
            f'<div class="dv-row"><div class="dv-label">{r["label"]}</div>'
            f'<div class="dv-track"><i class="dv-zero"></i><b style="{style}"></b></div>'
            f'<div class="dv-val {cls(r["pnl"])}">{fsign(r["pnl"])}</div></div>')
    parts.append('</div><p class="small muted" style="margin-bottom:0">'
                 "红线向右=盈利，绿线向左=亏损，同一比例尺。</p></div>")
    return "".join(parts)


def matrix_html(ym, bands):
    labels = [band_label(lo, hi) for lo, hi in bands]
    parts = ['<div class="table-wrap"><table class="matrix"><thead><tr><th>年份</th><th>全年</th>']
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


def signbar(rows, cons):
    parts = ['<div class="panel"><h3>符号条（红=该年正 / 绿=该年负 / 灰=无样本）</h3>']
    for r in rows:
        if r["n"] == 0:
            continue
        c = cons[r["label"]]
        cells = []
        for p in c["per_year"]:
            if p["n"] == 0:
                cells.append('<i class="sb none"></i>')
            else:
                cells.append(f'<i class="sb {"pos" if p["pnl"] > 0 else "neg"}"></i>')
        parts.append(f'<div class="dv-row" style="grid-template-columns:96px 1fr 150px">'
                     f'<div class="dv-label">{r["label"]}</div>'
                     f'<div class="signbar">{"".join(cells)}</div>'
                     f'<div class="dv-val small muted">{c["P"]}/{c["M"]} 年正</div></div>')
    parts.append('<p class="small muted" style="margin-bottom:0">'
                 "每格一年，左起 2005 → 2018。</p></div>")
    return "".join(parts)


def scorecard(rows, cons):
    parts = ['<div class="table-wrap"><table><thead><tr>'
             "<th>分带</th><th>有样本年 M</th><th>正号年 P</th><th>P/M</th>"
             "<th>过滤后 M′</th><th>过滤后 P′</th><th>P′/M′</th>"
             "<th>最长连续同号</th><th>正号年净利占比</th><th>裁定</th></tr></thead><tbody>"]
    for r in rows:
        if r["n"] == 0:
            continue
        c = cons[r["label"]]
        parts.append(
            f'<tr><td><strong>{r["label"]}</strong></td><td>{c["M"]}</td><td>{c["P"]}</td>'
            f'<td>{fpct(c["ratio"])}</td><td>{c["Mf"]}</td><td>{c["Pf"]}</td>'
            f'<td><strong>{fpct(c["ratio_f"])}</strong></td>'
            f'<td>{c["longest_run"]} 年</td><td>{fpct(c["pos_share"])}</td>'
            f'<td>{vd_html(r["verdict"])}</td></tr>')
    parts.append("</tbody></table></div>")
    return "".join(parts)


def conv_t2b(t):
    return t / (1 - t / 100) if t < 100 else float("inf")


def render_html(data):
    m = data["meta"]
    tl, bl = data["top_l1"], data["bottom_l1"]
    tm, bm = data["top_merge"], data["bottom_merge"]
    ov = m["overall"]
    cal = data["calibration"]
    hard_bad = [c for c in cal if c["level"] == "HARD" and not c["ok"]]
    warn_bad = [c for c in cal if c["level"] == "WARN" and not c["ok"]]

    def get(rows, lo):
        return next((r for r in rows if r["lo"] == lo), None)

    b510, b1015, b1520 = get(tl, 5), get(tl, 10), get(tl, 15)
    b2025, b2530 = get(tl, 20), get(tl, 25)
    b40 = get(tl, 40)
    l3 = data["top_l3"]
    l3_get = lambda lo: next(r for r in l3 if r["lo"] == lo)
    l3_2021, l3_2122, l3_2425 = l3_get(20), l3_get(21), l3_get(24)
    b2530_bottom = get(bl, 25)
    m25p = next((r for r in tm if r["lo"] == 25), None)

    drift = ""
    if hard_bad or warn_bad:
        items = "".join(
            f'<li>[{c["level"]}] {c["name"]}：期望 {c["expected"]}，实测 {c["actual"]}'
            + (f'（{c["note"]}）' if c["note"] else "") + "</li>"
            for c in (hard_bad + warn_bad))
        drift = (f'<div class="callout red"><strong>⚠ 本次数据与基线快照不一致，'
                 f'以下数字不可与历史报告直接比较。</strong><ul class="small">{items}</ul></div>')

    conv_rows = "".join(
        f'<tr><td>{t}%</td><td>{conv_t2b(t):.2f}%</td></tr>'
        for t in [5, 10, 15, 20, 25, 30, 35, 40])

    kpi2_note = (f'剔除前三 {fsign(b1015["pnl_ex_top3"])}，集中度 {b1015["conc"]:.1f}%'
                 if b1015 and b1015["conc"] is not None else "—")

    html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>P0-1 box 高度分带实验报告</title><style>{CSS}</style></head><body><main class="page">
<header><div class="eyebrow">DARVAS BOX · P0-1 BOX HEIGHT GRADIENT</div>
<h1>box 高度分带实验报告</h1>
<p class="sub">实验 ID：P0-1（十维 #2 box 高度）· A 类零成本后验 · 覆盖 2005–2018 共 14 年
· {m["funnel"]["leg1"]} 笔建仓 / {m["total_closed"]} 笔平仓 · 生成于 {m["generated"]}</p></header>
{drift}
<section class="kpis">
<div class="kpi"><div class="label">14 年累计净利</div>
<div class="value {cls(m["total_pnl"])}">{fsign(m["total_pnl"])}</div>
<div class="note">{m["total_closed"]} 笔平仓 · 整体胜率 {fpct(ov["wr"])}</div></div>
<div class="kpi"><div class="label">/top 10–15%（峰）每单位</div>
<div class="value {cls(b1015["pu"])}">{fsign(b1015["pu"])}</div>
<div class="note">{kpi2_note}</div></div>
<div class="kpi"><div class="label">/top 5–10% 剔除前三</div>
<div class="value {cls(b510["pnl_ex_top3"])}">{fsign(b510["pnl_ex_top3"])}</div>
<div class="note">总账 {fsign(b510["pnl"])}，但前三赢家 {fsign(b510["top3"])}</div></div>
<div class="kpi"><div class="label">/top 25%+ 汇总（H1）</div>
<div class="value {cls(m25p["pnl"])}">{fsign(m25p["pnl"])}</div>
<div class="note">{m25p["n"]} 笔 · 每单位 {fsign(m25p["pu"])} · {vd_html(m25p["verdict"])}</div></div>
</section>

<section class="summary"><h2>核心结论</h2><ul>
<li><strong>符号断裂点在 <code>/top</code> 25%，不在 40%。</strong>
20–25% 每单位 <span class="up">{fsign(b2025["pu"])}</span>（全表最高）、PF {fnum(b2025["pf"],2)}；
紧邻的 25–30% 直接翻成 <span class="down">{fsign(b2530["pu"])}</span>、PF {fnum(b2530["pf"],2)}。
若按原设计把 20–40 合并成一档，这个峰与谷会被平均掉。</li>
<li><strong>但「20–25% 的峰」经不起 1% 加密检验——它不是一片区域，是几条窄缝。</strong>
降到 1% 后：21–22% 净利 <span class="up">{fsign(l3_2122["pnl"])}</span> 看着惊人，却只有 {l3_2122["n"]} 笔、
集中度 {l3_2122["conc"]:.1f}%、<strong>剔除前三为 <span class="down">{fsign(l3_2122["pnl_ex_top3"])}</span></strong>；
24–25% 同样（{l3_2425["n"]} 笔、集中度 {l3_2425["conc"]:.1f}%、剔除前三 {fsign(l3_2425["pnl_ex_top3"])}）；
紧邻的 20–21% 直接是 <span class="down">{fsign(l3_2021["pnl"])}</span>。
<strong>结论：20–25% 的高每单位由少数交易与抽样波动造成，不可作为规则依据。</strong></li>
<li><strong>「越窄越好」不成立——5–10% 是少数赢家撑起的假 edge。</strong>
该带总账 <span class="up">{fsign(b510["pnl"])}</span> 看着很正，但前三赢家合计 {fsign(b510["top3"])}，
<strong>剔除后为 <span class="down">{fsign(b510["pnl_ex_top3"])}</span></strong>，集中度 {b510["conc"]:.1f}%。
同样的坑还有 15–20%：总账 <span class="up">{fsign(b1520["pnl"])}</span>，剔除前三后
<span class="down">{fsign(b1520["pnl_ex_top3"])}</span>。两个带合计「表面盈利」
{fsign(b510["pnl"] + b1520["pnl"])}，实质是 <span class="down">{fsign(b510["pnl_ex_top3"] + b1520["pnl_ex_top3"])}</span>。</li>
<li><strong>唯一通过「剔除前三」检验的带是 10–15%，但它还不是稳定 edge。</strong>
每单位 <span class="up">{fsign(b1015["pu"])}</span>、PF {fnum(b1015["pf"],2)}、集中度仅 {b1015["conc"]:.1f}%，
剔除前三仍有 <span class="up">{fsign(b1015["pnl_ex_top3"])}</span>（全表唯一既大额又能剔除后留存的带）。
但跨年只有 {data["cons_top"][b1015["label"]]["Pf"]}/{data["cons_top"][b1015["label"]]["Mf"]} 年为正，
按判定矩阵（≥70% 才算稳定）仍记「不稳定」——<strong>它是利润主体，还不是可宣称的稳定 edge</strong>。</li>
<li><strong>高箱有害，但对分母敏感。</strong>
同一批交易，<code>/top 40%+</code> 只剩 {b40["n"]} 笔（{fsign(b40["pnl"])}），
<code>/bottom 40%+</code> 却有 {bm[0]["n"]} 笔（{fsign(bm[0]["pnl"])}）——
因为 <code>/bottom 40%</code> 只等价于 <code>/top 28.6%</code>。结论方向一致，但区间宽度取决于尺子。</li>
</ul></section>

<div class="callout gold"><strong>阅读方式：</strong>本报告以 <code>/top</code> 为主口径（与代码准入闸门同尺），
<code>/bottom</code> 为历史对照口径。<strong>「剔除前三赢家」列是识破假 edge 的关键</strong>——
某带总账为正、剔除后转负，说明利润来自两三笔运气而非广泛优势。
红线为盈、绿线为亏（中国惯例）。⚠ 角标表示 n&lt;30，结论仅供参考。</div>

<div class="toc"><a href="#s1">① 实验设定与双口径</a><a href="#s2">② 主表 /top</a>
<a href="#s3">③ 对照 /bottom 与分母敏感性</a><a href="#s4">④ 2% 加密层</a>
<a href="#s5">⑤ 逐年矩阵与一致性</a><a href="#s6">⑥ 判定汇总</a><a href="#s7">诚实标注</a></div>

<h2 id="s1">① 实验设定与双口径</h2>
<p class="sub">先立规矩再看数。框架文档：<code>docs/experiments/e17_box_height_framework.md</code>（零结果纯规则）。</p>
<div class="panel"><h3>两套分母：同一箱体，两个数</h3>
<p class="small">代码准入闸门用 <code>(top−bottom)/top ≥ box.min_width(5%)</code>
（<code>internal/box/engine.go:127</code>）；文档与历史脚本多用 <code>(top−bottom)/bottom</code>。
两者不是同一个数，换算关系 <code>b = t/(1−t)</code>。</p>
<div class="table-wrap"><table style="min-width:340px"><thead><tr>
<th>/top 口径</th><th>≈ /bottom 口径</th></tr></thead><tbody>{conv_rows}</tbody></table></div>
<p class="small muted" style="margin-bottom:0">
推论一：<code>/top 5%</code> 闸门 ⟺ <code>/bottom 5.26%</code>，所以两个口径的 0–5% 都是空带。<br>
推论二：<code>/bottom 40%</code> 只等于 <code>/top 28.6%</code>，高箱端两把尺子差 11pp 以上。</p></div>
<div class="callout blue"><strong>为什么 0–5% 带是空的：</strong>
<code>(top−bottom)/top ≥ 5%</code> 是准入条件，样本天然不含窄箱。
这不是数据缺失，也<strong>不构成「窄箱差」的证据——窄箱从未被测过</strong>。
因此「5% 阈值是否过严」本实验<strong>不回答</strong>，须走 B 类（参数化 <code>box.min_width</code> 重跑 14 年）。</div>
<div class="panel"><h3>四项固定口径</h3>
<p class="small" style="margin:0">胜率 = 盈利笔数/已平仓；每单位净利 = Σ净利/Σ单位（{TIER_NOTE}）；
保本胜率 = 1/(1+平均盈利/|平均亏损|)；<strong>缺口 = 实际胜率 − 保本胜率</strong>（≥+2pp 才算有 edge）；
利润集中度 = 前三赢家/毛盈利；<strong>剔除前三净利 = 净利 − 前三赢家合计</strong>。</p></div>

<h2 id="s2">② 主表：<code>/top</code> 5% 等距（与闸门同尺）</h2>
<p class="sub">L1 层。n&lt;30 标 ⚠；末行为尾部合并。</p>
{maintable(tl)}
{diverging(tl, "/top 各带净利（同一比例尺）")}
<div class="panel"><h3>汇总行（H1 裁定用）</h3>{maintable(tm)}</div>

<h2 id="s3">③ 对照表：<code>/bottom</code> 与分母敏感性</h2>
<p class="sub">L1 层，历史口径。最右列是换算到 <code>/top</code> 的等价区间——横向读表时以此对齐。</p>
{maintable(bl, show_equiv=conv_t2b)}
<div class="panel"><h3>分母敏感性：同一事实，两个读数</h3>
<p class="small">把同一批交易换个分母，<strong>带的标签就整体挪位</strong>：</p>
<ul class="tight small" style="margin:0 0 8px 18px;padding:0">
<li><code>/bottom 25–30%</code> 在表上显示 {fsign(b2530_bottom["pnl"])}，
但它换算后只等价于 <code>/top 20.0–23.1%</code>——落在「峰」区，不是「高箱区」。</li>
<li>只有 <code>/bottom 35%+</code>（≈<code>/top 25.9%+</code>）才真正进入高箱负区。</li>
<li>所以「<code>/bottom 25–35</code> 也有盈利」与「<code>/top 25%+ 全线为负」<strong>并不矛盾</strong>——
它们描述的是不同的箱体集合。不换算就对比，会得出错误的「结论不稳」印象。</li>
</ul>
<div class="table-wrap"><table style="min-width:640px"><thead><tr>
<th>口径</th><th>高箱区定义</th><th>笔数</th><th>每单位</th><th>净利</th><th>PF</th></tr></thead><tbody>
<tr><td>/bottom 40%+（合并）</td><td>≈/top 28.6%+</td><td>{bm[0]["n"]}</td>
<td class="{cls(bm[0]["pu"])}">{fsign(bm[0]["pu"])}</td>
<td class="{cls(bm[0]["pnl"])}">{fsign(bm[0]["pnl"])}</td>
<td>{fnum(bm[0]["pf"],2)}</td></tr>
<tr><td>/top 40%+</td><td>真·尾部</td><td>{b40["n"]}</td>
<td class="{cls(b40["pu"])}">{fsign(b40["pu"])}</td>
<td class="{cls(b40["pnl"])}">{fsign(b40["pnl"])}</td>
<td>{fnum(b40["pf"],2)}</td></tr>
<tr class="sum"><td>/top 25%+（H1 裁定行）</td><td>高箱负区</td><td>{m25p["n"]}</td>
<td class="{cls(m25p["pu"])}">{fsign(m25p["pu"])}</td>
<td class="{cls(m25p["pnl"])}">{fsign(m25p["pnl"])}</td>
<td>{fnum(m25p["pf"],2)}</td></tr>
</tbody></table></div>
<p class="small muted" style="margin-bottom:0">三个口径描述三个不同的箱体集合。
裁定 H1 用 <code>/top 25%+</code>（样本最足、区间最贴合闸门），<code>40%+</code> 只作尾部展示。</p></div>

<h2 id="s4">④ 2% 加密层（L2，10–30% / 15–40%）</h2>
<p class="sub">触发规则是<strong>事先写死</strong>的（n≥30 且出现符号反转/集中度&gt;50%/剔除前三后符号翻转），不是看着结果挑区间。</p>
<div class="panel"><h3>/top 10–30% 按 2% 加密</h3>{maintable(data["top_l2"])}</div>
<div class="panel"><h3>/bottom 15–40% 按 2% 加密</h3>{maintable(data["bottom_l2"])}</div>

<h3>L3 · 第三层 1% 加密（规则触发，复核「峰是否成片」）</h3>
<div class="callout blue">触发依据（框架 §5.3 事先写死）：L2 中满足 <code>n≥30</code> 且集中度 &gt;50% 的段。
命中：<code>/top</code> 20–22（集中度 81.4%）、24–26（85.0%）、26–28（88.6%）→ 覆盖 <strong>20–28%</strong>；
<code>/bottom</code> 17–19（78.9%）、21–23（67.1%）、23–25（53.4%）、25–27（92.2%）、27–29（90.0%）→ 覆盖 <strong>17–29%</strong>。
1% 加密的目的是判断这些「高集中度段」是<strong>一条窄缝</strong>还是<strong>一片区域</strong>——若峰只存在于 1 个百分点内，则不可作为规则依据。</div>
<div class="panel"><h3>/top 20–28% 按 1% 加密</h3>{maintable(data["top_l3"])}</div>
<div class="panel"><h3>/bottom 17–29% 按 1% 加密</h3>{maintable(data["bottom_l3"])}</div>
<p class="small muted">L3 结果仅用于「峰是否成片」的稳健性复核，<strong>不据此新增阈值</strong>（框架 §5.5）。</p>

<h2 id="s5">⑤ 逐年 × 分带矩阵与一致性记分卡</h2>
<h3>/top 逐年矩阵（格内：净利 (笔数)）</h3>
{matrix_html(data["ym_top"], BANDS_TOP_L1)}
{signbar(tl, data["cons_top"])}
<h3>一致性记分卡（判定用过滤后 P′/M′）</h3>
<p class="sub small">过滤规则：只把「该年该带 n≥3」的年计入符号统计，避免 1 笔撞对被算成一个正号年。
阈值：P′/M′ ≥0.70 稳定为正；≤0.30 稳定为负；中间为不稳定。</p>
{scorecard(tl, data["cons_top"])}
<p class="small muted">「正号年净利占比」= 正号年净利合计 ÷ 该带净利合计。
超过 100% 说明负号年抵销了一部分；出现负值说明正号年净利与总账符号相反（如正号年小赚、负号年大亏）。</p>
<h3>/bottom 逐年矩阵</h3>
{matrix_html(data["ym_bottom"], BANDS_BOTTOM_L1)}

<h2 id="s6">⑥ 判定汇总</h2>
<h3>假设裁定（框架 §2 的 H1–H4）</h3>
<div class="table-wrap"><table><thead><tr><th style="width:52px">假设</th><th style="text-align:left">主张</th>
<th style="text-align:left">证据</th><th style="width:150px">裁定</th></tr></thead><tbody>
{"".join(f'<tr><td><strong>{h["id"]}</strong></td>'
         f'<td style="text-align:left;white-space:normal">{h["claim"]}</td>'
         f'<td style="text-align:left;white-space:normal" class="small">{h["evidence"]}</td>'
         f'<td>{hyp_html(h["verdict"])}</td></tr>' for h in data["hypotheses"])}
</tbody></table></div>
<h3>逐带裁定（/top）</h3>
<div class="table-wrap"><table><thead><tr><th>分带（/top）</th><th>n</th>
<th>每单位</th><th>缺口</th><th>剔除前三</th><th>P′/M′</th><th>裁定</th></tr></thead><tbody>
{"".join(f'<tr><td><strong>{r["label"]}</strong></td><td>{r["n"]}</td>'
         f'<td class="{cls(r["pu"])}">{fsign(r["pu"])}</td>'
         f'<td class="{cls(r["gap"])}">{fpp(r["gap"])}</td>'
         f'<td class="{cls(r["pnl_ex_top3"])}">{fsign(r["pnl_ex_top3"])}</td>'
         f'<td>{fpct(data["cons_top"][r["label"]]["ratio_f"])}</td>'
         f'<td>{vd_html(r["verdict"])}</td></tr>' for r in tl if r["n"] > 0)}
{"".join(f'<tr class="merge"><td>{r["label"]}</td><td>{r["n"]}</td>'
         f'<td class="{cls(r["pu"])}">{fsign(r["pu"])}</td>'
         f'<td class="{cls(r["gap"])}">{fpp(r["gap"])}</td>'
         f'<td class="{cls(r["pnl_ex_top3"])}">{fsign(r["pnl_ex_top3"])}</td>'
         f'<td>{fpct(data["cons_top_merge"][r["label"]]["ratio_f"])}</td>'
         f'<td>{vd_html(r["verdict"])}</td></tr>' for r in tm)}
</tbody></table></div>

<h2 id="s7">诚实标注</h2>
<div class="callout red"><ul class="tight small" style="margin:0 0 0 18px;padding:0">
<li><strong>0–5% 带不可检验</strong>：结构性无样本（闸门所致），不得解读为「窄箱差」。</li>
<li><strong>A 类不能证伪 5% 阈值本身</strong>：要回答「5% 是否过严」须走 B 类参数化重跑。</li>
<li><strong>后验切分，不是因果</strong>：分带不改变交易路径，带间差异含市场环境混杂。</li>
<li><strong>非样本外</strong>：2005–2018 是参数选定的同一段历史，结论只用于提假设。</li>
<li><strong>与历史 E8 不可直接比</strong>：E8 覆盖 2014–2018（570 笔）、边界为 5-8/8-12/12-15/15-20/20+；
本报告覆盖 2005–2018（1328 笔）、边界为 5-10/10-15/…，只能定性参照。</li>
<li><strong>本文与「距新高」报告是两个维度</strong>：距新高甜点区（第 5 维）见
<code>e17_newhigh_distance_report.html</code>，勿混用。</li>
</ul></div>

<h2>附录 · 校准与溯源</h2>
<div class="table-wrap"><table><thead><tr><th>层</th><th>断言</th><th>期望</th>
<th>实测</th><th>结果</th></tr></thead><tbody>
{"".join(f'<tr><td>{c["level"]}</td><td>{c["name"]}'
         + (f'<br><span class="muted small">{c["note"]}</span>' if c["note"] else "")
         + f'</td><td>{c["expected"]}</td><td>{c["actual"]}</td>'
         f'<td class="{"up" if c["ok"] else "down"}">{"通过" if c["ok"] else "失败"}</td></tr>'
         for c in cal)}
</tbody></table></div>
<div class="panel"><h3>探索日志（本次跑过的全部切法，不删）</h3>
<ul class="tight small" style="margin:0 0 0 18px;padding:0">
<li>L1：两种口径各按 5% 等距全切（主表 / 对照表）。</li>
<li>L2：/top 10–30% 按 2% 加密；/bottom 15–40% 按 2% 加密（末段 39–40 为区间截断）。</li>
<li>合并行：/top 5–25%、/top 25%+、/bottom 40%+（仅尾部与裁定行允许合并）。</li>
<li>未尝试：1% 加密、非等距「找拐点」切法（后者违反框架 §5.1 纪律）。</li>
</ul>
<p class="small muted" style="margin-bottom:0">声明：本报告<strong>非盲测</strong>——
框架定稿前已跑过一次基线统计（见框架 §7.4 诚实声明），故属「探索性后验分析」，不是预注册盲测。</p></div>

<div class="foot">
<p><strong>数据溯源：</strong>14 库 <code>var/bt/bt_e17_&lt;year&gt;.db</code>（{m["funnel"]["dbs"]} 个）→
<code>position_leg.leg_no=1</code> → <code>plan_order.reason_json.box_top/box_bottom</code>。</p>
<p><strong>样本漏斗：</strong>建仓 {m["funnel"]["leg1"]} → 已平仓 {m["total_closed"]}
（剔除未平仓）→ 字段缺失 {m["funnel"]["miss_box"]} → 脏几何 {m["funnel"]["bad_geom"]}。</p>
<p><strong>生成器：</strong><code>scripts/e17_box_height_report.py</code> sha256:{m["generator_sha256"]} ·
启用的层级：{m["layers"]}</p>
<p><strong>免责声明：</strong>以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。
市场有风险，投资需谨慎。过往表现不预示未来收益。</p>
</div>
</main></body></html>'''
    return html


def render_md(data):
    m = data["meta"]
    tl, bl, tm = data["top_l1"], data["bottom_l1"], data["top_merge"]
    ov = m["overall"]

    def row(b, show_eq=False):
        if b["n"] == 0:
            return f'| {b["label"]} | 0 | — | — | — | — | — | — | — | — | — | 结构性无样本 |'
        conc = f'{b["conc"]:.1f}%' if b["conc"] is not None else "—"
        s = (f'| {b["label"]} | {b["n"]} | {fpct(b["wr"])} | {fsign(b["pu"])} | '
             f'{fpct(b["bewr"])} | {fpp(b["gap"])} | {fnum(b["pf"],2) if b["pf"] is not None else "—"} | '
             f'{conc} | {fsign(b["top3"])} | {fsign(b["pnl_ex_top3"])} | {fsign(b["pnl"])} | {b["verdict"]} |')
        if show_eq:
            s += f' {conv_t2b(b["lo"]):.1f}% |'
        return s

    head = ("| 分带 | 笔数 | 胜率 | 每单位净利 | 保本胜率 | 缺口 | PF | 集中度 | 前三赢家 "
            "| 剔除前三净利 | 净利 | 裁定 |")

    L = []
    L.append("# P0-1 · box 高度分带实验报告\n")
    L.append(f"> 实验 ID：P0-1（十维 #2 box 高度）· A 类零成本后验 · 2005–2018 共 14 年")
    L.append(f"> {m['funnel']['leg1']} 笔建仓 / {m['total_closed']} 笔平仓 · "
             f"生成于 {m['generated']} · 生成器 sha256:{m['generator_sha256']}\n")

    L.append("## 核心结论\n")
    g = lambda rows, lo: next(r for r in rows if r["lo"] == lo)
    b510, b1015, b1520, b2025, b2530, b40 = (g(tl, 5), g(tl, 10), g(tl, 15),
                                             g(tl, 20), g(tl, 25), g(tl, 40))
    c1015 = data["cons_top"][b1015["label"]]
    m25p = next(r for r in tm if r["lo"] == 25)
    l3 = data["top_l3"]
    l3_2021, l3_2122, l3_2425 = (g(l3, 20), g(l3, 21), g(l3, 24))
    L.append(f"- **符号断裂点在 `/top` 25%**：20–25% 每单位 {fsign(b2025['pu'])}"
             f"（全表最高），25–30% 翻成 {fsign(b2530['pu'])}。")
    L.append(f"- **但「20–25% 的峰」经不起 1% 加密检验**：21–22% 净利 {fsign(l3_2122['pnl'])} 却只有 "
             f"{l3_2122['n']} 笔、集中度 {l3_2122['conc']:.1f}%、剔除前三 {fsign(l3_2122['pnl_ex_top3'])}；"
             f"24–25% 同样（{l3_2425['n']} 笔、集中度 {l3_2425['conc']:.1f}%、剔除前三 "
             f"{fsign(l3_2425['pnl_ex_top3'])}）；紧邻的 20–21% 是 {fsign(l3_2021['pnl'])}。"
             f"→ 峰由少数交易与抽样波动造成，不可作为规则依据。")
    L.append(f"- **5–10% 是假 edge**：总账 {fsign(b510['pnl'])}，前三赢家 {fsign(b510['top3'])}，"
             f"剔除后 **{fsign(b510['pnl_ex_top3'])}**，集中度 {b510['conc']:.1f}%。"
             f"同类坑：15–20% 总账 {fsign(b1520['pnl'])}、剔除前三 {fsign(b1520['pnl_ex_top3'])}。")
    L.append(f"- **唯一通过「剔除前三」检验的带是 10–15%**：每单位 {fsign(b1015['pu'])}、"
             f"PF {fnum(b1015['pf'],2)}、集中度 {b1015['conc']:.1f}%，"
             f"剔除前三仍 {fsign(b1015['pnl_ex_top3'])}。但跨年仅 {c1015['Pf']}/{c1015['Mf']} 年为正，"
             f"按矩阵（≥70% 才算稳定）记「{b1015['verdict']}」——利润主体，还不是稳定 edge。")
    L.append(f"- **高箱负区（H1 裁定行 `/top` 25%+）**：{m25p['n']} 笔，"
             f"每单位 {fsign(m25p['pu'])}，净利 {fsign(m25p['pnl'])}，裁定「{m25p['verdict']}」。")
    L.append(f"- **分母敏感**：`/top 40%+` 仅 {b40['n']} 笔（{fsign(b40['pnl'])}）；"
             f"`/bottom 40%+` 有 {data['bottom_merge'][0]['n']} 笔"
             f"（{fsign(data['bottom_merge'][0]['pnl'])}）——因 `/bottom 40%` 只等于 `/top 28.6%`。\n")

    L.append("## ① 双口径换算表\n")
    L.append("`b = t/(1−t)`\n")
    L.append("| /top | /bottom |")
    L.append("|---|---|")
    for t in [5, 10, 15, 20, 25, 30, 35, 40]:
        L.append(f"| {t}% | {conv_t2b(t):.2f}% |")
    L.append("")

    L.append("## ② 主表：/top 5% 等距\n")
    L.append(head)
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in tl:
        L.append(row(b))
    L.append("")
    L.append("### 汇总结论行\n")
    L.append(head)
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in tm:
        L.append(row(b))
    L.append("")

    L.append("## ③ 对照表：/bottom 5% 等距\n")
    L.append(head + " ≈/top |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in bl:
        L.append(row(b, show_eq=True))
    L.append("")

    L.append("## ④ 2% 加密层\n")
    L.append("### /top 10–30%\n")
    L.append(head)
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in data["top_l2"]:
        L.append(row(b))
    L.append("")
    L.append("### /bottom 15–40%\n")
    L.append(head)
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in data["bottom_l2"]:
        L.append(row(b))
    L.append("")
    L.append("### L3 · 1% 加密（规则触发，复核峰是否成片）\n")
    L.append("触发：L2 中 `n≥30` 且集中度>50% —— `/top` 20–22 / 24–26 / 26–28；"
             "`/bottom` 17–19 / 21–23 / 23–25 / 25–27 / 27–29。\n")
    L.append("#### /top 20–28%\n")
    L.append(head)
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in data["top_l3"]:
        L.append(row(b))
    L.append("")
    L.append("#### /bottom 17–29%\n")
    L.append(head)
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in data["bottom_l3"]:
        L.append(row(b))
    L.append("")

    L.append("## ⑤ 一致性记分卡（判定用 P′/M′）\n")
    L.append("| 分带 | M | P | P/M | M′ | P′ | P′/M′ | 最长连续同号 | 正号年净利占比 | 裁定 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for b in tl:
        if b["n"] == 0:
            continue
        c = data["cons_top"][b["label"]]
        L.append(f'| {b["label"]} | {c["M"]} | {c["P"]} | {fpct(c["ratio"])} | '
                 f'{c["Mf"]} | {c["Pf"]} | {fpct(c["ratio_f"])} | {c["longest_run"]} 年 | '
                 f'{fpct(c["pos_share"])} | {b["verdict"]} |')
    L.append("")

    L.append("## ⑥ 假设裁定（H1–H4）\n")
    L.append("| 假设 | 主张 | 证据 | 裁定 |")
    L.append("|---|---|---|---|")
    for h in data["hypotheses"]:
        L.append(f'| **{h["id"]}** | {h["claim"]} | {h["evidence"]} | **{h["verdict"]}** |')
    L.append("")
    L.append("## ⑦ 逐带裁定汇总（/top）\n")
    L.append("| 分带 | n | 每单位 | 缺口 | 剔除前三 | P′/M′ | 裁定 |")
    L.append("|---|---|---|---|---|---|---|")
    for b in tl:
        if b["n"] == 0:
            L.append(f'| {b["label"]} | 0 | — | — | — | — | {b["verdict"]} |')
            continue
        c = data["cons_top"][b["label"]]
        L.append(f'| {b["label"]} | {b["n"]} | {fsign(b["pu"])} | {fpp(b["gap"])} | '
                 f'{fsign(b["pnl_ex_top3"])} | {fpct(c["ratio_f"])} | {b["verdict"]} |')
    L.append("")

    L.append("## 诚实标注\n")
    for s in [
        "0–5% 带结构性无样本（闸门所致），不得解读为「窄箱差」——窄箱从未被测过。",
        "A 类不能证伪 5% 阈值本身；要回答须走 B 类参数化重跑。",
        "后验切分，不是因果；带间差异含市场环境混杂。",
        "非样本外：2005–2018 是参数选定的同一段历史。",
        "与历史 E8 不可直接比（年份、边界、样本量均不同），只能定性参照。",
        "本报告非盲测（框架定稿前已跑过基线），属探索性后验分析。",
    ]:
        L.append(f"- {s}")
    L.append("")

    L.append("## 附录 · 校准结果\n")
    L.append("| 层 | 断言 | 期望 | 实测 | 结果 |")
    L.append("|---|---|---|---|---|")
    for c in data["calibration"]:
        L.append(f'| {c["level"]} | {c["name"]} | {c["expected"]} | {c["actual"]} | '
                 f'{"通过" if c["ok"] else "失败"} |')
    L.append("")
    L.append("---")
    L.append("*本报告由 scripts/e17_box_height_report.py 自动生成；框架见 "
             "docs/experiments/e17_box_height_framework.md。*")
    return "\n".join(L)


def render_check(data):
    cal = data["calibration"]
    w = max(len(c["name"]) for c in cal) + 2
    print(f"\n=== P0-1 校准表（{datetime.now():%Y-%m-%d %H:%M}）===")
    for c in cal:
        flag = "OK  " if c["ok"] else ("FAIL" if c["level"] == "HARD" else "WARN")
        print(f'[{c["level"]:4s}] {flag} {c["name"]:<{w}} 期望={c["expected"]}  实测={c["actual"]}')
    nh = sum(1 for c in cal if c["level"] == "HARD" and c["ok"])
    th = sum(1 for c in cal if c["level"] == "HARD")
    nw = sum(1 for c in cal if c["level"] == "WARN" and not c["ok"])
    print(f"--- HARD {nh}/{th} 通过，WARN 失败 {nw} 项 ---\n")


def main():
    args = set(sys.argv[1:])
    allow_drift = "--allow-drift" in args
    check_only = "--check-only" in args

    data = build()
    cal = data["calibration"]
    hard_bad = [c for c in cal if c["level"] == "HARD" and not c["ok"]]

    render_check(data)

    if hard_bad:
        print("!! HARD 断言失败，未写出任何文件：", file=sys.stderr)
        for c in hard_bad:
            print(f'  - {c["name"]}：期望 {c["expected"]}，实测 {c["actual"]}'
                  + (f'（{c["note"]}）' if c["note"] else ""), file=sys.stderr)
        print("\n可能原因：新增/减少年份、回测库被重写、box.min_width 参数变化、"
              "分带逻辑改动。确认原因后如属预期，可加 --allow-drift。", file=sys.stderr)
        if not allow_drift:
            sys.exit(2)
        print("（--allow-drift：降级为 WARN 继续）", file=sys.stderr)

    if check_only:
        print("--check-only：未写文件。")
        return

    EXP.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(render_html(data), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"written: {OUT_HTML}")
    print(f"written: {OUT_MD}")
    print(f"written: {OUT_JSON}")
    print(f"样本 {data['meta']['funnel']['leg1']} 建仓 / {data['meta']['total_closed']} 平仓 / "
          f"净利 {data['meta']['total_pnl']:+,.1f}")


if __name__ == "__main__":
    main()
