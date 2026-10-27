#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-5 · 浮盈回吐分布（MFE）实验 · 专项报告生成器。

实验：docs/29 §五 队列第 1 位 P2-5（十维 #8/#9 何时卖），A 类零成本后验，不重跑回测、不改回测器。
框架：docs/experiments/e17_giveback_framework.md（零结果纯规则，含 H1/H2/H3 判据与加密规则）

核心三式（框架 §4.1）：
  cost_basis = Σ_legs(qty×price)          ← position.qty 平仓后清零，必须从 legs 求和
  MFE        = max(high)/avg_cost − 1      ← 持仓窗口 [opened, closed] 含两端
  giveback   = (MFE − ret) / MFE           ← 仅 MFE>0 可检验

输出（docs/experiments/，实验报告唯一沉淀处）：
  e17_giveback_report.html / .md / _data.json

用法：
  python scripts/e17_giveback.py               # 正式生成（HARD 失败即中止）
  python scripts/e17_giveback.py --check-only  # 只跑校准，不写文件
  python scripts/e17_giveback.py --allow-drift # HARD 降级为 WARN（须明确知道为何漂移）
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
OUT_HTML = EXP / "e17_giveback_report.html"
OUT_MD = EXP / "e17_giveback_report.md"
OUT_JSON = EXP / "e17_giveback_data.json"
METRICS = BT / "e17_metrics.csv"

# ---------------------------------------------------------------- 分带定义
# L1：持有交易日 5 带（框架 §5.1，照抄任务书预设，不按结果调整）
BANDS_L1 = [(1, 6), (6, 11), (11, 21), (21, 61), (61, None)]
# L2：命中带二分（框架 §5.4）
BANDS_L2 = [(1, 3), (3, 6), (6, 8), (8, 11), (11, 16), (16, 21), (21, 41), (41, 61)]

# 回吐率直方图 7 箱（框架 §5.2）——百分数口径
GB_BINS = [(-99, 0), (0, 25), (25, 50), (50, 75), (75, 100), (100, 150), (150, None)]

# ---------------------------------------------------------------- 校准基线（框架 §7.6，全为结构性锚点）
BASELINE = {
    "closed": 1328,
    "pnl": 842799.7,
    "pnl_tol": 0.5,
    "single_leg_ratio": 1.0,
    "untestable_missing_units": 0,
}

TIER_NOTE = "1 单位 = 2% 权益；E17 阶梯 t1=3 / t2=2 / t3=1 / t4=0.5"
EXRIGHT_DROP = -0.20      # 单日跌幅 < −20% → 疑似除权污染（框架 §3.5）
HOLD_CAL_MIN_AGREE = 0.95  # 两口径一致率门槛（HARD④）


# ---------------------------------------------------------------- 基础工具
def years_list():
    return sorted(int(os.path.basename(p).split("_")[2].split(".")[0])
                  for p in BT.glob("bt_e17_*.db"))


def glob_db():
    return [str(p) for p in sorted(BT.glob("bt_e17_*.db"))]


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
        return f"{lo-1} 日+"
    if hi - lo == 1:
        return f"{lo} 日"
    return f"{lo}–{hi-1} 日"


def gb_label(lo, hi):
    if lo < 0:
        return "<0%（异常）"
    if hi is None:
        return f"≥{lo}%"
    return f"{lo}–{hi}%"


def share_pct(x):
    """占比显示：微小负零记 0.0%。"""
    if x is None:
        return "—"
    if abs(x) < 5e-5:
        return "0.0%"
    return f"{x * 100:.1f}%"


def week_pct(x):
    """0.3721 → 37.2%"""
    return "—" if x is None else f"{x * 100:.1f}%"


