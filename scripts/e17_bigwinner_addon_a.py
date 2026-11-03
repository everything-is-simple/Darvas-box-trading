#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大赢家加仓 A 类分析 · 突破信号密度 vs 后续涨幅（docs/47 实验一，2026-11-03）。

研究问题（docs/29 §640 立项）：
  系统利润高度集中在少数大赢家（MFE>50%），但仓位是「均等首买 + 一律不加码」。
  本实验统计大赢家持仓期间「突破信号密度」与「后续涨幅」的关系，回答：
  「连续突破」本身是不是更强的做多信号？加仓（在更高 box 再突破时追加）有无概率依据？

数据来源（docs/47 §〇 定案，只读不重算）：
  var/bt/base/bt_baseA_<year>.db（2005–2018 新基线 14 年库，阶梯制）

口径（复刻 e17_giveback.py，保持一致）：
  MFE      = max(daily_bar.high 于 [opened,closed]) / avg_cost − 1
  大赢家   = MFE > 0.5（MFE>50%）
  成本     = Σ(position_leg.qty×price)，不用 avg_cost×qty
  突破信号 = box.broken_date 非空且落在持仓窗口 [opened_date, closed_date] 内
              （box 表的 broken_date 才是真实向上突破；signal 表 stage 全是 watching 不可用）
  信号密度 = 持仓期内再突破 box 数 / 持有天数（交易日）

输出（docs/experiments/）：
  e17_bigwinner_addon_a_report.html / .md / _data.json
