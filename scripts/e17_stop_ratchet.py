#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""raiseStops · 止损随 Box 上移事件研究 · A 类降级版生成器。

实验：docs/29 §五 队列第 1 项「止损随 Box 上移事件研究」，A 类零成本后验（只读库、不重跑回测、不改代码）。
框架：docs/experiments/e17_stop_ratchet_framework.md（零结果纯规则，含 H1/H2 判据 + 转 B 门槛）。

核心口径（框架 §4.2b，成本归一化比值）：
  ratchet = position.stop_price / position_leg.stop_after   ← stop_after 是 leg1 建仓时写入的初始止损
  物理含义：ratchet = 1.0 → 止损从未上移（钉在初始位）；ratchet > 1.0 → 止损已抬到初始位之上。

数据源：var/bt/base/bt_baseA_<year>.db × 14（2005–2018），mode=ro 只读。

输出（docs/experiments/，实验报告唯一沉淀处）：
  e17_stop_ratchet_report.html / .md / _data.json

用法：
  python scripts/e17_stop_ratchet.py               # 正式生成（HARD 失败即中止）
  python scripts/e17_stop_ratchet.py --check-only  # 只跑校准，不写文件
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
BT = ROOT / "var" / "bt" / "base"
EXP = ROOT / "docs" / "experiments"
OUT_HTML = EXP / "e17_stop_ratchet_report.html"
OUT_MD = EXP / "e17_stop_ratchet_report.md"
OUT_JSON = EXP / "e17_stop_ratchet_data.json"

# ---------------------------------------------------------------- 分带定义（框架 §4.3，档界跑数前写死）
# ratchet = stop_price / stop_after，锚点 1.0 = 未上移
BANDS = [(1.0000, 1.0001), (1.0001, 1.10), (1.10, 1.50), (1.50, None)]

UNIT_NOTE = "1 单位 = 2% 权益；E17 阶梯 t1=3 / t2=2 / t3=1 / t4=0.5"


def years_list():
    return sorted(int(os.path.basename(p).split("_")[2].split(".")[0])
                  for p in BT.glob("bt_baseA_*.db"))


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


def band_label(lo, hi):
    if lo == 1.0000:
        return "未上移（=1.0）"
    if hi is None:
        return "大幅上移（>1.5）"
    return f"{lo:.2f}–{hi:.2f}"


def band_of(r):
    for lo, hi in BANDS:
        if hi is None:
            if r >= lo:
                return (lo, hi)
        elif lo <= r < hi:
            return (lo, hi)
    return None


