#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-3 · 大盘 regime 归因 · 专项报告生成器。

实验：docs/29 §五 P0-3（十维 #10 固定单位头寸管理），A 类零成本归因，不重跑回测、不改回测器。
框架：docs/experiments/e17_regime_attribution_framework.md（零结果纯规则，含 H1–H6 与 10 级裁定矩阵）

三口径（框架 §4.1）：
  C1  MA20  上证 close vs MA20 上下方
  C2  MA60  上证 close vs MA60 上下方      ← 主表
  C3  A判据 60 日低点相邻日比 → 升势/降势/震荡（internal/regime/regime.go:181-236 同口径）

三时点（框架 §4.2）：
  P plan_date（信号日，仅审计轨，reason_json.regime 即此日 C3）
  O opened_date（建仓日）← 主
  C closed_date（平仓日）← 对照

有效样本（框架 §4.5）：每桶同时报 n（笔数）与 m（regime 段数）。同一段内的多笔交易共享同一
大盘状态，不是独立观测；m < 20 不给裁定。

输出（docs/experiments/，实验报告唯一沉淀处）：
  e17_regime_attribution_report.html / .md / e17_regime_attribution_data.json

用法：
  python scripts/e17_regime_attribution_report.py              # 正式生成（HARD 失败即中止）
  python scripts/e17_regime_attribution_report.py --check-only # 只跑校准，不写文件
  python scripts/e17_regime_attribution_report.py --allow-drift# HARD 降级为 WARN（需明确知道为何漂移）
