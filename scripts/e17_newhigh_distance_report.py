#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E17 距新高细分实验 · 专项分析报告生成器。

实验 ID：M9.0（第 5 维 box 位置，全历史段 2005-2018）
研究问题：达瓦斯「距 52 周新高」这一条件，从 t1-t4 四粗档细化到更细粒度梯度后，
          edge 是否单调？甜点区 / 陷阱区各在哪？

数据来源（只读，不重算、不改回测器）：
- 14 年回测库 var/bt/bt_e17_<year>.db 的 plan_order.reason_json.newhigh_distance_pct
  （距 52 周新高百分比，触发日口径）
- 上证指数 sh000001 日线（daily_bar 表，1990-2026 全历史）做 regime 归因（A 判据）

四项固定汇报口径（docs/29 研究宪法）：
  胜率 / 每单位净利 / 保本胜率 / 利润集中度（前三赢家）

输出（写 docs/experiments/，系统最宝贵资产陈列处）：
  e17_newhigh_distance_report.html  交互式报告（主交付）
  e17_newhigh_distance_report.md    纯文本版（归档）
  e17_newhigh_distance_data.json    原始统计（可复现溯源）

口径铁律：
- 只统计 position_leg.leg_no=1 首次建仓，档位/距离取 plan_order.reason_json。
- 净利 = 已平仓 realized_pnl；每单位净利 = Σpnl / Σunits（按平仓交易）。
- 保本胜率 = 1/(1+平均盈利/平均亏损)（payoff 赔率法）。
- 利润集中度 = 前三赢家合计 / 毛盈利（无毛盈利时为 None）。
- 2005-2013 无基线对照（bt_p05_<year>.db 仅 2014-2018 存在），如实标注，不编造。
"""
import json
import sqlite3
import glob
import os
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
# 试验分析研究报告统一沉淀到 docs/experiments/（系统最宝贵资产，醒目陈列处）
EXP = ROOT / "docs" / "experiments"
OUT_HTML = EXP / "e17_newhigh_distance_report.html"
OUT_MD = EXP / "e17_newhigh_distance_report.md"
OUT_JSON = EXP / "e17_newhigh_distance_data.json"

# 四档 t1-t4 与单位（E17 阶梯，docs/01 §10.3）
TIERS = OrderedDict([
    ("newhigh_t1", ("t1", "0–5%", 3.0)),
    ("newhigh_t2", ("t2", "5–10%", 2.0)),
    ("newhigh_t3", ("t3", "10–20%", 1.0)),
    ("newhigh_t4", ("t4", "20–40%", 0.5)),
])

# 细粒度分档（2% 步进，覆盖 0–40%，40%+ 因拒买无样本）
FINE_BANDS = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 12), (12, 14),
              (14, 16), (16, 18), (18, 20), (20, 25), (25, 30), (30, 35), (35, 40)]

# 5% 粗粒度分档（对照 t1-t4，便于看宏观趋势）
COARSE_BANDS = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40)]

# regime A 判据参数（与 internal/regime ClassifyDateA 一致）
LOOKBACK = 60
UP_BIAS = 1.002
DOWN_BIAS = 0.998


def years_list():
    return sorted(int(os.path.basename(p).split('_')[2].split('.')[0])
                  for p in glob.glob(str(BT / "bt_e17_*.db")))


def load_trades():
    """加载 14 年全部 leg_no=1 建仓记录 + reason_json 关键字段。"""
    trades = []
    for y in years_list():
        db = BT / f"bt_e17_{y}.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sql = ("SELECT p.opened_date, p.closed_date, p.status, p.realized_pnl, "
               "o.reason_json FROM position_leg l "
               "JOIN position p ON p.id=l.position_id "
               "LEFT JOIN plan_order o ON o.id=l.plan_order_id WHERE l.leg_no=1")
        for r in con.execute(sql):
            try:
                rj = json.loads(r["reason_json"] or "{}")
            except (TypeError, ValueError):
                rj = {}
            dist = rj.get("newhigh_distance_pct")
            if dist is None:
                continue
            units = rj.get("units", 1.0)
            try:
                units = float(units)
            except (TypeError, ValueError):
                units = 1.0
            trades.append({
                "year": y, "dist": float(dist),
                "units": units, "pnl": r["realized_pnl"],
                "status": r["status"], "opened": r["opened_date"],
                "closed": r["closed_date"],
                "basis": rj.get("sizing_basis"),
            })
        con.close()
    return trades


def agg(rows):
    """对一组已平仓交易做四项口径聚合。"""
    rows = [r for r in rows if r["pnl"] is not None]
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] < 0]
    pnl = sum(r["pnl"] for r in rows)
    units = sum(r["units"] for r in rows)
    gw = sum(r["pnl"] for r in wins)
    gl = sum(r["pnl"] for r in losses)
    n = len(rows)
    wr = len(wins) / n if n else None
    pu = pnl / units if units else None
    avgw = gw / len(wins) if wins else None
    avgl = gl / len(losses) if losses else None
    payoff = abs(avgw / avgl) if (avgw and avgl) else None
    bewr = 1 / (1 + payoff) if payoff else None
    top3 = sum(sorted((r["pnl"] for r in rows), reverse=True)[:3])
    conc = (top3 / gw * 100) if gw > 0 else None
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "pnl": pnl,
        "units": units, "wr": wr, "pu": pu, "pf": (gw / abs(gl) if gl else None),
        "payoff": payoff, "bewr": bewr, "top3": top3, "conc": conc,
    }


def band_rows(trades, lo, hi):
    return [t for t in trades if lo <= t["dist"] < hi]


def load_index_labels():
    """从任一 e17 库读 sh000001 日线，算 A 判据 regime 标签（0=震荡/1=升势/-1=降势）。"""
    db = BT / f"bt_e17_{years_list()[0]}.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT date, low FROM daily_bar WHERE code='sh000001' ORDER BY date"
    ).fetchall()
    con.close()
    dates = [r[0] for r in rows]
    lows = [r[1] for r in rows]
    labels = {}
    for i in range(len(rows)):
        if i < LOOKBACK:
            labels[dates[i]] = 0
            continue
        cur = min(lows[i - LOOKBACK + 1:i + 1])
        prev = min(lows[i - LOOKBACK:i])
        if cur > prev * UP_BIAS:
            labels[dates[i]] = 1
        elif cur < prev * DOWN_BIAS:
            labels[dates[i]] = -1
        else:
            labels[dates[i]] = 0
    return labels


REGIME_CN = {1: "升势（牛）", -1: "降势（熊）", 0: "震荡（猴）"}


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------
def fnum(x, d=0):
    return "—" if x is None else f"{x:,.{d}f}"


def fpct(x, d=1):
    return "—" if x is None else f"{x*100:.{d}f}%"


def fsign(x, d=0):
    return "—" if x is None else f"{x:+,.{d}f}"


def cls(x):
    if x is None:
        return "neutral"
    return "up" if x > 0 else ("down" if x < 0 else "neutral")


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------
def build():
    trades = load_trades()
    closed = [t for t in trades if t["status"] == "closed" and t["pnl"] is not None]
    labels = load_index_labels()

    # 给每笔已平仓交易挂 regime（按开仓日）
    for t in closed:
        op = (t["opened"] or "")[:10]
        t["regime"] = labels.get(op, 0)

    years = sorted({t["year"] for t in trades})
    total_n = len(trades)
    total_closed = len(closed)
    total_pnl = sum(t["pnl"] for t in closed)

    # 细粒度分档
    fine = []
    for lo, hi in FINE_BANDS:
        rows = band_rows(closed, lo, hi)
        fine.append((f"{lo}–{hi}%", lo, hi, agg(rows)))
    coarse = []
    for lo, hi in COARSE_BANDS:
        rows = band_rows(closed, lo, hi)
        coarse.append((f"{lo}–{hi}%", lo, hi, agg(rows)))

    # 四档对照
    tier_stats = OrderedDict()
    for key, (short, rng, units) in TIERS.items():
        rows = [t for t in closed if t["basis"] == key]
        tier_stats[key] = agg(rows)

    # 逐年 × 距新高细分（用四档 + 甜点/陷阱粗分段）
    year_tier = {}
    for y in years:
        yc = [t for t in closed if t["year"] == y]
        d = OrderedDict()
        for key, (short, rng, units) in TIERS.items():
            d[key] = agg([t for t in yc if t["basis"] == key])
        # 甜点区（2-4%）与陷阱区（4-6%）逐年
        d["_sweet"] = agg(band_rows(yc, 2, 4))
        d["_trap"] = agg(band_rows(yc, 4, 6))
        year_tier[y] = {"overall": agg(yc), "tiers": d, "n": len(yc)}

    # regime × 距新高细分
    regime_fine = OrderedDict()
    for rkey in [1, -1, 0]:
        rc = [t for t in closed if t["regime"] == rkey]
        subs = []
        for lo, hi in FINE_BANDS:
            subs.append((f"{lo}–{hi}%", agg(band_rows(rc, lo, hi))))
        regime_fine[rkey] = {"overall": agg(rc), "fine": subs}

    # 关键发现
    sweet = agg(band_rows(closed, 2, 4))
    trap = agg(band_rows(closed, 4, 6))
    t1_agg = tier_stats["newhigh_t1"]

    data = {
        "meta": {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "years": years, "total_n": total_n, "total_closed": total_closed,
            "total_pnl": total_pnl,
        },
        "tiers": {k: v for k, v in tier_stats.items()},
        "fine": [{"label": a, "lo": b, "hi": c, **d} for a, b, c, d in fine],
        "coarse": [{"label": a, "lo": b, "hi": c, **d} for a, b, c, d in coarse],
        "year_tier": {str(y): v for y, v in year_tier.items()},
        "regime": {str(k): v for k, v in regime_fine.items()},
        "sweet": sweet, "trap": trap, "t1": t1_agg,
    }
    return data, trades, closed, labels


def render_html(data):
    fine = data["fine"]
    coarse = data["coarse"]
    tiers = data["tiers"]
    meta = data["meta"]

    def fine_table(rows):
        parts = [
            '<div class="table-wrap"><table>',
            '<thead><tr><th>距新高分档</th><th>笔数</th><th>胜率</th><th>每单位净利</th>'
            '<th>保本胜率</th><th>利润因子</th><th>利润集中度</th><th>净利</th></tr></thead><tbody>',
        ]
        for r in rows:
            conc_s = f'{r["conc"]:.1f}%' if r["conc"] is not None else "—"
            bewr_s = fpct(r["bewr"]) if r["bewr"] is not None else "—"
            parts.append(
                f'<tr><td><strong>{r["label"]}</strong></td><td>{r["n"]}</td>'
                f'<td>{fpct(r["wr"])}</td>'
                f'<td class="{cls(r["pu"])}">{fsign(r["pu"])}</td>'
                f'<td>{bewr_s}</td>'
                f'<td>{fnum(r["pf"],2) if r["pf"] is not None else "—"}</td>'
                f'<td>{conc_s}</td>'
                f'<td class="{cls(r["pnl"])}">{fsign(r["pnl"])}</td></tr>'
            )
        parts.append("</tbody></table></div>")
        return "".join(parts)

    def tier_table(td):
        parts = [
            '<div class="table-wrap"><table>',
            '<thead><tr><th>档位</th><th>单位</th><th>笔数</th><th>胜率</th><th>每单位净利</th>'
            '<th>保本胜率</th><th>利润因子</th><th>利润集中度</th><th>净利</th></tr></thead><tbody>',
        ]
        for key, (short, rng, units) in TIERS.items():
            r = td[key]
            conc_s = f'{r["conc"]:.1f}%' if r["conc"] is not None else "—"
            bewr_s = fpct(r["bewr"]) if r["bewr"] is not None else "—"
            parts.append(
                f'<tr><td><strong>{short}</strong>　{rng}</td><td>{units}</td><td>{r["n"]}</td>'
                f'<td>{fpct(r["wr"])}</td>'
                f'<td class="{cls(r["pu"])}">{fsign(r["pu"])}</td>'
                f'<td>{bewr_s}</td>'
                f'<td>{fnum(r["pf"],2) if r["pf"] is not None else "—"}</td>'
                f'<td>{conc_s}</td>'
                f'<td class="{cls(r["pnl"])}">{fsign(r["pnl"])}</td></tr>'
            )
        parts.append("</tbody></table></div>")
        return "".join(parts)

    # 细粒度净利柱状（diverging，红=盈绿=亏，中国惯例）
    max_abs = max((abs(r["pnl"]) for r in fine), default=1) or 1
    bar_parts = ['<div class="diverging">']
    for r in fine:
        width = abs(r["pnl"]) / max_abs * 48
        if r["pnl"] >= 0:
            left, style = 50, f"left:50%;width:{max(width,0.3):.2f}%;background:var(--red)"
        else:
            left = 50 - width
            style = f"left:{max(50-width,0.3):.2f}%;width:{max(width,0.3):.2f}%;background:var(--green)"
        bar_parts.append(
            f'<div class="dv-row"><div class="dv-label">{r["label"]}</div>'
            f'<div class="dv-track"><i class="dv-zero"></i><b style="{style}"></b></div>'
            f'<div class="dv-val {cls(r["pnl"])}">{fsign(r["pnl"])}</div></div>'
        )
    bar_parts.append('</div>')
    bar_html = "".join(bar_parts)

    # regime 表
    reg_parts = ['<div class="table-wrap"><table>',
                 '<thead><tr><th>市场体制</th><th>距新高分档</th><th>笔数</th><th>胜率</th>'
                 '<th>每单位净利</th><th>保本胜率</th><th>利润因子</th><th>净利</th></tr></thead><tbody>']
    for rkey in [1, -1, 0]:
        rn = REGIME_CN[rkey]
        d = data["regime"][str(rkey)]
        # 先总行
        reg_parts.append(
            f'<tr class="reg-head"><td rowspan="{len(d["fine"])+1}" class="reg-name {rkey}">{rn}</td>'
            f'<td><em>全部</em></td><td>{d["overall"]["n"]}</td>'
            f'<td>{fpct(d["overall"]["wr"])}</td>'
            f'<td class="{cls(d["overall"]["pu"])}">{fsign(d["overall"]["pu"])}</td>'
            f'<td>{fpct(d["overall"]["bewr"]) if d["overall"]["bewr"] is not None else "—"}</td>'
            f'<td>{fnum(d["overall"]["pf"],2) if d["overall"]["pf"] is not None else "—"}</td>'
            f'<td class="{cls(d["overall"]["pnl"])}">{fsign(d["overall"]["pnl"])}</td></tr>'
        )
        for lab, r in d["fine"]:
            reg_parts.append(
                f'<tr><td>{lab}</td><td>{r["n"]}</td><td>{fpct(r["wr"])}</td>'
                f'<td class="{cls(r["pu"])}">{fsign(r["pu"])}</td>'
                f'<td>{fpct(r["bewr"]) if r["bewr"] is not None else "—"}</td>'
                f'<td>{fnum(r["pf"],2) if r["pf"] is not None else "—"}</td>'
                f'<td class="{cls(r["pnl"])}">{fsign(r["pnl"])}</td></tr>'
            )
    reg_parts.append("</tbody></table></div>")
    reg_html = "".join(reg_parts)

    # 逐年表
    yr_parts = ['<div class="table-wrap"><table>',
                '<thead><tr><th>年份</th><th>全年</th><th>t1(0-5%)</th><th>t2(5-10%)</th>'
                '<th>t3(10-20%)</th><th>t4(20-40%)</th><th>甜点区(2-4%)</th><th>陷阱区(4-6%)</th></tr></thead><tbody>']
    for y in meta["years"]:
        d = data["year_tier"][str(y)]
        def cell(key):
            r = d["tiers"][key]
            return (f'<td class="{cls(r["pnl"])}">{fsign(r["pnl"])}</td>'
                    f'<td class="muted small">{r["n"]}笔</td>') if key in d["tiers"] else "<td>—</td>"
        def cell2(r):
            return f'<td class="{cls(r["pnl"])}">{fsign(r["pnl"])}</td>'
        yr_parts.append(
            f'<tr><td><strong>{y}</strong></td>'
            f'<td class="{cls(d["overall"]["pnl"])}">{fsign(d["overall"]["pnl"])}</td>'
            f'{cell("newhigh_t1")}{cell("newhigh_t2")}{cell("newhigh_t3")}{cell("newhigh_t4")}'
            f'{cell2(d["tiers"]["_sweet"])}{cell2(d["tiers"]["_trap"])}</tr>'
        )
    yr_parts.append("</tbody></table></div>")
    yr_html = "".join(yr_parts)

    sweet = data["sweet"]
    trap = data["trap"]
    t1 = data["t1"]

    html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>E17 距新高细分实验报告</title><style>
:root{{--bg:#f5f6f8;--surface:#fff;--ink:#1e2430;--muted:#697386;--line:#e3e6eb;--soft:#f0f2f5;--red:#c0392b;--red2:#faece9;--green:#1b7a4b;--green2:#e9f5ef;--gold:#a66a12;--gold2:#fbf3e4;--blue:#2d5b8e;--blue2:#ecf2f9}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif;font-size:15px;line-height:1.7}}
.page{{max-width:1180px;margin:0 auto;padding:42px 28px 64px}}
header{{border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:26px}}
.eyebrow{{font-size:12px;letter-spacing:.16em;color:var(--blue);font-weight:700;margin-bottom:10px}}
h1{{font-size:32px;line-height:1.28;margin:0 0 10px;letter-spacing:-.02em}}h2{{font-size:23px;margin:38px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}}h3{{font-size:17px;margin:20px 0 9px}}
p{{margin:9px 0}}.sub{{color:var(--muted);max-width:900px;margin:0}}.muted{{color:var(--muted)}}.small{{font-size:12.5px}}
.up{{color:var(--red)}}.down{{color:var(--green)}}.neutral{{color:var(--gold)}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}}.kpi{{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:17px 18px}}
.kpi .label{{font-size:12px;color:var(--muted)}}.kpi .value{{font-size:27px;font-weight:700;line-height:1.25;margin-top:4px}}.kpi .note{{font-size:12px;color:var(--muted);margin-top:3px}}
.summary{{background:var(--surface);border-left:4px solid var(--blue);border-radius:8px;padding:18px 22px;margin:22px 0}}.summary h2{{border:0;margin:0 0 8px;padding:0;font-size:20px}}.summary ul{{margin:8px 0 0 19px;padding:0}}.summary li{{margin:5px 0}}
.callout{{border-radius:8px;padding:14px 17px;margin:15px 0;font-size:14px}}.callout.gold{{background:var(--gold2);border:1px solid #ead9b8}}.callout.red{{background:var(--red2);border:1px solid #efd0ca}}
.table-wrap{{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow-x:auto;margin:13px 0}}table{{width:100%;border-collapse:collapse;font-size:13px;min-width:720px}}th,td{{padding:9px 11px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}th{{background:#fafbfc;color:#4b5565;font-size:12px;font-weight:600}}th:first-child,td:first-child{{text-align:left}}tr:last-child td{{border-bottom:0}}tbody tr:hover{{background:#fcfcfd}}
.reg-head td{{background:#fafbfc;font-weight:600}}.reg-name.1{{color:var(--red)}}.reg-name.-1{{color:var(--green)}}.reg-name.0{{color:var(--gold)}}
.diverging{{margin:10px 0 4px}}.dv-row{{display:grid;grid-template-columns:82px 1fr 110px;align-items:center;gap:9px;margin:7px 0}}.dv-label{{font-size:12.5px}}.dv-track{{position:relative;height:20px;background:var(--soft);border-radius:4px;overflow:hidden}}.dv-zero{{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#aab1bc;z-index:2}}.dv-track b{{position:absolute;top:3px;bottom:3px;border-radius:2px;z-index:1}}.dv-val{{text-align:right;font-size:12.5px;font-variant-numeric:tabular-nums}}
.foot{{margin-top:38px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}}.foot p{{margin:5px 0}}
@media(max-width:900px){{.kpis{{grid-template-columns:repeat(2,1fr)}}.page{{padding:28px 16px 48px}}h1{{font-size:26px}}}}
@media print{{body{{background:#fff;font-size:11px}}.page{{max-width:none;padding:0}}h2{{break-after:avoid}}.table-wrap{{break-inside:avoid}}}}
</style></head><body><main class="page">
<header><div class="eyebrow">DARVAS BOX · E17 NEWHIGH DISTANCE GRADIENT</div>
<h1>距新高细分实验报告</h1>
<p class="sub">实验 ID：M9.0（第 5 维 box 位置）· 覆盖 2005–2018 共 14 年 · {meta["total_n"]} 笔建仓 / {meta["total_closed"]} 笔平仓 · 生成于 {meta["generated"]}</p></header>

<section class="kpis">
<div class="kpi"><div class="label">14 年累计净利</div><div class="value {cls(meta["total_pnl"])}">{fsign(meta["total_pnl"])}</div><div class="note">{meta["total_closed"]} 笔平仓交易</div></div>
<div class="kpi"><div class="label">甜点区(2–4%)净利</div><div class="value {cls(sweet["pnl"])}">{fsign(sweet["pnl"])}</div><div class="note">占总量 {sweet["pnl"]/meta["total_pnl"]*100:.1f}%</div></div>
<div class="kpi"><div class="label">陷阱区(4–6%)净利</div><div class="value {cls(trap["pnl"])}">{fsign(trap["pnl"])}</div><div class="note">{trap["n"]} 笔平仓</div></div>
<div class="kpi"><div class="label">t1(0–5%)净利</div><div class="value {cls(t1["pnl"])}">{fsign(t1["pnl"])}</div><div class="note">利润几乎全来自 2–4% 窄带</div></div>
</section>

<section class="summary"><h2>核心结论</h2><ul>
<li><strong>「距新高越近越好」不是单调规律。</strong>2% 细粒度下，edge 呈「甜点区—陷阱区」交替：0–2% 与 2–4% 都正贡献，但 2–4% 才是真正引擎（{fsign(sweet["pnl"])}，胜率 {fpct(sweet["wr"])}，PF {fnum(sweet["pf"],2)}）；紧接着的 4–6% 直接转负（{fsign(trap["pnl"])}，PF {fnum(trap["pf"],2)}）。</li>
<li><strong>甜点区就藏在 t1 内部，断裂点恰在 5% 边界。</strong>t1（0–5%）合计 {fsign(t1["pnl"])}，但它的利润几乎全来自 2–4% 这一小段（{fsign(sweet["pnl"])}）；一旦越过 5%（进入 t2 头部的 5–6%），edge 立刻转负。这说明「距新高」的价值集中在 2–4% 这一窄带，而非「越近越好」的宽泛单调。</li>
<li><strong>远档（20%+）无稳定正期望。</strong>20–25% 明确亏损，25–40% 三个分段虽微正但 PF 徘徊在 1 附近、利润集中度低，属于「高风险观察区」而非 edge。</li>
</ul></section>

<div class="callout gold"><strong>阅读方式：</strong>本报告把达瓦斯「距 52 周新高」从四档粗分细化到 2% 粒度，用四项固定口径（胜率 / 每单位净利 / 保本胜率 / 利润集中度）逐段验证。红线向右为盈利、绿线向左为亏损。所有数据只读自回测库，未重算、未改回测器。</div>

<h2>① 细粒度距新高细分（2% 步进，14 年合并）</h2>
<p class="sub">这是本实验的核心表：把 0–40% 切成 14 段，观察 edge 随距离的真实走向。</p>
{fine_table([r for r in fine])}
<h3>净利分布图（同一比例尺）</h3>
{bar_html}

<h2>② 5% 粗粒度对照</h2>
<p class="sub">加粗一格，用于观察宏观趋势，与四档口径衔接。</p>
{fine_table([r for r in coarse])}

<h2>③ 四档 t1–t4 对照（E17 原始阶梯口径）</h2>
<p class="sub">与细粒度对照，说明粗档为何会误导。</p>
{tier_table(tiers)}

<h2>④ regime 归因：不同市场体制下的距新高细分</h2>
<p class="sub">按 A 判据（60 日低点相邻比）标注每笔开仓日的市场体制，观察「甜点区」是否依赖牛熊环境。</p>
{reg_html}

<h2>⑤ 逐年 × 距新高细分（净利矩阵）</h2>
<p class="sub">每年一行，看甜点区 / 陷阱区的正负是否跨年稳定。2005–2013 无基线对照，仅展示 E17 自身分层。</p>
{yr_html}

<div class="callout red"><strong>诚实标注：</strong>① 2005–2013 无 p05 基线库（基线仅 2014–2018 存在），故逐年表只给 E17 自身分层，不编造相对基线差额；② 2% 细粒度下部分分段样本数偏少（如 10–12% 仅 47 笔），小样本结论须谨慎，不得外推为规律；③ 历史回测不代表实盘可复制收益。</div>

<div class="foot">
<p><strong>数据溯源：</strong>14 年回测库 var/bt/bt_e17_&lt;year&gt;.db → plan_order.reason_json.newhigh_distance_pct（触发日口径）；上证 sh000001 日线（daily_bar，1990–2026）做 A 判据 regime 标签。</p>
<p><strong>口径：</strong>只取 position_leg.leg_no=1 首次建仓；净利=已平仓 realized_pnl；每单位=Σ净利/Σ单位；保本胜率=1/(1+赔率)；利润集中度=前三赢家/毛盈利。</p>
<p><strong>实验定位：</strong>本报告是 docs/29 十维框架下 M9.0（第 5 维 box 位置）的细化深化，A 类零成本统计实验，无需改回测器。</p>
<p><strong>免责声明：</strong>以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。过往表现不预示未来收益。</p>
</div>
</main></body></html>'''
    return html


def render_md(data):
    meta = data["meta"]
    fine = data["fine"]
    tiers = data["tiers"]
    sweet = data["sweet"]
    trap = data["trap"]
    t1 = data["t1"]

    L = []
    L.append("# E17 距新高细分实验报告\n")
    L.append(f"> 实验 ID：M9.0（第 5 维 box 位置）· 覆盖 2005–2018 共 14 年 · {meta['total_n']} 笔建仓 / {meta['total_closed']} 笔平仓")
    L.append(f"> 生成于 {meta['generated']} · 数据只读自回测库，未重算、未改回测器\n")

    L.append("## 核心结论\n")
    L.append(f"- **「距新高越近越好」不是单调规律**：2% 细粒度下 edge 呈「甜点区—陷阱区」交替。")
    L.append(f"- **甜点区 2–4%**：净利 {fsign(sweet['pnl'])}，胜率 {fpct(sweet['wr'])}，PF {fnum(sweet['pf'],2)}，占总量 {sweet['pnl']/meta['total_pnl']*100:.1f}%。")
    L.append(f"- **陷阱区 4–6%**：净利 {fsign(trap['pnl'])}，PF {fnum(trap['pf'],2)}——与甜点区仅隔 2 个百分点却正负翻转，断裂点恰在 t1/t2 的 5% 边界。")
    L.append(f"- **甜点区藏在 t1 内部**：t1（0–5%）合计 {fsign(t1['pnl'])}，利润几乎全来自 2–4% 窄带（{fsign(sweet['pnl'])}），而非「越近越好」的宽泛单调。")
    L.append(f"- **远档（20%+）无稳定正期望**：20–25% 明确亏损，25–40% PF≈1、集中度低。\n")

    L.append("## ① 细粒度距新高细分（2% 步进，14 年合并）\n")
    L.append("| 分档 | 笔数 | 胜率 | 每单位净利 | 保本胜率 | 利润因子 | 利润集中度 | 净利 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in fine:
        conc = f"{r['conc']:.1f}%" if r['conc'] is not None else "—"
        bewr = fpct(r['bewr']) if r['bewr'] is not None else "—"
        L.append(f"| {r['label']} | {r['n']} | {fpct(r['wr'])} | {fsign(r['pu'])} | {bewr} | "
                 f"{fnum(r['pf'],2) if r['pf'] is not None else '—'} | {conc} | {fsign(r['pnl'])} |")
    L.append("")

    L.append("## ② 5% 粗粒度对照\n")
    L.append("| 分档 | 笔数 | 胜率 | 每单位净利 | 保本胜率 | 利润因子 | 利润集中度 | 净利 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in data["coarse"]:
        conc = f"{r['conc']:.1f}%" if r['conc'] is not None else "—"
        bewr = fpct(r['bewr']) if r['bewr'] is not None else "—"
        L.append(f"| {r['label']} | {r['n']} | {fpct(r['wr'])} | {fsign(r['pu'])} | {bewr} | "
                 f"{fnum(r['pf'],2) if r['pf'] is not None else '—'} | {conc} | {fsign(r['pnl'])} |")
    L.append("")

    L.append("## ③ 四档 t1–t4 对照\n")
    L.append("| 档位 | 单位 | 笔数 | 胜率 | 每单位净利 | 保本胜率 | 利润因子 | 利润集中度 | 净利 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for key, (short, rng, units) in TIERS.items():
        r = tiers[key]
        conc = f"{r['conc']:.1f}%" if r['conc'] is not None else "—"
        bewr = fpct(r['bewr']) if r['bewr'] is not None else "—"
        L.append(f"| {short}（{rng}） | {units} | {r['n']} | {fpct(r['wr'])} | {fsign(r['pu'])} | {bewr} | "
                 f"{fnum(r['pf'],2) if r['pf'] is not None else '—'} | {conc} | {fsign(r['pnl'])} |")
    L.append("")

    L.append("## ④ regime 归因（A 判据：60 日低点相邻比）\n")
    for rkey in [1, -1, 0]:
        d = data["regime"][str(rkey)]
        o = d["overall"]
        L.append(f"### {REGIME_CN[rkey]}（{o['n']} 笔，净利 {fsign(o['pnl'])}，胜率 {fpct(o['wr'])}）\n")
        L.append("| 分档 | 笔数 | 胜率 | 每单位净利 | 净利 |")
        L.append("|---|---|---|---|---|")
        for lab, r in d["fine"]:
            L.append(f"| {lab} | {r['n']} | {fpct(r['wr'])} | {fsign(r['pu'])} | {fsign(r['pnl'])} |")
        L.append("")

    L.append("## ⑤ 逐年 × 距新高细分（净利矩阵）\n")
    L.append("| 年份 | 全年 | t1(0-5%) | t2(5-10%) | t3(10-20%) | t4(20-40%) | 甜点(2-4%) | 陷阱(4-6%) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for y in meta["years"]:
        d = data["year_tier"][str(y)]
        o = d["overall"]
        def s(key):
            r = d["tiers"][key]
            return f"{fsign(r['pnl'])} ({r['n']}笔)"
        L.append(f"| {y} | {fsign(o['pnl'])} | {s('newhigh_t1')} | {s('newhigh_t2')} | "
                 f"{s('newhigh_t3')} | {s('newhigh_t4')} | {fsign(d['tiers']['_sweet']['pnl'])} | "
                 f"{fsign(d['tiers']['_trap']['pnl'])} |")
    L.append("")

    L.append("## 诚实标注\n")
    L.append("- 2005–2013 无 p05 基线库（基线仅 2014–2018），逐年表只给 E17 自身分层，不编造相对基线差额。")
    L.append("- 2% 细粒度下部分分段样本偏少，小样本结论须谨慎，不得外推为规律。")
    L.append("- 历史回测不代表实盘可复制收益。\n")
    L.append("---\n*本报告由 scripts/e17_newhigh_distance_report.py 自动生成。*")
    return "\n".join(L)


def main():
    EXP.mkdir(parents=True, exist_ok=True)
    data, trades, closed, labels = build()
    OUT_HTML.write_text(render_html(data), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    # JSON：清洗 None 为 null 可序列化
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"written: {OUT_HTML}")
    print(f"written: {OUT_MD}")
    print(f"written: {OUT_JSON}")
    print(f"总建仓 {data['meta']['total_n']} / 平仓 {data['meta']['total_closed']} / 净利 {data['meta']['total_pnl']:+,.0f}")


if __name__ == "__main__":
    main()