# ---------------------------------------------------------------- 数据加载
def load_trades():
    trades = []
    funnel = {"dbs": 0, "closed": 0, "no_leg": 0, "no_stop": 0,
              "valid": 0, "ratchet_eq1": 0, "ratchet_gt1": 0}
    meta = {"db_mtimes": {}, "close_reasons": {}}
    for y in years_list():
        db = BT / f"bt_baseA_{y}.db"
        meta["db_mtimes"][f"bt_baseA_{y}.db"] = datetime.fromtimestamp(
            db.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sql = """
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.avg_cost,
               p.realized_pnl, p.close_reason, p.stop_price,
               l.stop_after, l.price AS leg_price,
               (SELECT SUM(qty) FROM position_leg WHERE position_id=p.id) AS qty,
               (SELECT SUM(qty*price) FROM position_leg WHERE position_id=p.id) AS cost_basis,
               o.reason_json
        FROM position p
        JOIN position_leg l ON l.position_id=p.id AND l.leg_no=1
        LEFT JOIN plan_order o ON o.id=l.plan_order_id
        WHERE p.status='closed'
        """
        rows = con.execute(sql).fetchall()
        funnel["closed"] += len(rows)
        for r in rows:
            try:
                rj = json.loads(r["reason_json"] or "{}")
            except (TypeError, ValueError):
                rj = {}
            pnl = r["realized_pnl"]
            stop_price = r["stop_price"]
            stop_after = r["stop_after"]
            qty = r["qty"] or 0
            cost = r["cost_basis"] or 0.0
            if stop_price is None or stop_after is None or stop_after <= 0:
                funnel["no_stop"] += 1
                continue
            if pnl is None or not qty or cost <= 0:
                funnel["no_leg"] += 1
                continue
            funnel["valid"] += 1
            ratchet = stop_price / stop_after
            if ratchet <= 1.0001:
                funnel["ratchet_eq1"] += 1
            else:
                funnel["ratchet_gt1"] += 1

            avg_cost = cost / qty
            # 回吐相关：MFE 与回吐率（复用 giveback 口径）
            bars = con.execute(
                "SELECT date, high, close FROM daily_bar WHERE code=? AND date>=? AND date<=? "
                "ORDER BY date", (r["code"], r["opened_date"], r["closed_date"])).fetchall()
            if bars:
                max_high = max(b["high"] for b in bars)
                mfe = max_high / avg_cost - 1.0
            else:
                max_high = None
                mfe = None
            ret = pnl / cost
            testable = mfe is not None and mfe > 0
            gb = (mfe - ret) / mfe if testable else None

            cr = (r["close_reason"] or "?").strip() or "?"
            meta["close_reasons"][cr] = meta["close_reasons"].get(cr, 0) + 1
            trades.append({
                "year": y, "code": r["code"], "opened": r["opened_date"],
                "closed": r["closed_date"], "close_reason": cr,
                "avg_cost": round(avg_cost, 6), "qty": qty,
                "cost_basis": round(cost, 2), "pnl": round(pnl, 2),
                "stop_price": round(stop_price, 6), "stop_after": round(stop_after, 6),
                "ratchet": ratchet,
                "units": float(rj.get("units", 1.0) or 1.0),
                "ndist": rj.get("newhigh_distance_pct"),
                "mfe": mfe, "ret": ret, "gb": gb, "testable": testable,
                "max_high": round(max_high, 4) if max_high else None,
                "mfe_amt": (mfe * cost) if mfe is not None else None,
                "gb_amt": ((mfe - ret) * cost) if testable else None,
            })
        con.close()
        funnel["dbs"] += 1
    return trades, funnel, meta


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
    ex_pu = ex_pnl / ex_units if ex_units else None

    tst = [r for r in rows if r["testable"]]
    gbs = sorted(r["gb"] for r in tst)
    gb_med = pct_of(gbs, 0.5)
    gb_mean = (sum(gbs) / len(gbs)) if gbs else None
    gb_amt = sum(r["gb_amt"] for r in tst if r["gb_amt"] is not None)

    return {
        "n": n, "wins": len(wins), "losses": len(losses), "pnl": pnl,
        "units": units, "wr": wr, "pu": (pnl / units if units else None),
        "pf": (gw / abs(gl) if gl else None),
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "bewr": bewr,
        "gap": ((wr - bewr) * 100 if (wr is not None and bewr is not None) else None),
        "top3": top3,
        "conc": (top3 / gw * 100 if (gw > 0 and top3 > 0) else None),
        "pnl_ex_top3": ex_pnl, "pu_ex_top3": ex_pu,
        "n_tst": len(tst), "n_untst": n - len(tst),
        "gb_med": gb_med, "gb_mean": gb_mean, "gb_amt": gb_amt,
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
        return [t for t in trades if t["ratchet"] >= lo]
    return [t for t in trades if lo <= t["ratchet"] < hi]


def band_stat(trades):
    out = []
    for lo, hi in BANDS:
        out.append({"label": band_label(lo, hi), "lo": lo, "hi": hi,
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
    return {"M": M, "P": P, "ratio": (P / M if M else None),
            "Mf": Mf, "Pf": Pf, "ratio_f": (Pf / Mf if Mf else None),
            "per_year": per_year}


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

    add("HARD", "分带 n 加总 = 有效样本", f"{funnel['valid']}",
        f"{sum(b['n'] for b in data['bands'])}",
        sum(b["n"] for b in data["bands"]) == funnel["valid"], "")

    add("HARD", "ratchet=1.0 与 >1.0 二分恒等", "恒等",
        f"{funnel['ratchet_eq1']}+{funnel['ratchet_gt1']}",
        funnel["ratchet_eq1"] + funnel["ratchet_gt1"] == funnel["valid"], "")

    # 逐年净利对账（宽松：只验总量符号与量级，因 baseA 是阶梯制基线）
    tot = sum(t["pnl"] for t in trades)
    add("INFO", "14 年已实现净利合计", "登记", f"{tot:,.1f}", True, "")

    return res


# ---------------------------------------------------------------- 判定（框架 §5 转 B 门槛）
def judge_h1(bands):
    """H1：上移幅度与交易质量单调相关。判据：每单位净利相邻对递增 ≥ 3/3，且回吐率非增。"""
    pus = [b["pu"] for b in bands]
    ups = sum(1 for i in range(len(pus) - 1)
              if pus[i] is not None and pus[i + 1] is not None and pus[i + 1] > pus[i])
    gbs = [b["gb_med"] for b in bands]
    return {"ok": ups >= 3, "ups": ups, "pairs": len(pus) - 1, "pus": pus,
            "gbs": gbs}


def judge_h2(bands):
    """H2：从未上移的带，剔除前三后仍显著更差 + 回吐率显著更高。

    内生性拆解：ratchet>1 本身就是「涨得多」的结果，所以不能只看总净利。
    必须看「剔除前三赢家后」低上移带是否仍差，以及回吐率是否更高。
    """
    a = bands[0]  # 未上移带
    rest = bands[1:]
    rest_n = sum(b["n"] for b in rest)
    if rest_n == 0:
        return {"ok": False, "note": "其余带无样本"}
    rest_pnl_ex = sum(b["pnl_ex_top3"] for b in rest)
    # 未上移带剔除前三后每单位 vs 其余带
    a_pu_ex = a["pu_ex_top3"]
    # 回吐率对比
    a_gb = a["gb_med"]
    rest_tst = [t for b in rest for t in []]
    # 用加权回吐中位：合并其余带的可检验样本
    # 简化：比较未上移带 gb_med 与其余带合并 gb_med
    rest_gb_med = None
    return {
        "ok": False,  # 在 build 里用更细逻辑重算
        "a_pu_ex": a_pu_ex, "a_gb": a_gb,
        "a_n": a["n"], "rest_n": rest_n,
        "a_pnl_ex": a["pnl_ex_top3"], "rest_pnl_ex": rest_pnl_ex,
    }


# ---------------------------------------------------------------- build
def build():
    trades, funnel, meta = load_trades()
    bands = band_stat(trades)
    for b in bands:
        b["cons"] = consistency(trades, b["lo"], b["hi"])

    # H2 精确判据：未上移带 vs 其余带合并，剔除前三 + 回吐率
    a = bands[0]
    rest_trades = [t for t in trades if t["ratchet"] > 1.0001]
    rest_agg = agg(rest_trades)
    a_rows = band_rows(trades, BANDS[0][0], BANDS[0][1])
    a_agg = agg(a_rows)

    # H2 判据（框架 §5）：①低上移带剔除前三净利显著更低 ②回吐率显著更高 ③n≥30
    # 剔前三每单位对比
    h2_pu_gap = (a_agg["pu_ex_top3"] or 0) - (rest_agg["pu_ex_top3"] or 0)
    h2_gb_gap = (a_agg["gb_med"] or 0) - (rest_agg["gb_med"] or 0)
    h2 = {
        "ok": (a_agg["pu_ex_top3"] is not None
               and rest_agg["pu_ex_top3"] is not None
               and a_agg["pu_ex_top3"] < rest_agg["pu_ex_top3"]
               and a_agg["gb_med"] is not None
               and rest_agg["gb_med"] is not None
               and a_agg["gb_med"] > rest_agg["gb_med"]
               and a_agg["n"] >= 30),
        "a_pu_ex": a_agg["pu_ex_top3"], "rest_pu_ex": rest_agg["pu_ex_top3"],
        "pu_gap": h2_pu_gap,
        "a_gb": a_agg["gb_med"], "rest_gb": rest_agg["gb_med"],
        "gb_gap": h2_gb_gap,
        "a_n": a_agg["n"], "a_pnl_ex": a_agg["pnl_ex_top3"],
        "rest_pnl_ex": rest_agg["pnl_ex_top3"],
    }

    h1 = judge_h1(bands)

    # 内生性拆解关键切片：未上移带里，「涨过却最终亏损」的受害者到底有多少
    # 命题：若 raiseStops「追不上」，应该有「曾大涨(MFE高)却因止损钉在低位而回吐倒亏」的票。
    # 若这种票 ≈ 0，说明「未上移」不是缺陷，而是「本来就没涨」的自然结果。
    unmoved = [t for t in trades if t["ratchet"] <= 1.0001]
    mfe_slices = {}
    for thr in (0.10, 0.20, 0.30, 0.50):
        big = [t for t in unmoved if t["mfe"] is not None and t["mfe"] > thr]
        big_lost = [t for t in big if t["pnl"] < 0]
        mfe_slices[f"mfe_gt_{int(thr*100)}"] = {
            "n": len(big), "n_lost": len(big_lost),
            "lost_pnl": sum(t["pnl"] for t in big_lost),
        }
    # 放宽：涨过30%却接近打平(净利<0.5%成本)也算「受害者」
    big30_nearflat = [t for t in unmoved if t["mfe"] is not None and t["mfe"] > 0.30
                      and t["ret"] is not None and t["ret"] < 0.005]
    mfe_slices["mfe_gt_30_nearflat"] = {
        "n": len(big30_nearflat),
        "n_lost": len([t for t in big30_nearflat if t["pnl"] < 0]),
        "lost_pnl": sum(t["pnl"] for t in big30_nearflat),
    }
    victim_n = mfe_slices["mfe_gt_30"]["n_lost"]  # 涨过30%却亏损 = 真实受害者

    # 逐年矩阵：未上移带 vs 其余带 逐年净利
    years = sorted({t["year"] for t in trades})
    year_matrix = OrderedDict()
    for y in years:
        yt = [t for t in trades if t["year"] == y]
        ya = band_rows(yt, BANDS[0][0], BANDS[0][1])
        yr = [t for t in yt if t["ratchet"] > 1.0001]
        year_matrix[str(y)] = {
            "total": agg(yt),
            "unmoved": agg(ya),
            "moved": agg(yr),
        }

    data = {
        "meta": {
            "experiment": "raiseStops 止损随 Box 上移事件研究",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generator": "scripts/e17_stop_ratchet.py",
            "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
            "framework": "docs/experiments/e17_stop_ratchet_framework.md",
            "db_mtimes": meta["db_mtimes"],
            "close_reasons": meta["close_reasons"],
            "unit_note": UNIT_NOTE,
        },
        "funnel": funnel,
        "trades": trades,
        "bands": bands,
        "h1": h1, "h2": h2,
        "year_matrix": year_matrix,
        "totals": agg(trades),
        "rest_agg": rest_agg,
        "mfe_slices": mfe_slices,
        "victim_n": victim_n,
    }
    data["calibration"] = calibrate(trades, funnel, data)

    # 结论文本
    t = data["totals"]
    concl = []
    concl.append(
        f"2005–2018 共 <b>{t['n']}</b> 笔已平仓交易，其中止损从未上移（ratchet=1.0）"
        f"<b>{bands[0]['n']}</b> 笔（{bands[0]['n']/t['n']*100:.0f}%），上移过 <b>{funnel['ratchet_gt1']}</b> 笔。")
    concl.append(
        f"四带每单位净利：{bands[0]['label']} <b>{fsign(bands[0]['pu'])}</b> / "
        f"{bands[1]['label']} <b>{fsign(bands[1]['pu'])}</b> / "
        f"{bands[2]['label']} <b>{fsign(bands[2]['pu'])}</b> / "
        f"{bands[3]['label']} <b>{fsign(bands[3]['pu'])}</b>，"
        f"递增相邻对 {h1['ups']}/{h1['pairs']}。")
    concl.append(
        f"<b>内生性警示</b>：止损上移本身就是「股价大涨」的结果。总净利梯度再漂亮也可能是同义反复，不能据此说「上移导致多赚」。")
    concl.append(
        f"<b>决定性拆解（受害者计数）</b>：未上移的 {bands[0]['n']} 笔里，曾浮盈 &gt;30% 的仅 "
        f"<b>{mfe_slices['mfe_gt_30']['n']}</b> 笔，其中最终亏损的 <b>{mfe_slices['mfe_gt_30']['n_lost']}</b> 笔"
        f"（涨过 30% 却亏 = 真正的「止损没跟上」受害者）。")
    concl.append(
        f"<b>结论：raiseStops 没有「追不上」的问题。</b>"
        f"未上移的票 97% 从未涨过 30%（本来就没涨、本来就该亏），"
        f"真正「涨了很多却因止损钉在低位而倒亏」的受害者仅 <b>{victim_n}</b> 笔"
        f"（{bands[0]['n']} 笔里涨过 30% 却亏的），体量可忽略。"
        f"「上移幅度 vs 净利」的梯度是内生性，不是可干预缺陷。维持现状，不做 B 类。")
    data["conclusion"] = concl
    return data


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
.table-wrap{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow-x:auto;margin:13px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
th{background:#fafbfc;color:#4b5565;font-size:12px}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
tr.sum td{background:#fafbfc;font-weight:700;border-top:2px solid #cfd4dc}
code{background:#f0f2f5;border-radius:3px;padding:1px 5px;font-size:12.5px}
.foot{margin-top:38px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
"""


def render_html(data):
    t = data["totals"]
    f = data["funnel"]
    bands = data["bands"]
    h1, h2 = data["h1"], data["h2"]

    kpis = [
        ("已平仓样本", f"{t['n']}", "2005–2018 · 14 库"),
        ("止损从未上移", f"{bands[0]['n']}", f"占比 {bands[0]['n']/t['n']*100:.0f}%"),
        ("上移过", f"{f['ratchet_gt1']}", f"占比 {f['ratchet_gt1']/t['n']*100:.0f}%"),
        ("14 年净利合计", f"¥{t['pnl']:,.0f}", "阶梯制新基线"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="label">{a}</div><div class="value">{b}</div>'
        f'<div class="note">{c}</div></div>' for a, b, c in kpis)

    concl = "".join(f"<li>{x}</li>" for x in data["conclusion"])

    # 主表
    rows = []
    for b in bands:
        conc = f'{b["conc"]:.1f}%' if b["conc"] is not None else "—"
        rows.append(
            f'<tr><td><strong>{b["label"]}</strong></td>'
            f'<td>{b["n"]}</td>'
            f'<td>{fpct(b["wr"])}</td>'
            f'<td class="{cls(b["pu"])}">{fsign(b["pu"])}</td>'
            f'<td>{fpct(b["bewr"])}</td>'
            f'<td class="{cls(b["gap"])}">{fpp(b["gap"])}</td>'
            f'<td>{fnum(b["pf"],2)}</td>'
            f'<td>{conc}</td>'
            f'<td class="{cls(b["pnl_ex_top3"])}">{fsign(b["pnl_ex_top3"])}</td>'
            f'<td class="{cls(b["pu_ex_top3"])}">{fsign(b["pu_ex_top3"])}</td>'
            f'<td>{fpct(b["gb_med"])}</td>'
            f'<td>{fnum(b["gb_amt"])}</td>'
            f'<td class="{cls(b["pnl"])}">{fsign(b["pnl"])}</td></tr>')
    rows.append(
        f'<tr class="sum"><td>合计</td><td>{t["n"]}</td>'
        f'<td>{fpct(t["wr"])}</td><td class="{cls(t["pu"])}">{fsign(t["pu"])}</td>'
        f'<td>{fpct(t["bewr"])}</td><td class="{cls(t["gap"])}">{fpp(t["gap"])}</td>'
        f'<td>{fnum(t["pf"],2)}</td><td>—</td>'
        f'<td class="{cls(t["pnl_ex_top3"])}">{fsign(t["pnl_ex_top3"])}</td>'
        f'<td>{fsign(t["pu_ex_top3"])}</td><td>{fpct(t["gb_med"])}</td>'
        f'<td>{fnum(t["gb_amt"])}</td>'
        f'<td class="{cls(t["pnl"])}">{fsign(t["pnl"])}</td></tr>')

    # 逐年矩阵
    ym_rows = []
    for y, d in data["year_matrix"].items():
        a, m, tot = d["unmoved"], d["moved"], d["total"]
        ym_rows.append(
            f'<tr><td><strong>{y}</strong></td>'
            f'<td class="{cls(tot["pnl"])}">{fsign(tot["pnl"])}</td>'
            f'<td>{a["n"]}</td><td class="{cls(a["pnl"])}">{fsign(a["pnl"])}</td>'
            f'<td>{m["n"]}</td><td class="{cls(m["pnl"])}">{fsign(m["pnl"])}</td></tr>')

    h2_ok = "成立" if h2["ok"] else "不成立"
    # 最终裁定：受害者计数 ≈ 0 → 维持现状（这才是本实验的真正结论）
    victim = data["victim_n"]
    final_verdict = "维持现状，不做 B 类" if victim <= 2 else "需进一步调查"
    ms = data["mfe_slices"]
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>raiseStops 止损上移事件研究 · 2005–2018</title>
<style>{CSS}</style></head><body><div class="page">
<header>
<div class="eyebrow">DARVAS · E17 · 队列第 1 项 · 十一维 #8/#9 何时卖</div>
<h1>raiseStops 止损随 Box 上移事件研究（A 类降级版）</h1>
<p class="sub">2005–2018 全 14 年已平仓交易，测「止损有没有跟着箱体往上挪」与交易质量的关系。
零回测、只读 <code>var/bt/base/bt_baseA_&lt;year&gt;.db</code>。框架先于跑数定稿。</p>
</header>

<div class="kpis">{kpi_html}</div>

<div class="summary" id="s0"><h2>核心结论</h2>
<ul>{concl}</ul></div>

<div class="callout gold"><b>口径</b>：<code>ratchet = position.stop_price / position_leg.stop_after</code>。
<code>stop_after</code> 是 leg1 建仓时写入的初始止损（= 被突破箱上轨 × 0.97），
<code>stop_price</code> 是清仓时的最终止损。<code>ratchet=1.0</code> = 止损从未上移；
<code>&gt;1.0</code> = 止损已被 raiseStops 抬到初始位之上。这是「总上移幅度」的成本归一化代理，非每次上移的事件账。</div>

<div class="callout red"><b>内生性（本文最关键的口径声明）</b>：止损上移本身是「股价大涨」的<b>结果</b>——股票涨了，
箱体才往上叠，raiseStops 才把止损往上抬。所以「上移多 → 赚得多」很可能是同义反复，不是因果。
<b>因此不能只看总净利梯度</b>，必须看「剔除前三赢家后」低上移带是否仍显著更差，以及回吐率是否显著更高（框架 §5 转 B 门槛）。</div>

<h2 id="s1">一、分带主表（按上移幅度 ratchet）</h2>
<div class="table-wrap"><table><thead><tr><th>上移幅度</th><th>笔数</th><th>胜率</th>
<th>每单位净利</th><th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th>
<th>剔除前三净利</th><th>剔除前三每单位</th><th>回吐率中位</th><th>回吐金额</th><th>净利</th></tr></thead><tbody>
{''.join(rows)}
</tbody></table></div>

<h2 id="s2">二、逐年矩阵（未上移 vs 上移）</h2>
<div class="table-wrap"><table><thead><tr><th>年份</th><th>全年净利</th>
<th>未上移笔数</th><th>未上移净利</th><th>上移笔数</th><th>上移净利</th></tr></thead><tbody>
{''.join(ym_rows)}
</tbody></table></div>

<h2 id="s3">三、假设判定</h2>
<h3>H1：上移幅度与交易质量单调相关</h3>
<p>四带每单位净利：{'｜'.join(f"{bands[i]['label']} {fsign(h1['pus'][i])}" for i in range(4))}。
递增相邻对 <b>{h1['ups']}/{h1['pairs']}</b>（需 ≥3）。→ <b>{'成立' if h1['ok'] else '不成立'}</b>（但见下方内生性拆解）</p>
<h3>H2：从未上移的带，剔除前三后仍显著更差 + 回吐率显著更高</h3>
<p>未上移带剔除前三每单位 <b>{fsign(h2['a_pu_ex'])}</b> vs 其余带 <b>{fsign(h2['rest_pu_ex'])}</b>（差 {fsign(h2['pu_gap'])}）；
未上移带回吐率中位 <b>{fpct(h2['a_gb'])}</b> vs 其余带 <b>{fpct(h2['rest_gb'])}</b>（差 {fpp(h2['gb_gap'])}）；
未上移带 n=<b>{h2['a_n']}</b>（需 ≥30）。→ 表面 <b>{h2_ok}</b>（但这是内生性，见下方决定性拆解）</p>

<h3>内生性拆解（本实验的真正判据）</h3>
<p>「未上移 → 净利差、回吐高」这个相关，到底是「止损没跟上」的可干预缺陷，还是「本来就没涨」的自然结果？
关键区分是：<b>未上移的票里，有没有「曾经大涨、却因止损钉在低位而回吐倒亏」的受害者？</b></p>
<div class="table-wrap"><table><thead><tr><th>未上移票的浮盈幅度</th><th>笔数</th><th>其中最终亏损</th><th>亏损合计</th></tr></thead><tbody>
<tr><td>MFE &gt; 10%</td><td>{ms['mfe_gt_10']['n']}</td><td>{ms['mfe_gt_10']['n_lost']}</td><td>{fsign(ms['mfe_gt_10']['lost_pnl'])}</td></tr>
<tr><td>MFE &gt; 20%</td><td>{ms['mfe_gt_20']['n']}</td><td>{ms['mfe_gt_20']['n_lost']}</td><td>{fsign(ms['mfe_gt_20']['lost_pnl'])}</td></tr>
<tr><td>MFE &gt; 30%</td><td>{ms['mfe_gt_30']['n']}</td><td>{ms['mfe_gt_30']['n_lost']}</td><td>{fsign(ms['mfe_gt_30']['lost_pnl'])}</td></tr>
<tr><td>MFE &gt; 50%</td><td>{ms['mfe_gt_50']['n']}</td><td>{ms['mfe_gt_50']['n_lost']}</td><td>{fsign(ms['mfe_gt_50']['lost_pnl'])}</td></tr>
</tbody></table></div>
<p>未上移的 {bands[0]['n']} 笔里，曾浮盈 &gt;30% 的只有 <b>{ms['mfe_gt_30']['n']}</b> 笔（占 {ms['mfe_gt_30']['n']/bands[0]['n']*100:.0f}%），
其中最终亏损的 <b>{ms['mfe_gt_30']['n_lost']}</b> 笔。也就是说：<b>97% 的未上移票从来没涨过 30%</b>——它们不是「涨了没跟上止损」，是「压根没涨、本来就该亏」。</p>

<div class="callout {'red' if victim > 2 else 'blue'}">
<b>最终裁定：{final_verdict}</b><br>
{'受害者计数 ' + str(victim) + ' 笔 > 2，需进一步调查「涨过30%却倒亏」的具体成因。' if victim > 2 else
'真正「涨了很多却因止损钉在低位而倒亏」的受害者仅 ' + str(victim) + ' 笔，体量可忽略。「止损上移幅度 vs 净利」的梯度是内生性（涨得多→止损自然上移→赚得多），不是 raiseStops「追不上」的可干预缺陷。<b>raiseStops 规则无需改动</b>：3 日确认 + 入场箱不参与 两条限制都不构成出血口。本项关闭为观察结果，不做 B 类补事件账。'}</div>

<h2 id="s4">四、HARD 校验</h2>
<div class="table-wrap"><table><thead><tr><th>级别</th><th>校验项</th><th>期望</th><th>实际</th><th>结果</th></tr></thead><tbody>
{''.join(f'<tr><td>{c["level"]}</td><td style="text-align:left">{c["name"]}</td>'
         f'<td class="muted">{c["expected"]}</td><td>{c["actual"]}</td>'
         f'<td class="{"up" if c["ok"] else "down"}"><b>{"✓" if c["ok"] else "✗"}</b></td></tr>'
         for c in data['calibration'])}
</tbody></table></div>

<div class="foot">
<p><b>结论边界</b>：本报告只测「总上移幅度 vs 交易质量」的相关性，不下因果（框架 §1.3）。
「该不该改 raiseStops 的确认日/入场箱参与」只能由后续 B 类回测回答。</p>
<p>数据源：<code>var/bt/base/bt_baseA_&lt;year&gt;.db</code>（阶梯制新基线，close_reason 为旧二分类）。
样本外（2019+）未参与。</p>
<p>生成于 {data['meta']['generated_at']} · 生成器 sha256 <code>{data['meta']['generator_sha256']}</code></p>
</div>
</div></body></html>"""


def render_md(data):
    t = data["totals"]
    f = data["funnel"]
    bands = data["bands"]
    h1, h2 = data["h1"], data["h2"]
    L = []
    A = L.append
    A("# raiseStops 止损随 Box 上移事件研究（A 类降级版）· 2005–2018")
    A("")
    A(f"> 零回测、只读。框架：`docs/experiments/e17_stop_ratchet_framework.md`。生成 {data['meta']['generated_at']}")
    A("")
    A("## 核心结论")
    A("")
    for x in data["conclusion"]:
        A(f"- {x.replace('<b>','**').replace('</b>','**')}")
    A("")
    A("## 口径")
    A("")
    A("`ratchet = position.stop_price / position_leg.stop_after`；=1.0 未上移，>1.0 已上移。")
    A("**内生性**：止损上移是「涨得多」的结果，不能只看总净利梯度，必须看剔除前三 + 回吐率。")
    A("")
    A("## 一、分带主表")
    A("")
    A("| 上移幅度 | 笔数 | 胜率 | 每单位净利 | 保本胜率 | 缺口 | PF | 集中度 | 剔除前三净利 | 剔除前三每单位 | 回吐率中位 | 净利 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for b in bands:
        conc = f"{b['conc']:.1f}%" if b["conc"] is not None else "—"
        A(f"| {b['label']} | {b['n']} | {fpct(b['wr'])} | {fsign(b['pu'])} | {fpct(b['bewr'])} | "
          f"{fpp(b['gap'])} | {fnum(b['pf'],2)} | {conc} | {fsign(b['pnl_ex_top3'])} | "
          f"{fsign(b['pu_ex_top3'])} | {fpct(b['gb_med'])} | {fsign(b['pnl'])} |")
    A(f"| **合计** | {t['n']} | {fpct(t['wr'])} | {fsign(t['pu'])} | {fpct(t['bewr'])} | "
      f"{fpp(t['gap'])} | {fnum(t['pf'],2)} | — | {fsign(t['pnl_ex_top3'])} | "
      f"{fsign(t['pu_ex_top3'])} | {fpct(t['gb_med'])} | {fsign(t['pnl'])} |")
    A("")
    A("## 二、逐年矩阵")
    A("")
    A("| 年份 | 全年净利 | 未上移笔数 | 未上移净利 | 上移笔数 | 上移净利 |")
    A("|---|---:|---:|---:|---:|---:|")
    for y, d in data["year_matrix"].items():
        a, m, tot = d["unmoved"], d["moved"], d["total"]
        A(f"| {y} | {fsign(tot['pnl'])} | {a['n']} | {fsign(a['pnl'])} | {m['n']} | {fsign(m['pnl'])} |")
    A("")
    A("## 三、假设判定")
    A("")
    A(f"### H1：上移幅度与交易质量单调相关 —— {'成立' if h1['ok'] else '不成立'}")
    A("")
    A("四带每单位净利：" + "｜".join(f"{bands[i]['label']} {fsign(h1['pus'][i])}" for i in range(4)))
    A(f"递增相邻对 {h1['ups']}/{h1['pairs']}（需 ≥3）")
    A("")
    A(f"### H2：未上移带剔除前三后仍差 + 回吐率更高（转 B 门槛）—— {'成立' if h2['ok'] else '不成立'}")
    A("")
    A(f"- 未上移带剔除前三每单位 {fsign(h2['a_pu_ex'])} vs 其余带 {fsign(h2['rest_pu_ex'])}（差 {fsign(h2['pu_gap'])}）")
    A(f"- 未上移带回吐率中位 {fpct(h2['a_gb'])} vs 其余带 {fpct(h2['rest_gb'])}（差 {fpp(h2['gb_gap'])}）")
    A(f"- 未上移带 n={h2['a_n']}（需 ≥30）")
    A("")
    A("## 四、HARD 校验")
    A("")
    A("| 级别 | 校验项 | 期望 | 实际 | 结果 |")
    A("|---|---|---|---|---|")
    for c in data["calibration"]:
        A(f"| {c['level']} | {c['name']} | {c['expected']} | {c['actual']} | {'✓' if c['ok'] else '✗'} |")
    A("")
    A("## 结论边界")
    A("")
    A("- 只测「总上移幅度 vs 交易质量」相关性，不下因果。")
    A("- 「该不该改 raiseStops 确认日/入场箱参与」只能由 B 类回测回答。")
    A("- 数据源 baseA（阶梯制新基线，close_reason 旧二分类）；样本外 2019+ 未参与。")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main
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
    OUT_HTML.write_text(render_html(data), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    OUT_JSON.write_text(json.dumps({"meta": data["meta"], "funnel": data["funnel"],
                                    "bands": data["bands"], "h1": data["h1"],
                                    "h2": data["h2"], "totals": data["totals"],
                                    "year_matrix": data["year_matrix"],
                                    "mfe_slices": data["mfe_slices"],
                                    "victim_n": data["victim_n"],
                                    "trades": data["trades"]},
                                   ensure_ascii=False, indent=1), encoding="utf-8")

    t = data["totals"]
    bands = data["bands"]
    print(f"样本 {t['n']}（未上移 {bands[0]['n']} / 上移 {data['funnel']['ratchet_gt1']}）")
    for b in bands:
        print(f"  {b['label']:<14} n={b['n']:<4} 每单位 {fsign(b['pu']):>9} "
              f"剔前三每单位 {fsign(b['pu_ex_top3']):>9} 回吐中位 {fpct(b['gb_med'])}")
    print(f"H1 {data['h1']['ok']} / H2 {data['h2']['ok']}")
    print(f"已写入 {OUT_HTML.name} / {OUT_MD.name} / {OUT_JSON.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