"""
import bisect
import csv
import hashlib
import json
import os
import sqlite3
import statistics
import sys
import time
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
EXP = ROOT / "docs" / "experiments"
OUT_HTML = EXP / "e17_regime_attribution_report.html"
OUT_MD = EXP / "e17_regime_attribution_report.md"
OUT_JSON = EXP / "e17_regime_attribution_data.json"
METRICS = BT / "e17_metrics.csv"

INDEX_CODE = "sh000001"
SAMPLE_START = "2005-01-04"     # 也是指数峰值回撤的基准峰起点（框架 §4.6）
SAMPLE_END = "2018-12-31"
MA_WINDOWS = (20, 60)
A_PARAMS = {"lookback": 60, "up_bias": 1.002, "down_bias": 0.998}

# ---------------------------------------------------------------- 分桶定义（框架 §5.2）
# MA 距离：L1 二元 / L2 对称八带 / L3 边界加密。元组 (lo, hi, label)，左闭右开，None 表示开口。
MA_L1 = [(None, 0.0, "下方 d<0"), (0.0, None, "上方 d≥0")]
MA_L2 = [(None, -10.0, "≤−10%"), (-10.0, -5.0, "−10~−5%"), (-5.0, -2.0, "−5~−2%"),
         (-2.0, 0.0, "−2~0%"), (0.0, 2.0, "0~+2%"), (2.0, 5.0, "+2~+5%"),
         (5.0, 10.0, "+5~+10%"), (10.0, None, "≥+10%")]
MA_L3 = [(-4.0, -3.0, "−4~−3%"), (-3.0, -2.0, "−3~−2%"), (-2.0, -1.0, "−2~−1%"),
         (-1.0, 0.0, "−1~0%"), (0.0, 1.0, "0~+1%"), (1.0, 2.0, "+1~+2%"),
         (2.0, 3.0, "+2~+3%"), (3.0, 4.0, "+3~+4%")]

# A 判据三态（行序固定：看空在前）
A_BUCKETS = [(-1, "降势（熊）"), (0, "震荡（猴）"), (1, "升势（牛）")]

# 回撤分带
IDX_DD = [(0.0, 5.0, "0–5%"), (5.0, 10.0, "5–10%"), (10.0, 20.0, "10–20%"),
          (20.0, 30.0, "20–30%"), (30.0, 40.0, "30–40%"), (40.0, None, "40%+")]
# 距 52 周（250 交易日）高点回撤：操作口径。与 2005 基准峰并存——
# 后者被 2007 年泡沫高点锚死（78% 样本挤在 40%+），失去判别力（见框架 §10 差异 #8）。
IDX_DD250 = [(0.0, 5.0, "0–5%"), (5.0, 10.0, "5–10%"), (10.0, 20.0, "10–20%"),
             (20.0, 30.0, "20–30%"), (30.0, None, "30%+")]
ACCT_DD = [(0.0, 5.0, "<5%"), (5.0, 10.0, "5–10%"), (10.0, None, ">10%")]

# ---------------------------------------------------------------- 校准基线
# 基线锁定：2026-10-26 首次基线跑（14 库 2005-2018）。改基线必须走一次 diff 评审。
BASELINE = {
    "leg1": 1405,
    "closed": 1328,
    "pnl": 842799.7,
    "pnl_tol": 0.5,
    # C3@P = reason_json.regime 落库值；C3@O = M9.0 报告口径
    "regime_stored": {"bear": 56, "range": 1165, "bull": 107},
    "regime_a_open": {"bear": 51, "range": 1186, "bull": 91},
    "regime_a_pnl": {"bear": -27292.0, "range": 604560.0, "bull": 265532.0},
    "ma20": {"below": 527, "above": 801},
    "ma60": {"below": 605, "above": 723},
    "segments": {"a": 494, "ma20": 303, "ma60": 147},
    "trading_days": 3402,
    "acct_dd_days": {"lt5": 2861, "5to10": 541, "gt10": 0},
    "acct_dd_max": -9.08,
    "overall_pf": 1.68,
}

TIER_NOTE = "1 单位 = 2% 权益；E17 阶梯 t1=3 / t2=2 / t3=1 / t4=0.5"


# ---------------------------------------------------------------- 基础工具
def years_list():
    return sorted(int(os.path.basename(p).split("_")[2].split(".")[0])
                  for p in glob_db())


def glob_db():
    return [str(p) for p in BT.glob("bt_e17_*.db")]


def fnum(x, d=0):
    return "—" if x is None else f"{x:,.{d}f}"


def fpct(x, d=1):
    return "—" if x is None else f"{x:.{d}f}%"


def fsign(x, d=0):
    return "—" if x is None else f"{x:+,.{d}f}"


def fpp(x):
    return "—" if x is None else f"{x:+.1f}pp"


def cls(x):
    if x is None:
        return "neutral"
    return "up" if x > 0 else ("down" if x < 0 else "neutral")


def ym(ts):
    return (ts or "")[:10]


# ---------------------------------------------------------------- 数据加载
def load_trades():
    """加载 14 库 leg_no=1 建仓记录。返回 (trades, funnel)。"""
    trades, funnel = [], {"dbs": 0, "leg1": 0, "closed": 0,
                          "miss_units": 0, "bad_units": 0, "miss_regime": 0}
    for y in years_list():
        db = BT / f"bt_e17_{y}.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sql = ("SELECT p.opened_date, p.closed_date, p.status, p.realized_pnl, p.code, "
               "o.reason_json, t.plan_date FROM position_leg l "
               "JOIN position p ON p.id=l.position_id "
               "LEFT JOIN plan_order o ON o.id=l.plan_order_id "
               "LEFT JOIN trade_plan t ON t.id=o.trade_plan_id "
               "WHERE l.leg_no=1")
        for r in con.execute(sql):
            funnel["leg1"] += 1
            try:
                rj = json.loads(r["reason_json"] or "{}")
            except (TypeError, ValueError):
                rj = {}
            if rj.get("units") is None:
                funnel["miss_units"] += 1
                units = 1.0
            else:
                try:
                    units = float(rj["units"])
                except (TypeError, ValueError):
                    units = 1.0
                if units <= 0:
                    funnel["bad_units"] += 1
                    units = 1.0
            if rj.get("regime") is None:
                funnel["miss_regime"] += 1
            closed = r["status"] == "closed" and r["realized_pnl"] is not None
            if closed:
                funnel["closed"] += 1
            trades.append({
                "year": y, "code": r["code"], "units": units,
                "pnl": r["realized_pnl"] if closed else None,
                "status": r["status"],
                "O": ym(r["opened_date"]), "C": ym(r["closed_date"]),
                "P": ym(r["plan_date"]),
                "regime": rj.get("regime"),
                "basis": rj.get("sizing_basis"),
            })
        con.close()
        funnel["dbs"] += 1
    return trades, funnel


def load_metrics():
    """读 e17_metrics.csv 做跨源对账。无表头，列序见 docs。
    返回 {year: (days, maxdd_pct, closed_count, pnl)}。"""
    out = {}
    if not METRICS.exists():
        return out
    with METRICS.open(encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip().isdigit():
                continue
            try:
                out[int(row[0])] = (int(float(row[1])), float(row[5]),
                                    int(float(row[8])), float(row[10]))
            except (IndexError, ValueError):
                continue
    return out


def load_index_series():
    """从样本首库读 sh000001 全序列 + 14 库指纹（HARD 断言用）。"""
    db = BT / f"bt_e17_{years_list()[0]}.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT date, close, low FROM daily_bar "
                       "WHERE code=? ORDER BY date", (INDEX_CODE,)).fetchall()
    con.close()
    dates = [r[0] for r in rows]
    closes = [r[1] for r in rows]
    lows = [r[2] for r in rows]

    fps = []
    for y in years_list():
        c2 = sqlite3.connect(f"file:{BT / f'bt_e17_{y}.db'}?mode=ro", uri=True)
        r = c2.execute("SELECT COUNT(*), MIN(date), MAX(date), SUM(close) "
                       "FROM daily_bar WHERE code=?", (INDEX_CODE,)).fetchone()
        c2.close()
        fps.append({"year": y, "n": r[0], "min": r[1], "max": r[2],
                    "sum_close": round(r[3] or 0.0, 2)})
    return dates, closes, lows, fps


def load_equity():
    """读各年 equity_snapshot.drawdown，返回 (逐日 {date: dd_pct 正数}, 逐年 {year: (最深, n天, 天数分档)})。"""
    by_date, by_year = {}, {}
    for y in years_list():
        db = BT / f"bt_e17_{y}.db"
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("SELECT date, drawdown FROM equity_snapshot").fetchall()
        con.close()
        dds = []
        for d, v in rows:
            if v is None:
                continue
            p = abs(v) * 100.0
            by_date[d] = p
            dds.append(p)
        if dds:
            by_year[y] = {"max": -max(dds), "days": len(dds),
                          "lt5": sum(1 for x in dds if x < 5),
                          "5to10": sum(1 for x in dds if 5 <= x <= 10),
                          "gt10": sum(1 for x in dds if x > 10)}
    return by_date, by_year


# ---------------------------------------------------------------- 指数特征（零未来信息）
def build_features(dates, closes, lows):
    """单趟正向滚动，只用 date <= 当日的 K 线。返回 {date: {...}}。"""
    feat = OrderedDict()
    lb = A_PARAMS["lookback"]
    up, dn = A_PARAMS["up_bias"], A_PARAMS["down_bias"]
    peak = None
    peak_1990 = None
    dq = deque()          # 250 日滚动最高的单调队列（存下标，close 递减）
    dd250_win = 250
    for i, d in enumerate(dates):
        f = {"close": closes[i], "low": lows[i], "ma20": None, "ma60": None,
             "d20": None, "d60": None, "a": 0, "dd": None, "dd1990": None,
             "dd250": None}
        for w in MA_WINDOWS:
            if i >= w - 1:
                f[f"ma{w}"] = sum(closes[i - w + 1:i + 1]) / w
        if f["ma20"]:
            f["d20"] = (closes[i] - f["ma20"]) / f["ma20"] * 100.0
        if f["ma60"]:
            f["d60"] = (closes[i] - f["ma60"]) / f["ma60"] * 100.0
        # A 判据：cur = 含当日最近 lb 根 low 的最小值；prev = 前一日回溯 lb 根
        if i >= lb:
            cur = min(lows[i - lb + 1:i + 1])
            prev = min(lows[i - lb:i])
            f["a"] = 1 if cur > prev * up else (-1 if cur < prev * dn else 0)
        # 指数峰值回撤：主口径自样本起点累积；1990 口径仅作敏感性
        peak_1990 = closes[i] if peak_1990 is None else max(peak_1990, closes[i])
        f["dd1990"] = (closes[i] / peak_1990 - 1.0) * 100.0
        if d >= SAMPLE_START:
            peak = closes[i] if peak is None else max(peak, closes[i])
            f["dd"] = (closes[i] / peak - 1.0) * 100.0
        # 距 250 交易日（约 52 周）高点的回撤：操作口径，滚动窗口、零未来信息
        while dq and closes[dq[-1]] <= closes[i]:
            dq.pop()
        dq.append(i)
        while dq[0] < i - dd250_win + 1:
            dq.popleft()
        if d >= SAMPLE_START:
            f["dd250"] = (closes[i] / closes[dq[0]] - 1.0) * 100.0
        feat[d] = f
    return feat


def label_at(dates, feat, day, key):
    """取 <= day 的最后一个交易日的特征值；越界返回 (None, False)。"""
    if not day:
        return None, False
    i = bisect.bisect_right(dates, day) - 1
    if i < 0 or i >= len(dates):
        return None, False
    return feat[dates[i]][key], True


def attach_labels(trades, dates, feat, eq_by_date):
    """给每笔挂 6 个 regime 标签 + 2 个回撤字段。缺失计数记入返回值。"""
    miss = {"d20_O": 0, "d60_O": 0, "a_O": 0, "d20_C": 0, "d60_C": 0, "a_C": 0,
            "a_P": 0, "dd_idx": 0, "dd250": 0, "dd_acct": 0}
    for t in trades:
        for day, suffix in ((t["O"], "O"), (t["C"], "C")):
            for k in ("d20", "d60", "a"):
                v, ok = label_at(dates, feat, day, k)
                t[f"{k}_{suffix}"] = v
                if not ok:
                    miss[f"{k}_{suffix}"] += 1
        v, ok = label_at(dates, feat, t["P"], "a")
        t["a_P"] = v
        if not ok:
            miss["a_P"] += 1
        # 指数回撤：按建仓日（2005 基准峰 + 250 日滚动峰两种口径）
        v, ok = label_at(dates, feat, t["O"], "dd")
        t["dd_idx"] = abs(v) if (ok and v is not None) else None
        if t["dd_idx"] is None:
            miss["dd_idx"] += 1
        v, ok = label_at(dates, feat, t["O"], "dd250")
        t["dd250"] = abs(v) if (ok and v is not None) else None
        if t["dd250"] is None:
            miss["dd250"] += 1
        # 账户回撤：按建仓日，取 <= 当日的最后一个快照
        t["dd_acct"] = None
        if t["O"]:
            cand = [d for d in (t["O"],) if d in eq_by_date]
            if cand:
                t["dd_acct"] = eq_by_date[cand[0]]
            else:
                ks = sorted(eq_by_date)
                j = bisect.bisect_right(ks, t["O"]) - 1
                if j >= 0:
                    t["dd_acct"] = eq_by_date[ks[j]]
        if t["dd_acct"] is None:
            miss["dd_acct"] += 1
    return miss


# ---------------------------------------------------------------- 段切分（有效样本 m）
def build_episodes(dates, feat, key, start=SAMPLE_START, end=SAMPLE_END):
    """把 [start,end] 的交易日切成连续同标签区间。返回 (runs, {date: ep_index})。"""
    runs, ep = [], {}
    prev = None
    for d in dates:
        if not (start <= d <= end):
            continue
        v = feat[d][key]
        if prev is None or v != prev:
            runs.append({"i": len(runs), "label": v, "start": d, "end": d, "days": 1})
            prev = v
        else:
            runs[-1]["end"] = d
            runs[-1]["days"] += 1
        ep[d] = runs[-1]["i"]
    return runs, ep


_EP_KEYS = {}


def ep_of(ep_map, dates, day):
    """把交易日映射到 episode；若当日无收盘（停牌/非交易日）取 <= day 的最近交易日。"""
    if not day:
        return None
    if day in ep_map:
        return ep_map[day]
    ks = _EP_KEYS.get(id(ep_map))
    if ks is None:
        ks = sorted(ep_map)
        _EP_KEYS[id(ep_map)] = ks
    j = bisect.bisect_right(ks, day) - 1
    return ep_map[ks[j]] if j >= 0 else None


def seg_stats(rows, ep_map, dates, daykey):
    """段级聚合：段数 M_seg / 正段 P_seg / ratio_seg / 段级每单位中位数 / 前三盈利段集中度。"""
    groups = {}
    for t in rows:
        e = ep_of(ep_map, dates, t[daykey])
        if e is None:
            continue
        groups.setdefault(e, []).append(t)
    M = len(groups)
    if M == 0:
        return {"m": 0, "M_seg": 0, "P_seg": 0, "ratio_seg": None,
                "pu_seg_med": None, "seg_top3_share": None}
    pus, pnls = [], []
    for ts in groups.values():
        p = sum(t["pnl"] for t in ts)
        u = sum(t["units"] for t in ts)
        pnls.append(p)
        pus.append(p / u if u else 0.0)
    pos = sum(1 for p in pnls if p > 0)
    pos_sum = sum(p for p in pnls if p > 0)
    top3 = sum(sorted(pnls, reverse=True)[:3])
    return {
        "m": M, "M_seg": M, "P_seg": pos,
        "ratio_seg": pos / M,
        "pu_seg_med": statistics.median(pus) if pus else None,
        "seg_top3_share": (top3 / pos_sum * 100 if pos_sum > 0 else None),
    }


# ---------------------------------------------------------------- 聚合（框架 §4.7）
def agg(rows):
    """四项固定口径 + PF + 缺口 + 剔除前三。"""
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
        "conc": (top3 / gw * 100 if (gw > 0 and top3 > 0) else None),
        "pnl_ex_top3": ex_pnl,
        "pu_ex_top3": (ex_pnl / ex_units if ex_units else None),
    }


def num_rows(trades, key, lo, hi):
    if lo is None:
        return [t for t in trades if t[key] is not None and t[key] < hi]
    if hi is None:
        return [t for t in trades if t[key] is not None and t[key] >= lo]
    return [t for t in trades if t[key] is not None and lo <= t[key] < hi]


def ma_stat(trades, key, bands, ep_map, dates, daykey, is_empty=None):
    out = []
    for lo, hi, label in bands:
        rows = num_rows(trades, key, lo, hi)
        s = agg(rows)
        sg = seg_stats(rows, ep_map, dates, daykey)
        out.append({"label": label, "lo": lo, "hi": hi, **s, **sg})
    return out


def a_stat(trades, key, ep_map, dates, daykey):
    out = []
    for v, label in A_BUCKETS:
        rows = [t for t in trades if t[key] == v]
        s = agg(rows)
        sg = seg_stats(rows, ep_map, dates, daykey)
        out.append({"label": label, "val": v, **s, **sg})
    return out


def dd_stat(trades, key, bands):
    out = []
    for lo, hi, label in bands:
        rows = num_rows(trades, key, lo, hi)
        out.append({"label": label, "lo": lo, "hi": hi, **agg(rows)})
    return out


# ---------------------------------------------------------------- 跨年一致性（框架 §7.2）
def consistency(trades, pred, years):
    per_year = []
    for y in years:
        rows = [t for t in trades if t["year"] == y and pred(t)]
        s = agg(rows)
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


def verdict(s, is_empty=False):
    """裁定标签（框架 §7.3，10 级顺序不可调）。"""
    if is_empty:
        return "不可检验（结构性无样本）"
    if s["n"] < 30:
        return "样本不足"
    if s.get("m", 0) < 20:
        return "有效样本不足（段数 <20）"
    ratio, gap = s.get("ratio_f"), s["gap"]
    pu, pu_ex = s["pu"], s["pu_ex_top3"]
    ratio_seg = s.get("ratio_seg")
    gapok = gap is not None and gap >= 2
    gapneg = gap is not None and gap <= -2

    # 5) 剔除前三后翻负 → 假 edge（必须先于稳定性检查）
    if pu is not None and pu > 0 and pu_ex is not None and pu_ex <= 0:
        return "假 edge（前三撑起）"
    # 6) 段级一致性：比的是「多数 regime 段是否为正」这个比例，不是「段级中位数 vs 笔级均值」
    #    （后者是均值与中位数相比，天然偏向标记，不构成矛盾）
    if pu is not None and ratio_seg is not None and pu != 0:
        if pu > 0 and ratio_seg < 0.50:
            return "笔级为正但多数段为负"
        if pu < 0 and ratio_seg >= 0.50:
            return "笔级为负但多数段为正"
    # 7/8/9) 跨年
    if ratio is not None and ratio >= 0.70:
        if gapok:
            # 补充降级：上方桶即便跨年达标，若集中度过高或剔除后剩不到一半 → 利润主体
            if (s.get("conc") is not None and s["conc"] > 50) or \
               (pu and pu_ex is not None and pu_ex / pu < 0.5):
                return "利润主体（非 edge）"
            return "edge"
        return "稳但缺口不足"
    if ratio is not None and ratio <= 0.30:
        return "负 edge" if gapneg else "跨年偏负（缺口未达 −2pp）"
    if pu is not None and pu > 0:
        return "不稳定（正但非跨年）"
    return "不稳定（负但非跨年）"


# ---------------------------------------------------------------- 诊断
def transition(trades, k1, k2):
    """2×2 转移矩阵（上方/下方）：区分「买错」与「卖晚」。"""
    def lab(v):
        return None if v is None else ("上" if v >= 0 else "下")
    cells = OrderedDict()
    for a, b in (("下", "下"), ("下", "上"), ("上", "下"), ("上", "上")):
        cells[(a, b)] = []
    for t in trades:
        a, b = lab(t.get(k1)), lab(t.get(k2))
        if a is None or b is None:
            continue
        cells[(a, b)].append(t)
    out = []
    for (a, b), rows in cells.items():
        tag = {("下", "下"): "全程下方（买错候选）", ("下", "上"): "下方买入·期内收复",
               ("上", "下"): "上方买入·期内跌破（卖晚候选）", ("上", "上"): "全程上方"}[(a, b)]
        out.append({"from": a, "to": b, "tag": tag, **agg(rows)})
    return out


def crosstab(trades, kma, ka):
    """MA × A 判据交叉表（2×3），检验两口径是否在讲同一件事。"""
    out = []
    for mv, mlab in ((0, "MA60 下方"), (1, "MA60 上方")):
        for av, alab in A_BUCKETS:
            rows = [t for t in trades
                    if t.get(kma) is not None and t.get(ka) == av
                    and ((t[kma] < 0) if mv == 0 else (t[kma] >= 0))]
            out.append({"ma": mlab, "a": alab, **agg(rows)})
    return out


def jaccard(trades, pa, pb):
    """两个「看空」集合的 Jaccard 与双向漏判率。"""
    sa = {id(t) for t in trades if pa(t)}
    sb = {id(t) for t in trades if pb(t)}
    inter = len(sa & sb)
    uni = len(sa | sb)
    return {
        "n_a": len(sa), "n_b": len(sb), "inter": inter,
        "jaccard": (inter / uni if uni else None),
        "only_a": len(sa - sb), "only_b": len(sb - sa),
        "share_a_in_b": (inter / len(sa) if sa else None),
        "share_b_in_a": (inter / len(sb) if sb else None),
    }


# ---------------------------------------------------------------- 校准
def calibrate(trades, funnel, metrics, eq_by_year, feat, dates, fps, data):
    """返回断言列表 [{level,name,expected,actual,ok,note}]。"""
    res = []

    def add(level, name, expected, actual, ok, note=""):
        res.append({"level": level, "name": name, "expected": expected,
                    "actual": actual, "ok": bool(ok), "note": note})

    ys = years_list()
    closed = [t for t in trades if t["pnl"] is not None]

    # ---- HARD
    add("HARD", "年份连续 2005–2018", "14 年连续",
        f"{len(ys)} 年 {ys[0]}–{ys[-1]}", ys == list(range(2005, 2019)), "缺年或乱序")

    bad_n, bad_p, bad_m = [], [], []
    for y in ys:
        yc = [t for t in closed if t["year"] == y]
        if y not in metrics:
            bad_n.append(f"{y}:metrics缺失")
            continue
        days, maxdd, mn, mp = metrics[y]
        if len(yc) != mn:
            bad_n.append(f"{y}:{len(yc)}≠{mn}")
        if abs(sum(t["pnl"] for t in yc) - mp) > 1.0:
            bad_p.append(f"{y}:{sum(t['pnl'] for t in yc):.0f}≠{mp:.0f}")
        if y in eq_by_year and abs(eq_by_year[y]["max"] - maxdd) > 0.01:
            bad_m.append(f"{y}:{eq_by_year[y]['max']:.2f}≠{maxdd:.2f}")
    add("HARD", "每年平仓笔数与 e17_metrics.csv 一致", "全部一致",
        "一致" if not bad_n else "; ".join(bad_n), not bad_n)
    add("HARD", "每年净利与 e17_metrics.csv 差 ≤1.0", "全部一致",
        "一致" if not bad_p else "; ".join(bad_p), not bad_p)
    add("HARD", "逐年账户最深回撤 = metrics 列 5", "全部一致",
        "一致" if not bad_m else "; ".join(bad_m), not bad_m)

    add("HARD", "leg1 建仓数（P0-1 基线对账）", str(BASELINE["leg1"]),
        str(funnel["leg1"]), funnel["leg1"] == BASELINE["leg1"])
    add("HARD", "已平仓数（P0-1 基线对账）", str(BASELINE["closed"]),
        str(len(closed)), len(closed) == BASELINE["closed"])
    sp = sum(t["pnl"] for t in closed)
    add("HARD", "Σrealized_pnl（P0-1 基线对账，非 842,801）",
        f"≈{BASELINE['pnl']:,.1f}", f"{sp:,.1f}",
        abs(sp - BASELINE["pnl"]) <= BASELINE["pnl_tol"],
        "842,801 是 metrics 按年取整之和")

    # 六表合计（L3 未触发时不计）
    sums = {"T1_L1": sum(b["n"] for b in data["t1"]["l1"]),
            "T1_L2": sum(b["n"] for b in data["t1"]["l2"]),
            "T2_L1": sum(b["n"] for b in data["t2"]["l1"]),
            "T2_L2": sum(b["n"] for b in data["t2"]["l2"]),
            "T3": sum(b["n"] for b in data["t3"]),
            "T4": sum(b["n"] for b in data["t4"]),
            "T5": sum(b["n"] for b in data["t5"]),
            "T6": sum(b["n"] for b in data["t6"])}
    # L3 只覆盖 ±4%，不是全样本划分，故不进 sums（由下方专门断言）
    bad_s = [f"{k}:{v}" for k, v in sums.items() if v != len(closed)]
    add("HARD", "各表分桶笔数合计 = 已平仓样本", f"全部 = {len(closed)}",
        "一致" if not bad_s else "; ".join(bad_s), not bad_s, "上限/下限边界漏算或重叠")

    # L3 是局部加密层（只覆盖 ±4%），不参与全样本划分；单独断言其覆盖区间与 L2 一致
    if data["t1"]["l3_enabled"]:
        n_l3 = sum(b["n"] for b in data["t1"]["l3"])
        n_l2win = len(num_rows(closed, "d60_O", -4.0, 4.0))
        add("HARD", "T1 L3（局部加密，非全样本划分）区间笔数 = L2 同区间笔数",
            str(n_l2win), str(n_l3), n_l3 == n_l2win, "±4% 覆盖区间应完全对应")

    # C3@P（落库值）与 C3@O
    stored = {"bear": 0, "range": 0, "bull": 0}
    for t in closed:
        k = {"bear": "bear", "range": "range", "bull": "bull"}.get(t.get("regime"))
        if k:
            stored[k] += 1
    bad = [f"{k}:{stored[k]}≠{BASELINE['regime_stored'][k]}"
           for k in ("bull", "bear", "range") if stored[k] != BASELINE["regime_stored"][k]]
    add("HARD", "C3@P 复现 reason_json.regime 分布", "107/56/1165",
        f"{stored['bull']}/{stored['bear']}/{stored['range']}", not bad,
        "升/降/震（plan_date 口径）")

    a_open = {1: [], 0: [], -1: []}
    for t in closed:
        if t["a_O"] in a_open:
            a_open[t["a_O"]].append(t)
    got = {k: len(v) for k, v in a_open.items()}
    exp = BASELINE["regime_a_open"]
    ok = (got[1] == exp["bull"] and got[-1] == exp["bear"] and got[0] == exp["range"])
    add("HARD", "C3@O 复现 M9.0 报告分布", "91/51/1186",
        f"{got[1]}/{got[-1]}/{got[0]}", ok, "升/降/震（opened_date 口径）")
    gp = {k: sum(t["pnl"] for t in v) for k, v in a_open.items()}
    bad = []
    for k, key in ((1, "bull"), (-1, "bear"), (0, "range")):
        if abs(gp[k] - BASELINE["regime_a_pnl"][key]) > 1.0:
            bad.append(f"{key}:{gp[k]:,.0f}≠{BASELINE['regime_a_pnl'][key]:,.0f}")
    add("HARD", "C3@O 三桶净利 = M9.0 报告", "+265,532/−27,292/+604,560",
        f"{gp[1]:+,.0f}/{gp[-1]:+,.0f}/{gp[0]:+,.0f}", not bad, "跨报告对账")

    # MA 分布
    n20u = len(num_rows(closed, "d20_O", 0.0, None))
    n20d = len(num_rows(closed, "d20_O", None, 0.0))
    n60u = len(num_rows(closed, "d60_O", 0.0, None))
    n60d = len(num_rows(closed, "d60_O", None, 0.0))
    add("HARD", "C1@O 复现 801/527（上/下）", "801 / 527",
        f"{n20u} / {n20d}",
        n20u == BASELINE["ma20"]["above"] and n20d == BASELINE["ma20"]["below"])
    add("HARD", "C2@O 复现 723/605（上/下）", "723 / 605",
        f"{n60u} / {n60d}",
        n60u == BASELINE["ma60"]["above"] and n60d == BASELINE["ma60"]["below"])

    # 标签缺失：建仓日三标签 + a_P 必须全覆盖；平仓日三标签只在已平仓交易上要求
    # （未平仓的 77 笔没有 closed_date，本就该缺，不属缺陷）
    bad = []
    for k in ("d20_O", "d60_O", "a_O", "a_P"):
        c = sum(1 for t in closed if t.get(k) is None)
        if c:
            bad.append(f"{k}:{c}")
    for k in ("d20_C", "d60_C", "a_C"):
        c = sum(1 for t in closed if t.get(k) is None)
        if c:
            bad.append(f"{k}:{c}")
    add("HARD", "regime 标签缺失 = 0（建仓日/信号日全覆盖 + 平仓日按已平仓）",
        "全部 0", "0" if not bad else "; ".join(bad), not bad,
        f"全部交易 {len(trades)} 笔中 {len(trades) - len(closed)} 笔未平仓，无平仓日标签属正常")

    # 指数指纹一致性
    same = len({(f["n"], f["min"], f["max"], f["sum_close"]) for f in fps}) == 1
    add("HARD", "14 库指数序列指纹一致", "全部一致",
        f"{len({(f['n'], f['min'], f['max'], f['sum_close']) for f in fps})} 种",
        same, f"{INDEX_CODE} n/min/max/Σclose")

    # 未来数据隔离自检：抽 24 日，用 date<=T 截断重算
    idx = {d: i for i, d in enumerate(dates)}
    sample_days = [t["O"] for t in closed if t["O"] in idx][::max(1, len(closed) // 24)][:24]
    bad = []
    for d in sample_days:
        i = idx[d]
        f = feat[d]
        if i >= 59:
            ma60 = sum(feat[x]["close"] for x in dates[i - 59:i + 1]) / 60
            if abs(ma60 - f["ma60"]) > 1e-9:
                bad.append(f"{d}:ma60")
            cur = min(feat[x]["low"] for x in dates[i - 59:i + 1])
            prev = min(feat[x]["low"] for x in dates[i - 60:i])
            a = 1 if cur > prev * A_PARAMS["up_bias"] else (
                -1 if cur < prev * A_PARAMS["down_bias"] else 0)
            if a != f["a"]:
                bad.append(f"{d}:a")
    add("HARD", "未来数据隔离自检（24 日截断重算）", "全等",
        "全等" if not bad else "; ".join(bad[:5]), not bad,
        "库内日线到 2026，必须只用 date<=T")

    # 账户回撤空桶
    days_lt5 = sum(v["lt5"] for v in eq_by_year.values())
    days_5to10 = sum(v["5to10"] for v in eq_by_year.values())
    days_gt10 = sum(v["gt10"] for v in eq_by_year.values())
    add("HARD", "账户回撤 >10% 档天数 = 0（结构性空桶）", "0", str(days_gt10),
        days_gt10 == 0, "逐年重置，14 年最深约 −9.08%")
    add("HARD", "账户回撤 5–10% 档天数 > 0", ">0", str(days_5to10), days_5to10 > 0,
        "若为 0 说明回撤口径实现有误")
    tot_days = days_lt5 + days_5to10 + days_gt10
    mdays = sum(v[0] for v in metrics.values()) if metrics else 0
    add("HARD", "回撤三档天数和 = 样本交易日 = Σmetrics.days",
        f"{BASELINE['trading_days']}", f"{tot_days} / {mdays}",
        tot_days == mdays == BASELINE["trading_days"], "日历对齐")
    add("HARD", "指数回撤两口径分桶合计 = 已平仓样本",
        f"{len(closed)} / {len(closed)}",
        f"{sum(b['n'] for b in data['dd']['idx'])} / "
        f"{sum(b['n'] for b in data['dd']['idx250'])}",
        sum(b["n"] for b in data["dd"]["idx"]) == len(closed)
        and sum(b["n"] for b in data["dd"]["idx250"]) == len(closed))

    add("HARD", "reason_json.regime 值域 ⊆ {bull,bear,range} 且缺失 0",
        "值域合法 + 缺失 0",
        f"缺失 {funnel['miss_regime']}",
        funnel["miss_regime"] == 0)

    # ---- WARN
    seg = data["meta"]["segments"]
    add("WARN", "regime 段数快照（A/MA20/MA60）", "494 / 303 / 147",
        f"{seg['a']} / {seg['ma20']} / {seg['ma60']}",
        seg["a"] == 494 and seg["ma20"] == 303 and seg["ma60"] == 147,
        "采样日历或指数序列变动则漂移")

    agree = data["t7"]["agree_rate"]
    add("WARN", "C3@P Python 重算与落库值逐笔一致率", "≥98%",
        f"{agree * 100:.2f}%", agree >= 0.98, "未达说明 Go/Python 实现或参数漂移")

    mx = min(v["max"] for v in eq_by_year.values()) if eq_by_year else None
    add("WARN", "账户回撤最深 = −9.08%", f"≈{BASELINE['acct_dd_max']}",
        f"{mx:.2f}" if mx is not None else "NA",
        mx is not None and abs(mx - BASELINE["acct_dd_max"]) <= 0.05)

    oall = agg(closed)
    add("WARN", "整体 PF ≈ 1.68（1328 笔）", f"≈{BASELINE['overall_pf']}",
        f"{oall['pf']:.2f}" if oall["pf"] else "—",
        oall["pf"] is not None and abs(oall["pf"] - BASELINE["overall_pf"]) <= 0.02)

    gap = data["t7"]["bull_gap"]
    add("WARN", "C3@P 与 C3@O 的升势笔数差（1 日错位规模）", "基线 16 笔（容许 6–26）",
        f"{gap} 笔", 6 <= gap <= 26,
        "基线锁定 2026-10-26；T 日判定 + T+1 买入的必然错位")

    # ---- INFO
    add("INFO", "各表 Σbucket.pnl = 总净利", f"{sp:,.1f}",
        f"{sum(b['pnl'] for b in data['t1']['l1']):,.1f}",
        abs(sum(b["pnl"] for b in data["t1"]["l1"]) - sp) <= 1.0)
    add("INFO", "指数回撤峰值起点敏感性（1990 起 vs 2005 起）",
        "两版并存", data["dd"]["sens_note"], True)
    add("INFO", "2014 年 plan/opened 一致率（时点错位证据）", "100/100 vs 86/100",
        data["t7"]["y2014_note"], True)
    add("INFO", "生成耗时 / 生成器 sha256 前 16 位", "记录",
        f"{data['meta']['elapsed']}s / {data['meta']['generator_sha256'][:16]}", True)
    return res


# ---------------------------------------------------------------- 组装
def build():
    t0 = time.time()
    trades, funnel = load_trades()
    metrics = load_metrics()
    dates, closes, lows, fps = load_index_series()
    eq_by_date, eq_by_year = load_equity()
    feat = build_features(dates, closes, lows)

    label_miss = attach_labels(trades, dates, feat, eq_by_date)
    closed = [t for t in trades if t["pnl"] is not None]
    years = sorted({t["year"] for t in closed})

    # 段切分（三种口径）
    ep = {}
    runs = {}
    for name, key in (("ma20", "d20"), ("ma60", "d60"), ("a", "a")):
        if name == "a":
            runs[name], ep[name] = build_episodes(dates, feat, "a")
        else:
            # MA 用二元上下方切段
            binary = {d: (1 if (feat[d][key] is not None and feat[d][key] >= 0) else 0)
                      for d in dates}
            fk = "__%s_bin" % name
            for d in dates:
                feat[d][fk] = binary[d]
            runs[name], ep[name] = build_episodes(dates, feat, fk)

    # ---- 六表
    t1 = {
        "l1": ma_stat(closed, "d60_O", MA_L1, ep["ma60"], dates, "O"),
        "l2": ma_stat(closed, "d60_O", MA_L2, ep["ma60"], dates, "O"),
        "l3": ma_stat(closed, "d60_O", MA_L3, ep["ma60"], dates, "O"),
    }
    t2 = {
        "l1": ma_stat(closed, "d20_O", MA_L1, ep["ma20"], dates, "O"),
        "l2": ma_stat(closed, "d20_O", MA_L2, ep["ma20"], dates, "O"),
    }
    t3 = a_stat(closed, "a_O", ep["a"], dates, "O")
    t4 = ma_stat(closed, "d60_C", MA_L1, ep["ma60"], dates, "C")
    t5 = ma_stat(closed, "d20_C", MA_L1, ep["ma20"], dates, "C")
    t6 = a_stat(closed, "a_C", ep["a"], dates, "C")

    # ---- L3 触发判定（框架 §5.2，规则事先写死）
    def band_by(rows, lo, hi):
        return next((b for b in rows if b["lo"] == lo and b["hi"] == hi), None)
    b_neg2_0 = band_by(t1["l2"], -2.0, 0.0)
    b_0_2 = band_by(t1["l2"], 0.0, 2.0)
    l3_trig = False
    if b_neg2_0 and b_0_2 and b_neg2_0["n"] >= 30 and b_0_2["n"] >= 30:
        pu_a, pu_b = b_neg2_0["pu"], b_0_2["pu"]
        c_a, c_b = b_neg2_0["conc"], b_0_2["conc"]
        sign_flip = pu_a is not None and pu_b is not None and (pu_a > 0) != (pu_b > 0)
        conc_gap = (c_a is not None and c_b is not None and abs(c_a - c_b) > 30)
        l3_trig = bool(sign_flip or conc_gap)
    t1["l3_enabled"] = l3_trig
    if not l3_trig:
        t1["l3"] = []

    # ---- 逐桶裁定
    def fill(rows, pred_of):
        for b in rows:
            b["cons"] = consistency(closed, pred_of(b), years)
            b["ratio_f"] = b["cons"]["ratio_f"]
            b["Mf"] = b["cons"]["Mf"]
            b["verdict"] = verdict(b, is_empty=(b["n"] == 0))

    fill(t1["l1"], lambda b: (lambda t: t["d60_O"] is not None and t["d60_O"] < 0.0)
         if b["hi"] == 0.0 else (lambda t: t["d60_O"] is not None and t["d60_O"] >= 0.0))
    fill(t1["l2"], lambda b: (lambda t: (t["d60_O"] is not None
                                         and (b["lo"] is None or t["d60_O"] >= b["lo"])
                                         and (b["hi"] is None or t["d60_O"] < b["hi"]))))
    fill(t1["l3"], lambda b: (lambda t: (t["d60_O"] is not None
                                         and (b["lo"] is None or t["d60_O"] >= b["lo"])
                                         and (b["hi"] is None or t["d60_O"] < b["hi"]))))
    fill(t2["l1"], lambda b: (lambda t: t["d20_O"] is not None and t["d20_O"] < 0.0)
         if b["hi"] == 0.0 else (lambda t: t["d20_O"] is not None and t["d20_O"] >= 0.0))
    fill(t2["l2"], lambda b: (lambda t: (t["d20_O"] is not None
                                         and (b["lo"] is None or t["d20_O"] >= b["lo"])
                                         and (b["hi"] is None or t["d20_O"] < b["hi"]))))
    fill(t3, lambda b: (lambda t: t["a_O"] == b["val"]))
    fill(t4, lambda b: (lambda t: t["d60_C"] is not None and t["d60_C"] < 0.0)
         if b["hi"] == 0.0 else (lambda t: t["d60_C"] is not None and t["d60_C"] >= 0.0))
    fill(t5, lambda b: (lambda t: t["d20_C"] is not None and t["d20_C"] < 0.0)
         if b["hi"] == 0.0 else (lambda t: t["d20_C"] is not None and t["d20_C"] >= 0.0))
    fill(t6, lambda b: (lambda t: t["a_C"] == b["val"]))

    # ---- T7 审计轨：落库 regime vs Python 重算 C3@P
    reg2v = {"bull": 1, "bear": -1, "range": 0}
    stored_rows, recomp_rows = [], []
    for v, label in A_BUCKETS:
        s_rows = [t for t in closed if reg2v.get(t.get("regime")) == v]
        r_rows = [t for t in closed if t["a_P"] == v]
        stored_rows.append({"label": label, "val": v, **agg(s_rows)})
        recomp_rows.append({"label": label, "val": v, **agg(r_rows)})
    same = sum(1 for t in closed
               if t["a_P"] is not None and reg2v.get(t.get("regime")) == t["a_P"])
    agree_rate = same / len(closed) if closed else 0.0
    per_year_agree = []
    for y in years:
        yc = [t for t in closed if t["year"] == y]
        s = sum(1 for t in yc if t["a_P"] is not None and reg2v.get(t.get("regime")) == t["a_P"])
        per_year_agree.append({"year": y, "same": s, "n": len(yc),
                               "rate": (s / len(yc) if yc else None)})
    # 真正的「1 日错位」证据：plan_date 标签 vs opened_date 标签的逐年一致率
    plan_open = []
    for y in years:
        yc = [t for t in closed if t["year"] == y]
        s = sum(1 for t in yc if t["a_P"] is not None and t["a_O"] is not None
                and t["a_P"] == t["a_O"])
        plan_open.append({"year": y, "same": s, "n": len(yc),
                          "rate": (s / len(yc) if yc else None)})

    # ---- 诊断
    trans = transition(closed, "d60_O", "d60_C")
    cross = crosstab(closed, "d60_O", "a_O")
    def bear_ma20(t):
        return t["d20_O"] is not None and t["d20_O"] < 0
    def bear_ma60(t):
        return t["d60_O"] is not None and t["d60_O"] < 0
    def bear_a(t):
        return t["a_O"] == -1
    ov = {
        "ma20_ma60": jaccard(closed, bear_ma20, bear_ma60),
        "ma60_a": jaccard(closed, bear_ma60, bear_a),
        "ma20_a": jaccard(closed, bear_ma20, bear_a),
    }

    # ---- 回撤表
    dd_idx = dd_stat(closed, "dd_idx", IDX_DD)
    dd_idx250 = dd_stat(closed, "dd250", IDX_DD250)
    dd_acct = dd_stat(closed, "dd_acct", ACCT_DD)
    for b in dd_acct:
        b["verdict"] = ("不可检验（结构性无样本）" if b["n"] == 0 else "仅描述性（内生变量）")
    acct_yearly = [{"year": y, **v} for y, v in sorted(eq_by_year.items())]
    # 1990 起峰值敏感性
    sens = []
    for lo, hi, label in IDX_DD:
        rows = [t for t in closed
                if t["O"] in feat and feat[t["O"]]["dd1990"] is not None
                and ((lo is None or abs(feat[t["O"]]["dd1990"]) >= lo)
                     and (hi is None or abs(feat[t["O"]]["dd1990"]) < hi))]
        sens.append({"label": label, "n": len(rows)})

    # ---- 段级总表
    seg_total = {k: len(v) for k, v in runs.items()}
    seg_rows = []
    for name, label in (("a", "C3 A判据"), ("ma20", "C1 MA20"), ("ma60", "C2 MA60")):
        bins = {}
        for b in (t1["l1"] if name == "ma60" else t2["l1"] if name == "ma20" else t3):
            bins[b["label"]] = {"n": b["n"], "m": b.get("m", 0),
                                "pu": b["pu"], "ratio_seg": b.get("ratio_seg")}
        seg_rows.append({"criterion": label, "total": seg_total[name], "buckets": bins})

    # ---- 假设裁定
    hyps = []
    def addh(code, claim, ok, value, note):
        hyps.append({"code": code, "claim": claim,
                     "result": ("成立" if ok else "不成立"), "value": value, "note": note})

    bear3 = next((b for b in t3 if b["val"] == -1), None)
    if bear3:
        ok1 = (bear3["pu"] is not None and bear3["pu"] < 0
               and bear3["pnl_ex_top3"] < 0 and bear3["Mf"] >= 8)
        c3 = bear3["cons"]
        addh("H1", "「降势有害」在 14 年全样本上成立", ok1,
             f"每单位 {fsign(bear3['pu'])}、剔除前三 {fsign(bear3['pnl_ex_top3'])}、"
             f"出现年数 {bear3['Mf']}、跨年正年 {c3['Pf']}/{c3['Mf']}"
             f"——裁定矩阵要求 ≤30% 才算「跨年一致为负」，本桶 {c3['ratio_f'] * 100:.0f}% "
             f"故标签为「不稳定」；两者门槛不同，不矛盾",
             "判据：pu<0 且剔除前三<0 且 Mf≥8")

    below1 = next((b for b in t1["l1"] if b["hi"] == 0.0), None)
    if below1:
        c = below1["cons"]
        ok2 = (below1["n"] >= 30 and below1.get("m", 0) >= 20
               and below1["pu"] is not None and below1["pu"] < 0
               and below1["pnl_ex_top3"] < 0
               and c["ratio_f"] is not None and c["ratio_f"] <= 0.30
               and below1.get("ratio_seg") is not None and below1["ratio_seg"] <= 0.30
               and below1["gap"] is not None and below1["gap"] <= -2)
        addh("H2", "「MA60 下方」是负 edge（不只是方向为负）", ok2,
             f"n={below1['n']} m={below1.get('m', 0)} 每单位 {fsign(below1['pu'])} "
             f"剔除前三 {fsign(below1['pnl_ex_top3'])} 跨年 {c['Pf']}/{c['Mf']} "
             f"段级 {below1.get('P_seg')}/{below1.get('M_seg')} 缺口 {fpp(below1['gap'])}",
             "判据：n≥30 且 m≥20 且 pu<0 且剔除前三<0 且笔级/段级 ratio≤0.30 且缺口≤−2pp")

    above1 = next((b for b in t1["l1"] if b["lo"] == 0.0), None)
    if above1:
        c = above1["cons"]
        pu, pue = above1["pu"], above1["pu_ex_top3"]
        cond = []
        if above1.get("conc") is not None and above1["conc"] > 50:
            cond.append(f"集中度 {above1['conc']:.1f}%>50%")
        if pu and pue is not None and pue / pu < 0.5:
            cond.append(f"剔除前三后仅剩 {pue / pu * 100:.0f}%")
        if c["ratio_f"] is not None and c["ratio_f"] < 0.70:
            cond.append(f"跨年仅 {c['Pf']}/{c['Mf']}")
        addh("H3", "「MA60 上方」是利润主体而非 edge", bool(cond),
             f"每单位 {fsign(pu)}" + ("；" + "；".join(cond) if cond else "；三项均不成立"),
             "判据：pu>0 且（集中度>50% 或 剔除后<50% 或 跨年<70%）")

    # H4
    def bear_pu(rows, key):
        b = next((x for x in rows if (x.get("hi") == 0.0 or x.get("val") == -1)), None)
        return b["pu"] if b else None
    p1 = bear_pu(t2["l1"], "d20")
    p2 = bear_pu(t1["l1"], "d60")
    p3 = bear_pu(t3, "a")
    same_sign = all(x is not None and x < 0 for x in (p1, p2, p3))
    jac_ok = all(v["jaccard"] is not None and v["jaccard"] >= 0.50 for v in ov.values())
    cross_ok = True
    mab = [b for b in cross if b["ma"] == "MA60 下方"]
    if len(mab) >= 2:
        vals = [b["pu"] for b in mab if b["pu"] is not None]
        cross_ok = not vals or all((v > 0) == (vals[0] > 0) for v in vals)
    addh("H4", "三口径在方向上是同一件事（口径稳健）", bool(same_sign and jac_ok and cross_ok),
         f"看空桶每单位 {fsign(p1)}/{fsign(p2)}/{fsign(p3)}；Jaccard "
         + "/".join(f"{v['jaccard']:.2f}" if v["jaccard"] is not None else "—" for v in ov.values()),
         "判据：三口径看空桶同号 且 两两 Jaccard≥0.50 且 交叉表同号")

    # H5
    ud = next((x for x in trans if x["from"] == "上" and x["to"] == "下"), None)
    dd_ = next((x for x in trans if x["from"] == "下" and x["to"] == "下"), None)
    if ud and dd_:
        ok5 = (ud["pu"] is not None and ud["pu"] < 0 and ud["n"] >= 30
               and abs(ud["pnl"]) > abs(dd_["pnl"]))
        addh("H5", "「卖晚」比「买错」更贵", ok5,
             f"上方→下方 n={ud['n']} 净利 {fsign(ud['pnl'])}；下方→下方 n={dd_['n']} "
             f"净利 {fsign(dd_['pnl'])}",
             "判据：上→下 pu<0 且 n≥30 且 |pnl| 大于 下→下")

    # H6
    acct_gt10 = next((b for b in dd_acct if b["lo"] == 10.0), None)
    deep = [b for b in dd_idx250 if (b["lo"] or 0) >= 20.0]
    deep_n = sum(b["n"] for b in deep)
    deep_pnl = sum(b["pnl"] for b in deep)
    deep_units = sum(b["units"] for b in deep)
    deep_pu = (deep_pnl / deep_units) if deep_units else None
    ok6 = (acct_gt10 is not None and acct_gt10["n"] == 0 and deep_n >= 30
           and deep_pu is not None and deep_pu < 0)
    addh("H6", "「按账户回撤停手」不可检验；「按指数深度回撤降仓」可检验", ok6,
         f"账户>10% 桶 n={acct_gt10['n'] if acct_gt10 else 'NA'}；"
         f"距 52 周高点回撤≥20% 桶 n={deep_n} 每单位 {fsign(deep_pu)}",
         "判据：账户>10% n=0 且 距52周高点回撤≥20% n≥30 且 pu<0")

    # ---- meta
    seg_total = {k: len(v) for k, v in runs.items()}
    data = {
        "meta": {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "years": years, "funnel": funnel,
            "total_closed": len(closed), "total_pnl": sp_of(closed),
            "overall": agg(closed),
            "segments": seg_total,
            "label_miss": label_miss,
            "sample": {"start": SAMPLE_START, "end": SAMPLE_END,
                       "trading_days": sum(v["days"] for v in eq_by_year.values())},
            "index_meta": {"code": INDEX_CODE, "n": len(dates),
                           "min": dates[0], "max": dates[-1],
                           "fingerprints": fps},
            "a_params": A_PARAMS, "ma_windows": list(MA_WINDOWS),
            "layers": {"L1": True, "L2": True, "L3": t1["l3_enabled"]},
            "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "db_mtimes": {y: datetime.fromtimestamp(
                (BT / f"bt_e17_{y}.db").stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                for y in years if (BT / f"bt_e17_{y}.db").exists()},
            "elapsed": round(time.time() - t0, 2),
        },
        "t1": t1, "t2": t2, "t3": t3, "t4": t4, "t5": t5, "t6": t6,
        "t7": {"stored": stored_rows, "recomp": recomp_rows,
               "agree_rate": agree_rate, "per_year": per_year_agree,
               "plan_open": plan_open,
               "bull_gap": stored_rows[2]["n"] - next(
                   (b["n"] for b in t3 if b["val"] == 1), 0),
               "y2014_note": next((f"{p['year']}: {p['same']}/{p['n']}"
                                   for p in plan_open if p["year"] == 2014), "—")},
        "overlap": ov, "transition": trans, "crosstab": cross,
        "dd": {"idx": dd_idx, "idx250": dd_idx250,
               "acct": dd_acct, "acct_yearly": acct_yearly,
               "sens_1990": sens,
               "sens_note": " / ".join(f"{x['label']}:{x['n']}" for x in sens)},
        "seg_table": seg_rows,
        "year_matrix": {},
        "hypotheses": hyps,
    }
    # 逐年矩阵（T1 二元 + T3 三态）
    for y in years:
        yc = [t for t in closed if t["year"] == y]
        data["year_matrix"][str(y)] = {
            "t1_l1": {
                "下": agg([t for t in yc if t["d60_O"] is not None and t["d60_O"] < 0]),
                "上": agg([t for t in yc if t["d60_O"] is not None and t["d60_O"] >= 0])},
            "t3": {lab: agg([t for t in yc if t["a_O"] == v]) for v, lab in A_BUCKETS}}

    data["calibration"] = calibrate(trades, funnel, metrics, eq_by_year,
                                   feat, dates, fps, data)
    # 「多数 regime 段为负」的规模：用于核心结论（中位数 vs 均值不作裁定依据，只作描述）
    probe = t2["l1"] + t1["l1"] + t1["l2"] + t3 + t4
    neg = [b for b in probe if b.get("pu_seg_med") is not None and b["pu_seg_med"] < 0
           and b["n"] >= 30]
    data["meta"]["seg_median"] = {"neg": len(neg), "total": len(
        [b for b in probe if b.get("pu_seg_med") is not None and b["n"] >= 30])}
    return data


def sp_of(rows):
    return sum(t["pnl"] for t in rows if t["pnl"] is not None)


# ---------------------------------------------------------------- 渲染
CSS = """
:root{--bg:#f5f6f8;--surface:#fff;--ink:#1e2430;--muted:#697386;--line:#e3e6eb;
--soft:#f0f2f5;--red:#c0392b;--red2:#faece9;--green:#1b7a4b;--green2:#e9f5ef;
--gold:#a66a12;--gold2:#fbf3e4;--blue:#2d5b8e;--blue2:#ecf2f9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif;font-size:15px;line-height:1.7}
.page{max-width:1240px;margin:0 auto;padding:42px 28px 64px}
header{border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:26px}
.eyebrow{font-size:12px;letter-spacing:.16em;color:var(--blue);font-weight:700;margin-bottom:10px}
h1{font-size:32px;line-height:1.28;margin:0 0 10px;letter-spacing:-.02em}
h2{font-size:23px;margin:38px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:17px;margin:22px 0 9px}
h4{font-size:15px;margin:16px 0 7px;color:#3c4553}
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
.summary ul{margin:8px 0 0 19px;padding:0}.summary li{margin:7px 0}
.callout{border-radius:8px;padding:14px 17px;margin:15px 0;font-size:14px}
.callout.gold{background:var(--gold2);border:1px solid #ead9b8}
.callout.red{background:var(--red2);border:1px solid #efd0ca}
.callout.blue{background:var(--blue2);border:1px solid #d4e0ee}
.table-wrap{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow-x:auto;margin:13px 0}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:980px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
th{background:#fafbfc;color:#4b5565;font-size:12px;font-weight:600}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fcfcfd}
tr.empty td{background:#f4f5f7;color:#9aa1ad;font-style:italic}
tr.sum td{background:#fafbfc;font-weight:700;border-top:2px solid #cfd4dc}
.vd{font-size:11.5px;padding:1px 7px;border-radius:999px;white-space:nowrap}
.vd-edge{background:var(--red2);color:var(--red);font-weight:700}
.vd-fake{background:var(--gold2);color:var(--gold);font-weight:700}
.vd-neg{background:var(--green2);color:var(--green);font-weight:700}
.vd-unstable{background:var(--soft);color:#5a6472}
.vd-small{background:#f1f3f6;color:#8b93a0}
.nm{font-size:11.5px;color:var(--muted)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:17px 19px;margin:14px 0}
.matrix td{font-size:12px}
.toc{display:flex;gap:9px;flex-wrap:wrap;margin:18px 0}
.toc a{text-decoration:none;color:var(--blue);background:var(--blue2);border:1px solid #d4e0ee;border-radius:999px;padding:3px 12px;font-size:12.5px}
.nav6{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}
.nav6 a{display:block;text-decoration:none;color:inherit;background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:8px;padding:12px 14px}
.nav6 a.main{border-left-color:var(--red)}
.nav6 .t{font-size:12px;color:var(--muted)}
.nav6 .n{font-weight:700;font-size:14.5px;margin:2px 0}
.nav6 .d{font-size:12.5px;color:var(--muted)}
.foot{margin-top:38px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
.foot p{margin:5px 0}
code{background:var(--soft);border-radius:3px;padding:1px 5px;font-size:12.5px}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}.nav6{grid-template-columns:1fr}.page{padding:28px 16px 48px}h1{font-size:26px}}
@media print{body{background:#fff;font-size:11px}.page{max-width:none;padding:0}
h2{break-after:avoid}.table-wrap,.panel{break-inside:avoid}}
"""


def vd_html(v):
    if v == "edge":
        k = "vd-edge"
    elif v.startswith("假 edge"):
        k = "vd-fake"
    elif v.startswith(("负 edge", "利润主体")):
        k = "vd-neg"
    elif v.startswith(("不稳定", "稳但", "跨年偏负", "笔级")):
        k = "vd-unstable"
    else:
        k = "vd-small"
    return f'<span class="vd {k}">{v}</span>'


def hyp_html(v):
    if v == "成立":
        return '<span class="vd vd-edge">成立</span>'
    if v == "不成立":
        return '<span class="vd vd-neg">不成立</span>'
    return f'<span class="vd vd-fake">{v}</span>'


def table_head(first="分带", with_m=True, with_seg=True):
    cols = [first]
    if with_m:
        cols += ["n / m"]
    cols += ["胜率", "每单位净利", "保本胜率", "缺口", "PF", "集中度",
             "前三赢家", "剔除前三净利"]
    if with_seg:
        cols += ["段级 P/M"]
    cols += ["净利", "裁定"]
    return "<thead><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead><tbody>"


def row_html(b, with_m=True, with_seg=True, forced_verdict=None):
    zero = b["n"] == 0
    v = forced_verdict if forced_verdict is not None else b.get("verdict", "")
    cells = [b["label"]]
    if with_m:
        cells.append(f'<span class="nm">{b["n"]} / {b.get("m", 0)}</span>'
                     if not zero else '<span class="nm">0 / 0</span>')
    cells += [
        fpct(b["wr"] * 100 if b["wr"] is not None else None),
        f'<b class="{cls(b["pu"])}">{fsign(b["pu"])}</b>',
        fpct(b["bewr"] * 100 if b["bewr"] is not None else None),
        fpp(b["gap"]),
        fnum(b["pf"], 2),
        fpct(b["conc"]),
        fsign(b["top3"]),
        f'<span class="{cls(b["pnl_ex_top3"])}">{fsign(b["pnl_ex_top3"])}</span>',
    ]
    if with_seg:
        cells.append(f'<span class="nm">{b.get("P_seg", "—")}/{b.get("M_seg", "—")}</span>')
    cells += [f'<span class="{cls(b["pnl"])}">{fsign(b["pnl"])}</span>',
              vd_html(v) if v else ""]
    trc = ' class="empty"' if zero else ""
    return f"<tr{trc}>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def table(rows, first="分带", with_m=True, with_seg=True, foot=None):
    parts = ['<div class="table-wrap"><table>', table_head(first, with_m, with_seg)]
    for b in rows:
        parts.append(row_html(b, with_m, with_seg))
    parts.append("</tbody></table></div>")
    if foot:
        parts.append(f'<p class="small muted">{foot}</p>')
    return "".join(parts)


def matrix_html(data):
    years = data["meta"]["years"]
    head = ("<thead><tr><th>年份</th><th>下方 n</th><th>下方每单位</th>"
            "<th>上方 n</th><th>上方每单位</th><th>升势 n</th><th>降势 n</th>"
            "<th>震荡 n</th><th>全年净利</th></tr></thead><tbody>")
    parts = ['<div class="table-wrap"><table class="matrix">', head]
    for y in years:
        r = data["year_matrix"][str(y)]
        d, u = r["t1_l1"]["下"], r["t1_l1"]["上"]
        t3 = r["t3"]
        tot = d["pnl"] + u["pnl"]
        parts.append("<tr>"
                     f"<td>{y}</td>"
                     f'<td class="nm">{d["n"]}</td><td class="{cls(d["pu"])}">{fsign(d["pu"])}</td>'
                     f'<td class="nm">{u["n"]}</td><td class="{cls(u["pu"])}">{fsign(u["pu"])}</td>'
                     f'<td class="nm">{t3["升势（牛）"]["n"]}</td>'
                     f'<td class="nm">{t3["降势（熊）"]["n"]}</td>'
                     f'<td class="nm">{t3["震荡（猴）"]["n"]}</td>'
                     f'<td class="{cls(tot)}">{fsign(tot)}</td>'
                     "</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def scorecard_html(rows, title):
    head = ("<thead><tr><th>分带</th><th>n / m</th><th>M(有交易年)</th><th>P 正年</th>"
            "<th>P′/M′(≥3笔)</th><th>最长连续</th><th>段级 P/M</th><th>裁定</th></tr></thead><tbody>")
    parts = [f'<h4>{title}</h4><div class="table-wrap"><table class="matrix">', head]
    for b in rows:
        c = b.get("cons")
        if not c:
            continue
        parts.append("<tr>"
                     f'<td>{b["label"]}</td>'
                     f'<td class="nm">{b["n"]} / {b.get("m", 0)}</td>'
                     f'<td class="nm">{c["M"]}</td><td class="nm">{c["P"]}</td>'
                     f'<td class="nm">{c["Pf"]}/{c["Mf"]}'
                     f'{" (" + format(c["ratio_f"] * 100, ".0f") + "%)" if c["ratio_f"] is not None else ""}</td>'
                     f'<td class="nm">{c["longest_run"]}</td>'
                     f'<td class="nm">{b.get("P_seg", "—")}/{b.get("M_seg", "—")}</td>'
                     f'<td>{vd_html(b.get("verdict", ""))}</td>'
                     "</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def cal_html(cal):
    head = "<thead><tr><th>层</th><th>断言</th><th>期望</th><th>实测</th><th>结果</th></tr></thead><tbody>"
    parts = ['<div class="table-wrap"><table class="matrix">', head]
    for c in cal:
        flag = ('<span class="vd vd-edge">通过</span>' if c["ok"]
                else ('<span class="vd vd-neg">失败</span>' if c["level"] == "HARD"
                      else '<span class="vd vd-fake">WARN</span>'))
        parts.append(f'<tr><td class="nm">{c["level"]}</td><td>{c["name"]}</td>'
                     f'<td class="nm">{c["expected"]}</td><td class="nm">{c["actual"]}</td>'
                     f"<td>{flag}</td></tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def render_html(data):
    m = data["meta"]
    ov = data["overall"] if "overall" in data else m["overall"]
    t1l1 = data["t1"]["l1"]
    below = next((b for b in t1l1 if b["hi"] == 0.0), None)
    above = next((b for b in t1l1 if b["lo"] == 0.0), None)
    acct_gt10 = next((b for b in data["dd"]["acct"] if b["lo"] == 10.0), None)
    nh = sum(1 for c in data["calibration"] if c["level"] == "HARD" and c["ok"])
    th = sum(1 for c in data["calibration"] if c["level"] == "HARD")
    nw = sum(1 for c in data["calibration"] if c["level"] == "WARN" and not c["ok"])

    H = []
    H.append(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>P0-3 · 大盘 regime 归因报告（第 10 维）</title><style>{CSS}</style></head><body><div class="page">""")
    H.append(f"""<header>
<div class="eyebrow">P0-3 ｜ 十维 #10 固定单位头寸管理 ｜ A 类归因 · 零回测</div>
<h1>大盘 regime 归因：1328 笔平仓交易按市况分桶</h1>
<p class="sub">2005–2018 共 14 年 · 三口径（MA20 / MA60 / A 判据）× 两时点（建仓日 / 平仓日）
· 每桶同时报笔数 n 与 regime 段数 m · 四项固定口径 + PF + 缺口 + 剔除前三赢家
· 生成于 {m['generated']} · 生成器 sha256 <code>{m['generator_sha256'][:16]}</code></p>
</header>""")

    # KPI
    H.append('<div class="kpis">')
    H.append(f'<div class="kpi"><div class="label">已平仓样本</div>'
             f'<div class="value">{m["total_closed"]:,}</div>'
             f'<div class="note">建仓 {m["funnel"]["leg1"]:,} 笔，未平仓 {m["funnel"]["leg1"] - m["total_closed"]} 笔剔除</div></div>')
    H.append(f'<div class="kpi"><div class="label">14 年合计净利</div>'
             f'<div class="value {cls(m["total_pnl"])}">{fsign(m["total_pnl"])}</div>'
             f'<div class="note">整体 PF {fnum(ov["pf"], 2)} · 胜率 {fpct(ov["wr"] * 100 if ov["wr"] else None)}</div></div>')
    if below:
        H.append(f'<div class="kpi"><div class="label">MA60 下方 · 每单位净利</div>'
                 f'<div class="value {cls(below["pu"])}">{fsign(below["pu"])}</div>'
                 f'<div class="note">n={below["n"]} / m={below.get("m", 0)} 段'
                 f' · 上方 {fsign(above["pu"]) if above else "—"}</div></div>')
    H.append(f'<div class="kpi"><div class="label">账户回撤 &gt;10% 档</div>'
             f'<div class="value neutral">{acct_gt10["n"] if acct_gt10 else 0} 天</div>'
             f'<div class="note">结构性无样本（逐年重置，最深约 −9.08%）</div></div>')
    H.append("</div>")

    # 一页结论
    concl = []
    if below and above:
        concl.append(
            f'<b>T1</b>　C2（MA60）× 建仓日：下方桶 <b class="{cls(below["pu"])}">每单位 {fsign(below["pu"])}</b>'
            f'（n={below["n"]} / m={below.get("m", 0)} 段，剔除前三 {fsign(below["pnl_ex_top3"])}）'
            f'，上方桶 <b class="{cls(above["pu"])}">{fsign(above["pu"])}</b>'
            f'（n={above["n"]} / m={above.get("m", 0)} 段，剔除前三 {fsign(above["pnl_ex_top3"])}）。'
            f'两桶裁定：{vd_html(below["verdict"])} / {vd_html(above["verdict"])}。')
    # 距 52 周高点回撤：本次最干净的单调梯度
    dd250 = data["dd"]["idx250"]
    if dd250:
        cells = "　→　".join(
            f'{b["label"]} <b class="{cls(b["pu"])}">{fsign(b["pu"])}</b>' for b in dd250)
        first = dd250[0]
        zero = next((b for b in dd250 if b["pu"] is not None and b["pu"] < 0), None)
        concl.append(
            f'<b>§7.2</b>　距 52 周高点回撤给出本次<b>最干净的单调梯度</b>（每单位净利）：{cells}。'
            f'回撤 &lt;5% 时每单位 <b>{fsign(first["pu"])}</b>（n={first["n"]}，剔除前三仍 '
            f'{fsign(first["pnl_ex_top3"])}），到 {zero["label"] if zero else "深档"} 转负。'
            f'3 口径 × 2 时点里，只有这一条「越远离高点越差」是单调成立的。')
    # 三口径不是同一件事
    jac = data["overlap"]
    concl.append(
        f'<b>§2.3 · H4 不成立</b>　三个口径<b>不是在讲同一件事</b>：MA20 下方与 MA60 下方 Jaccard '
        f'{fnum(jac["ma20_ma60"]["jaccard"], 2)}，但 MA60 下方与 A 判据降势只有 '
        f'<b>{fnum(jac["ma60_a"]["jaccard"], 2)}</b>（{jac["ma60_a"]["n_a"]} 笔 vs '
        f'{jac["ma60_a"]["n_b"]} 笔，交集仅 {jac["ma60_a"]["inter"]} 笔）。方向上三者一致（看空桶都为负），'
        f'但覆盖的交易几乎不重叠——「哪个口径对」不能靠常识判断，只能靠 B 类同参数重跑。')
    sm = m.get("seg_median", {})
    if sm.get("total"):
        concl.append(
            f'<b>§8 · 有效样本</b>　MA60 只有 <b>{m["segments"]["ma60"]} 段</b>、A 判据 '
            f'<b>{m["segments"]["a"]} 段</b>、MA20 <b>{m["segments"]["ma20"]} 段</b>——同段内多笔交易'
            f'共享同一大盘状态，不是独立观测。更关键的是：n≥30 的 {sm["total"]} 个桶里，'
            f'有 <b>{sm["neg"]}</b> 个的「中位 regime 段」亏钱。'
            f'<b>不论大盘什么状态，中位数行情段都不赚钱</b>，利润由少数行情段撑起。')
    concl.append(
        '<b>§9 · 边界</b>　本报告只产出待验假设与 B 类验证清单，<b>不产出任何闸门阈值</b>；'
        '<code>docs/23</code> §9 P5 已硬否决「熊市闸门」，本报告不因此复活它。'
        '另有两处已登记的口径缺陷：2005 基准峰回撤退化（单档独占 78% 样本，改用距 52 周高点）、'
        '账户权益回撤 <code>&gt;10%</code> 档 3,402 个交易日里 0 天（「不可检验」）。')
    H.append('<div class="summary"><h2>一页结论</h2><ul>'
             + "".join(f"<li>{c}</li>" for c in concl) + "</ul></div>")

    # 导航
    H.append('<div class="nav6">')
    for tid, name, desc, is_main in (
        ("s3", "T1 · C2 MA60 × 建仓日", f"{len(t1l1) + len(data['t1']['l2'])} 桶（L1+L2）· 主表", True),
        ("s4", "T2/T3 · MA20 与 A判据", "2 + 8 + 3 桶 · 口径对照", False),
        ("s5", "T4/T5/T6 · 平仓日", "2+2+3 桶 · 时点对照", False),
        ("s6", "买卖错位诊断", "2×2 转移 + 2×3 交叉表", False),
        ("s7", "回撤口径", "指数 6 带 + 账户 3 带", False),
        ("s8", "有效样本与跨年", f"段数 {m['segments']['ma60']}/{m['segments']['ma20']}/{m['segments']['a']}", False),
    ):
        cls_ = "main" if is_main else ""
        H.append(f'<a class="{cls_}" href="#{tid}"><div class="t">{tid.upper()}</div>'
                 f'<div class="n">{name}</div><div class="d">{desc}</div></a>')
    H.append("</div>")

    # §1 设定
    H.append('<h2 id="s1">§1 实验设定</h2>')
    H.append(f"""<p>本实验把 14 年 <b>{m['total_closed']:,}</b> 笔已平仓交易（建仓 {m['funnel']['leg1']:,} 笔中的已平仓部分）
按交易时点的大盘状态分桶。数据来自 <code>var/bt/bt_e17_&lt;year&gt;.db</code> × 14，全部
<code>mode=ro</code> 只读；指数序列取自同库 <code>daily_bar</code> 的 <code>{INDEX_CODE}</code>
（{m['index_meta']['n']:,} 行，{m['index_meta']['min']} → {m['index_meta']['max']}）。</p>""")
    H.append("""<h3>1.1 三判据 × 三时点</h3>
<div class="table-wrap"><table class="matrix"><thead><tr><th>代号</th><th>判据 / 时点</th><th>定义</th><th>角色</th></tr></thead><tbody>
<tr><td>C1</td><td>MA20</td><td><code>d20 = (close − MA20)/MA20 × 100</code>；d≥0 上方，d&lt;0 下方</td><td>对照</td></tr>
<tr><td>C2</td><td>MA60</td><td><code>d60 = (close − MA60)/MA60 × 100</code>；同上</td><td><b>主表</b></td></tr>
<tr><td>C3</td><td>A 判据</td><td>60 日低点相邻日比：<code>cur &gt; prev×1.002</code> 升势 / <code>&lt; prev×0.998</code> 降势 / 其余震荡</td><td>对照（与 M9.0 同口径）</td></tr>
<tr><td>P</td><td>plan_date 信号日</td><td><code>trade_plan.plan_date</code></td><td><b>仅审计轨</b>（落库 regime 即此日 C3）</td></tr>
<tr><td>O</td><td>opened_date 建仓日</td><td><code>position.opened_date</code></td><td><b>主</b></td></tr>
<tr><td>C</td><td>closed_date 平仓日</td><td><code>position.closed_date</code></td><td>对照</td></tr>
</tbody></table></div>
<div class="callout gold"><b>为什么 <code>reason_json.regime</code> 不是第四个口径：</b>它是 C3 在 <b>P 时点</b>的落库值。
「T 日判定 → T+1 买入」存在 1 日错位，用它归因等于把「判据在 T 日怎么说」当成「买入时市场什么样」。
本报告把它只用于 §2.2 对账，主表一律用建仓日重算。</div>""")
    H.append(f"""<h3>1.2 样本漏斗</h3>
<div class="table-wrap"><table class="matrix"><thead><tr><th>步</th><th>说明</th><th>剩余</th></tr></thead><tbody>
<tr><td>1</td><td>14 个回测库（2005–2018）</td><td>{m['funnel']['dbs']} 库</td></tr>
<tr><td>2</td><td><code>leg_no = 1</code> 首次建仓（排除加仓腿）</td><td>{m['funnel']['leg1']:,} 笔</td></tr>
<tr><td>3</td><td><code>status='closed'</code> 且 <code>realized_pnl</code> 非空</td><td>{m['total_closed']:,} 笔</td></tr>
<tr><td>4</td><td>regime 标签定位失败</td><td>{sum(1 for k, v in m['label_miss'].items() if k.startswith(('d2', 'd6', 'a_')) and v)} 笔</td></tr>
<tr class="sum"><td>5</td><td><b>有效样本</b></td><td><b>{m['total_closed']:,} 笔</b></td></tr>
</tbody></table></div>
<p class="small muted">唯一剔除动作是第 3 步的 {m['funnel']['leg1'] - m['total_closed']} 笔未平仓头寸：未实现盈亏与已实现盈亏不可比。
不剔除极端盈亏、不剔除单年、不按 <code>sizing_basis</code> 过滤（OFAT 单因子纪律）。</p>""")
    H.append(f"""<h3>1.3 统计口径（每桶齐备）</h3>
<p class="small">胜率 = 盈利笔数/已平仓笔数；每单位净利 = Σpnl/Σunits（{TIER_NOTE}）；
保本胜率 = |avgLoss|/(avgWin+|avgLoss|)；缺口 = 胜率 − 保本胜率；PF = 毛盈利/|毛亏损|；
集中度 = 前三赢家/毛盈利；<b>剔除前三净利</b> = 净利 − 前三赢家合计。
<b>n / m</b>：n 是名义笔数，m 是该桶覆盖的 regime 段数——同一段内的多笔交易共享同一大盘状态，
不是独立观测；m &lt; 20 一律不给裁定。</p>""")

    # §2 口径互检
    H.append('<h2 id="s2">§2 口径互检：三个口径是不是在讲同一件事</h2>')
    H.append("<p>先证明口径之间的一致性，再看后面的分桶表——否则 6 张表各说各话，读者无从判断哪个可信。</p>")
    H.append("<h3>2.1 三口径并排（均按建仓日）</h3>")
    side = []
    for label, rows, key in (("C1 MA20", data["t2"]["l1"], "d20_O"),
                             ("C2 MA60", data["t1"]["l1"], "d60_O")):
        for b in rows:
            side.append({"label": f"{label} · {b['label']}", **{k: v for k, v in b.items()
                                                              if k != "label"}})
    for b in data["t3"]:
        side.append({"label": f"C3 A判据 · {b['label']}", **{k: v for k, v in b.items()
                                                          if k != "label"}})
    H.append(table(side, with_seg=False))
    H.append("""<h3>2.2 审计轨对账：落库 regime vs 重算 C3@P vs C3@O</h3>""")
    H.append(f'<p class="small">逐笔一致率（落库 vs Python 重算，同为 plan_date 口径）= '
             f'<b>{data["t7"]["agree_rate"] * 100:.2f}%</b>。'
             f'2014 年样例：<code>{data["t7"]["y2014_note"]}</code>——'
             f'这一年的 plan 日与 opened 日标签一致率不同，正是「1 日错位」的直接证据。</p>')
    audit = []
    for i, (v, label) in enumerate(A_BUCKETS):
        s, r = data["t7"]["stored"][i], data["t7"]["recomp"][i]
        o = next((b for b in data["t3"] if b["val"] == v), None)
        audit.append({"label": label, "n": s["n"], "m": o.get("m", 0) if o else 0,
                      **{k: s[k] for k in ("wr", "pu", "bewr", "gap", "pf", "conc",
                                           "top3", "pnl_ex_top3", "pnl")},
                      "P_seg": "—", "M_seg": "—"})
    H.append(table(audit, first="C3@P（落库 plan_date）", with_seg=False,
                   foot="本表为审计轨：落库值的 n 与主表 T3（建仓日口径）必然不同，"
                        "差值即 1 日错位的规模。不得混用。"))
    H.append("<h3>2.3 看空集合重叠度</h3>")
    ovrows = []
    for name, key in (("MA20 下方 vs MA60 下方", "ma20_ma60"),
                      ("MA60 下方 vs A判据降势", "ma60_a"),
                      ("MA20 下方 vs A判据降势", "ma20_a")):
        o = data["overlap"][key]
        ovrows.append(f'<tr><td>{name}</td><td class="nm">{o["n_a"]}</td>'
                      f'<td class="nm">{o["n_b"]}</td><td class="nm">{o["inter"]}</td>'
                      f'<td class="nm"><b>{fnum(o["jaccard"], 3)}</b></td>'
                      f'<td class="nm">{fpct(o["share_a_in_b"] * 100 if o["share_a_in_b"] else None)}</td>'
                      f'<td class="nm">{fpct(o["share_b_in_a"] * 100 if o["share_b_in_a"] else None)}</td></tr>')
    H.append('<div class="table-wrap"><table class="matrix"><thead><tr><th>口径对</th>'
             '<th>前者笔数</th><th>后者笔数</th><th>交集</th><th>Jaccard</th>'
             '<th>前者被后者覆盖</th><th>后者被前者覆盖</th></tr></thead><tbody>'
             + "".join(ovrows) + "</tbody></table></div>")
    H.append('<p class="small muted">Jaccard 越高说明两个口径在看同一批交易。'
             '若 MA 与 A 判据的 Jaccard 很低，说明它们是<b>互补而非重复</b>的口径，'
             '此时「哪个口径对」不能靠常识判断，只能靠后续 B 类同参数重跑。</p>')

    # §3 主表
    H.append('<h2 id="s3">§3 主表 T1 ｜ C2 MA60 × 建仓日</h2>')
    H.append("<h3>3.1 L1 二元</h3>")
    H.append(table(data["t1"]["l1"]))
    H.append("<h3>3.2 L2 对称八带（d60 距离）</h3>")
    H.append(table(data["t1"]["l2"],
                   foot="分带对称、等距、预先写死（±2% ≈ 上证一个 ATR，±5/±10 覆盖尾部）。"
                        "不按结果合并或拆分。"))
    H.append("<h3>3.3 L3 边界 1% 加密</h3>")
    if data["t1"]["l3_enabled"]:
        H.append('<div class="callout blue">L3 触发条件命中（跨 0 的两相邻带同时 n≥30，'
                 '且符号相反或集中度差 &gt;30pp）→ 已按 1% 加密。'
                 '<b>本节结果为探索性，不据此提任何阈值。</b></div>')
        H.append(table(data["t1"]["l3"]))
    else:
        H.append('<div class="callout blue">L3 <b>未触发</b>：跨 0 的两个相邻带未同时满足'
                 '「n≥30 且符号相反/集中度差 &gt;30pp」。故 0 轴边界不做 1% 加密。</div>')
    H.append("<h3>3.4 逐桶一致性记分卡</h3>")
    H.append("<p class=\"small\">M = 有交易的年份数，P = 净利为正的年份数；"
             "P′/M′ = 只计 n≥3 的年份（避免小样本年扰动）；"
             "<b>段级 P/M</b> = 该桶覆盖的 regime 段中净利为正的段数 / 总段数。"
             "笔级与段级符号不一致时，裁定自动降级。</p>")
    H.append(scorecard_html(data["t1"]["l1"], "T1 L1 二元"))
    H.append(scorecard_html(data["t1"]["l2"], "T1 L2 八带"))

    # §4 T2/T3
    H.append('<h2 id="s4">§4 对照表 T2（MA20）与 T3（A 判据）</h2>')
    H.append("<h3>4.1 T2 ｜ C1 MA20 × 建仓日</h3>")
    H.append(table(data["t2"]["l1"]))
    H.append("<h4>T2 · L2 八带</h4>")
    H.append(table(data["t2"]["l2"]))
    H.append("<h3>4.2 T3 ｜ C3 A 判据 × 建仓日（与 M9.0 同口径）</h3>")
    H.append(table(data["t3"],
                   foot="本表与 docs/experiments/e17_newhigh_distance_report 第 ④ 章的 regime 桶同口径，"
                        "笔数与净利应精确一致（校准 HARD 已断言）。"))

    # §5 平仓时点
    H.append('<h2 id="s5">§5 平仓时点 T4 / T5 / T6</h2>')
    H.append('<p>建仓日回答「该不该在这种市况开仓」；平仓日回答「回吐发生在什么阶段」。'
             '两张表方向一致时结论更稳，方向相反时说明「买对但卖在坏时候」或反之。</p>')
    H.append("<h3>5.1 T4 ｜ C2 MA60 × 平仓日</h3>")
    H.append(table(data["t4"], with_m=False, with_seg=False))
    H.append("<h3>5.2 T5 / T6 ｜ 平仓日简表</h3>")
    simple = ([{"label": f"C1 MA20 · {b['label']}", **b} for b in data["t5"]]
              + [{"label": f"C3 A判据 · {b['label']}", **b} for b in data["t6"]])
    H.append(table(simple, with_m=False, with_seg=False))

    # §6 诊断
    H.append('<h2 id="s6">§6 买卖错位诊断</h2>')
    H.append("<h3>6.1 2×2 转移矩阵（MA60：建仓日 → 平仓日）</h3>")
    tr_rows = [{"label": f"{x['from']} → {x['to']}　{x['tag']}", **x}
               for x in data["transition"]]
    H.append(table(tr_rows, with_m=False, with_seg=False,
                   foot="（上→下）是「买对了但持有期内跌破 MA60」——卖晚候选；"
                        "（下→下）是「全程在 MA60 下方」——买错候选。"
                        "两者对比即 H5 的判据。"))
    H.append("<h3>6.2 2×3 交叉表（MA60 × A 判据，均按建仓日）</h3>")
    cr_rows = [{"label": f"{x['ma']} × {x['a']}", **x} for x in data["crosstab"]]
    H.append(table(cr_rows, with_m=False, with_seg=False,
                   foot="若「MA60 下方」的负值在 A 判据=震荡时也为负，说明负值不是「熊市」独有，"
                        "而是「均线下方」这个更宽的状态；反之若只在降势里为负，"
                        "说明两个口径在讲同一件事。这是 H4 的核心证据。"))

    # §7 回撤
    H.append('<h2 id="s7">§7 回撤口径</h2>')
    H.append("""<h3>7.1 指数峰值回撤（主口径，外生、跨年不重置）</h3>
<p class="small">峰值基准 = 样本起点 2005-01-04 的收盘（<b>不是</b> 1990 年）。若用全历史峰值，
2005–2007 会带着 2001 年 2245 点的历史峰，整段被标成「从历史高点回撤 40%+」，与当年是大牛市的事实冲突。</p>""")
    H.append(table(data["dd"]["idx"], with_m=False, with_seg=False))
    H.append('<p class="small muted">峰值起点敏感性（1990 起版本的分桶笔数，仅 INFO 不作裁定）：'
             + data["dd"]["sens_note"] + "</p>")
    # 2005 基准峰的退化诊断
    big = max(data["dd"]["idx"], key=lambda b: b["n"])
    share = big["n"] / m["total_closed"] * 100 if m["total_closed"] else 0
    H.append(f"""<div class="callout red"><b>口径缺陷（执行中发现，已登记框架 §10 差异 #8）：</b>
以 2005-01-04 为基准峰的「累计回撤」在 2007 年 10 月泡沫高点后<b>永不复位</b>——
此后所有交易都记在深回撤档。<code>{big['label']}</code> 一档独占 <b>{big['n']}</b> 笔
（{share:.1f}%），分桶几乎失去判别力。故 7.2 改用系统自身语言（距 52 周新高）的滚动口径。</div>""")
    H.append("""<h3>7.2 距 52 周（250 交易日）高点回撤 —— 操作口径</h3>
<p class="small">滚动窗口取过去 250 个交易日的最高收盘为峰，<b>不重置、零未来信息</b>。
与系统既有的「52 周新高距离」同尺，是「现在离近期高位跌了多少」的直接度量。</p>""")
    H.append(table(data["dd"]["idx250"], with_m=False, with_seg=False,
                   foot="本表是 §7 的裁定依据；上一节的 2005 基准峰口径保留作审计对照。"))
    H.append("""<h3>7.3 账户权益回撤（描述性，内生、逐年重置）</h3>
<div class="callout red"><b>不得据此得出「回撤闸门有效/无效」任一结论。</b>
账户回撤是<b>内生</b>变量（由此前交易盈亏决定），且回测每年把权益重置为 1,000,000，
14 年最深回撤仅约 −9.08%，<code>&gt;10%</code> 档在 3,402 个交易日里<b>一天都没有</b>。
该桶登记为「不可检验（结构性无样本）」，与 P0-1 的 <code>/top 0–5%</code> 空带同处理。</div>""")
    H.append(table(data["dd"]["acct"], with_m=False, with_seg=False,
                   foot="本表只作描述，不给裁定标签（唯一例外：空桶标注「不可检验」）。"))
    H.append("<h4>7.4 逐年账户最深回撤</h4>")
    yrows = "".join(f'<tr><td>{r["year"]}</td><td class="nm">{r["days"]}</td>'
                    f'<td class="down">{r["max"]:.2f}%</td>'
                    f'<td class="nm">{r["lt5"]}</td><td class="nm">{r["5to10"]}</td>'
                    f'<td class="nm">{r["gt10"]}</td></tr>'
                    for r in data["dd"]["acct_yearly"])
    H.append('<div class="table-wrap"><table class="matrix"><thead><tr><th>年份</th>'
             '<th>交易日</th><th>最深回撤</th><th>&lt;5% 天数</th><th>5–10% 天数</th>'
             '<th>&gt;10% 天数</th></tr></thead><tbody>' + yrows + "</tbody></table></div>")

    # §8 有效样本
    H.append('<h2 id="s8">§8 有效样本与跨年一致性</h2>')
    H.append(f"""<div class="callout gold"><b>为什么必须报 m：</b>
2005–2018 共 {m['sample']['trading_days']:,} 个交易日，A 判据翻转 <b>{m['segments']['a']}</b> 段、
MA20 切换 <b>{m['segments']['ma20']}</b> 段、MA60 只切换 <b>{m['segments']['ma60']}</b> 段。
MA60 上方桶那些交易全部来自这 {m['segments']['ma60']} 个连续区间——把它们当成几百个独立样本做显著性检验，
会得到虚假的显著性。先例：<code>docs/18</code> R1 记录过「20 日窗口重叠，独立样本≈14 个」。
故本报告把 <code>m ≥ 20</code> 列为硬门槛，并单独给出段级记分卡。</div>""")
    H.append("<h3>8.1 三口径段数与桶覆盖</h3>")
    srows = ""
    for r in data["seg_table"]:
        detail = "　".join(f'<b>{k}</b> {v["n"]}/{v["m"]}' for k, v in r["buckets"].items())
        srows += (f'<tr><td>{r["criterion"]}</td><td class="nm">{r["total"]}</td>'
                  f'<td style="text-align:left">{detail}</td></tr>')
    H.append('<div class="table-wrap"><table class="matrix"><thead><tr>'
             '<th>判据</th><th>全期段数</th><th>各桶覆盖（笔数 / 段数）</th>'
             '</tr></thead><tbody>' + srows + "</tbody></table></div>")
    H.append('<p class="small muted">单元格为「笔数 / 段数」。段数远小于笔数说明该桶严重依赖少数几段行情——'
             '这正是「有效样本」要压制的东西。</p>')
    H.append("<h3>8.2 逐年 × 桶矩阵</h3>")
    H.append(matrix_html(data))
    H.append("<h3>8.3 跨年一致性记分卡（T3 与 T2）</h3>")
    H.append(scorecard_html(data["t3"], "T3 A 判据 × 建仓日"))
    H.append(scorecard_html(data["t2"]["l1"], "T2 MA20 × 建仓日"))
    H.append("""<h3>8.4 笔级均值 vs 段级中位数——一个必须看见的分离</h3>
<p class="small">笔级每单位净利是「合计÷合计」的<b>均值</b>；段级中位数是「按 regime 段分组后每段每单位净利的中位数」。
两者不可直接比对大小（均值与中位数不同量），但<b>它们的分离本身是重要信息</b>：
若中位段为负而均值为正，说明利润由少数行情段贡献，多数行情段在亏钱。</p>""")
    cmp_rows = []
    for grp, rows in (("T1 L1 · MA60×建仓日", data["t1"]["l1"]),
                      ("T2 L1 · MA20×建仓日", data["t2"]["l1"]),
                      ("T3 · A判据×建仓日", data["t3"]),
                      ("T4 · MA60×平仓日", data["t4"])):
        for b in rows:
            if b["n"] < 30:
                continue
            cmp_rows.append(f'<tr><td>{grp}</td><td>{b["label"]}</td>'
                            f'<td class="nm">{b["n"]} / {b.get("m", 0)}</td>'
                            f'<td class="{cls(b["pu"])}"><b>{fsign(b["pu"])}</b></td>'
                            f'<td class="{cls(b.get("pu_seg_med"))}">{fsign(b.get("pu_seg_med"))}</td>'
                            f'<td class="nm">{b.get("P_seg")}/{b.get("M_seg")}'
                            f'（{(b.get("ratio_seg") or 0) * 100:.0f}%）</td>'
                            f'<td class="nm">{fpct(b.get("seg_top3_share"))}</td></tr>')
    H.append('<div class="table-wrap"><table class="matrix"><thead><tr>'
             '<th>口径</th><th>桶</th><th>n / m</th><th>笔级每单位（均值）</th>'
             '<th>段级每单位（中位数）</th><th>段级正段占比</th><th>正段前三集中</th>'
             '</tr></thead><tbody>' + "".join(cmp_rows) + "</tbody></table></div>")
    H.append('<p class="small muted">读法：若「笔级每单位」为正而「中位数」为负，'
             '说明这个桶的钱来自少数几段行情；「正段前三集中」越高，越说明是少数行情段撑起来的。'
             '<b>裁定不依据中位数与均值的比较</b>（那是不可比的两种统计量），'
             '而是依据「多数段是否为正」（裁定矩阵第 6 级）。</p>')

    # §9 假设
    H.append('<h2 id="s9">§9 假设裁定 H1–H6 与 B 类验证清单</h2>')
    hrows = "".join(f'<tr><td><b>{h["code"]}</b></td><td>{h["claim"]}</td>'
                    f'<td class="nm">{h["value"]}</td><td>{hyp_html(h["result"])}</td>'
                    f'<td class="nm">{h["note"]}</td></tr>' for h in data["hypotheses"])
    H.append('<div class="table-wrap"><table><thead><tr><th>编号</th><th>主张</th>'
             '<th>实测</th><th>裁定</th><th>判据</th></tr></thead><tbody>'
             + hrows + "</tbody></table></div>")
    H.append("""<h3>9.1 B 类验证清单（只有假设，没有阈值）</h3>
<div class="table-wrap"><table class="matrix"><thead><tr><th>候选</th><th>规则形态</th>
<th>前置</th><th>成本</th><th>判定口径</th></tr></thead><tbody>
<tr><td>B-1</td><td>MA60 下方 首买 0.5 单位（不是停手）</td><td>H2 成立</td><td>代表年 3.5h / 全量 14h</td><td>四项 + 剔除前三 + 逐年符号</td></tr>
<tr><td>B-2</td><td>MA60 下方 停手（B-1 的对照）</td><td>H2 成立</td><td>同上，且须与 B-1 同轮同参数</td><td>同上</td></tr>
<tr><td>B-3</td><td>「上→下」触发减半仓</td><td>H5 成立</td><td>需 MFE + trailing 支持</td><td>回吐率 + 每单位</td></tr>
<tr><td>B-4</td><td>指数峰值回撤 ≥20% 降仓</td><td>H6 成立</td><td>回测器需读指数回撤（当前无此参数）</td><td>同上</td></tr>
</tbody></table></div>
<div class="callout red"><b>固定声明：</b>以上均为待验假设。<code>docs/23</code> §9 P5 已硬否决「熊市闸门」
（C 判据六轮证伪：开仓放大器 −28k~−58k、熊市闸门错杀牛市回调、A 判据在熊市不触发）。
<b>本报告不产出任何阈值</b>，任何阈值只能由 B 类同参数重跑得出。</div>""")

    # §10 诚实标注
    H.append("""<h2 id="s10">§10 诚实标注</h2>
<ul>
<li><b>不是因果。</b>分桶不改变信号、成交、仓位路径；regime 与大势、个股波动率、箱体质量高度共线，桶间差异含大量混杂。</li>
<li><b>不是样本外。</b>2005–2018 是全部参数被选定所用的同一段历史；结论只用于提假设，不得直接改参数上线。</li>
<li><b>不是盲测。</b>框架 §7.4 的校准锚点（样本量、C3 分布、MA 分布、段数、回撤天数）是在看到既有报告之后登记的；但 H1–H6 判据与裁定矩阵在看到本轮分桶结果之前已定稿。</li>
<li><b>账户回撤 <code>&gt;10%</code> 档不可检验</b>（结构性无样本），不得据此得出「回撤闸门有效/无效」。</li>
<li><b>「低账户回撤段更优」不可作因果</b>：账户回撤内生，低回撤段与「刚赚完钱」高度重合。</li>
<li><b>幸存者偏差</b>：只用当前仍存在的股票，退市股不在样本内；2005–2008 段收益偏乐观。</li>
<li><b>2008 已并入</b>：原为样本外验证年，现与其余年份合并统计，不再具备样本外属性。</li>
<li><b>0–5% / &gt;10% 这类空桶</b>一律保留行并注明原因，不删行、不并入相邻桶、不写成「无效应」。</li>
</ul>""")

    # 附录
    H.append('<h2 id="apx">附录 · 校准表</h2>')
    H.append(f'<p class="small">HARD <b>{nh}/{th}</b> 通过，WARN 失败 <b>{nw}</b> 项。'
             'HARD 任一失败时生成器 <code>exit(2)</code> 且不写出任何文件。</p>')
    H.append(cal_html(data["calibration"]))
    H.append(f"""<div class="foot">
<p><b>数据溯源</b>　{len(m['index_meta']['fingerprints'])} 个回测库，库 mtime：
{', '.join(f'{k}:{v[11:]}' for k, v in list(m['db_mtimes'].items())[:4])} …</p>
<p><b>指数指纹</b>　{INDEX_CODE}：n={m['index_meta']['fingerprints'][0]['n']}，
{m['index_meta']['fingerprints'][0]['min']} → {m['index_meta']['fingerprints'][0]['max']}，
Σclose={m['index_meta']['fingerprints'][0]['sum_close']:,.2f}（14 库一致，HARD 已断言）</p>
<p><b>生成器</b>　<code>scripts/e17_regime_attribution_report.py</code>　sha256 {m['generator_sha256']}</p>
<p><b>框架</b>　<code>docs/experiments/e17_regime_attribution_framework.md</code>（零结果纯规则）·
上游 <code>docs/29</code> §五 P0-3 · 理论位置 <code>docs/00</code> §3.6 十维 #10</p>
<p><b>只读声明</b>　本报告由只读查询生成，未改任何回测库、未改任何参数、未跑任何回测。
耗时 {m['elapsed']}s。</p>
</div></div></body></html>""")
    return "".join(H)


def md_table(rows, first="分带", with_m=True, with_seg=True):
    cols = [first] + (["n / m"] if with_m else []) + \
           ["胜率", "每单位净利", "保本胜率", "缺口", "PF", "集中度",
            "前三赢家", "剔除前三净利"] + (["段级 P/M"] if with_seg else []) + \
           ["净利", "裁定"]
    out = ["| " + " | ".join(cols) + " |",
           "|" + "|".join(["---"] * len(cols)) + "|"]
    for b in rows:
        c = [b["label"]]
        if with_m:
            c.append(f'{b["n"]} / {b.get("m", 0)}')
        c += [fpct(b["wr"] * 100 if b["wr"] is not None else None),
              fsign(b["pu"]),
              fpct(b["bewr"] * 100 if b["bewr"] is not None else None),
              fpp(b["gap"]),
              fnum(b["pf"], 2),
              fpct(b["conc"]),
              fsign(b["top3"]),
              fsign(b["pnl_ex_top3"])]
        if with_seg:
            c.append(f'{b.get("P_seg", "—")}/{b.get("M_seg", "—")}')
        c += [fsign(b["pnl"]), b.get("verdict", "")]
        out.append("| " + " | ".join(str(x) for x in c) + " |")
    return "\n".join(out)


def render_md(data):
    m = data["meta"]
    ov = m["overall"]
    below = next((b for b in data["t1"]["l1"] if b["hi"] == 0.0), None)
    above = next((b for b in data["t1"]["l1"] if b["lo"] == 0.0), None)
    M = []
    M.append("# P0-3 · 大盘 regime 归因报告\n")
    M.append(f"> 实验 ID：P0-3（十维 #10 固定单位头寸管理）· A 类归因 · 2005–2018 共 14 年\n"
             f"> {m['funnel']['leg1']:,} 笔建仓 / {m['total_closed']:,} 笔平仓 · "
             f"Σ净利 {fsign(m['total_pnl'], 1)} · 整体 PF {fnum(ov['pf'], 2)}\n"
             f"> 生成于 {m['generated']} · 生成器 sha256:{m['generator_sha256'][:16]}\n")

    M.append("## 核心结论\n")
    if below and above:
        M.append(f"- **T1 主表（MA60 × 建仓日）**：下方桶 n={below['n']} / m={below.get('m', 0)} 段，"
                 f"每单位 **{fsign(below['pu'])}**，剔除前三 {fsign(below['pnl_ex_top3'])}，"
                 f"裁定「{below['verdict']}」；上方桶 n={above['n']} / m={above.get('m', 0)} 段，"
                 f"每单位 **{fsign(above['pu'])}**，剔除前三 {fsign(above['pnl_ex_top3'])}，"
                 f"裁定「{above['verdict']}」。")
    for h in data["hypotheses"]:
        M.append(f"- **{h['code']} {h['claim']}** → **{h['result']}**（{h['value']}）")
    M.append(f"- **有效样本**：MA60 仅 {m['segments']['ma60']} 段、MA20 {m['segments']['ma20']} 段、"
             f"A 判据 {m['segments']['a']} 段。同一段内多笔交易共享同一大盘状态，"
             f"不是独立观测；这是本报告与朴素分桶最重要的区别。")
    sm = m.get("seg_median", {})
    if sm.get("total"):
        M.append(f"- **段级视角**：n≥30 的 {sm['total']} 个桶里，有 **{sm['neg']}** 个的"
                 f"「中位 regime 段」亏钱——不论大盘什么状态，中位数行情段都不赚钱，"
                 f"利润由少数行情段撑起。")
    M.append(f"- **回撤口径退化**：以 2005 为基准峰的累计回撤在 2007 泡沫高点后永不复位"
             f"（单档独占 78% 样本），已改用「距 52 周高点回撤」作裁定依据；"
             f"账户权益回撤 `>10%` 档 3,402 个交易日里 0 天，登记「不可检验」。")
    M.append(f"- **审计轨**：`reason_json.regime`（plan_date 口径）与 Python 重算逐笔一致率 "
             f"**{data['t7']['agree_rate'] * 100:.2f}%**；plan 日与建仓日错 1 日，主表一律用建仓日重算。")
    M.append("- **不产出任何阈值**：本报告只给待验假设与 B 类清单。`docs/23` §9 P5 已硬否决熊市闸门。\n")

    M.append("## §1 设定\n")
    M.append("三判据：C1 = 上证 vs MA20；C2 = 上证 vs MA60（**主表**）；C3 = A 判据（60 日低点相邻日比，"
             "`cur > prev×1.002` 升势 / `< prev×0.998` 降势 / 其余震荡）。\n")
    M.append("三时点：P = `plan_date`（仅审计轨）/ O = `opened_date`（**主**）/ C = `closed_date`（对照）。\n")
    M.append(f"样本漏斗：14 库 → `leg_no=1` **{m['funnel']['leg1']:,}** 笔 → 已平仓 "
             f"**{m['total_closed']:,}** 笔（剔除 {m['funnel']['leg1'] - m['total_closed']} 笔未平仓）。"
             "唯一剔除动作，不剔极端值、不剔单年。\n")

    M.append("## §2 口径互检\n")
    side = []
    for label, rows in (("C1 MA20", data["t2"]["l1"]), ("C2 MA60", data["t1"]["l1"])):
        for b in rows:
            side.append({"label": f"{label} · {b['label']}",
                         **{k: v for k, v in b.items() if k != "label"}})
    for b in data["t3"]:
        side.append({"label": f"C3 A判据 · {b['label']}",
                     **{k: v for k, v in b.items() if k != "label"}})
    M.append(md_table(side, with_seg=False) + "\n")
    M.append(f"落库 regime vs 重算一致率 **{data['t7']['agree_rate'] * 100:.2f}%**；"
             f"2014 年样例 `{data['t7']['y2014_note']}`。\n")
    M.append("看空集合重叠：\n")
    M.append("| 口径对 | 前者 | 后者 | 交集 | Jaccard |\n|---|---|---|---|---|")
    for name, key in (("MA20 ↓ vs MA60 ↓", "ma20_ma60"),
                      ("MA60 ↓ vs A降势", "ma60_a"),
                      ("MA20 ↓ vs A降势", "ma20_a")):
        o = data["overlap"][key]
        M.append(f"| {name} | {o['n_a']} | {o['n_b']} | {o['inter']} | {fnum(o['jaccard'], 3)} |")
    M.append("")

    M.append("## §3 主表 T1 ｜ C2 MA60 × 建仓日\n")
    M.append("### L1 二元\n")
    M.append(md_table(data["t1"]["l1"]) + "\n")
    M.append("### L2 对称八带\n")
    M.append(md_table(data["t1"]["l2"]) + "\n")
    M.append("### L3 边界 1% 加密\n")
    if data["t1"]["l3_enabled"]:
        M.append("触发条件命中，已按 1% 加密（**探索性，不据此提阈值**）。\n")
        M.append(md_table(data["t1"]["l3"]) + "\n")
    else:
        M.append("**未触发**：跨 0 的两个相邻带未同时满足「n≥30 且符号相反/集中度差>30pp」。\n")

    M.append("## §4 对照 T2（MA20）与 T3（A 判据）\n")
    M.append("### T2 ｜ C1 MA20 × 建仓日\n")
    M.append(md_table(data["t2"]["l1"]) + "\n")
    M.append("### T3 ｜ C3 A 判据 × 建仓日（与 M9.0 同口径）\n")
    M.append(md_table(data["t3"]) + "\n")

    M.append("## §5 平仓时点 T4 / T5 / T6\n")
    M.append("### T4 ｜ C2 MA60 × 平仓日\n")
    M.append(md_table(data["t4"], with_m=False, with_seg=False) + "\n")
    M.append("### T5 / T6\n")
    simple = ([{"label": f"C1 MA20 · {b['label']}", **b} for b in data["t5"]]
              + [{"label": f"C3 A判据 · {b['label']}", **b} for b in data["t6"]])
    M.append(md_table(simple, with_m=False, with_seg=False) + "\n")

    M.append("## §6 买卖错位诊断\n")
    M.append("### 2×2 转移矩阵（MA60）\n")
    M.append(md_table([{"label": f"{x['from']} → {x['to']}　{x['tag']}", **x}
                       for x in data["transition"]], with_m=False, with_seg=False) + "\n")
    M.append("### 2×3 交叉表（MA60 × A 判据）\n")
    M.append(md_table([{"label": f"{x['ma']} × {x['a']}", **x} for x in data["crosstab"]],
                      with_m=False, with_seg=False) + "\n")

    M.append("## §7 回撤口径\n")
    M.append("### 7.1 指数累计回撤（2005 基准峰，审计对照）\n")
    M.append(md_table(data["dd"]["idx"], with_m=False, with_seg=False) + "\n")
    M.append(f"峰值起点敏感性（1990 起）：{data['dd']['sens_note']}\n")
    big = max(data["dd"]["idx"], key=lambda b: b["n"])
    share = big["n"] / m["total_closed"] * 100 if m["total_closed"] else 0
    M.append(f"> **口径缺陷（执行中发现，已登记框架 §10 差异 #8）：** 以 2005-01-04 为基准峰的累计回撤"
             f"在 2007 年 10 月泡沫高点后永不复位，`{big['label']}` 一档独占 **{big['n']}** 笔"
             f"（{share:.1f}%），分桶几乎失去判别力。故 7.2 改用「距 52 周高点回撤」。\n")
    M.append("### 7.2 距 52 周（250 交易日）高点回撤 —— 操作口径（本报告裁定依据）\n")
    M.append(md_table(data["dd"]["idx250"], with_m=False, with_seg=False) + "\n")
    M.append("### 7.3 账户权益回撤（描述性，内生逐年重置）\n")
    M.append(md_table(data["dd"]["acct"], with_m=False, with_seg=False) + "\n")
    M.append("> **不得据此得出回撤闸门有效/无效。** 账户回撤内生，且逐年重置，"
             "`>10%` 档在 3,402 个交易日里 0 天，登记为「不可检验（结构性无样本）」。\n")

    M.append("## §8 有效样本与跨年一致性\n")
    M.append(f"2005–2018 共 {m['sample']['trading_days']:,} 个交易日：A 判据 {m['segments']['a']} 段、"
             f"MA20 {m['segments']['ma20']} 段、MA60 **{m['segments']['ma60']} 段**。"
             "`m < 20` 不给裁定。\n")
    M.append("### 逐年 × 桶矩阵\n")
    M.append("| 年份 | 下方 n | 下方每单位 | 上方 n | 上方每单位 | 升势 n | 降势 n | 震荡 n | 全年净利 |")
    M.append("|---|---|---|---|---|---|---|---|---|")
    for y in m["years"]:
        r = data["year_matrix"][str(y)]
        d, u = r["t1_l1"]["下"], r["t1_l1"]["上"]
        t3 = r["t3"]
        M.append(f"| {y} | {d['n']} | {fsign(d['pu'])} | {u['n']} | {fsign(u['pu'])} | "
                 f"{t3['升势（牛）']['n']} | {t3['降势（熊）']['n']} | {t3['震荡（猴）']['n']} | "
                 f"{fsign(d['pnl'] + u['pnl'])} |")
    M.append("")

    M.append("### 8.4 笔级均值 vs 段级中位数\n")
    sm = m.get("seg_median", {})
    M.append(f"n≥30 的 {sm.get('total', 0)} 个桶里，有 **{sm.get('neg', 0)}** 个桶的"
             "「中位 regime 段」是亏钱的。即：不论大盘处于什么状态，中位数行情段都不赚钱，"
             "利润由少数行情段撑起。**裁定不依据中位数与均值的比较**，只依据「多数段是否为正」。\n")
    M.append("| 口径 | 桶 | n / m | 笔级每单位（均值） | 段级每单位（中位数） | 段级正段占比 | 正段前三集中 |")
    M.append("|---|---|---|---|---|---|---|")
    for grp, rows in (("T1 L1", data["t1"]["l1"]), ("T2 L1", data["t2"]["l1"]),
                      ("T3", data["t3"]), ("T4", data["t4"])):
        for b in rows:
            if b["n"] < 30:
                continue
            M.append(f"| {grp} | {b['label']} | {b['n']} / {b.get('m', 0)} | "
                     f"{fsign(b['pu'])} | {fsign(b.get('pu_seg_med'))} | "
                     f"{b.get('P_seg')}/{b.get('M_seg')}（{(b.get('ratio_seg') or 0) * 100:.0f}%） | "
                     f"{fpct(b.get('seg_top3_share'))} |")
    M.append("")

    M.append("## §9 假设裁定 H1–H6\n")
    M.append("| 编号 | 主张 | 实测 | 裁定 |\n|---|---|---|---|")
    for h in data["hypotheses"]:
        M.append(f"| {h['code']} | {h['claim']} | {h['value']} | **{h['result']}** |")
    M.append("")
    M.append("### B 类验证清单\n")
    M.append("| 候选 | 规则形态 | 前置 |\n|---|---|---|")
    M.append("| B-1 | MA60 下方首买 0.5 单位（不是停手） | H2 成立 |")
    M.append("| B-2 | MA60 下方停手（B-1 对照，同轮同参数） | H2 成立 |")
    M.append("| B-3 | 「上→下」触发减半仓（需 MFE + trailing） | H5 成立 |")
    M.append("| B-4 | 指数峰值回撤 ≥20% 降仓（回测器需改） | H6 成立 |")
    M.append("\n> **本报告不产出任何阈值。** `docs/23` §9 P5 已硬否决熊市闸门；"
             "任何阈值只能由 B 类同参数重跑得出。\n")

    M.append("## §10 诚实标注\n")
    M.append("- 不是因果：分桶不改变交易路径，regime 与波动率、箱体质量高度共线。")
    M.append("- 不是样本外：2005–2018 是参数选定所用的同一段历史。")
    M.append("- 不是盲测：校准锚点在看既有报告后登记，但 H1–H6 判据与裁定矩阵在看到本轮结果前定稿。")
    M.append("- 口径偏差已登记：2005 基准峰回撤退化（框架 §10 差异 #8），改用距 52 周高点回撤；"
             "两种口径并存，前者保留作审计对照。")
    M.append("- 账户回撤 `>10%` 档结构性无样本，不得据此判断回撤闸门。")
    M.append("- 幸存者偏差：退市股不在样本内；2008 已并入，不再具样本外属性。")
    M.append("")
    nh = sum(1 for c in data["calibration"] if c["level"] == "HARD" and c["ok"])
    th = sum(1 for c in data["calibration"] if c["level"] == "HARD")
    nw = sum(1 for c in data["calibration"] if c["level"] == "WARN" and not c["ok"])
    M.append("## 附录 · 校准\n")
    M.append(f"HARD **{nh}/{th}** 通过，WARN 失败 **{nw}** 项。\n")
    M.append("| 层 | 断言 | 期望 | 实测 | 结果 |\n|---|---|---|---|---|")
    for c in data["calibration"]:
        flag = "通过" if c["ok"] else ("失败" if c["level"] == "HARD" else "WARN")
        M.append(f"| {c['level']} | {c['name']} | {c['expected']} | {c['actual']} | {flag} |")
    M.append("")
    return "\n".join(M)


def render_check(data):
    cal = data["calibration"]
    w = max(len(c["name"]) for c in cal) + 2
    print(f"\n=== P0-3 校准表（{datetime.now():%Y-%m-%d %H:%M}）===")
    for c in cal:
        flag = "OK  " if c["ok"] else ("FAIL" if c["level"] == "HARD" else "WARN")
        print(f'[{c["level"]:4s}] {flag} {c["name"]:<{w}} 期望={c["expected"]}  实测={c["actual"]}')
    nh = sum(1 for c in cal if c["level"] == "HARD" and c["ok"])
    th = sum(1 for c in cal if c["level"] == "HARD")
    nw = sum(1 for c in cal if c["level"] == "WARN" and not c["ok"])
    print(f"--- HARD {nh}/{th} 通过，WARN 失败 {nw} 项 ---")
    print(f"六表合计: T1L1={sum(b['n'] for b in data['t1']['l1'])} "
          f"T1L2={sum(b['n'] for b in data['t1']['l2'])} "
          f"T1L3={sum(b['n'] for b in data['t1']['l3'])} "
          f"T2L1={sum(b['n'] for b in data['t2']['l1'])} "
          f"T3={sum(b['n'] for b in data['t3'])} "
          f"T4={sum(b['n'] for b in data['t4'])} "
          f"T5={sum(b['n'] for b in data['t5'])} "
          f"T6={sum(b['n'] for b in data['t6'])}")


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
        print("\n可能原因：新增/减少年份、回测库被重写、指数序列变动、"
              "分桶逻辑改动。确认原因后如属预期，可加 --allow-drift。", file=sys.stderr)
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
