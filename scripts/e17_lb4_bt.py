#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LB-4 B 类成长速度准入门槛扫描生成器（docs/29 §七 LB-4，2026-10-31 立项）。

读 var/bt/lb4_bt/ 下 6 档(box.max_growth_days = off/5/6/7/8/10) × N 年的库，
按「档位」横向对比（不是带内分带），报四项固定口径 + PF + 剔除前三 + 笔数 + 已实现净利。

核心研究问题：把「只买快成型箱体（成长速度 ≤ 阈值）」做成买入准入门槛，是否提升收益？最优门槛在哪？

口径（与 A 类 e17_lb4_growthspeed.py / B 类 e17_volmult.py 同源）：
  每单位净利 = Σrealized_pnl / Σunits；units = cost / unit_val，unit_val = 开仓日权益 × 0.02
  胜率 / 保本胜率 / 缺口 / 利润集中度(前三赢家占 gross_win) / PF / 剔除前三净利
  ⚠️ 关键：门槛每收紧一档，笔数必然下降——报告并列「笔数」与「已实现净利总额」，
     区分「每单位变好」与「总盘子缩水」两件事。

用法：
  python scripts/e17_lb4_bt.py --diag      # 诊断：档位×年份 n 分布
  python scripts/e17_lb4_bt.py             # 全量产出 report.html/md + data.json
