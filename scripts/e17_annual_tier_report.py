#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E17 逐年分层详细报告生成器。

按 e17_metrics.csv 中已完成的年份，逐年输出 t1-t4 收益/风险明细。
只读回测库；输出 var/bt/e17_annual_tier_report.html。
"""
import csv
import html
import json
import sqlite3
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
METRICS = BT / "e17_metrics.csv"
OUT = BT / "e17_annual_tier_report.html"

TIERS = OrderedDict([
    ("newhigh_t1", ("t1", "0–5%", "3.0")),
    ("newhigh_t2", ("t2", "5–10%", "2.0")),
    ("newhigh_t3", ("t3", "10–20%", "1.0")),
    ("newhigh_t4", ("t4", "20–40%", "0.5")),
])
YEAR_NOTES = {
    2014: "t1 是绝对主引擎，贡献接近全部利润；t2 当年已经亏损，t4 基本归零。利润集中度很高，少数大赢家决定全年结果。",
    2015: "四个档位中三个为正，t1 的利润因子达到 8.73；t4 仍是唯一亏损档。这是四年里分层结构最健康的一年。",
    2016: "年度整体亏损，但盈利来源发生切换：t1 转为明显亏损，t4 反而成为唯一有分量的盈利档。t4 的盈利高度依赖少数交易，不能直接外推。",
    2017: "低胜率环境：t2 全年 13 笔平仓全亏，t1 靠少数赢家维持正贡献，t4 再次转亏。E17 仍显著好于基线，但主要来自少亏而非多赚。",
}


def fnum(x, digits=0):
    if x is None:
        return "—"
    return f"{x:,.{digits}f}"


def fpct(x, digits=1):
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def fsign(x, digits=0):
    if x is None:
        return "—"
    return f"{x:+,.{digits}f}"


def fdec(x, digits=2):
    if x is None:
        return "—"
    return f"{x:.{digits}f}"


def cls(x):
    if x is None:
        return "neutral"
    return "up" if x > 0 else ("down" if x < 0 else "neutral")


def query_one(con, sql, args=()):
    row = con.execute(sql, args).fetchone()
    return row[0] if row else None


def equity_stats(con):
    rows = con.execute(
        "SELECT date,total,market_value,drawdown FROM equity_snapshot ORDER BY date"
    ).fetchall()
    if not rows:
        return None
    rets = [
        rows[i]["total"] / rows[i - 1]["total"] - 1
        for i in range(1, len(rows)) if rows[i - 1]["total"] > 0
    ]
    sharpe = 0.0
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        if var > 0:
            sharpe = mean / (var ** 0.5) * (252 ** 0.5)
    return {
        "days": len(rows), "start": rows[0]["total"], "end": rows[-1]["total"],
        "ret": rows[-1]["total"] / rows[0]["total"] - 1 if rows[0]["total"] else 0,
        "maxdd": min((r["drawdown"] for r in rows), default=0),
        "sharpe": sharpe,
    }


def _pct(lo, hi):
    """(hi-lo)/lo*100，None 或除零时返回 None。"""
    try:
        lo = float(lo); hi = float(hi)
    except (TypeError, ValueError):
        return None
    if not lo:
        return None
    return (hi - lo) / lo * 100


def _is_topbox(box_top, high_52w):
    """该 box 顶是否即 52 周最高（上升走势最高的那个 box）。"""
    try:
        bt = float(box_top); h = float(high_52w)
    except (TypeError, ValueError):
        return None
    if not h:
        return None
    return bt >= h * 0.999


def aggregate(rows):
    closed = [r for r in rows if r["status"] == "closed" and r["pnl"] is not None]
    wins = [r for r in closed if r["pnl"] > 0]
    losses = [r for r in closed if r["pnl"] < 0]
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = sum(r["pnl"] for r in losses)
    pnl = sum(r["pnl"] for r in closed)
    units = sum(r["units"] for r in closed)
    per_unit_values = [r["pnl"] / r["units"] for r in closed if r["units"]]
    holds = []
    for r in closed:
        try:
            holds.append((date.fromisoformat(r["closed"][:10]) - date.fromisoformat(r["opened"][:10])).days)
        except (TypeError, ValueError):
            pass
    dists = [r["dist"] for r in rows if r["dist"] is not None]
    avg_win = gross_win / len(wins) if wins else None
    avg_loss = gross_loss / len(losses) if losses else None
    payoff = abs(avg_win / avg_loss) if avg_win and avg_loss else None
    return {
        "n": len(rows), "closed": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(closed) if closed else None,
        "pnl": pnl, "gross_win": gross_win, "gross_loss": gross_loss,
        "pf": gross_win / abs(gross_loss) if gross_loss else None,
        "units": units, "per_unit": pnl / units if units else None,
        "avg_pnl": pnl / len(closed) if closed else None,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "breakeven_wr": 1 / (1 + payoff) if payoff else None,
        "worst": min((r["pnl"] for r in closed), default=None),
        "worst_unit": min(per_unit_values) if per_unit_values else None,
        "best": max((r["pnl"] for r in closed), default=None),
        "avg_hold": sum(holds) / len(holds) if holds else None,
        "med_hold": median(holds) if holds else None,
        "avg_dist": sum(dists) / len(dists) if dists else None,
        "top3": sum(sorted((r["pnl"] for r in closed), reverse=True)[:3]),
    }


def load_year(year):
    db = BT / f"bt_e17_{year}.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    eq = equity_stats(con)
    raw = con.execute(
        """SELECT p.opened_date,p.closed_date,p.status,p.realized_pnl,o.reason_json
           FROM position_leg l JOIN position p ON p.id=l.position_id
           LEFT JOIN plan_order o ON o.id=l.plan_order_id WHERE l.leg_no=1"""
    ).fetchall()
    con.close()
    rows = []
    for r in raw:
        try:
            rj = json.loads(r["reason_json"] or "{}")
        except (TypeError, ValueError):
            rj = {}
        basis = rj.get("sizing_basis")
        if basis not in TIERS:
            continue
        units = rj.get("units", rj.get("unit_value", 1))
        try:
            units = float(units)
        except (TypeError, ValueError):
            units = 1.0
        rows.append({
            "opened": r["opened_date"], "closed": r["closed_date"], "status": r["status"],
            "pnl": r["realized_pnl"], "tier": basis, "units": units,
            "dist": rj.get("newhigh_distance_pct"),
            "box_height": _pct(rj.get("box_bottom"), rj.get("box_top")),
            "break_strength": _pct(rj.get("box_top"), rj.get("trigger")),
            "is_topbox": _is_topbox(rj.get("box_top"), rj.get("high_52w")),
        })
    tiers = OrderedDict()
    for key, meta in TIERS.items():
        tiers[key] = aggregate([r for r in rows if r["tier"] == key])
    base = None
    p05 = BT / f"bt_p05_{year}.db"
    if p05.exists():
        bc = sqlite3.connect(f"file:{p05}?mode=ro", uri=True)
        bc.row_factory = sqlite3.Row
        beq = equity_stats(bc)
        bpnl = query_one(bc, "SELECT COALESCE(SUM(realized_pnl),0) FROM position WHERE status='closed'") or 0
        bc.close()
        if beq:
            base = {**beq, "pnl": bpnl}
    return {"year": year, "equity": eq, "tiers": tiers, "overall": aggregate(rows), "base": base, "raw_rows": rows}


def completed_years():
    if not METRICS.exists():
        return []
    with METRICS.open(encoding="utf-8", errors="replace", newline="") as f:
        return sorted({int(r[0]) for r in csv.reader(f) if r and r[0].strip().isdigit()})


def tier_table(tiers):
    parts = [
        '<div class="table-wrap"><table>',
        '<thead><tr><th>档位</th><th>单位</th><th>建仓/平仓</th><th>胜率</th><th>利润因子</th>'
        '<th>净利</th><th>前三赢家</th><th>每单位</th><th>平均盈利</th><th>平均亏损</th><th>赔率</th>'
        '<th>保本胜率</th><th>最差单笔</th><th>平均持有</th><th>平均距新高</th></tr></thead><tbody>',
    ]
    for key, (short, rng, units) in TIERS.items():
        d = tiers[key]
        if not d["n"]:
            parts.append(f'<tr><td><strong>{short}</strong>　{rng}</td><td>{units}</td><td colspan="13" class="muted">本年无成交</td></tr>')
            continue
        parts.append(
            f'<tr><td><strong>{short}</strong>　{rng}</td><td>{units}</td>'
            f'<td>{d["n"]} / {d["closed"]}</td><td>{fpct(d["win_rate"])}</td>'
            f'<td>{fdec(d["pf"])}</td><td class="{cls(d["pnl"])}">{fsign(d["pnl"])}</td>'
            f'<td class="{cls(d["top3"])}">{fsign(d["top3"])}</td>'
            f'<td class="{cls(d["per_unit"])}">{fsign(d["per_unit"])}</td>'
            f'<td>{fsign(d["avg_win"])}</td><td>{fsign(d["avg_loss"])}</td>'
            f'<td>{fdec(d["payoff"])}</td><td>{fpct(d["breakeven_wr"])}</td>'
            f'<td class="down">{fsign(d["worst"])}</td>'
            f'<td>{fnum(d["avg_hold"],0)} 天</td><td>{fnum(d["avg_dist"],1)}%</td></tr>'
        )
    parts.append("</tbody></table></div>")
    return "".join(parts)


def band_stats(rows, field, bands):
    """按 rows[i][field] 落入的分带统计四项指标。bands=[(label, lo, hi), ...]。"""
    out = []
    for label, lo, hi in bands:
        bucket = [r for r in rows if r.get(field) is not None and lo <= r[field] < hi]
        d = aggregate(bucket)
        out.append((label, d))
    return out


def band_table(title, note, bands_data):
    parts = [
        f'<div class="panel"><h3>{title}</h3><p class="small muted" style="margin:0 0 8px">{note}</p>',
        '<div class="table-wrap"><table>',
        '<thead><tr><th>分带</th><th>建/平</th><th>胜率</th><th>每单位净利</th>'
        '<th>保本胜率</th><th>利润集中度</th><th>净利</th></tr></thead><tbody>',
    ]
    for label, d in bands_data:
        gw = d["gross_win"] or 0
        conc = (d["top3"] / gw * 100) if gw > 0 else None
        parts.append(
            f'<tr><td><strong>{label}</strong></td><td>{d["n"]} / {d["closed"]}</td>'
            f'<td>{fpct(d["win_rate"])}</td>'
            f'<td class="{cls(d["per_unit"])}">{fsign(d["per_unit"])}</td>'
            f'<td>{fpct(d["breakeven_wr"])}</td>'
            f'<td>{fnum(conc,1)+"%" if conc is not None else "—"}</td>'
            f'<td class="{cls(d["pnl"])}">{fsign(d["pnl"])}</td></tr>'
        )
    parts.append("</tbody></table></div></div>")
    return "".join(parts)


def tier_bar_chart(tiers, year):
    max_abs = max((abs(d["pnl"]) for d in tiers.values()), default=1) or 1
    parts = [f'<div class="panel"><h3>{year} 年各档净利（同一比例尺）</h3><div class="diverging">']
    for key, (short, rng, units) in TIERS.items():
        d = tiers[key]
        width = abs(d["pnl"]) / max_abs * 48
        if d["pnl"] >= 0:
            left, style = 50, f"left:50%;width:{max(width,0.35):.2f}%;background:var(--red)"
        else:
            left, style = 50 - width, f"left:{50 - max(width,0.35):.2f}%;width:{max(width,0.35):.2f}%;background:var(--green)"
        parts.append(
            f'<div class="dv-row"><div class="dv-label">{short}<small>{rng}</small></div>'
            f'<div class="dv-track"><i class="dv-zero"></i><b style="{style}"></b></div>'
            f'<div class="dv-val {cls(d["pnl"])}">{fsign(d["pnl"])}</div></div>'
        )
    parts.append('</div><p class="small muted" style="margin-bottom:0">红线向右为盈利，绿线向左为亏损；t4 的柱长按同一年最大档位净利等比缩放。</p></div>')
    return "".join(parts)


def comment_block(year, data):
    tiers = data["tiers"]
    overall = data["overall"]
    ranked = sorted(((k, v["pnl"]) for k, v in tiers.items() if v["n"]), key=lambda x: x[1], reverse=True)
    top_key, top_pnl = ranked[0]
    bottom_key, bottom_pnl = ranked[-1]
    t4 = tiers["newhigh_t4"]
    base = data["base"]
    top_name = f'{TIERS[top_key][0]}（{TIERS[top_key][1]}）'
    bottom_name = f'{TIERS[bottom_key][0]}（{TIERS[bottom_key][1]}）'
    items = [
        f'<li><strong>主引擎：</strong>{top_name} 净利 <span class="{cls(top_pnl)}">{fsign(top_pnl)}</span>，'
        f'胜率 {fpct(tiers[top_key]["win_rate"])}、利润因子 {fdec(tiers[top_key]["pf"])}。</li>',
        f'<li><strong>最大拖累：</strong>{bottom_name} 净利 <span class="{cls(bottom_pnl)}">{fsign(bottom_pnl)}</span>，'
        f'建仓 {tiers[bottom_key]["n"]} 笔、平仓 {tiers[bottom_key]["closed"]} 笔。</li>',
    ]
    if t4["n"]:
        if t4["pnl"] > 0:
            share_text = f"，前三赢家合计 {fsign(t4['top3'])}（相当于该档净利的 {t4['top3']/t4['pnl']*100:.0f}%）"
        else:
            share_text = f"，但前三赢家合计 {fsign(t4['top3'])}，仍不足以覆盖其余交易的亏损"
        items.append(
            f'<li><strong>t4 专项：</strong>建仓 {t4["n"]} 笔、平仓 {t4["closed"]} 笔，'
            f'胜率 {fpct(t4["win_rate"])}，净利 <span class="{cls(t4["pnl"])}">{fsign(t4["pnl"])}</span>，'
            f'每单位 {fsign(t4["per_unit"])}{share_text}。</li>'
        )
    if base:
        delta = overall["pnl"] - base["pnl"]
        items.append(
            f'<li><strong>相对基线：</strong>E17 {fsign(overall["pnl"])} vs 基线 {fsign(base["pnl"])}，'
            f'差额 <span class="{cls(delta)}">{fsign(delta)}</span>。</li>'
        )
    detail = YEAR_NOTES.get(year, "该年度结论按档位实际盈亏自动生成，需结合当时市场环境理解。")
    return f'<p class="year-read">{html.escape(detail)}</p><ul class="tight">{"".join(items)}</ul>'


def build_report():
    years = completed_years()
    data = [x for x in (load_year(y) for y in years) if x]
    if not data:
        return "<!DOCTYPE html><html lang='zh-CN'><meta charset='utf-8'><body>暂无已完成年份。</body></html>"
    grand_rows = []
    for d in data:
        grand_rows.extend(d["raw_rows"])
    total_pnl = sum(d["overall"]["pnl"] for d in data)
    base_total = sum(d["base"]["pnl"] for d in data if d["base"])
    all_tiers = OrderedDict()
    # Rebuild all-year tier stats from retained raw rows.
    for key in TIERS:
        rows = []
        for d in data:
            for r in d["raw_rows"]:
                if r["tier"] == key:
                    rows.append(r)
        all_tiers[key] = aggregate(rows)
    total_closed = sum(d["overall"]["closed"] for d in data)
    total_n = sum(d["overall"]["n"] for d in data)
    win_rate = sum(d["overall"]["wins"] for d in data) / total_closed if total_closed else 0
    tot_win = sum(d["overall"]["gross_win"] for d in data)
    tot_loss = sum(d["overall"]["gross_loss"] for d in data)
    total_pf = tot_win / abs(tot_loss) if tot_loss else None

    nav = "".join(f'<a href="#y{d["year"]}">{d["year"]}</a>' for d in data)
    out = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '<title>E17 逐年分层回测报告</title><style>',
        ''':root{--bg:#f5f6f8;--surface:#fff;--ink:#1e2430;--muted:#697386;--line:#e3e6eb;--soft:#f0f2f5;--red:#c0392b;--red2:#faece9;--green:#1b7a4b;--green2:#e9f5ef;--gold:#a66a12;--gold2:#fbf3e4;--blue:#2d5b8e;--blue2:#ecf2f9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif;font-size:15px;line-height:1.7}
.page{max-width:1140px;margin:0 auto;padding:42px 28px 64px}
header{border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:26px}
.eyebrow{font-size:12px;letter-spacing:.16em;color:var(--blue);font-weight:700;margin-bottom:10px}
h1{font-size:32px;line-height:1.28;margin:0 0 10px;letter-spacing:-.02em}h2{font-size:23px;margin:38px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}h3{font-size:17px;margin:20px 0 9px}
p{margin:9px 0}.sub{color:var(--muted);max-width:850px;margin:0}.muted{color:var(--muted)}.small{font-size:12.5px}
.up{color:var(--red)}.down{color:var(--green)}.neutral{color:var(--gold)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:17px 18px}
.kpi .label{font-size:12px;color:var(--muted)}.kpi .value{font-size:27px;font-weight:700;line-height:1.25;margin-top:4px}.kpi .note{font-size:12px;color:var(--muted);margin-top:3px}
.summary{background:var(--surface);border-left:4px solid var(--blue);border-radius:8px;padding:18px 22px;margin:22px 0}.summary h2{border:0;margin:0 0 8px;padding:0;font-size:20px}.summary ul{margin:8px 0 0 19px;padding:0}.summary li{margin:5px 0}
.callout{border-radius:8px;padding:14px 17px;margin:15px 0;font-size:14px}.callout.gold{background:var(--gold2);border:1px solid #ead9b8}.callout.red{background:var(--red2);border:1px solid #efd0ca}
.table-wrap{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow-x:auto;margin:13px 0}table{width:100%;border-collapse:collapse;font-size:13px;min-width:960px}th,td{padding:9px 11px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}th{background:#fafbfc;color:#4b5565;font-size:12px;font-weight:600}th:first-child,td:first-child{text-align:left}tr:last-child td{border-bottom:0}tbody tr:hover{background:#fcfcfd}tfoot td{font-weight:700;background:#fafbfc;border-top:1px solid #cfd4dc}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:17px 19px;margin:14px 0}
.year-head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-top:42px}.year-head h2{margin:0;border:0;padding:0}.year-badge{font-size:12px;background:var(--blue2);color:var(--blue);border-radius:999px;padding:2px 10px;font-weight:700}
.year-kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:14px 0 16px}.yk{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 13px}.yk span{display:block;font-size:11.5px;color:var(--muted)}.yk strong{display:block;font-size:19px;margin-top:2px;font-variant-numeric:tabular-nums}
.year-read{background:var(--gold2);border-radius:7px;padding:12px 15px;margin:13px 0;border:1px solid #ead9b8}
ul.tight{margin:8px 0 0 18px;padding:0}ul.tight li{margin:5px 0}
.diverging{margin:10px 0 4px}.dv-row{display:grid;grid-template-columns:78px 1fr 102px;align-items:center;gap:9px;margin:8px 0}.dv-label{font-size:12.5px}.dv-label small{display:block;color:var(--muted);font-size:10.5px}.dv-track{position:relative;height:20px;background:var(--soft);border-radius:4px;overflow:hidden}.dv-zero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#aab1bc;z-index:2}.dv-track b{position:absolute;top:3px;bottom:3px;border-radius:2px;z-index:1}.dv-val{text-align:right;font-size:12.5px;font-variant-numeric:tabular-nums}
.toc{display:flex;gap:9px;flex-wrap:wrap;margin:18px 0}.toc a{text-decoration:none;color:var(--blue);background:var(--blue2);border:1px solid #d4e0ee;border-radius:999px;padding:3px 12px;font-size:12.5px}
.foot{margin-top:38px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}.foot p{margin:5px 0}
@media(max-width:900px){.kpis,.year-kpis{grid-template-columns:repeat(2,1fr)}.page{padding:28px 16px 48px}h1{font-size:26px}}
@media(max-width:480px){.kpis,.year-kpis{grid-template-columns:1fr}}
@media print{body{background:#fff;font-size:11px}.page{max-width:none;padding:0}h2{break-after:avoid}.table-wrap,.panel{break-inside:avoid}}
</style></head><body><main class="page">''',
        '<header><div class="eyebrow">DARVAS BOX · E17 ANNUAL TIER REVIEW</div>',
        '<h1>E17 逐年分层回测报告</h1>',
        f'<p class="sub">覆盖 {data[0]["year"]}–{data[-1]["year"]} 已完成年度 · 每年单独拆解 t1–t4 的收益、风险与利润集中度 · 生成于 {datetime.now():%Y-%m-%d %H:%M}</p></header>',
        f'<div class="toc">{nav}</div>',
        '<section class="kpis">',
        f'<div class="kpi"><div class="label">累计已实现净利</div><div class="value {cls(total_pnl)}">{fsign(total_pnl)}</div><div class="note">{total_n} 笔建仓 / {total_closed} 笔平仓</div></div>',
        f'<div class="kpi"><div class="label">整体胜率</div><div class="value">{win_rate*100:.1f}%</div><div class="note">按已平仓交易计</div></div>',
        f'<div class="kpi"><div class="label">整体利润因子</div><div class="value">{fdec(total_pf)}</div><div class="note">四年合并口径</div></div>',
        f'<div class="kpi"><div class="label">相对基线差额</div><div class="value {cls(total_pnl-base_total)}">{fsign(total_pnl-base_total)}</div><div class="note">基线合计 {fsign(base_total)}</div></div>',
        '</section>',
        '<section class="summary"><h2>先看结论</h2><ul>',
        f'<li><strong>利润不是均匀分布的。</strong>{data[0]["year"]}–{data[-1]["year"]} 累计净利 {fsign(total_pnl)}，其中 t1 贡献 {fsign(all_tiers["newhigh_t1"]["pnl"])}，占比 {all_tiers["newhigh_t1"]["pnl"]/total_pnl*100 if total_pnl else 0:.1f}%。</li>',
        f'<li><strong>t4 跨年接近打平。</strong>四年净利 {fsign(all_tiers["newhigh_t4"]["pnl"])}，利润因子 {fdec(all_tiers["newhigh_t4"]["pf"])}，胜率 {fpct(all_tiers["newhigh_t4"]["win_rate"])}，每单位仅 {fsign(all_tiers["newhigh_t4"]["per_unit"])}；2016 单年盈利不能视为稳定 edge。</li>',
        f'<li><strong>t2 是明确的负贡献档。</strong>四年净利 {fsign(all_tiers["newhigh_t2"]["pnl"])}，利润因子 {fdec(all_tiers["newhigh_t2"]["pf"])}，说明“距高点越近越好”并非严格单调。</li>',
        '</ul></section>',
        '<div class="callout gold"><strong>阅读方式：</strong>每一年先看年度总指标，再看四档明细，最后看“这一年发生了什么”。净利为已平仓已实现盈亏；“每单位”按平仓交易的建仓单位折算；保本胜率 = 1 /（1 + 平均盈利/平均亏损）。</div>',
        '<h2>年度总表</h2>',
        '<div class="table-wrap"><table><thead><tr><th>年份</th><th>建仓/平仓</th><th>胜率</th><th>净利</th><th>收益率</th><th>最大回撤</th><th>利润因子</th><th>基线净利</th><th>差额</th><th>t4 净利</th></tr></thead><tbody>',
    ]
    for d in data:
        o = d["overall"]
        base = d["base"]
        delta = o["pnl"] - base["pnl"] if base else None
        out.append(
            f'<tr><td><strong>{d["year"]}</strong></td><td>{o["n"]} / {o["closed"]}</td>'
            f'<td>{fpct(o["win_rate"])}</td><td class="{cls(o["pnl"])}">{fsign(o["pnl"])}</td>'
            f'<td class="{cls(d["equity"]["ret"])}">{fpct(d["equity"]["ret"],2)}</td>'
            f'<td class="down">{fpct(d["equity"]["maxdd"],2)}</td><td>{fdec(o["pf"])}</td>'
            f'<td>{fsign(base["pnl"]) if base else "—"}</td>'
            f'<td class="{cls(delta) if delta is not None else "neutral"}">{fsign(delta) if delta is not None else "—"}</td>'
            f'<td class="{cls(d["tiers"]["newhigh_t4"]["pnl"])}">{fsign(d["tiers"]["newhigh_t4"]["pnl"])}</td></tr>'
        )
    out.append(f'</tbody><tfoot><tr><td>合计</td><td>{total_n} / {total_closed}</td><td>{win_rate*100:.1f}%</td><td class="{cls(total_pnl)}">{fsign(total_pnl)}</td><td>—</td><td>—</td><td>{fdec(total_pf)}</td><td>{fsign(base_total)}</td><td class="{cls(total_pnl-base_total)}">{fsign(total_pnl-base_total)}</td><td class="{cls(all_tiers["newhigh_t4"]["pnl"])}">{fsign(all_tiers["newhigh_t4"]["pnl"])}</td></tr></tfoot></table></div>')
    out.append('<p class="small muted">年度净利不能简单相加成复利账户收益；上表“合计”只用于汇总已实现盈亏金额。收益率取自各年 equity_snapshot，最大回撤为年内回撤。</p>')

    for d in data:
        y = d["year"]
        o = d["overall"]
        e = d["equity"]
        base = d["base"]
        delta = o["pnl"] - base["pnl"] if base else None
        out.append(f'<section id="y{y}"><div class="year-head"><h2>{y} 年</h2><span class="year-badge">{o["n"]} 笔建仓 · {o["closed"]} 笔平仓</span></div>')
        out.append('<div class="year-kpis">')
        cards = [
            ("净利", f'<span class="{cls(o["pnl"])}">{fsign(o["pnl"])}</span>'),
            ("收益率", f'<span class="{cls(e["ret"])}">{fpct(e["ret"],2)}</span>'),
            ("最大回撤", f'<span class="down">{fpct(e["maxdd"],2)}</span>'),
            ("胜率", fpct(o["win_rate"])),
            ("利润因子", fdec(o["pf"])),
            ("相对基线", f'<span class="{cls(delta) if delta is not None else "neutral"}">{fsign(delta) if delta is not None else "—"}</span>'),
        ]
        for label, val in cards:
            out.append(f'<div class="yk"><span>{label}</span><strong>{val}</strong></div>')
        out.append('</div>')
        out.append(tier_table(d["tiers"]))
        out.append('<h3>概率分布统计（box 六维后验）</h3>')
        bh = [("0–5%",0,5),("5–10%",5,10),("10–20%",10,20),("20–40%",20,40),("40%+",40,1e9)]
        bs = [("0–1%",0,1),("1–2%",1,2),("2–3%",2,3),("3–5%",3,5),("5%+",5,1e9)]
        out.append(band_table("① box 高度梯度", "箱高=(箱顶−箱底)/箱底；当前规则仅 ≥5% 入选，此处按全分布观察哪一带真正有 edge", band_stats(d["raw_rows"], "box_height", bh)))
        out.append(band_table("② 突破强度梯度", "突破强度=(触发价−箱顶)/箱顶；当前回测器固定 3% 突破触发，本维无梯度可分，需参数化实验", band_stats(d["raw_rows"], "break_strength", bs)))
        tb_yes = aggregate([r for r in d["raw_rows"] if r.get("is_topbox") is True])
        tb_no = aggregate([r for r in d["raw_rows"] if r.get("is_topbox") is False])
        out.append(band_table("③ 最高 box（52周口径）对照", "箱顶≥52周高点×0.999 视为最高 box；当前口径下命中极少，需用 box 序列历史重新定义“上升走势最高箱体”", [("是（最高box）", tb_yes), ("否", tb_no)]))
        out.append('<p class="small muted">量比与 box 宽度两维需扩展回测器记录字段后才能后验；当前从 reason_json 已有字段提取。</p>')
        out.append('<h3>这一年发生了什么</h3>')
        out.append(comment_block(y, d))
        out.append(tier_bar_chart(d["tiers"], y))
        out.append('</section>')

    out.append('<h2>跨年观察与限制</h2>')
    out.append('<div class="callout red"><strong>不要把某一年的第一名当规律。</strong>2016 年 t4 盈利、t1 亏损，结构与 2014–2015 完全反转；这种单年排序变化说明档位表现对市场环境敏感。真正稳定的结论是“t1 是主要利润来源、t2 没有正期望”，而 t4 更像高风险观察仓。</div>')
    out.append('<div class="panel"><h3>四个档位的四年合并风险收益</h3>')
    out.append(tier_table(all_tiers))
    out.append('<p class="small muted" style="margin-bottom:0">合并口径包含所有已完成年度。t4 的“前三赢家合计”与净利高度接近，说明其正结果对未来少数极端行情的依赖很强。</p></div>')
    out.append('<div class="foot"><p><strong>数据：</strong>本地通达信日线 → E17 scratch 回测库；档位来自 plan_order.reason_json.sizing_basis；持仓只取 position_leg.leg_no=1 首次建仓；净利为已平仓 realized_pnl。</p>')
    out.append('<p><strong>限制：</strong>本报告只覆盖已完整写入 e17_metrics.csv 的年份；当前正在运行的年份与未完成的样本外年份不纳入。历史回测不代表实盘可复制收益。</p>')
    out.append('<p><strong>免责声明：</strong>以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。</p></div>')
    out.append('</main></body></html>')
    return "".join(out)


def main():
    OUT.write_text(build_report(), encoding="utf-8")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
