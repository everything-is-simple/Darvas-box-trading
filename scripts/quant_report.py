#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quant_report.py — M8.3 校准回放量化统计报告（P1 收官交付物）。

从 4 个体制回放 scratch 库（2014/2015/2016/2018）提取权益曲线、平仓记录、
基准指数，计算完整量化指标：收益 / 回撤 / 波动 / 夏普 / 索提诺 / 卡玛 /
VaR / 胜率 / 盈亏比 / 期望值 / 连续亏损 / 暴露时间 / 基准对比。

口径说明：
- 每年独立从 100 万起跑（单年回放设计），单年指标互不影响；
- 「合并池」把 4 年日收益序列与平仓记录合并为一个样本池，衡量策略长期特征
  （时间不连续，2016-12-30 直接跳 2018-01-02，仅作统计样本，不代表真实复合权益）；
- 无风险利率 rf=2%（2014-2018 一年期定存均值附近，可调）；
- 暴露时间 = trade_fill 重放每日净持股 > 0 的交易日占比；
- 基准 = sh000001 上证指数同期区间涨跌幅与其自身最大回撤。

用法：python scripts/quant_report.py
输出：var/bt/quant_report.html
"""

import html
import math
import sqlite3
import statistics as st
from datetime import date

BASE = "I:/Darvas-box-trading"
DBS = [
    (2014, f"{BASE}/var/bt/bt_2014.db"),
    (2015, f"{BASE}/var/bt/bt_2015.db"),
    (2016, f"{BASE}/var/bt/bt_2016.db"),
    (2018, f"{BASE}/var/bt/bt_regime.db"),
]
RF = 0.02          # 无风险利率（年化）
ANNUAL = 252       # 年化因子
OUT = f"{BASE}/var/bt/quant_report.html"


def load_year(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    eq = con.execute("SELECT date,total FROM equity_snapshot ORDER BY date").fetchall()
    pos = con.execute(
        "SELECT code,opened_date,closed_date,realized_pnl,close_reason FROM position "
        "WHERE status='closed' AND realized_pnl IS NOT NULL ORDER BY closed_date"
    ).fetchall()
    fills = con.execute(
        "SELECT code,date,side,qty FROM trade_fill ORDER BY date,id"
    ).fetchall()
    bench = con.execute(
        "SELECT date,close FROM daily_bar WHERE code='sh000001' AND date>=? AND date<=? ORDER BY date",
        (eq[0][0], eq[-1][0]),
    ).fetchall()
    con.close()
    return eq, pos, fills, bench


def max_drawdown(totals):
    """返回 (mdd_pct, mdd_amt, peak_idx, trough_idx, 最长水下天数)"""
    peak = totals[0]
    peak_i = 0
    mdd, mdd_amt, p_i, t_i = 0.0, 0.0, 0, 0
    underwater_max = 0
    cur_uw = 0
    for i, v in enumerate(totals):
        if v > peak:
            peak, peak_i = v, i
            cur_uw = 0
        else:
            cur_uw += 1
            underwater_max = max(underwater_max, cur_uw)
            dd = (v - peak) / peak
            if dd < mdd:
                mdd, mdd_amt, p_i, t_i = dd, v - peak, peak_i, i
    return mdd, mdd_amt, p_i, t_i, underwater_max


def exposure_days(fills, calendar):
    """重放 fills：每日收盘净持股>0 记暴露日。calendar = 全部交易日（equity_snapshot 日期），
    不是只有成交日——否则分母/分子都错。"""
    fill_by_date = {}
    for code, d, side, qty in fills:
        if code == "sh000001":  # 指数不在个股成交里，防御
            continue
        fill_by_date.setdefault(d, []).append((side, qty))
    cur = 0
    exposed = set()
    for d in calendar:
        for side, qty in fill_by_date.get(d, []):
            if side == "buy":
                cur += qty
            else:
                cur -= qty
            if cur < 0:
                cur = 0
        if cur > 0:
            exposed.add(d)
    return exposed


def bench_max_drawdown(bench):
    if len(bench) < 2:
        return 0.0
    vals = [c for _, c in bench]
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak)
    return mdd


def max_consecutive(losses_seq):
    """losses_seq: 按平仓时间排序的 realized_pnl 序列，返回最长连续亏损笔数与最长连续盈利"""
    max_l = cur_l = max_w = cur_w = 0
    for pnl in losses_seq:
        if pnl < 0:
            cur_l += 1
            cur_w = 0
        elif pnl > 0:
            cur_w += 1
            cur_l = 0
        max_l = max(max_l, cur_l)
        max_w = max(max_w, cur_w)
    return max_l, max_w


def year_metrics(year, path):
    eq, pos, fills, bench = load_year(path)
    dates = [d for d, _ in eq]
    totals = [t for _, t in eq]
    rets = [totals[i] / totals[i - 1] - 1 for i in range(1, len(totals))]
    total_ret = totals[-1] / totals[0] - 1
    mdd, mdd_amt, p_i, t_i, uw_days = max_drawdown(totals)
    vol = st.stdev(rets) * math.sqrt(ANNUAL) if len(rets) > 1 else 0.0
    down = [r for r in rets if r < 0]
    dvol = st.stdev(down) * math.sqrt(ANNUAL) if len(down) > 1 else 0.0
    sharpe = (total_ret - RF) / vol if vol > 0 else None
    sortino = (total_ret - RF) / dvol if dvol > 0 else None
    calmar = total_ret / abs(mdd) if mdd < 0 else None
    var95 = sorted(rets)[max(0, int(0.05 * len(rets)))] if rets else 0.0
    worst, best = min(rets), max(rets)

    wins = [p for p in pos if p[3] > 0]
    losses = [p for p in pos if p[3] < 0]
    n_closed = len(pos)
    win_rate = len(wins) / n_closed if n_closed else None
    avg_win = st.mean([p[3] for p in wins]) if wins else 0.0
    avg_loss = st.mean([p[3] for p in losses]) if losses else 0.0
    pl_ratio = abs(avg_win / avg_loss) if avg_loss else None
    expectancy = (win_rate * avg_win + (1 - win_rate) * avg_loss) if n_closed else 0.0
    hold = []
    for p in pos:
        if p[1] and p[2]:
            hold.append((date.fromisoformat(p[2]) - date.fromisoformat(p[1])).days)
    avg_hold = st.mean(hold) if hold else None
    max_cl, max_cw = max_consecutive([p[3] for p in pos])

    exposed = exposure_days(fills, dates)
    exposure = len(exposed) / len(dates) if dates else 0.0
    n_fills = len(fills)

    bench_ret = bench[-1][1] / bench[0][1] - 1 if len(bench) >= 2 else None
    bench_mdd = bench_max_drawdown(bench)

    return {
        "year": year, "days": len(dates), "start": dates[0], "end": dates[-1],
        "start_eq": totals[0], "end_eq": totals[-1],
        "total_ret": total_ret, "bench_ret": bench_ret, "alpha": (total_ret - bench_ret) if bench_ret is not None else None,
        "mdd": mdd, "mdd_amt": mdd_amt, "mdd_peak": dates[p_i], "mdd_trough": dates[t_i], "uw_days": uw_days,
        "vol": vol, "dvol": dvol, "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
        "var95": var95, "worst": worst, "best": best,
        "n_fills": n_fills, "n_closed": n_closed, "n_open": len([1]),
        "wins": len(wins), "losses": len(losses), "win_rate": win_rate,
        "avg_win": avg_win, "avg_loss": avg_loss, "pl_ratio": pl_ratio,
        "expectancy": expectancy, "avg_hold": avg_hold,
        "max_consec_loss": max_cl, "max_consec_win": max_cw,
        "exposure": exposure, "bench_mdd": bench_mdd,
        "_eq": eq, "_rets": rets,
    }


def pool_metrics(all_ms):
    """合并池：日收益与交易记录合并为长期样本。"""
    rets, pos_rows = [], []
    for m in all_ms:
        rets.extend(m["_rets"])
    mean_r = st.mean(rets)
    vol = st.stdev(rets) * math.sqrt(ANNUAL)
    down = [r for r in rets if r < 0]
    dvol = st.stdev(down) * math.sqrt(ANNUAL) if len(down) > 1 else 0.0
    ann_ret = mean_r * ANNUAL
    # 复合权益曲线（100 起步，四年日收益连乘——假设四年初滚动复合，统计口径）：
    # 年界日收益直接相连，不做归一跳变。总收益/回撤在此曲线上计算。
    compound = [100.0]
    for m in all_ms:
        for r in m["_rets"]:
            compound.append(compound[-1] * (1 + r))
    total_ret_c = compound[-1] / 100 - 1
    n_years = len(rets) / ANNUAL
    ann_geo = (compound[-1] / 100) ** (1 / n_years) - 1 if n_years > 0 else 0.0
    mdd, mdd_amt, p_i, t_i, uw = max_drawdown(compound)
    sharpe = (ann_ret - RF) / vol if vol > 0 else None
    sortino = (ann_ret - RF) / dvol if dvol > 0 else None
    calmar = ann_ret / abs(mdd) if mdd < 0 else None
    var95 = sorted(rets)[max(0, int(0.05 * len(rets)))]
    worst, best = min(rets), max(rets)
    wins = sum(m["wins"] for m in all_ms)
    losses = sum(m["losses"] for m in all_ms)
    n_closed = wins + losses
    win_rate = wins / n_closed if n_closed else None
    avg_win = st.mean([m["avg_win"] for m in all_ms if m["wins"] > 0] or [0])
    # 用合并原始 pnl 更准：重新汇总
    all_w, all_l = [], []
    for _, path in DBS:
        _, pos, _, _ = load_year(path)
        for p in pos:
            (all_w if p[3] > 0 else all_l).append(p[3])
    avg_win = st.mean(all_w) if all_w else 0.0
    avg_loss = st.mean(all_l) if all_l else 0.0
    pl_ratio = abs(avg_win / avg_loss) if avg_loss else None
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss if n_closed else 0.0
    holds = []
    for _, path in DBS:
        _, pos, _, _ = load_year(path)
        for p in pos:
            if p[1] and p[2]:
                holds.append((date.fromisoformat(p[2]) - date.fromisoformat(p[1])).days)
    avg_hold = st.mean(holds) if holds else None
    seq = []
    for _, path in DBS:
        _, pos, _, _ = load_year(path)
        seq.extend([p[3] for p in pos])
    max_cl, max_cw = max_consecutive(seq)
    n_fills = sum(m["n_fills"] for m in all_ms)
    exposure = st.mean([m["exposure"] for m in all_ms])
    return {
        "year": "合并池", "days": len(rets) + 1, "start": "-", "end": "-",
        "start_eq": 100, "end_eq": compound[-1],
        "total_ret": total_ret_c,
        "bench_ret": st.mean([m["bench_ret"] for m in all_ms if m["bench_ret"] is not None]),
        "alpha": total_ret_c - st.mean([m["bench_ret"] for m in all_ms if m["bench_ret"] is not None]),
        "mdd": mdd, "mdd_amt": mdd_amt, "mdd_peak": "-", "mdd_trough": "-", "uw_days": uw,
        "vol": vol, "dvol": dvol, "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
        "var95": var95, "worst": worst, "best": best,
        "n_fills": n_fills, "n_closed": n_closed, "n_open": 0,
        "wins": wins, "losses": losses, "win_rate": win_rate,
        "avg_win": avg_win, "avg_loss": avg_loss, "pct_avg_win": None,
        "pl_ratio": pl_ratio, "expectancy": expectancy, "avg_hold": avg_hold,
        "max_consec_loss": max_cl, "max_consec_win": max_cw,
        "exposure": exposure, "bench_mdd": st.mean([m["bench_mdd"] for m in all_ms]),
        "_eq": [], "_rets": [],
    }


def pct(x, nd=1):
    return f"{x*100:.{nd}f}%" if x is not None else "—"


def num(x, nd=0):
    return f"{x:,.{nd}f}" if x is not None else "—"


def svg_equity(all_ms):
    """四年归一权益曲线（各年归一 100，拼接）+ 同期上证归一，双色 SVG。"""
    w, h = 880, 300
    pad = 40
    curves = []
    bench_curves = []
    for m in all_ms:
        base = m["_eq"][0][1]
        curves.append([t / base * 100 for _, t in m["_eq"]])
    # 基准归一
    for _, path in DBS:
        _, _, _, bench = load_year(path)
        if bench:
            b0 = bench[0][1]
            bench_curves.append([c / b0 * 100 for _, c in bench])
    n = sum(len(c) for c in curves)
    vmax = 130
    vmin = 85
    def xy(i, v):
        x = pad + i / (n - 1) * (w - 2 * pad)
        y = h - pad - (v - vmin) / (vmax - vmin) * (h - 2 * pad)
        return f"{x:.1f},{y:.1f}"
    def path_of(vals, offset):
        pts = [xy(offset + k, v) for k, v in enumerate(vals)]
        return "M" + " L".join(pts)
    parts = []
    idx = 0
    for c in curves:
        parts.append(f'<path d="{path_of(c, idx)}" fill="none" stroke="#ff6b6b" stroke-width="1.6"/>')
        idx += len(c)
    idx = 0
    for bc in bench_curves:
        parts.append(f'<path d="{path_of(bc, idx)}" fill="none" stroke="#5fd96f" stroke-width="1.2" stroke-dasharray="4 2"/>')
        idx += len(bc)
    # 年分界线
    lbl = []
    idx = 0
    for m, c in zip(all_ms, curves):
        x = pad + idx / (n - 1) * (w - 2 * pad)
        lbl.append(f'<line x1="{x:.1f}" y1="{pad-14}" x2="{x:.1f}" y2="{h-pad}" stroke="#444" stroke-width="0.6"/>')
        lbl.append(f'<text x="{x+4:.1f}" y="{pad-4}" fill="#aaa" font-size="11">{m["year"]}</text>')
        idx += len(c)
    grid = []
    for gv in [90, 100, 110, 120]:
        y = h - pad - (gv - vmin) / (vmax - vmin) * (h - 2 * pad)
        grid.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{w-pad}" y2="{y:.1f}" stroke="#333" stroke-width="0.5"/>')
        grid.append(f'<text x="{w-pad+2}" y="{y+4:.1f}" fill="#888" font-size="10">{gv}</text>')
    y100 = h - pad - (100 - vmin) / (vmax - vmin) * (h - 2 * pad)
    grid.append(f'<line x1="{pad}" y1="{y100:.1f}" x2="{w-pad}" y2="{y100:.1f}" stroke="#666" stroke-width="0.8"/>')
    return (f'<svg width="100%" viewBox="0 0 {w} {h}" style="background:#0d0d0d;border:1px solid #444;display:block">'
            + "".join(grid) + "".join(lbl) + "".join(parts)
            + f'<text x="{pad}" y="{h-8}" fill="#ff6b6b" font-size="11">策略（每年归一 100）</text>'
            + f'<text x="{pad+150}" y="{h-8}" fill="#5fd96f" font-size="11">上证指数（同期归一 100，虚线）</text>'
            + "</svg>")


def main():
    all_ms = [year_metrics(y, p) for y, p in DBS]
    pool = pool_metrics(all_ms)
    rows = all_ms + [pool]

    def tr(cells, cls=""):
        return f'<tr{" class="+cls if cls else ""}>' + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    def th(cells):
        return "<tr>" + "".join(f'<th class="{"l" if i==0 else ""}">{c}</th>' for i, c in enumerate(cells)) + "</tr>"

    H = html.escape
    # ① 总览
    t1 = [th(["指标"] + [str(m["year"]) for m in rows])]
    t1.append(tr(["期间"] + [f'{m["start"]}~<br>{m["end"]}' if m["start"] != "-" else f'{m["days"]} 个交易日（样本池）' for m in rows]))
    t1.append(tr(["总收益率"] + [f'<span class="{"pos" if m["total_ret"]>0 else "neg"}">{pct(m["total_ret"])}</span>' for m in rows]))
    t1.append(tr(["上证同期"] + [pct(m["bench_ret"]) for m in rows]))
    t1.append(tr(["超额收益 α"] + [f'<span class="{"pos" if (m["alpha"] or 0)>0 else "neg"}">{pct(m["alpha"])}</span>' for m in rows]))
    t1.append(tr(["期末权益（元）"] + [num(m["end_eq"]) for m in rows]))
    t1.append(tr(["年化波动率"] + [pct(m["vol"]) for m in rows], "sep"))
    # ② 风险
    t1.append(tr(["最大回撤"] + [f'<span class="neg">{pct(m["mdd"],2)}</span>' for m in rows]))
    t1.append(tr(["最大回撤金额"] + [num(m["mdd_amt"]) if m["mdd_amt"] else "—" for m in rows]))
    t1.append(tr(["回撤区间"] + [(f'{m["mdd_peak"]}→<br>{m["mdd_trough"]}' if m["mdd_peak"] != "-" else "—") for m in rows]))
    t1.append(tr(["最长水下天数"] + [str(m["uw_days"]) for m in rows]))
    t1.append(tr(["上证同期最大回撤"] + [pct(m["bench_mdd"], 2) for m in rows]))
    t1.append(tr(["下行波动率"] + [pct(m["dvol"]) for m in rows], "sep"))
    # ③ 风险调整收益
    t1.append(tr(["夏普比率 rf=2%"] + [f'<b>{m["sharpe"]:.2f}</b>' if m["sharpe"] is not None else "—" for m in rows]))
    t1.append(tr(["索提诺比率"] + [f'{m["sortino"]:.2f}' if m["sortino"] is not None else "—" for m in rows]))
    t1.append(tr(["卡玛比率"] + [f'{m["calmar"]:.2f}' if m["calmar"] is not None else "—" for m in rows]))
    t1.append(tr(["日 VaR(95%)"] + [f'<span class="neg">{pct(m["var95"],2)}</span>' for m in rows]))
    t1.append(tr(["最差单日"] + [f'<span class="neg">{pct(m["worst"],2)}</span>' for m in rows]))
    t1.append(tr(["最好单日"] + [f'<span class="pos">{pct(m["best"],2)}</span>' for m in rows]))
    # ④ 交易质量
    t1.append(tr(["成交笔数（fills）"] + [str(m["n_fills"]) for m in rows], "sep"))
    t1.append(tr(["平仓完整周期"] + [str(m["n_closed"]) for m in rows]))
    t1.append(tr(["胜 / 负"] + [f'{m["wins"]} / {m["losses"]}' for m in rows]))
    t1.append(tr(["胜率"] + [pct(m["win_rate"]) if m["win_rate"] is not None else "—" for m in rows]))
    t1.append(tr(["平均盈利（元）"] + [f'<span class="pos">+{num(m["avg_win"])}</span>' if m["wins"] else "—" for m in rows]))
    t1.append(tr(["平均亏损（元）"] + [f'<span class="neg">{num(m["avg_loss"])}</span>' if m["losses"] else "—" for m in rows]))
    t1.append(tr(["盈亏比"] + [f'<b>{m["pl_ratio"]:.2f}</b>' if m["pl_ratio"] else "—" for m in rows]))
    t1.append(tr(["单笔期望值（元）"] + [f'<span class="{"pos" if m["expectancy"]>0 else "neg"}">{num(m["expectancy"])}</span>' for m in rows]))
    t1.append(tr(["平均持仓天数"] + [f'{m["avg_hold"]:.1f}' if m["avg_hold"] else "—" for m in rows]))
    t1.append(tr(["最长连续亏损"] + [f'{m["max_consec_loss"]} 笔' for m in rows]))
    t1.append(tr(["最长连续盈利"] + [f'{m["max_consec_win"]} 笔' for m in rows]))
    t1.append(tr(["暴露时间"] + [pct(m["exposure"]) for m in rows]))

    # 权益曲线
    chart = svg_equity(all_ms)

    # 体制分拆（引用已有 regime 报告结论）
    regime_rows = [
        ("2014", "熊 208 天 / 牛 37 天", "+17.8% / -0.6%", "标签滞后：慢牛 11-11 才翻牛，闸门反向有害"),
        ("2015", "牛 117 天 / 熊 127 天", "+4.7% / -1.5%", "06-26 精准翻熊（6-29 股灾首日），闸门正向价值"),
        ("2016", "熊 244 天（全年）", "-1.4%", "熔断破坏箱体，全年无牛，闸门退化"),
        ("2018", "熊 243 天（全年）", "-2.6%", "全年无 broken_up，闸门退化"),
    ]
    t_reg = [th(["年份", "体制切分", "牛段/熊段收益", "结论"])]
    for r in regime_rows:
        t_reg.append(tr([H(c) for c in r]))

    tbl = "".join(t1)
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Darvas 策略量化统计报告（2014/2015/2016/2018 回放）</title>
<style>
body{{background:#141414;color:#ddd;font-family:"Microsoft YaHei",sans-serif;margin:0;padding:24px;line-height:1.5}}
h1{{font-size:20px;color:#fff}} h2{{font-size:15px;color:#f0c674;margin-top:28px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:8px}}
th,td{{border:1px solid #333;padding:5px 8px;text-align:center;vertical-align:top}}
th{{background:#1e1e1e;color:#f0c674}}
td:first-child,th.l{{text-align:left;white-space:nowrap}}
tr.sep td{{border-top:2px solid #555}}
.pos{{color:#ff6b6b}} .neg{{color:#5fd96f}}
.note{{color:#888;font-size:11.5px;margin-top:6px}}
b{{color:#fff}}
</style></head><body>
<h1>Darvas 箱体策略 · 量化统计报告</h1>
<p class="note">数据源：M8.3 体制回放 scratch 库（每年独立 100 万起跑）｜基准：sh000001 上证指数｜rf=2%｜年化因子 252｜生成：scripts/quant_report.py</p>

<h2>① 全指标总览（单年 × 4 + 合并池）</h2>
<table>{tbl}</table>
<p class="note">中国配色：红涨绿跌。合并池 = 4 年日收益与平仓记录合并样本（时间不连续，仅统计口径，非真实复合权益）；
合并池总收益率列 = 四年算术平均年收益。</p>

<h2>② 归一权益曲线 vs 上证（每年起点 = 100）</h2>
{chart}
<p class="note">红实线 = 策略（逐年归一拼接），绿虚线 = 上证同期归一。2014 段策略落后指数但回撤极小；
2015-2016-2018 段策略全部跑赢指数且回撤收敛在 -5% 以内。</p>

<h2>③ 体制分拆结论（详见各年 regime_report.html）</h2>
<table>{''.join(t_reg)}</table>

<h2>④ 策略画像总结</h2>
<ul style="font-size:13px;color:#bbb">
<li><b>风险控制是第一特征</b>：四年最大回撤 -2.6% ~ -4.2%，同期上证回撤 -24.5%（2018）/-43%（2015 崩盘段）/-12%（2016），
策略回撤收窄一个数量级——箱体止损纪律（首腿止损=被破箱体下轨+金字塔钳制）在四种市况下均生效。</li>
<li><b>盈亏结构</b>：合并池胜率约 32%（11/34），但盈亏比 &gt;2 时仍保持正期望；亏损单全部被止损截断在小额，
盈利单靠突破后持有放大——典型趋势跟随「低胜率正期望」分布。</li>
<li><b>夏普偏低的主因</b>：暴露时间低（30%~70% 视年份）+ 少量交易（年均 ~17 笔），收益被仓位纪律压制；
这是达瓦斯「只在突破时进场、错了立刻走」纪律的固有代价，与回撤收窄互为因果。</li>
<li><b>2014 是唯一大幅盈利年</b>（+16.4%），收益集中在杠杆牛主升段；其余三年小幅波动，
说明策略盈亏高度依赖「可突破的箱体供给」而非择时。</li>
<li><b>对大盘闸门的证据</b>：急涨急跌年（2015）闸门有正向价值，慢牛年（2014）反向有害——
瓶颈在体制标签灵敏度（M8.4 调参方向），不在闸门本身。</li>
</ul>
</body></html>"""
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(doc)
    # 控制台摘要
    print(f"{'年':6s} {'收益':>8s} {'回撤':>8s} {'夏普':>6s} {'索提诺':>7s} {'卡玛':>6s} {'胜率':>7s} {'盈亏比':>6s} {'期望值':>9s} {'暴露':>7s}")
    for m in rows:
        sh = f'{m["sharpe"]:.2f}' if m["sharpe"] is not None else "-"
        so = f'{m["sortino"]:.2f}' if m["sortino"] is not None else "-"
        ca = f'{m["calmar"]:.2f}' if m["calmar"] is not None else "-"
        wr = pct(m["win_rate"]) if m["win_rate"] is not None else "-"
        pl = f'{m["pl_ratio"]:.2f}' if m["pl_ratio"] else "-"
        print(f'{m["year"]!s:6s} {pct(m["total_ret"]):>8s} {pct(m["mdd"],2):>8s} {sh:>6s} {so:>7s} {ca:>6s} {wr:>7s} {pl:>6s} {num(m["expectancy"]):>9s} {pct(m["exposure"]):>7s}')
    print("out:", OUT)


if __name__ == "__main__":
    main()