# ---------------------------------------------------------------- 数据加载
def load_trades():
    """14 库逐笔加载：持仓样本 + 持仓窗口行情 + 市场日历。返回 (trades, funnel, meta)。"""
    trades, funnel = [], {"dbs": 0, "leg1": 0, "closed": 0, "no_cost": 0,
                          "no_bar": 0, "testable": 0, "untestable": 0,
                          "gb_neg": 0, "exright": 0, "cal_agree": 0, "ts_exit": 0,
                          "leg1_all": 0}
    meta = {"db_mtimes": {}, "close_reasons": {}, "bars_total": None}
    for y in years_list():
        db = BT / f"bt_e17_{y}.db"
        meta["db_mtimes"][f"bt_e17_{y}.db"] = datetime.fromtimestamp(
            db.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sql = """
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.avg_cost,
               p.realized_pnl, p.close_reason, o.reason_json,
               (SELECT SUM(qty)       FROM position_leg WHERE position_id=p.id) AS qty,
               (SELECT SUM(qty*price) FROM position_leg WHERE position_id=p.id) AS cost_basis,
               (SELECT COUNT(*)       FROM position_leg WHERE position_id=p.id) AS n_legs
        FROM position p
        JOIN position_leg l ON l.position_id=p.id AND l.leg_no=1
        LEFT JOIN plan_order o ON o.id=l.plan_order_id
        WHERE p.status='closed'
        """
        funnel["leg1_all"] += con.execute(
            "SELECT COUNT(*) FROM position_leg WHERE leg_no=1").fetchone()[0]
        rows = con.execute(sql).fetchall()
        funnel["leg1"] += len(rows)
        # 结构性设施：time_stop_noprofit 的成交日（按 position_id 索引）
        ts_map = {}
        for ex, rj in con.execute(
                "SELECT t.execute_date, o.reason_json FROM plan_order o "
                "JOIN trade_plan t ON t.id=o.trade_plan_id "
                "WHERE o.side='sell' AND o.reason_json LIKE '%time_stop_noprofit%'"):
            try:
                pid = json.loads(rj or "{}").get("position_id")
            except (TypeError, ValueError):
                pid = None
            if pid:
                ts_map[int(pid)] = ex
        for r in rows:
            try:
                rj = json.loads(r["reason_json"] or "{}")
            except (TypeError, ValueError):
                rj = {}
            pnl = r["realized_pnl"]
            qty = r["qty"] or 0
            cost = r["cost_basis"] or 0.0
            if pnl is None or not qty or cost <= 0:
                funnel["no_cost"] += 1
                continue
            funnel["closed"] += 1

            bars = con.execute(
                "SELECT date, high, close FROM daily_bar WHERE code=? AND date>=? AND date<=? "
                "ORDER BY date", (r["code"], r["opened_date"], r["closed_date"])).fetchall()
            if not bars:
                funnel["no_bar"] += 1
                continue
            cal = con.execute(
                "SELECT COUNT(*) FROM daily_bar WHERE code='sh000001' AND date>=? AND date<=?",
                (r["opened_date"], r["closed_date"])).fetchone()[0]

            avg_cost = cost / qty
            max_high = max(b["high"] for b in bars)
            mfe = max_high / avg_cost - 1.0
            ret = pnl / cost
            hold = len(bars)

            # 疑似除权：窗口内收盘价单日跌幅 < −20%
            susp = 0
            prev = None
            for b in bars:
                if prev and prev > 0 and (b["close"] / prev - 1.0) < EXRIGHT_DROP:
                    susp = 1
                    break
                prev = b["close"]

            testable = mfe > 0
            gb = (mfe - ret) / mfe if testable else None
            if gb is not None and gb < 0 and abs(gb) >= 1e-6:
                funnel["gb_neg"] += 1
            if testable:
                funnel["testable"] += 1
            else:
                funnel["untestable"] += 1
            if susp:
                funnel["exright"] += 1
            if same_l1_band(hold, cal):
                funnel["cal_agree"] += 1
            cr = (r["close_reason"] or "?").strip() or "?"
            meta["close_reasons"][cr] = meta["close_reasons"].get(cr, 0) + 1
            ts_exit = ts_map.get(int(r["id"])) == r["closed_date"]
            if ts_exit:
                funnel["ts_exit"] += 1
            trades.append({
                "year": y, "code": r["code"], "opened": r["opened_date"],
                "closed": r["closed_date"], "close_reason": cr,
                "avg_cost": round(avg_cost, 6), "qty": qty,
                "cost_basis": round(cost, 2), "pnl": round(pnl, 2),
                "units": float(rj.get("units", 1.0) or 1.0),
                "ndist": rj.get("newhigh_distance_pct"),
                "box_top": rj.get("box_top"), "box_bottom": rj.get("box_bottom"),
                "n_legs": r["n_legs"],
                "max_high": round(max_high, 4),
                "hold": hold, "hold_cal": cal,
                "mfe": mfe, "ret": ret,
                "gb": gb,
                "mfe_amt": mfe * cost,
                "gb_amt": (mfe - ret) * cost,
                "testable": testable, "susp": susp, "ts": ts_exit,
            })
        con.close()
        funnel["dbs"] += 1
    return trades, funnel, meta


def band_of(d):
    for lo, hi in BANDS_L1:
        if (hi is None and d >= lo) or (hi is not None and lo <= d < hi):
            return (lo, hi)
    return None


def same_l1_band(a, b):
    return band_of(a) == band_of(b)


def load_metrics():
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
    """四项固定口径 + PF + 缺口 + 剔除前三 + 回吐分布（框架 §4.5）。

    四项口径用**全部样本**（含不可检验），回吐统计只用可检验子集。
    """
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
    # 剔除前三赢家：严格按 pnl 降序去前 3 笔（并列时不额外多删）
    ex_rows = sorted(rows, key=lambda x: -x["pnl"])[3:] if len(rows) > 3 else []
    ex_pnl = sum(r["pnl"] for r in ex_rows)
    ex_units = sum(r["units"] for r in ex_rows)

    tst = [r for r in rows if r["testable"]]
    gbs = sorted(r["gb"] for r in tst)
    gb_med = pct_of(gbs, 0.5)
    gb_mean = (sum(gbs) / len(gbs)) if gbs else None
    mfe_amts = [r["mfe_amt"] for r in tst]

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
        # 回吐专属
        "n_tst": len(tst), "n_untst": n - len(tst),
        "gb_med": gb_med, "gb_mean": gb_mean,
        "gb_p25": pct_of(gbs, 0.25), "gb_p75": pct_of(gbs, 0.75),
        "gb_amt": sum(r["gb_amt"] for r in tst),
        "mfe_amt": sum(mfe_amts),
        "mfe_med": pct_of(sorted(r["mfe"] for r in tst), 0.5),
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
    if hi is None:
        return [t for t in trades if t["hold"] >= lo]
    return [t for t in trades if lo <= t["hold"] < hi]


def band_stat(trades, bands):
    out = []
    for lo, hi in bands:
        out.append({"label": day_label(lo, hi), "lo": lo, "hi": hi,
                    **agg(band_rows(trades, lo, hi))})
    return out


def consistency(trades, lo, hi):
    """跨年一致性记分卡（框架 §7.2）。"""
    years = sorted({t["year"] for t in trades})
    per_year = []
    for y in years:
        s = agg(band_rows([t for t in trades if t["year"] == y], lo, hi))
        per_year.append({"year": y, "n": s["n"], "pnl": s["pnl"],
                         "pu": s["pu"], "gb_med": s["gb_med"]})
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
    """逐带裁定（框架 §7.3）。先查剔除前三翻负，再查跨年稳定性。"""
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


def trigger(s, c, neighbors):
    """加密触发（框架 §5.4）：n≥30 且（符号反转 / 集中度>50% / 剔除前三翻负）。"""
    if s["n"] < 30:
        return False
    rev = any(nb is not None and s["pu"] is not None and nb * s["pu"] < 0
              for nb in neighbors)
    conc = s["conc"] is not None and s["conc"] > 50
    flip = (s["pu"] is not None and s["pu"] > 0
            and s["pu_ex_top3"] is not None and s["pu_ex_top3"] <= 0)
    return rev or conc or flip


def gb_anomaly_low(s, neighbors):
    """框架 §5.4 末条：回吐率中位数显著低于相邻带（差 ≥5pp）→ 强制再降一级复核是否成片。"""
    if s["n"] < 30 or s["gb_med"] is None:
        return False
    cands = [nb for nb in neighbors if nb is not None]
    if not cands:
        return False
    return s["gb_med"] < min(cands) - 0.05


# ---------------------------------------------------------------- 校准
def calibrate(trades, funnel, metrics, data):
    res = []

    def add(level, name, expected, actual, ok, note=""):
        res.append({"level": level, "name": name, "expected": expected,
                    "actual": actual, "ok": bool(ok), "note": note})

    ys = years_list()
    add("HARD", "年份连续 2005–2018", "14 年连续",
        f"{len(ys)} 年 {ys[0]}–{ys[-1]}" if ys else "无库",
        ys == list(range(2005, 2019)), "缺年或乱序")

    # HARD① 逐年三方对账
    bad = []
    for y in ys:
        yt = [t for t in trades if t["year"] == y]
        if y not in metrics:
            bad.append(f"{y}:metrics缺失")
            continue
        mn, mp = metrics[y]
        if len(yt) != mn:
            bad.append(f"{y}:{len(yt)}≠{mn}")
        elif abs(sum(t["pnl"] for t in yt) - mp) > 1.0:
            bad.append(f"{y}:净利差{sum(t['pnl'] for t in yt) - mp:.1f}")
    add("HARD", "①逐年笔数与净利 vs e17_metrics.csv", "14/14 一致",
        "14/14 一致" if not bad else "；".join(bad[:6]), not bad, "跨源对账")

    add("HARD", "①样本总数 vs P0-1 基线 1328", f"{BASELINE['closed']}",
        f"{len(trades)}", len(trades) == BASELINE["closed"], "")

    # HARD② 加总恒等式
    n_l1 = sum(s["n"] for s in data["bands_l1"])
    n_un = sum(s["n_untst"] for s in data["bands_l1"])
    add("HARD", "②Σ(L1 五带 n) = 样本总数", f"{len(trades)}", f"{n_l1}",
        n_l1 == len(trades), "带内包含方案，不可检验样本不剥离")
    add("HARD", "②Σ(各带 n_untestable) = 不可检验子集行 n",
        f"{funnel['untestable']}", f"{n_un}",
        n_un == funnel["untestable"], "子集一致")

    # HARD③ 逐年矩阵齐备
    ym = data["year_matrix"]
    add("HARD", "③逐年符号表齐备 2005–2018", "14 行",
        f"{len(ym)} 行", len(ym) == 14 and all(str(y) in ym for y in range(2005, 2019)),
        "缺年须登记原因")

    # HARD④ 持有天数两口径一致率
    agree = funnel["cal_agree"] / funnel["closed"] if funnel["closed"] else 0
    add("HARD", "④持有天数两口径一致率 ≥95%", "≥95.0%", f"{agree * 100:.2f}%",
        agree >= HOLD_CAL_MIN_AGREE, "低于则主表切 hold_cal")

    # HARD⑤ 回吐率 <0 异常
    add("HARD", "⑤回吐率<0 的异常笔数 = 0", "0", f"{funnel['gb_neg']}",
        funnel["gb_neg"] == 0, "非零说明成本或行情口径有错")

    # HARD⑥ 成本两路一致（load 阶段已用 legs 求 cost；此处校验 avg_cost*qty）
    mismatch = 0
    for t in trades:
        if abs(t["avg_cost"] * t["qty"] - t["cost_basis"]) > 1e-3 * max(1.0, t["cost_basis"]):
            mismatch += 1
    add("HARD", "⑥成本两路一致 avg_cost×qty ≡ Σ(qty×price)", "0 笔不符",
        f"{mismatch} 笔", mismatch == 0, "position.qty 已清零，成本来自 legs")

    # HARD⑦ 窗口行情齐备
    add("HARD", "⑦每笔 hold_bars ≥ 1", "0 笔缺失", f"{funnel['no_bar']} 笔缺失",
        funnel["no_bar"] == 0, "")

    # HARD⑧ 逐年净利对账
    tot = sum(t["pnl"] for t in trades)
    add("HARD", "⑧Σ逐年 realized_pnl ≈ +842,799.7", f"{BASELINE['pnl']:,.1f}",
        f"{tot:,.1f}", abs(tot - BASELINE["pnl"]) <= BASELINE["pnl_tol"],
        "按年取整和是 842,801，差 1.3")

    # WARN
    add("WARN", "疑似除权污染笔数（单日跌幅<−20%）", "登记", f"{funnel['exright']} 笔",
        True, "未复权，稳健性切片见附录")
    add("WARN", "单腿头寸占比", "100%",
        f"{sum(1 for t in trades if t['n_legs'] == 1) / len(trades) * 100:.1f}%",
        all(t["n_legs"] == 1 for t in trades), "非单腿时成本口径需重审")
    add("INFO", "close_reason 取值集合", "登记",
        "/".join(f"{k}:{v}" for k, v in sorted(data["meta"]["close_reasons"].items())),
        True, "退出方式切片见附录，不作判据")
    return res


# ---------------------------------------------------------------- 构建
def hist_rows(trades):
    tst = [t for t in trades if t["testable"]]
    total = len(tst)
    out = []
    for lo, hi in GB_BINS:
        if hi is None:
            rows = [t for t in tst if t["gb"] * 100 >= lo]
        elif lo < 0:
            rows = [t for t in tst if t["gb"] * 100 < hi]
        else:
            rows = [t for t in tst if lo <= t["gb"] * 100 < hi]
        out.append({"label": gb_label(lo, hi), "lo": lo, "hi": hi,
                    "n": len(rows),
                    "share": (len(rows) / total if total else None),
                    "pnl": sum(r["pnl"] for r in rows),
                    "gb_amt": sum(r["gb_amt"] for r in rows),
                    "hold_med": pct_of(sorted(r["hold"] for r in rows), 0.5) if rows else None,
                    "wr": (sum(1 for r in rows if r["pnl"] > 0) / len(rows)) if rows else None})
    return out


def year_gb_matrix(trades):
    years = sorted({t["year"] for t in trades})
    out = OrderedDict()
    for y in years:
        yt = [t for t in trades if t["year"] == y]
        row = {"overall": agg(yt)}
        row["bands"] = {}
        for lo, hi in BANDS_L1:
            s = agg(band_rows(yt, lo, hi))
            row["bands"][day_label(lo, hi)] = {"gb_med": s["gb_med"], "n_tst": s["n_tst"]}
        out[str(y)] = row
    return out


def judge_h1(trades, bands, cons):
    win_t = [t for t in trades if t["pnl"] > 0 and t["testable"]]
    c1 = (sum(1 for t in win_t if t["gb"] > 0.5) / len(win_t)) if win_t else None
    tot_gb = sum(t["gb_amt"] for t in trades if t["testable"])
    long_gb = sum(t["gb_amt"] for t in trades if t["testable"] and t["hold"] >= 11)
    c2 = (long_gb / tot_gb) if tot_gb else None
    merged = agg([t for t in trades if t["hold"] >= 21])
    mc = consistency([t for t in trades if t["hold"] >= 21], 21, None)
    c3 = (merged["pu"] is not None and merged["pu"] > 0
          and mc["ratio_f"] is not None and mc["ratio_f"] >= 0.50 and mc["Mf"] >= 8)
    ok = (c1 is not None and c1 >= 0.5 and c2 is not None and c2 >= 0.5 and c3)
    return {"ok": ok, "c1": c1, "c2": c2, "c3": c3,
            "c3_detail": {"pu": merged["pu"], "ratio_f": mc["ratio_f"], "Mf": mc["Mf"],
                          "n": merged["n"], "pnl": merged["pnl"]},
            "n_win_testable": len(win_t), "gb_total": tot_gb, "gb_long": long_gb}


def judge_h2(trades):
    tot_gb = sum(t["gb_amt"] for t in trades if t["testable"])
    tot_mfe = sum(t["mfe_amt"] for t in trades if t["testable"])
    sub = [t for t in trades if t["testable"] and t["pnl"] < 0 and t["hold"] <= 10]
    c1 = (sum(t["gb_amt"] for t in sub) / tot_gb) if tot_gb else None
    c2 = (sum(t["mfe_amt"] for t in sub) / tot_mfe) if tot_mfe else None
    ok = (c1 is not None and c1 >= 0.5 and c2 is not None and c2 < 0.2)
    return {"ok": ok, "c1": c1, "c2": c2, "n": len(sub),
            "gb_amt": sum(t["gb_amt"] for t in sub),
            "mfe_amt": sum(t["mfe_amt"] for t in sub)}


def judge_h3(bands):
    pus = [b["pu"] for b in bands]
    ups = sum(1 for i in range(len(pus) - 1)
              if pus[i] is not None and pus[i + 1] is not None and pus[i + 1] > pus[i])
    return {"ok": ups >= 4, "ups": ups, "pairs": len(pus) - 1, "pus": pus}


def build():
    trades, funnel, meta = load_trades()
    metrics = load_metrics()
    con = sqlite3.connect(f"file:{BT / 'full_hist_10pct.db'}?mode=ro", uri=True)
    meta["bars_total"] = con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    con.close()

    bands_l1 = band_stat(trades, BANDS_L1)
    for b in bands_l1:
        b["cons"] = consistency(trades, b["lo"], b["hi"])
        b["verdict"] = verdict(b, b["cons"])

    # 不可检验子集行（非加总行）
    un_rows = [t for t in trades if not t["testable"]]
    un = agg(un_rows)
    un["label"] = "不可检验（MFE ≤ 0，从未浮盈）"

    # L2 加密：按 L1 命中触发
    l2 = []
    triggered_l1 = []
    for i, b in enumerate(bands_l1):
        nb, nb_gb = [], []
        for j in (i - 1, i + 1):
            if 0 <= j < len(bands_l1):
                nb.append(bands_l1[j]["pu"])
                nb_gb.append(bands_l1[j]["gb_med"])
        hit = trigger(b, b["cons"], nb)
        hit_gb = gb_anomaly_low(b, nb_gb)
        if hit or hit_gb:
            triggered_l1.append(b["label"] + ("（回吐中位异常低）" if hit_gb and not hit else ""))
            subs = [(lo, hi) for lo, hi in BANDS_L2
                    if b["lo"] <= lo and (b["hi"] is None or (hi or 10 ** 6) <= b["hi"])]
            for lo, hi in subs:
                s = agg(band_rows(trades, lo, hi))
                s.update({"label": day_label(lo, hi), "lo": lo, "hi": hi,
                          "parent": b["label"], "cons": consistency(trades, lo, hi)})
                s["verdict"] = verdict(s, s["cons"])
                l2.append(s)

    # L3：对 L2 中仍触发的段再二分
    l3 = []
    for i, s in enumerate(l2):
        nb, nb_gb = [], []
        for j in (i - 1, i + 1):
            if 0 <= j < len(l2) and l2[j]["parent"] == s["parent"]:
                nb.append(l2[j]["pu"])
                nb_gb.append(l2[j]["gb_med"])
        if trigger(s, s["cons"], nb) or gb_anomaly_low(s, nb_gb):
            mid = (s["lo"] + s["hi"]) // 2
            if mid <= s["lo"] or mid >= s["hi"]:
                continue
            for lo, hi in [(s["lo"], mid), (mid, s["hi"])]:
                q = agg(band_rows(trades, lo, hi))
                q.update({"label": day_label(lo, hi), "lo": lo, "hi": hi,
                          "parent": s["label"]})
                q["verdict"] = verdict(q, consistency(trades, lo, hi))
                l3.append(q)

    # 切片
    t1 = [t for t in trades if t["ndist"] is not None and t["ndist"] <= 15]
    big = [t for t in trades if t["testable"] and t["mfe"] > 0.5]
    slices = {
        "t1": {"label": "t1 甜区（距 52 周新高 ≤15%）", "n_rows": len(t1), **agg(t1)},
        "big": {"label": "大赢家（MFE > 50%）", "n_rows": len(big), **agg(big)},
        "rest": {"label": "对照：非 t1（距新高 >15%）",
                 "n_rows": len(trades) - len(t1), **agg([t for t in trades if t["ndist"] is not None and t["ndist"] > 15])},
        "normal": {"label": "对照：非大赢家（MFE ≤ 50%）",
                   "n_rows": len(trades) - len(big), **agg([t for t in trades if not (t["testable"] and t["mfe"] > 0.5)])},
    }

    # 退出方式切片（附录）
    by_cr = []
    for cr in sorted({t["close_reason"] for t in trades}):
        sub = [t for t in trades if t["close_reason"] == cr]
        by_cr.append({"label": cr, **agg(sub)})

    # 稳健性：剔除疑似除权污染
    clean = [t for t in trades if not t["susp"]]
    robust = {"n_removed": len(trades) - len(clean), **agg(clean),
              "h1": judge_h1(clean, bands_l1, None), "h2": judge_h2(clean)}

    hist = hist_rows(trades)
    h1 = judge_h1(trades, bands_l1, None)
    h2 = judge_h2(trades)
    h3 = judge_h3(bands_l1)

    # 结构性发现：time_stop_noprofit 的出口分布（执行中追查「持有 6 日尖峰」时发现，见附 A）
    ts_all = [t for t in trades if t["ts"]]
    ts_by_band = []
    for lo, hi in BANDS_L1:
        sub = band_rows(trades, lo, hi)
        tsb = [t for t in sub if t["ts"]]
        ts_by_band.append({"label": day_label(lo, hi), "n": len(sub), "ts": len(tsb),
                           "share": (len(tsb) / len(sub) if sub else None),
                           **{f"ts_{k}": v for k, v in agg(tsb).items()}})
    from collections import Counter as _C
    hold_hist = _C(t["hold"] for t in trades)
    spike_hold, spike_n = hold_hist.most_common(1)[0]
    ts_stats = {
        "n": len(ts_all), "share": len(ts_all) / len(trades),
        "staledays": 2,
        "agg": agg(ts_all), "other": agg([t for t in trades if not t["ts"]]),
        "by_band": ts_by_band,
        "spike_hold": spike_hold, "spike_n": spike_n,
        "spike_ts": sum(1 for t in trades if t["hold"] == spike_hold and t["ts"]),
        "hold_hist": {str(k): v for k, v in sorted(hold_hist.items())},
    }

    # P2-6 建议（由裁定与结构事实共同决定，措辞不含收益预测）
    big_rows = [t for t in trades if t["testable"] and t["mfe"] > 0.5]
    win_rows = [t for t in trades if t["pnl"] > 0 and t["testable"]]
    p26 = {
        "hit_share": h2["c1"], "mfe_share": h2["c2"],
        "win_gb_med": pct_of(sorted(t["gb"] for t in win_rows), 0.5) if win_rows else None,
        "big_gb_med": pct_of(sorted(t["gb"] for t in big_rows), 0.5) if big_rows else None,
        "big_n": len(big_rows), "big_pnl": sum(t["pnl"] for t in big_rows),
        "short_loss_pnl": sum(t["pnl"] for t in trades
                              if t["testable"] and t["pnl"] < 0 and t["hold"] <= 10),
        "ts_share": ts_stats["share"], "ts_n": ts_stats["n"],
        "rest_pnl": sum(t["pnl"] for t in trades if not (t["testable"] and t["mfe"] > 0.5)),
    }

    data = {
        "meta": {
            "experiment": "P2-5 浮盈回吐分布（MFE）",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generator": "scripts/e17_giveback.py",
            "generator_sha256": hashlib.sha256(
                Path(__file__).read_bytes()).hexdigest()[:16],
            "framework": "docs/experiments/e17_giveback_framework.md",
            "db_mtimes": meta["db_mtimes"],
            "close_reasons": meta["close_reasons"],
            "bars_total_check": meta["bars_total"],
            "unit_note": TIER_NOTE,
        },
        "funnel": funnel,
        "trades": trades,
        "bands_l1": bands_l1,
        "untestable": un,
        "bands_l2": l2,
        "triggered_l1": triggered_l1,
        "bands_l3": l3,
        "hist": hist,
        "year_matrix": year_gb_matrix(trades),
        "slices": slices,
        "by_close_reason": by_cr,
        "time_stop": ts_stats,
        "p26": p26,
        "robust_exright": robust,
        "h1": h1, "h2": h2, "h3": h3,
        "totals": agg(trades),
    }
    data["calibration"] = calibrate(trades, funnel, metrics, data)

    # 结论文本（全部由数据驱动，无硬编码结论）
    t = data["totals"]
    verdict_txt = []
    verdict_txt.append(
        f"2005–2018 共 <b>{t['n']}</b> 笔已平仓交易，其中可检验（MFE>0）<b>{t['n_tst']}</b> 笔、"
        f"不可检验（从未浮盈）<b>{t['n_untst']}</b> 笔。")
    verdict_txt.append(
        f"可检验样本的<b>回吐率中位数 {week_pct(t['gb_med'])}</b>"
        f"（P25 {week_pct(t['gb_p25'])} / P75 {week_pct(t['gb_p75'])}），"
        f"MFE 中位数 {week_pct(t['mfe_med'])}。")
    win_t = [x for x in trades if x["pnl"] > 0 and x["testable"]]
    w50 = (sum(1 for x in win_t if x["gb"] > 0.5) / len(win_t)) if win_t else None
    verdict_txt.append(
        f"盈利单中回吐率 >50% 的占 <b>{week_pct(w50)}</b>（{len(win_t)} 笔盈利单）。")
    verdict_txt.append(
        f"全样本回吐金额 <b>¥{t['gb_amt']:,.0f}</b>，其中持有 ≥11 日的占 "
        f"<b>{week_pct(h1['c2'])}</b>、「亏损且持有 ≤10 日」的占 <b>{week_pct(h2['c1'])}</b>。")
    verdict_txt.append(
        f"<b>{p26['big_n']} 笔大赢家（MFE&gt;50%）贡献 {p26['big_pnl']:,.0f} 元</b>"
        f"（其余 {t['n'] - p26['big_n']} 笔合计 {p26['rest_pnl']:,.0f}），其回吐率中位仅 {week_pct(p26['big_gb_med'])}"
        f"——真正跑起来的票并没有吐多少；而「亏损且持有≤10 日」的 {h2['n']} 笔合计 {p26['short_loss_pnl']:,.0f} 元。")
    verdict_txt.append(
        f"结构事实：<b>{ts_stats['n']} 笔（{fpct(ts_stats['share'])}）的出口由引擎既有的 "
        f"<code>time_stop_noprofit</code>（<code>position.staledays=2</code>，「两天不赚就走」）决定</b>，"
        f"这批合计 {fsign(ts_stats['agg']['pnl'])}；其余 {t['n'] - ts_stats['n']} 笔合计 "
        f"{fsign(ts_stats['other']['pnl'])}。持有天数分布的 {ts_stats['spike_hold']} 日尖峰即由此产生。")
    data["conclusion"] = verdict_txt

    data["hyp_text"] = {
        "h1": f"H1 移动止损潜在价值大 —— {'成立' if h1['ok'] else '不成立'}",
        "h2": f"H2 出场不是出血口 —— {'成立' if h2['ok'] else '不成立'}",
        "h3": f"H3 复现「持有时间 = 唯一赚钱因子」—— {'复现' if h3['ok'] else '未复现'}",
    }
    return data


# ---------------------------------------------------------------- 渲染：通用零件
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
table{width:100%;border-collapse:collapse;font-size:13px;min-width:900px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
th{background:#fafbfc;color:#4b5565;font-size:12px;font-weight:600}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fcfcfd}
tr.empty td{background:#f4f5f7;color:#9aa1ad;font-style:italic}
tr.subset td{background:#f8f9fb;font-style:italic;color:#5a6472}
tr.sum td{background:#fafbfc;font-weight:700;border-top:2px solid #cfd4dc}
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
.toc{display:flex;gap:9px;flex-wrap:wrap;margin:18px 0}
.toc a{text-decoration:none;color:var(--blue);background:var(--blue2);border:1px solid #d4e0ee;border-radius:999px;padding:3px 12px;font-size:12.5px}
.foot{margin-top:38px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
.foot p{margin:5px 0}
code{background:var(--soft);border-radius:3px;padding:1px 5px;font-size:12.5px}
.hyp{display:grid;grid-template-columns:1fr;gap:10px}
.hyp-card{background:var(--surface);border:1px solid var(--line);border-left-width:4px;border-radius:8px;padding:14px 17px}
.hyp-card.ok{border-left-color:var(--red)}
.hyp-card.no{border-left-color:var(--green)}
.hyp-card .t{font-weight:700;font-size:15.5px}
.hyp-card .d{font-size:13px;color:var(--muted);margin-top:4px}
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


def main_table(rows, extra_subset=None, show_parent=False):
    head = ("<thead><tr><th>持有天数</th><th>笔数</th><th>可检验/不可检</th><th>胜率</th>"
            "<th>每单位净利</th><th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th>"
            "<th>剔除前三净利</th><th>回吐率中位</th><th>回吐率均值</th>"
            "<th>回吐金额</th><th>MFE 金额</th><th>净利</th><th>裁定</th>"
            + ("<th>所属带</th>" if show_parent else "") + "</tr></thead><tbody>")
    parts = ['<div class="table-wrap"><table>', head]
    for b in rows:
        if b["n"] == 0:
            parts.append(f'<tr class="empty"><td><strong>{b["label"]}</strong></td><td>0</td>'
                         f'<td colspan="13">无样本</td>'
                         f'<td>{vd_html(b.get("verdict", "样本不足"))}</td>'
                         + ("<td>—</td>" if show_parent else "") + "</tr>")
            continue
        conc = f'{b["conc"]:.1f}%' if b["conc"] is not None else "—"
        parts.append(
            f'<tr><td><strong>{b["label"]}</strong></td>'
            f'<td>{b["n"]}{" ⚠" if b["n"] < 30 else ""}</td>'
            f'<td class="muted">{b["n_tst"]} / {b["n_untst"]}</td>'
            f'<td>{fpct(b["wr"])}</td>'
            f'<td class="{cls(b["pu"])}">{fsign(b["pu"])}</td>'
            f'<td>{fpct(b["bewr"])}</td>'
            f'<td class="{cls(b["gap"])}">{fpp(b["gap"])}</td>'
            f'<td>{fnum(b["pf"], 2) if b["pf"] is not None else "—"}</td>'
            f'<td>{conc}</td>'
            f'<td class="{cls(b["pnl_ex_top3"])}">{fsign(b["pnl_ex_top3"])}</td>'
            f'<td>{week_pct(b["gb_med"])}</td>'
            f'<td>{week_pct(b["gb_mean"])}</td>'
            f'<td class="muted">{fnum(b["gb_amt"])}</td>'
            f'<td class="muted">{fnum(b["mfe_amt"])}</td>'
            f'<td class="{cls(b["pnl"])}">{fsign(b["pnl"])}</td>'
            f'<td>{vd_html(b.get("verdict", "—"))}</td>'
            + (f'<td class="muted">{b.get("parent", "—")}</td>' if show_parent else "")
            + "</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def subset_row(un):
    conc = f'{un["conc"]:.1f}%' if un["conc"] is not None else "—"
    return (f'<tr class="subset"><td><strong>{un["label"]}</strong>'
            f'<br><span class="small">（子集标记，非加总行）</span></td>'
            f'<td>{un["n"]}</td><td class="muted">0 / {un["n"]}</td>'
            f'<td>{fpct(un["wr"])}</td><td class="{cls(un["pu"])}">{fsign(un["pu"])}</td>'
            f'<td>{fpct(un["bewr"])}</td><td class="{cls(un["gap"])}">{fpp(un["gap"])}</td>'
            f'<td>{fnum(un["pf"], 2) if un["pf"] is not None else "—"}</td><td>{conc}</td>'
            f'<td class="{cls(un["pnl_ex_top3"])}">{fsign(un["pnl_ex_top3"])}</td>'
            f'<td colspan="3">回吐率无定义（MFE ≤ 0）</td>'
            f'<td class="muted">0</td>'
            f'<td class="{cls(un["pnl"])}">{fsign(un["pnl"])}</td>'
            f'<td>{vd_html("不可检验（从未浮盈）")}</td></tr>')


def hist_html(hist):
    mx = max((h["share"] or 0) for h in hist) or 1
    parts = ['<div class="panel"><h3>回吐率分布（可检验样本）</h3>']
    for h in hist:
        sh = h["share"] or 0
        color = "var(--gold)" if h["lo"] < 0 else (
            "var(--green)" if (h["hi"] is None or h["hi"] > 100) else "var(--blue)")
        parts.append(
            f'<div class="hist-row"><div class="hist-label">{h["label"]}</div>'
            f'<div class="hist-track"><b style="width:{max(sh / mx * 100, 0.4):.2f}%;background:{color}"></b></div>'
            f'<div class="hist-val">{h["n"]} 笔 · {week_pct(h["share"])}</div></div>')
    parts.append('<p class="small muted" style="margin-bottom:0">'
                 "0–100% 区间 = 浮盈部分回吐；100%+ = 浮盈全部吐出并倒亏；"
                 "&lt;0% 仅为浮点异常（应为 0 笔）。</p></div>")
    return "".join(parts)


def year_matrix_html(ym, with_gb=False):
    labels = [day_label(lo, hi) for lo, hi in BANDS_L1]
    parts = ['<div class="table-wrap"><table class="matrix"><thead><tr><th>年份</th><th>全年净利</th>']
    for lb in labels:
        parts.append(f"<th>{lb}</th>")
    parts.append("</tr></thead><tbody>")
    for y, d in ym.items():
        parts.append(f'<tr><td><strong>{y}</strong></td>'
                     f'<td class="{cls(d["overall"]["pnl"])}">{fsign(d["overall"]["pnl"])}</td>')
        for lb in labels:
            b = d["bands"][lb]
            if with_gb:
                if b["n_tst"] == 0:
                    parts.append('<td class="muted">—</td>')
                else:
                    parts.append(f'<td>{week_pct(b["gb_med"])}'
                                 f'<span class="muted small">({b["n_tst"]})</span></td>')
            else:
                parts.append(f'<td class="muted">{b["n_tst"]}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def year_pnl_matrix_html(trades):
    labels = [day_label(lo, hi) for lo, hi in BANDS_L1]
    years = sorted({t["year"] for t in trades})
    parts = ['<div class="table-wrap"><table class="matrix"><thead><tr><th>年份</th><th>全年净利</th>']
    for lb in labels:
        parts.append(f"<th>{lb}</th>")
    parts.append("</tr></thead><tbody>")
    for y in years:
        yt = [t for t in trades if t["year"] == y]
        tot = agg(yt)
        parts.append(f'<tr><td><strong>{y}</strong></td>'
                     f'<td class="{cls(tot["pnl"])}">{fsign(tot["pnl"])}</td>')
        for lo, hi in BANDS_L1:
            s = agg(band_rows(yt, lo, hi))
            if s["n"] == 0:
                parts.append('<td class="muted">—</td>')
            else:
                parts.append(f'<td class="{cls(s["pnl"])}">{fsign(s["pnl"])}'
                             f'<span class="muted small">({s["n"]})</span></td>')
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
            f'{"成立/复现" if ok else "不成立/未复现"}</span></div>'
            f'<div class="d">{detail}</div></div>')


# ---------------------------------------------------------------- 渲染：HTML
def render_html(data):
    t = data["totals"]
    f = data["funnel"]
    h1, h2, h3 = data["h1"], data["h2"], data["h3"]
    ts = data["time_stop"]
    p26 = data["p26"]
    bands = data["bands_l1"]

    kpis = [
        ("已平仓样本", f"{t['n']}", "2005–2018 · leg1 · 全部单腿"),
        ("可检验（MFE>0）", f"{t['n_tst']}", f"不可检验 {t['n_untst']} 笔（从未浮盈）"),
        ("回吐率中位数", week_pct(t["gb_med"]), f"P25 {week_pct(t['gb_p25'])} / P75 {week_pct(t['gb_p75'])}"),
        ("MFE 中位数", week_pct(t["mfe_med"]), "持仓期最大浮盈幅度"),
        ("回吐金额合计", f"¥{t['gb_amt']:,.0f}", "任何完美出场的收益上界"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="label">{a}</div><div class="value">{b}</div>'
        f'<div class="note">{c}</div></div>' for a, b, c in kpis)

    concl = "".join(f"<li>{x}</li>" for x in data["conclusion"])

    # H 判定详细
    h1_detail = (f"判据① 盈利单回吐率&gt;50% 占比 <b>{week_pct(h1['c1'])}</b>（需≥50%）｜"
                 f"判据② 回吐金额中持有≥11 日占比 <b>{week_pct(h1['c2'])}</b>（需≥50%）｜"
                 f"判据③ 21 日+ 每单位 <b>{fsign(h1['c3_detail']['pu'])}</b>"
                 f"、ratio_f <b>{fpct(h1['c3_detail']['ratio_f'])}</b>"
                 f"（Mf={h1['c3_detail']['Mf']}，需≥0.50 且 Mf≥8）")
    h2_detail = (f"判据① 回吐金额中「亏损且持有≤10 日」占比 <b>{week_pct(h2['c1'])}</b>（需≥50%）｜"
                 f"判据② 同批 MFE 金额占比 <b>{week_pct(h2['c2'])}</b>（需&lt;20%）｜"
                 f"该批 {h2['n']} 笔，MFE 金额 ¥{h2['mfe_amt']:,.0f}")
    h3_detail = (f"5 带每单位净利：" +
                 "｜".join(f"{bands[i]['label']} {fsign(h3['pus'][i])}" for i in range(5)) +
                 f"｜递增相邻对 <b>{h3['ups']}/{h3['pairs']}</b>（需≥4）")

    hyp_html = (hyp_card(data["hyp_text"]["h1"], h1["ok"], h1_detail)
                + hyp_card(data["hyp_text"]["h2"], h2["ok"], h2_detail)
                + hyp_card(data["hyp_text"]["h3"], h3["ok"], h3_detail))

    # 切片表
    sl = data["slices"]
    sl_rows = []
    for k in ["t1", "rest", "big", "normal"]:
        s = sl[k]
        conc = f'{s["conc"]:.1f}%' if s["conc"] is not None else "—"
        sl_rows.append(
            f'<tr><td><strong>{s["label"]}</strong></td><td>{s["n"]}</td>'
            f'<td class="muted">{s["n_tst"]}/{s["n_untst"]}</td>'
            f'<td>{fpct(s["wr"])}</td><td class="{cls(s["pu"])}">{fsign(s["pu"])}</td>'
            f'<td>{fpct(s["bewr"])}</td><td class="{cls(s["gap"])}">{fpp(s["gap"])}</td>'
            f'<td>{conc}</td>'
            f'<td class="{cls(s["pnl_ex_top3"])}">{fsign(s["pnl_ex_top3"])}</td>'
            f'<td>{week_pct(s["gb_med"])}</td>'
            f'<td class="muted">{fnum(s["gb_amt"])}</td>'
            f'<td class="muted">{fnum(s["mfe_amt"])}</td>'
            f'<td class="{cls(s["pnl"])}">{fsign(s["pnl"])}</td></tr>')

    # 退出方式切片
    cr_rows = []
    for s in data["by_close_reason"]:
        conc = f'{s["conc"]:.1f}%' if s["conc"] is not None else "—"
        cr_rows.append(
            f'<tr><td><strong>{s["label"]}</strong></td><td>{s["n"]}</td>'
            f'<td class="muted">{s["n_tst"]}/{s["n_untst"]}</td>'
            f'<td>{fpct(s["wr"])}</td><td class="{cls(s["pu"])}">{fsign(s["pu"])}</td>'
            f'<td>{conc}</td><td class="{cls(s["pnl"])}">{fsign(s["pnl"])}</td>'
            f'<td>{week_pct(s["gb_med"])}</td>'
            f'<td class="muted">{fnum(s["gb_amt"])}</td>'
            f'<td>{week_pct(s["mfe_med"])}</td></tr>')

    rb = data["robust_exright"]
    rb_txt = (f"剔除疑似除权污染 <b>{rb['n_removed']}</b> 笔后，可检验 {rb['n_tst']} 笔，"
              f"回吐率中位数 <b>{week_pct(rb['gb_med'])}</b>、回吐金额 ¥{rb['gb_amt']:,.0f}；"
              f"重裁 H1 → <b>{'成立' if rb['h1']['ok'] else '不成立'}</b>"
              f"（判据①{week_pct(rb['h1']['c1'])} / ②{week_pct(rb['h1']['c2'])}）、"
              f"H2 → <b>{'成立' if rb['h2']['ok'] else '不成立'}</b>"
              f"（①{week_pct(rb['h2']['c1'])} / ②{week_pct(rb['h2']['c2'])}）。")

    l2_html = (main_table(data["bands_l2"], show_parent=True)
               if data["bands_l2"] else
               '<div class="callout blue">L1 五带均未触发加密条件（框架 §5.4），'
               "本层无分段。触发带列表为空即说明各带内部无符号反转、无集中度恶化、无剔除前三翻负。</div>")
    l3_html = (main_table(data["bands_l3"], show_parent=True)
               if data["bands_l3"] else
               '<div class="callout blue">L3 未启用（L2 无段再次触发）。</div>')

    trig = "、".join(data["triggered_l1"]) or "无"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>P2-5 浮盈回吐分布（MFE）· 2005–2018</title>
<style>{CSS}</style></head><body><div class="page">
<header>
<div class="eyebrow">DARVAS · E17 · 实验报告 · 十维 #8/#9 何时卖</div>
<h1>P2-5 浮盈回吐分布（MFE）</h1>
<p class="sub">2005–2018 全 14 年已平仓交易的最大浮盈与回吐率分布。A 类后验、零回测，只读
<code>var/bt/bt_e17_&lt;year&gt;.db</code>。框架先于跑数定稿：<code>docs/experiments/e17_giveback_framework.md</code>。</p>
</header>

<div class="toc"><a href="#s0">核心结论</a><a href="#s1">口径与样本</a><a href="#s2">回吐率分布</a>
<a href="#s3">持有天数主表</a><a href="#s4">加密复核</a><a href="#s5">逐年矩阵</a>
<a href="#s6">切片</a><a href="#s7">结构性发现</a><a href="#s8">判定</a><a href="#s9">附录</a></div>

<div class="kpis">{kpi_html}</div>

<div class="summary" id="s0"><h2>核心结论</h2>
<ul>{concl}</ul>
<p class="small muted" style="margin-top:10px">读法：「回吐金额」是任何完美出场规则能回收的<b>上界</b>
（连卖在最高点都只能拿回这么多），不是收益预测。真实移动止损会在假信号上被反复切碎——那要 P2-6 的 B 类回测回答。</p>
</div>

<h2 id="s1">一、口径与样本</h2>
<div class="table-wrap"><table><thead><tr><th>口径</th><th style="text-align:left">定义</th></tr></thead><tbody>
<tr><td><code>cost_basis</code></td><td style="text-align:left">Σ_legs(qty×price) —— <b>不从 position.qty 取</b>：平仓后 qty 已清零（框架 §3.3 陷阱）</td></tr>
<tr><td><code>MFE</code></td><td style="text-align:left">max(high)/avg_cost − 1，窗口 [opened_date, closed_date] 含两端（日频上界）</td></tr>
<tr><td><code>ret</code></td><td style="text-align:left">realized_pnl / cost_basis（净额，含双边费用）</td></tr>
<tr><td><code>giveback</code></td><td style="text-align:left">(MFE − ret) / MFE，仅 MFE&gt;0 可检验；&gt;100% 表示浮盈全部吐出并倒亏</td></tr>
<tr><td><code>hold_bars</code></td><td style="text-align:left">该股 daily_bar 在窗口内的行数（主口径）；对照口径为 sh000001 交易日历</td></tr>
<tr><td>分带</td><td style="text-align:left">1–5 / 6–10 / 11–20 / 21–60 / &gt;60 交易日（照抄任务书预设，未按结果调整）</td></tr>
</tbody></table></div>

<div class="callout blue"><b>样本漏斗</b>：14 库 leg1 建仓 {f['leg1_all']} 笔 →
已平仓且成本可算 {f['closed']} 笔 → 窗口无行情剔除 {f['no_bar']} 笔 → <b>有效 {t['n']} 笔</b>。
唯一的剔除动作是「未平仓」与「无行情」；MFE≤0 的 {t['n_untst']} 笔不剔除，留在本带内并单列子集行。</div>

<div class="callout gold"><b>已知数据偏差（结论必须携带）</b>：库内 <code>adj_factor</code> 恒 1.0（<b>未复权</b>）。
持仓期若除权，除权前高价会抬高 MFE、把回吐率打成虚假的大。诊断口径：窗口内收盘单日跌幅 &lt; −20%（涨跌停 ±10% 下基本只能是除权/数据错误），
命中 <b>{f['exright']}</b> 笔。稳健性切片见 §八。此外 MFE 是日高不是可成交价（日频上界），且样本只含存续股票（幸存者偏差会<b>低估</b>回吐）。</div>

<h2 id="s2">二、回吐率分布</h2>
{hist_html(data['hist'])}
<div class="table-wrap"><table><thead><tr><th>回吐率箱</th><th>笔数</th><th>占比</th><th>净利</th>
<th>回吐金额</th><th>持有天数中位</th><th>胜率</th></tr></thead><tbody>
{''.join(f'<tr><td><strong>{h["label"]}</strong></td><td>{h["n"]}</td><td>{week_pct(h["share"])}</td>'
         f'<td class="{cls(h["pnl"])}">{fsign(h["pnl"])}</td>'
         f'<td class="muted">{fnum(h["gb_amt"])}</td>'
         f'<td>{fnum(h["hold_med"],1) if h["hold_med"] is not None else "—"}</td>'
         f'<td>{fpct(h["wr"])}</td></tr>' for h in data['hist'])}
</tbody></table></div>

<h2 id="s3">三、持有天数主表（L1 五带）</h2>
<p>四项固定口径 + PF + 缺口 + 剔除前三 + 回吐分布。表格下附「不可检验」子集行（<b>非加总行</b>）：
Σ 五带 = {t['n']} 笔；Σ 各带不可检验 = {sum(b['n_untst'] for b in bands)} 笔 = 子集行笔数。</p>
{main_table(bands)}
<div class="table-wrap"><table><thead><tr><th>持有天数</th><th>笔数</th><th>可检验/不可检</th><th>胜率</th>
<th>每单位净利</th><th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th><th>剔除前三净利</th>
<th>回吐统计</th><th>回吐金额</th><th>净利</th><th>裁定</th></tr></thead><tbody>
{subset_row(data['untestable'])}
</tbody></table></div>

<h2 id="s4">四、加密复核（L2/L3）</h2>
<p>触发带：<b>{trig}</b>。规则见框架 §5.4（n≥30 且 符号反转/集中度&gt;50%/剔除前三翻负），切法为宽度二分，非拟合拐点。</p>
<h3>L2（二分）</h3>
{l2_html}
<h3>L3（再二分）</h3>
{l3_html}

<h2 id="s5">五、逐年矩阵</h2>
<p>左表：逐格「净利（笔数）」。右表：逐格「回吐率中位数（可检验笔数）」——用于区分
「某一年全市场普遍大回吐」（如 2015）与「长期结构性回吐」。</p>
<h3>5.1 净利矩阵</h3>
{year_pnl_matrix_html(data['trades'])}
<h3>5.2 回吐率中位数矩阵</h3>
{year_matrix_html(data['year_matrix'], with_gb=True)}

<h2 id="s6">六、专项切片</h2>
<h3>6.1 t1 甜区（距 52 周新高 ≤15%）vs 其余</h3>
<div class="table-wrap"><table><thead><tr><th>切片</th><th>笔数</th><th>可检验/不可检</th><th>胜率</th>
<th>每单位净利</th><th>保本胜率</th><th>缺口</th><th>集中度</th><th>剔除前三净利</th>
<th>回吐率中位</th><th>回吐金额</th><th>MFE 金额</th><th>净利</th></tr></thead><tbody>
{''.join(sl_rows)}
</tbody></table></div>
<p class="small muted">前两行为 t1 与「非 t1」对照，后两行为大赢家（MFE&gt;50%）与其余对照。
进场甜区（第 5 维）与出场回吐（第 8/9 维）是两个维度，<b>不得据此说「甜区该止盈」</b>。</p>

<h2 id="s7">七、结构性发现：出口早有一条时间止损</h2>
<p>追查「持有天数分布为何在 <b>{ts['spike_hold']} 日</b>出现 {ts['spike_n']} 笔尖峰（占全样本 {ts['spike_n'] / t['n'] * 100:.0f}%）」时发现：
其中 <b>{ts['spike_ts']}</b> 笔由引擎既有的 <code>time_stop_noprofit</code> 规则平仓，而非自然持有期。
该规则（<code>internal/planner/planner.go:195-218</code>，参数 <code>position.staledays=2</code>）语义是：
<b>持有 ≥ staledays 个交易日 且 当日收盘 ≤ 成本 → 次日以当日收盘价清仓</b>。它的 reason 里带
<code>signal:"time_stop_noprofit"</code>，因此可以逐笔精确识别。</p>

<div class="callout red"><b>这意味着「持有天数是唯一赚钱因子」有一个内生性问题</b>：
不赚钱的单子会在跌破成本后一两天内被这条规则砍掉，只有<b>从未跌破成本</b>的单子才可能活成 21–60 日、60 日+ 的长持。
所以「长持 = 赚钱」部分是<b>规则筛出来的结果</b>，不完全是「拿得住所以赚钱」。
本节把这个结构摊开，是因为它直接决定了 P2-6 该测什么（见 §八）。</div>

<div class="table-wrap"><table><thead><tr><th>持有天数</th><th>总笔数</th><th>其中 time_stop 平仓</th>
<th>占比</th><th>time_stop 这部分净利</th><th>该带净利合计</th><th>该带每单位净利</th></tr></thead><tbody>
{''.join(f'<tr><td><strong>{b["label"]}</strong></td><td>{b["n"]}</td><td>{b["ts"]}</td>'
         f'<td>{fpct(b["share"])}</td>'
         f'<td class="{cls(b["ts_pnl"])}">{fsign(b["ts_pnl"])}</td>'
         f'<td class="{cls(bands[i]["pnl"])}">{fsign(bands[i]["pnl"])}</td>'
         f'<td class="{cls(bands[i]["pu"])}">{fsign(bands[i]["pu"])}</td></tr>'
         for i, b in enumerate(ts['by_band']))}
<tr class="sum"><td>合计</td><td>{t['n']}</td><td>{ts['n']}</td>
<td>{fpct(ts['share'])}</td><td class="{cls(ts['agg']['pnl'])}">{fsign(ts['agg']['pnl'])}</td>
<td class="{cls(t['pnl'])}">{fsign(t['pnl'])}</td><td class="{cls(t['pu'])}">{fsign(t['pu'])}</td></tr>
</tbody></table></div>

<p><b>time_stop 出口 {ts['n']} 笔（{fpct(ts['share'])}）合计净利 {fsign(ts['agg']['pnl'])}，
每单位 {fsign(ts['agg']['pu'])}、胜率 {fpct(ts['agg']['wr'])}、回吐率中位 {week_pct(ts['agg']['gb_med'])}
、MFE 中位 {week_pct(ts['agg']['mfe_med'])}；其余 {t['n'] - ts['n']} 笔合计 {fsign(ts['other']['pnl'])}
（每单位 {fsign(ts['other']['pu'])}、胜率 {fpct(ts['other']['wr'])}）。</b>
也就是说：<b>{fpct(ts['share'])} 的出口由一条「两天不赚就走」的规则决定</b>，这批合计
{fsign(ts['agg']['pnl'])}；而剩下的 {fpct(1 - ts['share'])} 出口合计 {fsign(ts['other']['pnl'])}。</p>

<h2 id="s8">八、判定</h2>
<div class="hyp">{hyp_html}</div>
<div class="callout {'red' if h1['ok'] else 'blue'}">
<b>P2-6 立项建议（按预注册判据 + 结构事实）</b><br>
① {('H1 成立：回吐主体在长持赢家（≥11 日占 ' + week_pct(h1['c2']) + '），「保护浮盈」有证据支撑。') if h1['ok'] else
('H1 <b>不成立</b>：回吐金额只有 ' + week_pct(h1['c2']) + ' 来自持有 ≥11 日（需 ≥50%），盈利单回吐率 >50% 的仅占 '
 + week_pct(h1['c1']) + '（需 ≥50%）。更进一步——盈利单回吐率中位 ' + week_pct(p26['win_gb_med'])
 + '、' + str(p26['big_n']) + ' 笔大赢家（MFE>50%）回吐率中位仅 ' + week_pct(p26['big_gb_med'])
 + '，这些正是移动止损最容易切碎的对象。<b>「用 trail% 保护浮盈」在本口径下没有证据支撑。</b>')}<br>
② {('H2 成立：回吐金额 ' + week_pct(p26['hit_share']) + ' 来自「亏损且持有≤10 日」，其 MFE 金额仅占 '
 + week_pct(p26['mfe_share']) + '（&lt;20%）——这批单的浮盈天花板本来就低，任何 trail 规则都<b>无盈可保</b>。'
 + '但请注意这个结论很薄：判据 ② 是 ' + week_pct(p26['mfe_share']) + ' vs 20% 的门槛，去掉疑似除权样本后为 '
 + week_pct(data['robust_exright']['h2']['c2']) + '，方向不变但余量不大。这批单合计净利 '
 + f"{p26['short_loss_pnl']:,.0f}" + ' 元，是账本最大的单一失血块。') if h2['ok'] else
('H2 <b>不成立</b>：回吐金额中「亏损且持有≤10 日」占 ' + week_pct(p26['hit_share']) + '，其 MFE 金额占 '
 + week_pct(p26['mfe_share']) + ' —— 回吐不是「本来就没浮盈」的机械结果，出场端仍有研究价值。')}<br>
③ <b>结构事实改变了标的</b>：{fpct(p26['ts_share'])} 的出口已经由 <code>time_stop_noprofit</code>
（<code>position.staledays=2</code>）决定，它的实质就是一条「两天不赚就走」的时间止损。
因此 P2-6 的<b>第一优先不是 trail 5/8/10/15%，而是给 <code>position.staledays</code> 做梯度
（0/2/3/5/10）</b>——这是十维 #9 里唯一「已实现、零工程成本、却从未被验证」的旋钮，
且它直接解释了 6–10 日带 58% 的样本与最大失血块。<br>
④ 若 ③ 的梯度显示 staledays 越大越好，则矛盾会转向入场端（P1-4）：说明真正的问题不是「什么时候砍」，
而是「为什么买到的票两天就跌破成本」。
</div>

<h3>8.1 逐带裁定</h3>
<div class="table-wrap"><table><thead><tr><th>持有天数</th><th>n</th><th>Mf</th><th>P′/M′</th>
<th>最长连续同号年</th><th>正号年净利占比</th><th>裁定</th></tr></thead><tbody>
{''.join(f'<tr><td><strong>{b["label"]}</strong></td><td>{b["n"]}</td>'
         f'<td>{b["cons"]["Mf"]}</td>'
         f'<td>{b["cons"]["Pf"]}/{b["cons"]["Mf"]}'
         f'<span class="muted small">（{fpct(b["cons"]["ratio_f"])}）</span></td>'
         f'<td>{b["cons"]["longest_run"]}</td>'
         f'<td>{share_pct(b["cons"]["pos_share"])}</td>'
         f'<td>{vd_html(b["verdict"])}</td></tr>' for b in bands)}
</tbody></table></div>

<h2 id="s9">九、附录</h2>
<h3>9.1 稳健性：剔除疑似除权污染</h3>
<div class="callout blue">{rb_txt}</div>
<h3>9.2 退出方式切片（不作判据）</h3>
<div class="table-wrap"><table><thead><tr><th>close_reason</th><th>笔数</th><th>可检验/不可检</th>
<th>胜率</th><th>每单位净利</th><th>集中度</th><th>净利</th><th>回吐率中位</th>
<th>回吐金额</th><th>MFE 中位</th></tr></thead><tbody>
{''.join(cr_rows)}
</tbody></table></div>
<p class="small muted">引擎的 <code>stop</code> 可能同时包含初始止损与移动止损，语义未核实，<b>只作观察不作判据</b>（框架 §9）。</p>

<h3>9.3 HARD 校验</h3>
{calib_html(data['calibration'])}

<h3>9.4 复现</h3>
<div class="panel"><p class="small">生成器：<code>python scripts/e17_giveback.py</code>（先 <code>--check-only</code> 校准）<br>
独立校验：<code>python scripts/e17_giveback_verify.py</code>（3 笔手算抽查，不复用生成器函数）<br>
sha256（前 16 位）：<code>{data['meta']['generator_sha256']}</code><br>
数据源 mtime：{', '.join(f'{k} {v}' for k, v in list(data['meta']['db_mtimes'].items())[:3])} …</p></div>

<div class="foot">
<p><b>结论边界</b>：本报告只量化「回吐了多少」，不预测「移动止损能赚多少」。任何「理论上可多赚」都是上限，真实规则会在假信号上被切碎（框架 §1.2）。</p>
<p>单因子轮换：本实验只动「持有天数」一个分组变量，其余九维固定（docs/29 §2.3）。样本外（2019+）未参与。</p>
<p>生成于 {data['meta']['generated_at']} · {data['meta']['unit_note']}</p>
</div>
</div></body></html>"""


# ---------------------------------------------------------------- 渲染：MD
def render_md(data):
    t = data["totals"]
    f = data["funnel"]
    h1, h2, h3 = data["h1"], data["h2"], data["h3"]
    L = []
    A = L.append
    A("# P2-5 浮盈回吐分布（MFE）· 2005–2018")
    A("")
    A("> A 类后验、零回测。框架：`docs/experiments/e17_giveback_framework.md`（零结果纯规则）。")
    A("")
    A(f"生成时间：{data['meta']['generated_at']} · 生成器 `{data['meta']['generator']}` "
      f"sha256 `{data['meta']['generator_sha256']}`")
    A("")
    A("## 核心结论")
    A("")
    for x in data["conclusion"]:
        A(f"- {x.replace('<b>', '**').replace('</b>', '**')}")
    A("")
    A(f"回吐金额是所有完美出场规则的收益**上界**，不是收益预测。")
    A("")
    A("## 一、口径与样本")
    A("")
    A("| 口径 | 定义 |")
    A("|---|---|")
    A("| cost_basis | Σ_legs(qty×price)，不从 position.qty 取（平仓后已清零） |")
    A("| MFE | max(high)/avg_cost − 1，窗口 [opened, closed] 含两端 |")
    A("| ret | realized_pnl / cost_basis |")
    A("| giveback | (MFE − ret)/MFE，仅 MFE>0 可检验 |")
    A("| hold_bars | 该股 daily_bar 窗口内行数（对照：sh000001 交易日历） |")
    A("")
    A(f"样本漏斗：leg1 建仓 {f['leg1_all']} → 已平仓 {f['closed']} → 无行情剔除 {f['no_bar']} → "
      f"**有效 {t['n']}**。不可检验（MFE≤0）{t['n_untst']} 笔保留在本带内，另设子集行。")
    A("")
    A(f"数据偏差：未复权（adj_factor≡1.0），疑似除权污染 {f['exright']} 笔；MFE 为日频上界；幸存者偏差低估回吐。")
    A("")
    A("## 二、回吐率分布")
    A("")
    A("| 回吐率箱 | 笔数 | 占比 | 净利 | 回吐金额 | 持有中位 | 胜率 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for h in data["hist"]:
        A(f"| {h['label']} | {h['n']} | {week_pct(h['share'])} | {fsign(h['pnl'])} | "
          f"{fnum(h['gb_amt'])} | {fnum(h['hold_med'],1)} | {fpct(h['wr'])} |")
    A("")
    A("## 三、持有天数主表（L1）")
    A("")
    A("| 持有天数 | 笔数 | 可检/不可检 | 胜率 | 每单位净利 | 保本胜率 | 缺口 | PF | 集中度 | 剔除前三 | 回吐率中位 | 回吐金额 | MFE金额 | 净利 | 裁定 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for b in data["bands_l1"]:
        conc = f"{b['conc']:.1f}%" if b["conc"] is not None else "—"
        A(f"| {b['label']} | {b['n']} | {b['n_tst']}/{b['n_untst']} | {fpct(b['wr'])} | "
          f"{fsign(b['pu'])} | {fpct(b['bewr'])} | {fpp(b['gap'])} | "
          f"{fnum(b['pf'],2)} | {conc} | {fsign(b['pnl_ex_top3'])} | "
          f"{week_pct(b['gb_med'])} | {fnum(b['gb_amt'])} | {fnum(b['mfe_amt'])} | "
          f"{fsign(b['pnl'])} | {b['verdict']} |")
    un = data["untestable"]
    A(f"| *{un['label']}（子集，非加总行）* | {un['n']} | 0/{un['n']} | {fpct(un['wr'])} | "
      f"{fsign(un['pu'])} | {fpct(un['bewr'])} | {fpp(un['gap'])} | {fnum(un['pf'],2)} | "
      f"— | {fsign(un['pnl_ex_top3'])} | — | 0 | — | {fsign(un['pnl'])} | 不可检验 |")
    A("")
    A(f"加总恒等式：Σ 五带 = {t['n']}；Σ 各带不可检验 = {sum(b['n_untst'] for b in data['bands_l1'])} = 子集行 {un['n']}。")
    A("")
    A("## 四、加密复核")
    A("")
    A(f"触发带：{'、'.join(data['triggered_l1']) or '无'}")
    A("")
    if data["bands_l2"]:
        A("| L2 段 | 所属带 | 笔数 | 可检/不可检 | 每单位净利 | 剔除前三 | 回吐率中位 | 裁定 |")
        A("|---|---|---:|---:|---:|---:|---:|---|")
        for b in data["bands_l2"]:
            A(f"| {b['label']} | {b['parent']} | {b['n']} | {b['n_tst']}/{b['n_untst']} | "
              f"{fsign(b['pu'])} | {fsign(b['pnl_ex_top3'])} | {week_pct(b['gb_med'])} | {b['verdict']} |")
    else:
        A("L1 五带均未触发加密条件。")
    A("")
    if data["bands_l3"]:
        A("### L3")
        A("")
        A("| L3 段 | 所属 L2 | 笔数 | 每单位净利 | 回吐率中位 | 裁定 |")
        A("|---|---|---:|---:|---:|---|")
        for b in data["bands_l3"]:
            A(f"| {b['label']} | {b['parent']} | {b['n']} | {fsign(b['pu'])} | "
              f"{week_pct(b['gb_med'])} | {b['verdict']} |")
        A("")
    A("## 五、逐年矩阵")
    A("")
    labels = [day_label(lo, hi) for lo, hi in BANDS_L1]
    A("### 5.1 净利（笔数）")
    A("")
    A("| 年份 | 全年 | " + " | ".join(labels) + " |")
    A("|---|---:|" + "---:|" * len(labels))
    for y in sorted({x["year"] for x in data["trades"]}):
        yt = [x for x in data["trades"] if x["year"] == y]
        tot = agg(yt)
        cells = []
        for lo, hi in BANDS_L1:
            s = agg(band_rows(yt, lo, hi))
            cells.append(f"{fsign(s['pnl'])}（{s['n']}）" if s["n"] else "—")
        A(f"| {y} | {fsign(tot['pnl'])} | " + " | ".join(cells) + " |")
    A("")
    A("### 5.2 回吐率中位数（可检验笔数）")
    A("")
    A("| 年份 | " + " | ".join(labels) + " |")
    A("|---|" + "---:|" * len(labels))
    for y, d in data["year_matrix"].items():
        cells = []
        for lb in labels:
            b = d["bands"][lb]
            cells.append(f"{week_pct(b['gb_med'])}（{b['n_tst']}）" if b["n_tst"] else "—")
        A(f"| {y} | " + " | ".join(cells) + " |")
    A("")
    A("## 六、专项切片")
    A("")
    A("| 切片 | 笔数 | 可检/不可检 | 胜率 | 每单位净利 | 剔除前三 | 回吐率中位 | 回吐金额 | MFE金额 | 净利 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k in ["t1", "rest", "big", "normal"]:
        s = data["slices"][k]
        A(f"| {s['label']} | {s['n']} | {s['n_tst']}/{s['n_untst']} | {fpct(s['wr'])} | "
          f"{fsign(s['pu'])} | {fsign(s['pnl_ex_top3'])} | {week_pct(s['gb_med'])} | "
          f"{fnum(s['gb_amt'])} | {fnum(s['mfe_amt'])} | {fsign(s['pnl'])} |")
    A("")
    A("## 七、结构性发现：出口早有一条时间止损")
    A("")
    ts = data["time_stop"]
    A(f"持有天数分布在 **{ts['spike_hold']} 日**出现 {ts['spike_n']} 笔尖峰"
      f"（占全样本 {ts['spike_n'] / t['n'] * 100:.0f}%），其中 {ts['spike_ts']} 笔由引擎既有 "
      f"`time_stop_noprofit` 规则平仓。规则（`internal/planner/planner.go:195-218`，"
      f"`position.staledays=2`）：**持有 ≥2 个交易日且收盘 ≤ 成本 → 次日以收盘价清仓**。")
    A("")
    A(f"**time_stop 出口 {ts['n']} 笔（{fpct(ts['share'])}）合计 {fsign(ts['agg']['pnl'])}**，"
      f"每单位 {fsign(ts['agg']['pu'])}、胜率 {fpct(ts['agg']['wr'])}、回吐率中位 "
      f"{week_pct(ts['agg']['gb_med'])}；其余 {t['n'] - ts['n']} 笔合计 {fsign(ts['other']['pnl'])}"
      f"（每单位 {fsign(ts['other']['pu'])}、胜率 {fpct(ts['other']['wr'])}）。")
    A("")
    A("**内生性问题**：不赚钱的单子跌破成本后一两天就被砍掉，只有从未跌破成本的单子才能活成长持 → "
      "「长持=赚钱」部分是规则筛出来的结果。这条直接决定 P2-6 该测什么。")
    A("")
    A("| 持有天数 | 总笔数 | time_stop 平仓 | 占比 | 其中净利 | 该带净利 | 该带每单位 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for i, b in enumerate(ts["by_band"]):
        A(f"| {b['label']} | {b['n']} | {b['ts']} | {fpct(b['share'])} | {fsign(b['ts_pnl'])} | "
          f"{fsign(data['bands_l1'][i]['pnl'])} | {fsign(data['bands_l1'][i]['pu'])} |")
    A(f"| **合计** | {t['n']} | {ts['n']} | {fpct(ts['share'])} | {fsign(ts['agg']['pnl'])} | "
      f"{fsign(t['pnl'])} | {fsign(t['pu'])} |")
    A("")
    A("## 八、判定")
    A("")
    A(f"### {data['hyp_text']['h1']}")
    A("")
    A(f"- 判据① 盈利单回吐率>50% 占比：**{week_pct(h1['c1'])}**（需 ≥50%）")
    A(f"- 判据② 回吐金额中持有≥11 日占比：**{week_pct(h1['c2'])}**（需 ≥50%）")
    A(f"- 判据③ 21 日+ 每单位 {fsign(h1['c3_detail']['pu'])}、ratio_f "
      f"{fpct(h1['c3_detail']['ratio_f'])}（Mf={h1['c3_detail']['Mf']}，需 ≥0.50 且 Mf≥8）")
    A("")
    A(f"### {data['hyp_text']['h2']}")
    A("")
    A(f"- 判据① 回吐金额中「亏损且持有≤10 日」占比：**{week_pct(h2['c1'])}**（需 ≥50%）")
    A(f"- 判据② 同批 MFE 金额占比：**{week_pct(h2['c2'])}**（需 <20%），该批 {h2['n']} 笔")
    A("")
    A(f"### {data['hyp_text']['h3']}")
    A("")
    A("5 带每单位净利：" + "｜".join(
        f"{data['bands_l1'][i]['label']} {fsign(h3['pus'][i])}" for i in range(5)))
    A("")
    A(f"递增相邻对 **{h3['ups']}/{h3['pairs']}**（需 ≥4）")
    A("")
    A("### 逐带裁定")
    A("")
    A("| 持有天数 | n | Mf | P′/M′ | 最长同号 | 正号年净利占比 | 裁定 |")
    A("|---|---:|---:|---:|---:|---:|---|")
    for b in data["bands_l1"]:
        A(f"| {b['label']} | {b['n']} | {b['cons']['Mf']} | "
          f"{b['cons']['Pf']}/{b['cons']['Mf']} | {b['cons']['longest_run']} | "
          f"{share_pct(b['cons']['pos_share'])} | {b['verdict']} |")
    A("")
    p26 = data["p26"]
    A("### P2-6 立项建议")
    A("")
    A(f"- **H1 不成立**意味着「用 trail% 保护浮盈」无证据：盈利单回吐率中位 {week_pct(p26['win_gb_med'])}，"
      f"{p26['big_n']} 笔大赢家回吐率中位仅 {week_pct(p26['big_gb_med'])}。")
    A(f"- **H2 成立**（薄）：回吐金额 {week_pct(p26['hit_share'])} 来自亏损短持单，其 MFE 金额仅 "
      f"{week_pct(p26['mfe_share'])}；这批单合计 {p26['short_loss_pnl']:,.0f} 元，是账本最大单一失血块。")
    A(f"- **标的应改成 `position.staledays` 梯度（0/2/3/5/10）**：{fpct(p26['ts_share'])} 的出口已由它决定。")
    A("")
    A("## 九、附录")
    A("")
    rb = data["robust_exright"]
    A(f"### 8.1 剔除疑似除权污染（{rb['n_removed']} 笔）")
    A("")
    A(f"可检验 {rb['n_tst']} 笔，回吐率中位 {week_pct(rb['gb_med'])}，回吐金额 ¥{rb['gb_amt']:,.0f}；"
      f"H1 → {'成立' if rb['h1']['ok'] else '不成立'}（①{week_pct(rb['h1']['c1'])}/②{week_pct(rb['h1']['c2'])}），"
      f"H2 → {'成立' if rb['h2']['ok'] else '不成立'}（①{week_pct(rb['h2']['c1'])}/②{week_pct(rb['h2']['c2'])}）。")
    A("")
    A("### 9.2 退出方式切片（不作判据）")
    A("")
    A("| close_reason | 笔数 | 胜率 | 每单位净利 | 净利 | 回吐率中位 | MFE 中位 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for s in data["by_close_reason"]:
        A(f"| {s['label']} | {s['n']} | {fpct(s['wr'])} | {fsign(s['pu'])} | "
          f"{fsign(s['pnl'])} | {week_pct(s['gb_med'])} | {week_pct(s['mfe_med'])} |")
    A("")
    A("### 9.3 HARD 校验")
    A("")
    A("| 级别 | 校验项 | 期望 | 实际 | 结果 |")
    A("|---|---|---|---|---|")
    for c in data["calibration"]:
        A(f"| {c['level']} | {c['name']} | {c['expected']} | {c['actual']} | "
          f"{'✓' if c['ok'] else '✗'} |")
    A("")
    A("## 结论边界")
    A("")
    A("- 只量化「回吐了多少」，不预测「移动止损能赚多少」；回吐金额是上限，真实规则会被假信号切碎。")
    A("- 单因子轮换：只动「持有天数」，其余九维固定；样本外（2019+）未参与。")
    A(f"- 未复权（adj_factor≡1.0）：疑似除权污染 {f['exright']} 笔，稳健性见 §8.1。")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main
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
        print("确认漂移合理后可用 --allow-drift 强制生成（须在报告附 A 记录原因）。")
        return 1

    EXP.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(render_html(data), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    stats = {k: v for k, v in data.items() if k not in ("meta", "trades")}
    OUT_JSON.write_text(json.dumps({"meta": data["meta"], "stats": stats,
                                    "trades": data["trades"]},
                                   ensure_ascii=False, indent=1), encoding="utf-8")

    t = data["totals"]
    print(f"样本 {t['n']}（可检验 {t['n_tst']} / 不可检验 {t['n_untst']}）")
    print(f"回吐率中位 {week_pct(t['gb_med'])}，回吐金额 ¥{t['gb_amt']:,.0f}")
    for b in data["bands_l1"]:
        print(f"  {b['label']:<12} n={b['n']:<4} 每单位 {fsign(b['pu']):>9} "
              f"回吐中位 {week_pct(b['gb_med']):>7}  裁定 {b['verdict']}")
    print(f"H1 {data['h1']['ok']} / H2 {data['h2']['ok']} / H3 {data['h3']['ok']}")
    print(f"已写入 {OUT_HTML.name} / {OUT_MD.name} / {OUT_JSON.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
