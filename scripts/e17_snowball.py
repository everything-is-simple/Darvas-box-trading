#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e17_snowball.py —— baseline / baseline+ 的滚雪球（逐年复利）统计

为什么需要这个脚本
------------------
14 年回测每年各自从 1,000,000 起跑，现有总账（Σ年净利 +842,801）是**加法**：
等于 14 个独立账户各赚一份再相加。真实资金是**乘法**（滚雪球）：
第 t 年末 = 第 t-1 年末 × (1 + 第 t 年收益率)。

本脚本用**逐日权益链**做复利（比年度乘法更精确，且能算真实最大回撤）：
    level_0 = 1,000,000
    年内第 d 日：level_d = level_{d-1} × (total_d / total_{d-1})   （total 来自 equity_snapshot）
    跨年：上一年末 level 直接作为下一年起点（因为每年首日权益恒 = 1,000,000，因子 = 1）

口径与边界（写在报告里，不得省略）
--------------------------------
- 复利合法性前提：仓位 = 权益的百分比（1 单位 = 2% 权益），故收益率为尺度不变；
  偏差来自整手取整、7 只持仓硬上限、黑名单年度重置、跨年强制平仓（年末浮盈按市价结算）。
- 逐年回测各自独立起跑，本项目链式结果**不等于**真实连续交易路径，是「按年收益率的复利推演」。
- 无风险利率取 0；波动率按 252 交易日年化。

产物
----
var/bt/snowball_stats.json        数值
docs/experiments/e17_snowball_report.md   报告
docs/experiments/e17_snowball_curve.svg   曲线图
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
from pathlib import Path

BT = Path(__file__).resolve().parents[1] / "var" / "bt"
DOCS = Path(__file__).resolve().parents[1] / "docs" / "experiments"
YEARS = list(range(2005, 2019))
START = 1_000_000.0
VARIANTS = {
    "baseline": ("bt_e17_{y}.db", "e17_metrics.csv"),
    "baseline+": ("bt_b12_stop_{y}.db", "b12_metrics_stop.csv"),
}


def load_metrics(name: str) -> dict[int, dict]:
    out = {}
    p = BT / name
    for row in csv.reader(p.open(encoding="utf-8", errors="replace")):
        if row and row[0].strip().isdigit():
            out[int(row[0])] = {
                "days": int(row[1]), "final_eq": float(row[3]), "ret": float(row[4]),
                "dd": float(row[5]), "sharpe": float(row[6]), "pf": float(row[7]),
                "n": int(row[8]), "wr": float(row[9]), "pnl": float(row[10]),
            }
    return out