"""
import argparse
import json
import sqlite3
import statistics
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LB4_DIR = ROOT / "var" / "bt" / "lb4_bt"
OUT_DIR = ROOT / "docs" / "experiments"

# 档位顺序（off=基线）
TIERS = ["off", "5", "6", "7", "8", "10"]
TIER_LABEL = {
    "off": "off（基线）",
    "5": "≤5 日",
    "6": "≤6 日",
    "7": "≤7 日",
    "8": "≤8 日",
    "10": "≤10 日",
}
UNIT_PCT = 0.02  # 1 单位 = 2% 权益


def connect(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def pct(x, d=1):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def money(x, d=0):
    return "—" if x is None else f"{x:,.{d}f}"


def fnum(x, d=2):
    return "—" if x is None else f"{x:.{d}f}"


def fsign(x, d=0):
    return "—" if x is None else f"{x:+,.{d}f}"


def median(v):
    return statistics.median(v) if v else None


def parse_dbname(name):
    # bt_lb4_off_2018.db -> (tier, year)；bt_baseA_2018.db -> (off, year)
    stem = Path(name).stem
    if stem.startswith("bt_lb4_"):
        parts = stem.split("_")
        tier = parts[2]
        year = int(parts[3])
    elif stem.startswith("bt_baseA_"):
        tier = "off"
        year = int(stem.split("_")[2])
    else:
        return None, None
    return tier, year


def load_db(db: Path):
    """读一个库 → 每笔已平仓交易 + 权益轨迹。"""
    tier, year = parse_dbname(db.name)
    c = connect(db)
    try:
        eq_rows = c.execute("SELECT date, total FROM equity_snapshot ORDER BY date").fetchall()
        eq = dict(eq_rows)
        first_eq = eq_rows[0][1] if eq_rows else 0.0
        last_eq = eq_rows[-1][1] if eq_rows else 0.0

        # 每笔已平仓：cost 来自 position_leg 求和，units 用开仓日权益 × 0.02
        q = """
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.realized_pnl, p.close_reason,
               SUM(l.qty * l.price) AS cost,
               SUM(l.qty) AS qty
        FROM position p
        JOIN position_leg l ON l.position_id = p.id
        WHERE p.status = 'closed'
        GROUP BY p.id
        ORDER BY p.opened_date, p.code
        """
        rows = c.execute(q).fetchall()
        trades = []
        for (pid, code, od, cd, pnl, creason, cost, qty) in rows:
            base_eq = eq.get(od) or first_eq or 0.0
            unit_val = base_eq * UNIT_PCT
            units = (cost / unit_val) if unit_val > 0 else 0.0
            trades.append(dict(
                tier=tier, year=year, code=code, opened=od, closed=cd,
                pnl=pnl, close_reason=creason, cost=cost, qty=qty, units=units,
            ))
        return tier, year, trades, first_eq, last_eq
    finally:
        c.close()


def load_all():
    by_db = OrderedDict()
    # off 档 = 旧基线库（等价性已验证，不重跑）；5/6/7/8/10 档 = lb4_bt 库
    db_files = list(LB4_DIR.glob("bt_lb4_*.db"))
    db_files += list((ROOT / "var" / "bt" / "base").glob("bt_baseA_*.db"))
    for db in sorted(db_files):
        if "-wal" in db.name or "-shm" in db.name:
            continue
        tier, year = parse_dbname(db.name)
        if tier is None:
            continue
        if tier not in TIERS:
            continue
        trades, first_eq, last_eq = load_db(db)[2:]
        # 同一 (tier, year) 若 lb4_bt 库存在（如 off 也在 lb4_bt 下跑过），优先用 lb4_bt 库；
        # 否则用 base 库作 off 档
        key = f"{tier}_{year}"
        if key in by_db:
            continue  # 已存在（lb4_bt 库优先，先遍历）
        by_db[key] = dict(tier=tier, year=year, trades=trades,
                          first_eq=first_eq, last_eq=last_eq)
    return by_db


def agg(trades):
    """一笔一 dict → 四项口径 + PF + 剔除前三。"""
    closed = [r for r in trades if r["pnl"] is not None]
    n = len(closed)
    if n == 0:
        return dict(n=0)
    wins = [r["pnl"] for r in closed if r["pnl"] > 0]
    losses = [r["pnl"] for r in closed if r["pnl"] <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    tot = sum(r["pnl"] for r in closed)
    units = sum(r["units"] for r in closed)
    per_unit = tot / units if units > 0 else None
    win_rate = len(wins) / n
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    breakeven = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else None
    gap = (win_rate - breakeven) if breakeven is not None else None
    top3 = sum(sorted([r["pnl"] for r in closed], reverse=True)[:3])
    share = top3 / gross_win if gross_win > 0 else None
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else None)
    ex_top3 = tot - top3
    return dict(
        n=n, win_rate=win_rate, per_unit=per_unit, breakeven=breakeven, gap=gap,
        pf=pf, share=share, ex_top3=ex_top3, tot=tot, units=units,
    )


def tier_summary(by_db, tier):
    """某档位跨年合并：所有该档库的 trades 合并算口径 + 逐年明细。"""
    alltrades = []
    per_year = {}
    eq_start = 0.0
    eq_end = 0.0
    for key, d in by_db.items():
        if d["tier"] != tier:
            continue
        alltrades.extend(d["trades"])
        per_year[d["year"]] = agg(d["trades"])
        eq_start += d["first_eq"]
        eq_end += d["last_eq"]
    m = agg(alltrades)
    m["tier"] = tier
    m["label"] = TIER_LABEL[tier]
    m["per_year"] = per_year
    m["eq_start"] = eq_start
    m["eq_end"] = eq_end
    m["eq_var"] = eq_end - eq_start  # 权益变动（加法账，含年末浮盈）
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", action="store_true", help="只出诊断，不写产物")
    a = ap.parse_args()

    by_db = load_all()
    if not by_db:
        print("未找到任何库（var/bt/lb4_bt/ 为空）。先跑 e17_lb4_bt_run.py。")
        return 1

    # ★ 年集对齐（P2-6a 教训：可比口径按被比较对象的可用年集算）。
    # off 档读 base 库全 14 年，而 5/6/7/8/10 档只有代表年。若不裁剪，off(14 年) vs 其余(4 年) 不可比。
    # 规则：可用年集 = 5/6/7/8/10 档「至少一个库已存在」的年份；所有档位（含 off）都只在此年集上汇总。
    non_off_years = sorted({d["year"] for d in by_db.values() if d["tier"] != "off"})
    years = non_off_years if non_off_years else sorted({d["year"] for d in by_db.values()})
    # 裁剪 by_db：off 档只保留 years 内的年份；非 off 档照单全收
    by_db = {k: d for k, d in by_db.items()
             if (d["tier"] != "off" or d["year"] in years)}

    # 诊断：档位 × 年份 覆盖
    print(f"库数 {len(by_db)}  对齐年集 {years}")
    print("\n=== 档位 × 年份 n（已平仓笔数 / 已实现净利）===")
    for tier in TIERS:
        for y in years:
            key = f"{tier}_{y}"
            if key not in by_db:
                print(f"  {TIER_LABEL[tier]:<10} {y}: （缺失）")
                continue
            d = by_db[key]
            m = agg(d["trades"])
            print(f"  {TIER_LABEL[tier]:<10} {y}: n={m['n']} pnl={money(m['tot'])}")

    if a.diag:
        return 0

    # 档位汇总
    summaries = [tier_summary(by_db, t) for t in TIERS]

    # 逐年矩阵：档位 × 年份 每单位净利 + 笔数
    year_matrix = OrderedDict()
    for y in years:
        row = {}
        for t in TIERS:
            key = f"{t}_{y}"
            if key in by_db:
                m = agg(by_db[key]["trades"])
                row[t] = {"n": m["n"], "per_unit": m["per_unit"], "tot": m["tot"]}
            else:
                row[t] = None
        year_matrix[str(y)] = row

    data = dict(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        tiers=[{k: v for k, v in s.items() if k not in ("per_year",)}
               for s in summaries],
        per_year={t: {str(y): {k: v.get(k) for k in ("n", "win_rate", "per_unit", "ex_top3", "tot")}
                      for y, v in s["per_year"].items()} for t, s in zip(TIERS, summaries)},
        year_matrix=year_matrix,
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "e17_lb4_growthspeed_bt_data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # 控制台总表
    print("\n=== 档位总表（每单位净利 / 胜率 / PF / 剔除前三 / 笔数 / 已实现净利）===")
    hdr = f"{'档位':<10}{'笔数':>5}{'胜率':>7}{'每单位':>9}{'保本胜率':>8}{'缺口':>7}{'PF':>6}{'集中度':>7}{'剔前三':>11}{'已实现':>12}"
    print(hdr)
    for s in summaries:
        if s["n"] == 0:
            print(f"{s['label']:<10}{0:>5}   (无样本)")
            continue
        pf = "inf" if s["pf"] == float("inf") else (f"{s['pf']:.2f}" if s["pf"] is not None else "—")
        print(f"{s['label']:<10}{s['n']:>5}"
              f"{pct(s['win_rate'],1) if s['win_rate'] is not None else '—':>7}"
              f"{money(s['per_unit']):>9}"
              f"{pct(s['breakeven'],1) if s['breakeven'] is not None else '—':>8}"
              f"{fnum(s['gap'],1) if s['gap'] is not None else '—':>7}"
              f"{pf:>6}"
              f"{pct(s['share'],0) if s['share'] is not None else '—':>7}"
              f"{money(s['ex_top3']):>11}"
              f"{money(s['tot']):>12}")
    print("\ndata.json 已写。")

    # 渲染 HTML + MD 报告
    html = render_html(data, summaries, year_matrix, years)
    md = render_md(data, summaries, year_matrix, years)
    (OUT_DIR / "e17_lb4_growthspeed_bt_report.html").write_text(html, encoding="utf-8")
    (OUT_DIR / "e17_lb4_growthspeed_bt_report.md").write_text(md, encoding="utf-8")
    print("已写 report.html / report.md / data.json")
    return 0


CSS = """
:root{--bg:#f5f6f8;--surface:#fff;--ink:#1e2430;--muted:#697386;--line:#e3e6eb;
--soft:#f0f2f5;--red:#c0392b;--red2:#faece9;--green:#1b7a4b;--green2:#e9f5ef;
--gold:#a66a12;--gold2:#fbf3e4;--blue:#2d5b8e;--blue2:#ecf2f9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif;font-size:15px;line-height:1.7}
.page{max-width:1220px;margin:0 auto;padding:42px 28px 64px}
header{border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:26px}
.eyebrow{font-size:12px;letter-spacing:.16em;color:var(--blue);font-weight:700;margin-bottom:10px}
h1{font-size:30px;line-height:1.28;margin:0 0 10px;letter-spacing:-.02em}
h2{font-size:22px;margin:36px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:17px;margin:20px 0 9px}
p{margin:9px 0}.sub{color:var(--muted);max-width:960px;margin:0}
.muted{color:var(--muted)}.small{font-size:12.5px}
.up{color:var(--red)}.down{color:var(--green)}.neutral{color:var(--gold)}
.table-wrap{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow-x:auto;margin:13px 0}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:760px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
th{background:#fafbfc;color:#4b5565;font-size:12px;font-weight:600}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fcfcfd}
.callout{border-radius:8px;padding:14px 17px;margin:15px 0;font-size:14px}
.callout.gold{background:var(--gold2);border:1px solid #ead9b8}
.callout.blue{background:var(--blue2);border:1px solid #d4e0ee}
.foot{margin-top:38px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
code{background:var(--soft);border-radius:3px;padding:1px 5px;font-size:12.5px}
"""


def cls(x):
    if x is None:
        return "neutral"
    return "up" if x > 0 else ("down" if x < 0 else "neutral")


def render_html(data, summaries, year_matrix, years):
    rows = []
    for s in summaries:
        if s["n"] == 0:
            rows.append(f'<tr><td><strong>{s["label"]}</strong></td><td>0</td>'
                        f'<td colspan="9">无样本</td></tr>')
            continue
        pf = "∞" if s["pf"] == float("inf") else (f'{s["pf"]:.2f}' if s["pf"] is not None else "—")
        rows.append(
            f'<tr><td><strong>{s["label"]}</strong></td>'
            f'<td>{s["n"]}</td>'
            f'<td>{pct(s["win_rate"]) if s["win_rate"] is not None else "—"}</td>'
            f'<td class="{cls(s["per_unit"])}">{money(s["per_unit"])}</td>'
            f'<td>{pct(s["breakeven"]) if s["breakeven"] is not None else "—"}</td>'
            f'<td class="{cls(s["gap"])}">{fnum(s["gap"],1) if s["gap"] is not None else "—"}</td>'
            f'<td>{pf}</td>'
            f'<td>{pct(s["share"],0) if s["share"] is not None else "—"}</td>'
            f'<td class="{cls(s["ex_top3"])}">{money(s["ex_top3"])}</td>'
            f'<td class="{cls(s["tot"])}">{money(s["tot"])}</td></tr>')
    table_rows = "".join(rows)

    # 逐年矩阵
    yrows = []
    for y in years:
        cells = []
        for t in TIERS:
            d = year_matrix[str(y)].get(t)
            if d is None:
                cells.append('<td class="muted">—</td>')
            else:
                cells.append(f'<td class="{cls(d["per_unit"])}">{money(d["per_unit"])}'
                             f'<span class="muted small">({d["n"]})</span></td>')
        yrows.append(f'<tr><td><strong>{y}</strong></td>{"".join(cells)}</tr>')
    ytbl = "".join(yrows)
    yhead = "".join(f"<th>{TIER_LABEL[t]}</th>" for t in TIERS)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LB-4 B 类 · 成长速度准入门槛 · 档位对比</title>
<style>{CSS}</style></head><body><div class="page">
<header>
<div class="eyebrow">DARVAS · E17 · 实验报告 · 十一维 #11 Box 生命体征 · LB-4 成长速度 · B 类</div>
<h1>LB-4 B 类 · 成长速度买入准入门槛</h1>
<p class="sub">把「只买快成型箱体（成长速度 ≤ 阈值）」做成引擎买入准入门槛，逐档回测对比基线（off 档）。
研究问题：收紧成长速度门槛是否提升收益？最优门槛在哪？只读 <code>var/bt/lb4_bt/</code>。</p>
</header>

<h2>一、档位总表（跨年合并）</h2>
<p>四项固定口径 + PF + 剔除前三 + <b>笔数</b> + <b>已实现净利总额</b>。⚠️ 门槛每收紧一档，笔数必然下降——
读表须区分「每单位变好」与「总盘子缩水」两件事。</p>
<div class="table-wrap"><table><thead><tr><th>档位</th><th>笔数</th><th>胜率</th><th>每单位净利</th>
<th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th><th>剔除前三净利</th><th>已实现净利</th></tr></thead>
<tbody>{table_rows}</tbody></table></div>

<h2>二、逐年矩阵（档位 × 年份）</h2>
<p>逐格「每单位净利（笔数）」，用于区分「某一年全市场现象」与「长期结构」。</p>
<div class="table-wrap"><table><thead><tr><th>年份</th>{yhead}</tr></thead>
<tbody>{ytbl}</tbody></table></div>

<div class="callout blue"><b>读法</b>：每单位净利 = Σ已实现 realized_pnl / Σ单位（1 单位=2% 权益，按开仓日权益折算）。
「笔数下降」是准入收紧的必然结果，不是缺陷；关键看「每单位是否变好」且「剔除前三后是否仍为正」。</div>

<div class="foot">
<p><b>结论边界</b>：单年不结论、跨年才下结论；档位最优须再降粒度复核是否成片（P0-1 教训）。
单因子轮换：只动 <code>box.max_growth_days</code>，其余十维固定。样本外（2019+）未参与。</p>
<p>生成于 {data['generated']} · 生成器 <code>scripts/e17_lb4_bt.py</code></p>
</div>
</div></body></html>"""


def render_md(data, summaries, year_matrix, years):
    L = []
    A = L.append
    A("# LB-4 B 类 · 成长速度买入准入门槛 · 档位对比")
    A("")
    A("> 只动 `box.max_growth_days`（off/5/6/7/8/10），其余十维固定。生成器 `scripts/e17_lb4_bt.py`。")
    A("")
    A(f"生成时间：{data['generated']}")
    A("")
    A("## 一、档位总表（跨年合并）")
    A("")
    A("| 档位 | 笔数 | 胜率 | 每单位净利 | 保本胜率 | 缺口 | PF | 集中度 | 剔除前三 | 已实现净利 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in summaries:
        if s["n"] == 0:
            A(f"| {s['label']} | 0 | — | — | — | — | — | — | — | — |")
            continue
        pf = "∞" if s["pf"] == float("inf") else (f"{s['pf']:.2f}" if s["pf"] is not None else "—")
        A(f"| {s['label']} | {s['n']} | "
          f"{pct(s['win_rate']) if s['win_rate'] is not None else '—'} | "
          f"{money(s['per_unit'])} | "
          f"{pct(s['breakeven']) if s['breakeven'] is not None else '—'} | "
          f"{fnum(s['gap'],1) if s['gap'] is not None else '—'} | "
          f"{pf} | "
          f"{pct(s['share'],0) if s['share'] is not None else '—'} | "
          f"{money(s['ex_top3'])} | "
          f"{money(s['tot'])} |")
    A("")
    A("⚠️ 门槛每收紧一档，笔数必然下降——区分「每单位变好」与「总盘子缩水」。")
    A("")
    A("## 二、逐年矩阵（每单位净利（笔数））")
    A("")
    A("| 年份 | " + " | ".join(TIER_LABEL[t] for t in TIERS) + " |")
    A("|---|" + "---|" * len(TIERS))
    for y in years:
        cells = []
        for t in TIERS:
            d = year_matrix[str(y)].get(t)
            cells.append(f"{money(d['per_unit'])}（{d['n']}）" if d else "—")
        A(f"| {y} | " + " | ".join(cells) + " |")
    A("")
    A("## 结论边界")
    A("")
    A("- 单年不结论、跨年才下结论；档位最优须再降粒度复核（P0-1 教训）。")
    A("- 单因子轮换：只动 `box.max_growth_days`；样本外（2019+）未参与。")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