"""
import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt" / "base"
EXP = ROOT / "docs" / "experiments"
OUT_HTML = EXP / "e17_bigwinner_addon_a_report.html"
OUT_MD = EXP / "e17_bigwinner_addon_a_report.md"
OUT_JSON = EXP / "e17_bigwinner_addon_a_data.json"

MFE_THRESHOLD = 0.5  # 大赢家：MFE > 50%

# 信号密度三带（带界预设，禁按结果挪）
# 密度单位 = 持有期内再突破次数 / 持有天数（交易日）
DENSITY_BANDS = [
    ("稀疏", 0.0, 0.01),     # < 1 次/100 天（几乎无再突破）
    ("中等", 0.01, 0.03),    # 1~3 次/100 天
    ("密集", 0.03, float("inf")),  # ≥3 次/100 天
]

# 绝对再突破次数三带（交叉验证，对应 docs/29 §644「≥2 次突破」观察）
# 密度口径有内生性（持有久→密度天然低），绝对次数更直接对应「加仓机会」
COUNT_BANDS = [
    ("0 次再突破", lambda c: c == 0),
    ("1 次再突破", lambda c: c == 1),
    ("≥2 次再突破", lambda c: c >= 2),
]


def years_list():
    return sorted(int(p.stem.split("_")[2].split(".")[0])
                  for p in BT.glob("bt_baseA_*.db"))


def connect(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def load_trades():
    """14 库逐笔加载已平仓持仓 + MFE + 信号密度。"""
    trades = []
    funnel = {"dbs": 0, "closed": 0, "no_cost": 0, "no_bar": 0, "winners": 0}
    for y in years_list():
        db = BT / f"bt_baseA_{y}.db"
        con = connect(db)
        con.row_factory = sqlite3.Row
        sql = """
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.realized_pnl,
               (SELECT SUM(qty)       FROM position_leg WHERE position_id=p.id) AS qty,
               (SELECT SUM(qty*price) FROM position_leg WHERE position_id=p.id) AS cost_basis
        FROM position p
        WHERE p.status='closed'
        """
        rows = con.execute(sql).fetchall()
        funnel["dbs"] += 1
        for r in rows:
            pnl = r["realized_pnl"]
            qty = r["qty"] or 0
            cost = r["cost_basis"] or 0.0
            if pnl is None or not qty or cost <= 0:
                funnel["no_cost"] += 1
                continue
            funnel["closed"] += 1

            bars = con.execute(
                "SELECT date, high FROM daily_bar WHERE code=? AND date>=? AND date<=? "
                "ORDER BY date", (r["code"], r["opened_date"], r["closed_date"])).fetchall()
            if not bars:
                funnel["no_bar"] += 1
                continue

            avg_cost = cost / qty
            max_high = max(b["high"] for b in bars)
            mfe = max_high / avg_cost - 1.0
            hold_days = len(bars)

            # 信号密度：持仓窗口内该 code 被突破的 box 数
            n_breaks = con.execute(
                "SELECT COUNT(*) FROM box WHERE code=? AND broken_date IS NOT NULL "
                "AND broken_date>=? AND broken_date<=?",
                (r["code"], r["opened_date"], r["closed_date"])).fetchone()[0]

            density = n_breaks / hold_days if hold_days > 0 else 0.0

            trades.append({
                "year": y, "code": r["code"],
                "opened": r["opened_date"], "closed": r["closed_date"],
                "pnl": round(pnl, 2), "cost": round(cost, 2),
                "mfe": mfe, "hold_days": hold_days,
                "n_breaks": n_breaks, "density": density,
            })
        con.close()

    winners = [t for t in trades if t["mfe"] > MFE_THRESHOLD]
    funnel["winners"] = len(winners)
    return trades, winners, funnel


def band_of(density):
    for label, lo, hi in DENSITY_BANDS:
        if lo <= density < hi:
            return label
    return "密集"


def agg(sub):
    """一个子集的固定口径汇总。"""
    if not sub:
        return None
    n = len(sub)
    mfe_med = statistics.median([t["mfe"] for t in sub])
    pnl_med = statistics.median([t["pnl"] for t in sub])
    wins = sum(1 for t in sub if t["pnl"] > 0)
    winrate = wins / n
    gross_win = sum(t["pnl"] for t in sub if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else None
    # 利润集中度：前三赢家 / 毛盈利
    top3 = sum(sorted([t["pnl"] for t in sub], reverse=True)[:3])
    conc = (top3 / gross_win) if gross_win > 0 else None
    # 剔除前三净利
    pnl_ex_top3 = sum(t["pnl"] for t in sub) - top3
    # 保本胜率 = 1/(1+平均盈利/平均亏损)
    avg_win = statistics.mean([t["pnl"] for t in sub if t["pnl"] > 0]) if wins else 0
    avg_loss = statistics.mean([t["pnl"] for t in sub if t["pnl"] < 0]) if (n - wins) else 0
    breakeven = 1.0 / (1.0 + avg_win / avg_loss) if avg_loss > 0 else None
    return {
        "n": n, "mfe_med": mfe_med, "pnl_med": pnl_med,
        "winrate": winrate, "pf": pf, "conc": conc,
        "pnl_ex_top3": pnl_ex_top3, "breakeven": breakeven,
        "total_pnl": sum(t["pnl"] for t in sub),
    }


def build_report(winners, funnel):
    bands = {}
    for label, _, _ in DENSITY_BANDS:
        sub = [t for t in winners if band_of(t["density"]) == label]
        bands[label] = {"sub": sub, "agg": agg(sub)}

    # 绝对次数分带（交叉验证）
    count_bands = {}
    for label, pred in COUNT_BANDS:
        sub = [t for t in winners if pred(t["n_breaks"])]
        count_bands[label] = {"sub": sub, "agg": agg(sub)}

    # 全体大赢家汇总
    all_agg = agg(winners)

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "funnel": funnel,
        "mfe_threshold": MFE_THRESHOLD,
        "density_bands": [{"label": l, "lo": lo, "hi": hi} for l, lo, hi in DENSITY_BANDS],
        "all_winners": all_agg,
        "bands": {k: v["agg"] for k, v in bands.items()},
        "count_bands": {k: v["agg"] for k, v in count_bands.items()},
        "winners": winners,
    }
    return data


def _fmt(a, key, fmt):
    v = a[key] if a else None
    return "—" if v is None else fmt(v)


def _band_rows_md(band_map):
    rows = []
    for label, a in band_map.items():
        if a is None:
            rows.append(f"| {label} | 0 | — | — | — | — | — | — |")
            continue
        pf = f"{a['pf']:.2f}" if a['pf'] else "—"
        conc = f"{a['conc']*100:.1f}%" if a['conc'] else "—"
        rows.append(
            f"| {label} | {a['n']} | {a['mfe_med']*100:.1f}% | {a['pnl_med']:,.0f} | "
            f"{a['winrate']*100:.1f}% | {pf} | {conc} | {a['pnl_ex_top3']:,.0f} |")
    return rows


def _band_rows_html(band_map):
    rows = []
    for label, a in band_map.items():
        if a is None:
            rows.append(f"<tr><td>{label}</td><td>0</td><td colspan=6>—</td></tr>")
            continue
        pf = f"{a['pf']:.2f}" if a['pf'] else "—"
        conc = f"{a['conc']*100:.1f}%" if a['conc'] else "—"
        rows.append(
            f"<tr><td>{label}</td><td>{a['n']}</td><td>{a['mfe_med']*100:.1f}%</td>"
            f"<td>{a['pnl_med']:,.0f}</td><td>{a['winrate']*100:.1f}%</td><td>{pf}</td>"
            f"<td>{conc}</td><td>{a['pnl_ex_top3']:,.0f}</td></tr>")
    return rows


def main():
    trades, winners, funnel = load_trades()
    data = build_report(winners, funnel)

    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    HDR = "| 带 | n | MFE中位 | 每笔净利中位 | 胜率 | PF | 集中度 | 剔除前三 |"
    SEP = "|---|---|---|---|---|---|---|---|"

    # ---- markdown ----
    all_agg = data["all_winners"]
    lines = []
    lines.append("# 大赢家加仓 A 类分析 · 突破信号密度 vs 后续涨幅\n")
    lines.append(f"> 生成于 {data['generated_at']} · 数据源 var/bt/base/bt_baseA_<year>.db（新基线 14 年）\n")
    lines.append("## 漏斗\n")
    for k, v in funnel.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append(f"## 全体大赢家（MFE>{MFE_THRESHOLD*100:.0f}%，共 {all_agg['n']} 笔）\n")
    lines.append(f"- MFE 中位数: {all_agg['mfe_med']*100:.1f}%")
    lines.append(f"- 每笔净利中位数: {all_agg['pnl_med']:,.0f}")
    lines.append(f"- 胜率: {all_agg['winrate']*100:.1f}%")
    lines.append(f"- PF: {all_agg['pf']:.2f}" if all_agg['pf'] else "- PF: —")
    lines.append(f"- 利润集中度(前三/毛盈): {all_agg['conc']*100:.1f}%" if all_agg['conc'] else "- 集中度: —")
    lines.append(f"- 剔除前三净利: {all_agg['pnl_ex_top3']:,.0f}")
    lines.append("")
    lines.append("## 口径 A：信号密度分带（= 持有期再突破 box 数 / 持有天数）\n")
    lines.append("> 注：密度口径有内生性——持有越久密度天然越低。故另设口径 B（绝对次数）交叉验证。\n")
    lines.append(HDR)
    lines.append(SEP)
    lines.extend(_band_rows_md(data["bands"]))
    lines.append("")
    lines.append("## 口径 B：绝对再突破次数分带（交叉验证，对应 docs/29 §644「≥2 次突破」）\n")
    lines.append(HDR)
    lines.append(SEP)
    lines.extend(_band_rows_md(data["count_bands"]))
    lines.append("")
    lines.append("## 判定\n")
    dense = data["bands"]["密集"]
    sparse = data["bands"]["稀疏"]
    if dense and sparse and dense["n"] >= 5 and sparse["n"] >= 5:
        lines.append(f"- 密度口径：密集带 n={dense['n']} vs 稀疏带 n={sparse['n']}；")
        lines.append(f"  MFE 中位 {dense['mfe_med']*100:.1f}% vs {sparse['mfe_med']*100:.1f}%；"
                     f"剔除前三 {dense['pnl_ex_top3']:,.0f} vs {sparse['pnl_ex_top3']:,.0f}")
    else:
        lines.append("- 密度口径样本不足（某带 n<5），暂不判定。")
    ge2 = data["count_bands"]["≥2 次再突破"]
    z0 = data["count_bands"]["0 次再突破"]
    if ge2 and z0:
        lines.append(f"- 次数口径：≥2次 n={ge2['n']} vs 0次 n={z0['n']}；"
                     f"MFE中位 {ge2['mfe_med']*100:.1f}% vs {z0['mfe_med']*100:.1f}%；"
                     f"剔除前三 {ge2['pnl_ex_top3']:,.0f} vs {z0['pnl_ex_top3']:,.0f}")
    lines.append("")
    lines.append("---\n*本报告由 scripts/e17_bigwinner_addon_a.py 自动生成。*")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    # ---- html ----
    html = ["<!doctype html><html><head><meta charset='utf-8'><title>大赢家加仓 A 类</title>",
            "<style>body{font-family:sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222}"
            "table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border:1px solid #ccc;padding:6px 10px;text-align:right}"
            "th{background:#f2f2f2}td:first-child,th:first-child{text-align:left}"
            "h2{margin-top:28px}</style></head><body>"]
    html.append(f"<h1>大赢家加仓 A 类分析</h1><p>生成于 {data['generated_at']} · 数据源 baseA 14 年</p>")
    html.append(f"<p>全体大赢家（MFE&gt;50%）共 <b>{all_agg['n']}</b> 笔，MFE 中位 {all_agg['mfe_med']*100:.1f}%</p>")
    html.append("<h2>口径 A：信号密度分带（再突破 box 数 / 持有天数）</h2>")
    html.append("<table><tr><th>带</th><th>n</th><th>MFE中位</th><th>每笔净利中位</th><th>胜率</th><th>PF</th><th>集中度</th><th>剔除前三</th></tr>")
    html.extend(_band_rows_html(data["bands"]))
    html.append("</table>")
    html.append("<h2>口径 B：绝对再突破次数分带（交叉验证）</h2>")
    html.append("<table><tr><th>带</th><th>n</th><th>MFE中位</th><th>每笔净利中位</th><th>胜率</th><th>PF</th><th>集中度</th><th>剔除前三</th></tr>")
    html.extend(_band_rows_html(data["count_bands"]))
    html.append("</table></body></html>")
    OUT_HTML.write_text("\n".join(html), encoding="utf-8")

    # 控制台摘要
    print(f"funnel: {funnel}")
    print(f"全体大赢家 n={all_agg['n']}, MFE中位={all_agg['mfe_med']*100:.1f}%")
    print("口径A 密度分带:")
    for label, a in data["bands"].items():
        if a:
            print(f"  {label}: n={a['n']}, MFE中位={a['mfe_med']*100:.1f}%, "
                  f"每笔净利中位={a['pnl_med']:,.0f}, 剔除前三={a['pnl_ex_top3']:,.0f}")
    print("口径B 次数分带:")
    for label, a in data["count_bands"].items():
        if a:
            print(f"  {label}: n={a['n']}, MFE中位={a['mfe_med']*100:.1f}%, "
                  f"每笔净利中位={a['pnl_med']:,.0f}, 剔除前三={a['pnl_ex_top3']:,.0f}")
    print(f"产出: {OUT_MD.name}, {OUT_HTML.name}, {OUT_JSON.name}")


if __name__ == "__main__":
    main()