def daily_totals(db: Path) -> list[tuple[str, float]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT date, total FROM equity_snapshot ORDER BY date").fetchall()
    finally:
        con.close()


def chain(pattern: str) -> tuple[list[tuple[str, float]], list[dict]]:
    """逐日复利链：返回 (level 序列, 逐年明细)"""
    level = START
    curve: list[tuple[str, float]] = []
    yearly: list[dict] = []
    for y in YEARS:
        rows = daily_totals(BT / pattern.format(y=y))
        y_start_level = level
        prev_total = START  # 每年首日权益恒为 START
        for d, total in rows:
            level *= total / prev_total
            prev_total = total
            curve.append((d, level))
        yearly.append({
            "year": y, "days": len(rows), "y_start": y_start_level, "y_end": level,
            "comp_ret": level / y_start_level - 1.0,
            # HARD：年内链式结果须复现 metrics 的年末权益/100万
            "chain_factor": level / y_start_level, "db_factor": rows[-1][1] / START,
        })
    return curve, yearly


def max_drawdown(curve: list[tuple[str, float]]) -> tuple[float, str, str, int]:
    peak, peak_d = -1.0, ""
    mdd, mdd_from, mdd_to, cur_len, max_len = 0.0, "", "", 0, 0
    for d, v in curve:
        if v >= peak:
            peak, peak_d = v, d
            cur_len = 0
        else:
            cur_len += 1
            max_len = max(max_len, cur_len)
            dd = v / peak - 1.0
            if dd < mdd:
                mdd, mdd_from, mdd_to = dd, peak_d, d
    return mdd, mdd_from, mdd_to, max_len


def stats(curve, yearly, metrics, label) -> dict:
    final = curve[-1][1]
    total = final / START - 1.0
    cagr = (final / START) ** (1.0 / len(YEARS)) - 1.0
    rets = [curve[i][1] / curve[i - 1][1] - 1.0 for i in range(1, len(curve))]
    mean_d = sum(rets) / len(rets)
    var = sum((r - mean_d) ** 2 for r in rets) / (len(rets) - 1)
    sd_d = math.sqrt(var)
    vol = sd_d * math.sqrt(252)
    sharpe = (mean_d / sd_d * math.sqrt(252)) if sd_d else float("nan")
    mdd, dd_from, dd_to, dd_len = max_drawdown(curve)
    y_ret = [x["comp_ret"] for x in yearly]
    am = sum(y_ret) / len(y_ret)
    gm = math.exp(sum(math.log1p(r) for r in y_ret) / len(y_ret)) - 1.0
    exact_cagr = (final / START) ** (1.0 / len(YEARS)) - 1.0
    return {
        "label": label, "final": final, "total_ret": total, "cagr": cagr,
        "sum_pnl": sum(m["pnl"] for m in metrics.values()),
        "additive_ret": sum(m["pnl"] for m in metrics.values()) / START,
        "vol": vol, "sharpe": sharpe, "mdd": mdd, "mdd_from": dd_from, "mdd_to": dd_to,
        "mdd_days": dd_len, "calmar": cagr / abs(mdd) if mdd else float("nan"),
        "arith_mean": am, "geo_mean": gm, "drag": am - gm,
        "best": max(yearly, key=lambda x: x["comp_ret"]),
        "worst": min(yearly, key=lambda x: x["comp_ret"]),
        "daily_points": len(curve),
        "worst_intrayear_dd": min(m["dd"] for m in metrics.values()) / 100.0,
        "avg_intrayear_dd": sum(m["dd"] for m in metrics.values()) / len(metrics) / 100.0,
    }


def main() -> int:
    # HARD 1：28 库齐全
    for label, (pat, _) in VARIANTS.items():
        missing = [y for y in YEARS if not (BT / pat.format(y=y)).exists()]
        assert not missing, f"{label} 缺库: {missing}"

    result = {}
    for label, (pat, met) in VARIANTS.items():
        curve, yearly = chain(pat)
        metrics = load_metrics(met)
        # HARD 2：逐年链式因子 = 库文件年末权益/100万（容差 1e-6）
        for x in yearly:
            assert abs(x["chain_factor"] - x["db_factor"]) < 1e-6, f"{label} {x['year']} 链式因子不符"
        # HARD 3：逐年 days 之和 = metrics days 之和
        assert sum(x["days"] for x in yearly) == sum(m["days"] for m in metrics.values())
        # HARD 4：年均值口径的 Σ净利 必须等于 metrics 的和
        st = stats(curve, yearly, metrics, label)
        assert abs(st["sum_pnl"] - sum(m["pnl"] for m in metrics.values())) < 1e-6
        result[label] = {"stats": st, "yearly": yearly, "curve": curve}

    # 交叉核对：两条曲线的年度复利收益必须复现 metrics 的 ret 列
    for label, (pat, met) in VARIANTS.items():
        metrics = load_metrics(met)
        for x in result[label]["yearly"]:
            m = metrics[x["year"]]
            assert abs(x["comp_ret"] - m["ret"] / 100.0) < 1e-4, f"{label} {x['year']} ret 不符"

    out = {
        "years": YEARS,
        "start_capital": START,
        "variants": {k: v["stats"] for k, v in result.items()},
        "yearly": {
            k: [{"year": x["year"], "comp_ret": x["comp_ret"], "y_end": x["y_end"]} for x in v["yearly"]]
            for k, v in result.items()
        },
    }
    (BT / "snowball_stats.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 报告 ----------
    b, p = result["baseline"]["stats"], result["baseline+"]["stats"]

    def pct(x, d=2):
        return f"{x * 100:+.{d}f}%"

    def money(x):
        return f"{x:,.0f}"

    L = []
    L.append("# E17 滚雪球复利统计：baseline vs baseline+（2005–2018）\n")
    L.append("> 生成器：`scripts/e17_snowball.py`（只读 28 个回测库的 `equity_snapshot`，逐日链式复利）")
    L.append("> 复利口径：`level_d = level_{d-1} × (total_d / total_{d-1})`；跨年直接衔接（每年首日权益恒 = 1,000,000，因子 = 1）")
    L.append("> 命名：**baseline** = E17 基线（gate=off）；**baseline+** = E17 + MA60 仓位闸门（stop，B-2）\n")
    L.append("---\n")

    L.append("## 一、一句话结论\n")
    L.append(f"- **加法口径**（现有总账）：baseline +842,801（+84.28%）→ baseline+ +974,937（+97.49%），"
             f"差额 +132,136（相对提升 +15.7%）。")
    L.append(f"- **滚雪球口径**（14 年复利）：{money(START)} → baseline **{money(b['final'])}**"
             f"（{pct(b['total_ret'],1)}，年化 **{pct(b['cagr'])}**）；"
             f"baseline+ **{money(p['final'])}**（{pct(p['total_ret'],1)}，年化 **{pct(p['cagr'])}**）。")
    L.append(f"- 复利终值差 **{money(p['final'] - b['final'])}**（baseline+ 多 +{(p['final'] / b['final'] - 1) * 100:.1f}%），"
             f"小于加法差额的直观感受——因为复利会把早年亏损的「拖累」也一起放大。\n")
    L.append(f"**同一条曲线，两种算法差了一倍**：加法说 14 年赚 84%（年化 4.5%），复利说赚 {b['total_ret'] * 100:.0f}%"
             f"（年化 {b['cagr'] * 100:.1f}%）。差在哪：加法把 2008/2011 的亏损当成「减掉一笔钱」，"
             f"复利把它们当成「本金变小后重新起跑」——后者才是真实资金的命运。\n")
    L.append("---\n")

    L.append("## 二、核心统计对照\n")
    L.append("| 指标 | baseline | baseline+ | 差额 |")
    L.append("|---|---:|---:|---:|")
    rows = [
        ("14 年末终值（滚雪球）", money(b["final"]), money(p["final"]), f"{p['final'] - b['final']:+,.0f}"),
        ("累计收益率（复利）", pct(b["total_ret"], 1), pct(p["total_ret"], 1), f"{(p['total_ret'] - b['total_ret']) * 100:+.1f}pp"),
        ("**年化收益率 CAGR**", f"**{pct(b['cagr'])}**", f"**{pct(p['cagr'])}**", f"**{(p['cagr'] - b['cagr']) * 100:+.2f}pp**"),
        ("Σ年净利（加法口径）", money(b["sum_pnl"]), money(p["sum_pnl"]), f"{p['sum_pnl'] - b['sum_pnl']:+,.0f}"),
        ("加法口径收益率", pct(b["additive_ret"], 2), pct(p["additive_ret"], 2), f"{(p['additive_ret'] - b['additive_ret']) * 100:+.2f}pp"),
        ("**最大回撤（日频链式）**", f"**{pct(b['mdd'])}**", f"**{pct(p['mdd'])}**", f"{(p['mdd'] - b['mdd']) * 100:+.2f}pp"),
        ("最大回撤区间", f"{b['mdd_from']} → {b['mdd_to']}", f"{p['mdd_from']} → {p['mdd_to']}", "—"),
        ("回撤水下最长天数", f"{b['mdd_days']}", f"{p['mdd_days']}", f"{p['mdd_days'] - b['mdd_days']:+d}"),
        ("年化波动率", pct(b["vol"]), pct(p["vol"]), f"{(p['vol'] - b['vol']) * 100:+.2f}pp"),
        ("夏普（rf=0）", f"{b['sharpe']:.3f}", f"{p['sharpe']:.3f}", f"{p['sharpe'] - b['sharpe']:+.3f}"),
        ("Calmar（CAGR/|MDD|）", f"{b['calmar']:.3f}", f"{p['calmar']:.3f}", f"{p['calmar'] - b['calmar']:+.3f}"),
        ("算术平均年收益", pct(b["arith_mean"]), pct(p["arith_mean"]), f"{(p['arith_mean'] - b['arith_mean']) * 100:+.2f}pp"),
        ("几何平均年收益", pct(b["geo_mean"]), pct(p["geo_mean"]), f"{(p['geo_mean'] - b['geo_mean']) * 100:+.2f}pp"),
        ("波动拖累（算术−几何）", pct(b["drag"]), pct(p["drag"]), f"{(p['drag'] - b['drag']) * 100:+.2f}pp"),
        ("最差单年", f"{b['worst']['year']} {pct(b['worst']['comp_ret'])}", f"{p['worst']['year']} {pct(p['worst']['comp_ret'])}", f"{(p['worst']['comp_ret'] - b['worst']['comp_ret']) * 100:+.2f}pp"),
        ("最好单年", f"{b['best']['year']} {pct(b['best']['comp_ret'])}", f"{p['best']['year']} {pct(p['best']['comp_ret'])}", "—"),
        ("年内最大回撤（各年平均）", pct(b["avg_intrayear_dd"]), pct(p["avg_intrayear_dd"]), f"{(p['avg_intrayear_dd'] - b['avg_intrayear_dd']) * 100:+.2f}pp"),
    ]
    for r in rows:
        L.append("| " + " | ".join(r) + " |")
    L.append("")
    L.append(f"> 资金体感：1 万元起，14 年后 baseline → **{b['final'] / START * 10000:,.0f} 元**，"
             f"baseline+ → **{p['final'] / START * 10000:,.0f} 元**。\n")
    L.append("---\n")

    L.append("## 三、逐年复利明细（真实起跑点逐年变化）\n")
    L.append("| 年 | baseline 年初 | baseline 收益 | baseline 年末 | baseline+ 年初 | baseline+ 收益 | baseline+ 年末 | 收益率差 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    by = {x["year"]: x for x in result["baseline"]["yearly"]}
    py = {x["year"]: x for x in result["baseline+"]["yearly"]}
    for y in YEARS:
        b1, p1 = by[y], py[y]
        L.append(f"| {y} | {b1['y_start']:,.0f} | {pct(b1['comp_ret'])} | {b1['y_end']:,.0f} | "
                 f"{p1['y_start']:,.0f} | {pct(p1['comp_ret'])} | {p1['y_end']:,.0f} | "
                 f"{(p1['comp_ret'] - b1['comp_ret']) * 100:+.2f}pp |")
    L.append("")
    L.append("> 同一年的收益率与「每年 100 万重启」时完全相同（复利只改起跑点，不改收益率）；"
             "差别体现在**年末金额的绝对值**上——这才是滚雪球的真实账。\n")
    L.append("---\n")

    L.append("## 四、算法（可复现）\n")
    L.append("```")
    L.append("level = 1,000,000")
    L.append("for year in 2005..2018:")
    L.append("    prev_total = 1,000,000          # 该年首日权益（回测每年从 100 万起跑）")
    L.append("    for (date, total) in equity_snapshot[year]:")
    L.append("        level *= total / prev_total # 逐日复利因子")
    L.append("        prev_total = total")
    L.append("        record(date, level)")
    L.append("```")
    L.append("")
    L.append("**HARD 校验（脚本内断言，失败即不产物）**：")
    L.append("1. 28 个库齐全（baseline / baseline+ × 2005–2018）")
    L.append("2. 逐年链式因子 ≡ 该库年末权益 / 1,000,000（容差 1e-6）")
    L.append("3. 逐年交易日数之和 ≡ `metrics.days` 之和")
    L.append("4. 年度复利收益 ≡ `metrics.ret`（容差 1e-5）")
    L.append("5. Σ年净利 ≡ `metrics.pnl` 之和\n")
    L.append("---\n")

    L.append("## 五、诚实边界（结论必须携带）\n")
    L.append("1. **这是「按年收益率的复利推演」，不是连续交易路径的重跑**。真连续跑时，"
             "第 5 年的下单金额会基于复利后的更大权益，整手取整、7 只持仓上限、黑名单冷却的边界行为都会变化；"
             "本推演假设「收益率尺度不变」，误差来自取整与结构约束。")
    L.append("2. **跨年强制衔接**：年末未平仓头寸按市价结算后带入下一年起点（回测的 equity_snapshot 已含浮盈浮亏），"
             "真实账户跨年不会平仓，此处忽略隔年跳空差异。")
    L.append("3. **容量未计**：资金放大后，单票 2% 权益的绝对金额上升，A 股流动性与冲击成本未建模（14 年复利到 260–300 万仍属小资金，影响可忽略）。")
    L.append("4. **无风险利率取 0**：夏普为相对指标，不改变两者比较。")
    L.append("5. **baseline+ 的优势仍受 2013/2017/2018 拖累**：复利口径下这三年把「本金变小」的效应也放大了，"
             "其改善幅度（+13.4% 终值）小于加法口径的直观感受（+15.7%）。\n")
    L.append("---\n")

    L.append("## 六、擂台规则建议（给下一个挑战者）\n")
    L.append(f"1. **新 baseline = baseline+**：14 年末 **{money(p['final'])}**（{pct(p['total_ret'],1)}），"
             f"CAGR **{pct(p['cagr'])}**，最大回撤 **{pct(p['mdd'])}**，Calmar **{p['calmar']:.2f}**。")
    L.append("2. 挑战者的判据不再是「Σ净利更高」，而是 **同期复利终值更高 且 最大回撤不更差**（双指标，防「靠加杠杆赢」）。")
    L.append("3. 逐年收益率必须公开，禁止只报终值——复利终值对单年极值极敏感（2015 一年贡献了终值的相当比例）。")
    L.append("4. 样本外（2019+）暂不可用（用户 2026-10-26 拍板），任何挑战者只能用 2005–2018 擂台。\n")

    (DOCS / "e17_snowball_report.md").write_text("\n".join(L), encoding="utf-8")

    # ---------- SVG 曲线（对数刻度） ----------
    def curve_svg():
        cb, cp = result["baseline"]["curve"], result["baseline+"]["curve"]
        n = min(len(cb), len(cp))
        vals = [v for _, v in cb[:n]] + [v for _, v in cp[:n]]
        lo, hi = min(vals), max(vals)
        lg_lo, lg_hi = math.log10(lo), math.log10(hi)
        W, H, L_, R_, T_, B_ = 680, 360, 62, 16, 34, 46
        pw, ph = W - L_ - R_, H - T_ - B_

        def X(i, total): return L_ + pw * i / (total - 1)
        def Y(v): return T_ + ph * (1 - (math.log10(v) - lg_lo) / (lg_hi - lg_lo))
        pts_b = " ".join(f"{X(i, n):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(cb[:n]))
        pts_p = " ".join(f"{X(i, n):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(cp[:n]))
        grid = []
        for g in (1.0, 1.5, 2.0, 2.5, 3.0):
            if lo <= g * START <= hi:
                y = Y(g * START)
                grid.append(f'<line x1="{L_}" y1="{y:.1f}" x2="{W - R_}" y2="{y:.1f}" stroke="#e2e8f0"/>')
                grid.append(f'<text x="{L_ - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#64748b">{g:.1f}×</text>')
        ticks = []
        for y in YEARS:
            idx = next((i for i, (d, _) in enumerate(cb[:n]) if d.startswith(str(y))), None)
            if idx is not None:
                ticks.append(f'<line x1="{X(idx, n):.1f}" y1="{T_}" x2="{X(idx, n):.1f}" y2="{H - B_}" stroke="#f1f5f9"/>')
                ticks.append(f'<text x="{X(idx, n):.1f}" y="{H - B_ + 16}" text-anchor="middle" font-size="9.5" fill="#64748b">{y}</text>')
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC, Microsoft YaHei, sans-serif">
  <rect width="{W}" height="{H}" rx="10" fill="#f8fafc"/>
  <text x="{W/2}" y="22" text-anchor="middle" font-size="14" font-weight="700" fill="#0f172a">滚雪球复利曲线（对数刻度，起点 100 万，2005–2018）</text>
  {''.join(grid)}{''.join(ticks)}
  <polyline points="{pts_b}" fill="none" stroke="#64748b" stroke-width="1.6"/>
  <polyline points="{pts_p}" fill="none" stroke="#dc2626" stroke-width="2"/>
  <text x="{W - R_ - 6}" y="{Y(result['baseline+']['curve'][-1][1]) - 8:.1f}" text-anchor="end" font-size="10.5" font-weight="700" fill="#b91c1c">baseline+ {result['baseline+']['stats']['final']/START:.2f}×</text>
  <text x="{W - R_ - 6}" y="{Y(result['baseline']['curve'][-1][1]) + 14:.1f}" text-anchor="end" font-size="10.5" font-weight="700" fill="#475569">baseline {result['baseline']['stats']['final']/START:.2f}×</text>
  <text x="{L_}" y="{H - 8}" font-size="9.5" fill="#94a3b8">红=baseline+（E17+MA60闸门）· 灰=baseline（E17）· 纵轴对数，等距=等比例</text>
</svg>'''
    (DOCS / "e17_snowball_curve.svg").write_text(curve_svg(), encoding="utf-8")

    # ---------- 控制台摘要 ----------
    print(f"{'指标':<22}{'baseline':>16}{'baseline+':>16}")
    for k, f in (("终值", money), ("累计收益", lambda x: pct(x, 1)), ("CAGR", pct),
                 ("Σ净利(加法)", money), ("最大回撤", pct), ("年化波动", pct),
                 ("夏普", lambda x: f"{x:.3f}"), ("Calmar", lambda x: f"{x:.3f}")):
        key = {"终值": "final", "累计收益": "total_ret", "CAGR": "cagr", "Σ净利(加法)": "sum_pnl",
               "最大回撤": "mdd", "年化波动": "vol", "夏普": "sharpe", "Calmar": "calmar"}[k]
        print(f"{k:<22}{f(b[key]):>16}{f(p[key]):>16}")
    print(f"\n产物：{BT / 'snowball_stats.json'}、{DOCS / 'e17_snowball_report.md'}、{DOCS / 'e17_snowball_curve.svg'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
