#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3-8 回测统计：读已完成回测库，算概率统计指标 + 显著性检验，输出 MD/HTML 报表。

指标口径（遵循项目统一口径）：
- n = 已平仓笔数；胜率 = 盈利笔数 / n
- 保本胜率 = 1 / (1 + 平均盈利/平均亏损绝对值)；edge = 实际胜率 − 保本胜率
- PF = 毛盈利 / 毛亏损；利润集中度 = 前三赢家净利 / 毛盈利
- 剔除前三 = 总已实现 − 前三赢家净利
- 显著性：两比例 z 检验（pooled SE），|z| ≥ 1.96 才称显著

用法：
  python scripts/e17_p38_stats.py                    # 打印到终端
  python scripts/e17_p38_stats.py --md out.md        # 写 Markdown
  python scripts/e17_p38_stats.py --html out.html    # 写 HTML
"""
import argparse
import math
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stat_tools import fisher_exact_two_sided, t_two_sided_p  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt" / "p38_bt"

TIERS = ["off", "up", "t60", "t120", "t250"]
# 已否决档位：2026-11-03 13:20 用户裁决「历史最高箱」口径作废，不再跑，但已跑出的库仍展示作证据。
REJECTED = ["all", "up_all"]
TIER_LABEL = {
    "off": "off（基线）", "up": "升势 on", "t60": "trend N=60",
    "t120": "trend N=120", "t250": "trend N=250",
    "all": "alltime 历史最高 ⛔已否决", "up_all": "升势+历史最高 ⛔已否决",
}
YEARS = [2007, 2010, 2013, 2018]
YEAR_LABEL = {2007: "2007 单边牛", 2010: "2010 震荡", 2013: "2013 脱钩牛", 2018: "2018 洗耳熊"}


def db_complete(path, year):
    if not path.exists():
        return False
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            maxd = c.execute("SELECT MAX(date) FROM equity_snapshot").fetchone()[0]
            return bool(maxd) and maxd >= f"{year}-12-01"
        finally:
            c.close()
    except Exception:
        return False


def analyze(db_path):
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = c.cursor()
    cur.execute("""SELECT realized_pnl, close_reason, code, opened_date FROM position
                   WHERE status='closed' AND realized_pnl IS NOT NULL ORDER BY realized_pnl DESC""")
    rows = cur.fetchall()
    cur.execute("SELECT date, total, COALESCE(market_value,0) FROM equity_snapshot ORDER BY rowid DESC LIMIT 1")
    eq = cur.fetchone()
    cur.execute("""SELECT COALESCE(SUM(l.qty*l.price),0) FROM position_leg l
                   JOIN position p ON p.id=l.position_id
                   WHERE p.status<>'closed' AND l.qty>0""")
    open_cost = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM position WHERE status<>'closed'")
    open_n = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM position_leg WHERE qty>0")
    buys = cur.fetchone()[0]
    c.close()

    if not rows:
        return None
    pnls = [r[0] for r in rows]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    avg_win = gross_win / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 1
    win_rate = len(wins) / n
    be_rate = 1 / (1 + avg_win / avg_loss) if avg_loss > 0 and avg_win > 0 else 0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    top3 = sum(pnls[:3])
    conc = top3 / gross_win if gross_win > 0 else 0
    realized = sum(pnls)
    ex_top3 = realized - top3
    reasons = {}
    for p, r, _c, _d in rows:
        k = r or "unknown"
        reasons[k] = reasons.get(k, 0) + 1
    trades = {(r[2], r[3]): r[0] for r in rows}
    mv = eq[2] if eq else 0
    unrl = mv - open_cost
    total_pnl = realized + unrl

    return {
        "n": n, "buys": buys, "win_rate": win_rate, "be_rate": be_rate,
        "avg_win": avg_win, "avg_loss": avg_loss, "pf": pf,
        "realized": realized, "per_trade": realized / n,
        "top3": top3, "conc": conc, "ex_top3": ex_top3,
        "equity": eq[1] if eq else None, "eq_date": eq[0] if eq else None,
        "unrl": unrl, "total_pnl": total_pnl, "open_n": open_n,
        "reasons": reasons, "trades": trades, "pnls": pnls,
    }


def ztest(n1, k1, n2, k2):
    """两比例 z 检验（pooled）。返回 (p1, p2, diff_pp, se_pp, z, pvalue_two_sided_approx)。"""
    if n1 == 0 or n2 == 0:
        return 0, 0, 0, 0, 0, 1.0
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return p1, p2, 0, 0, 0, 1.0
    z = (p2 - p1) / se
    # 正态近似双尾 p
    pval = math.erfc(abs(z) / math.sqrt(2))
    return p1, p2, (p2 - p1) * 100, se * 100, z, pval


def welch(a, b):
    """Welch t 检验（不假设等方差）。返回 (mean_a, mean_b, diff, se, t, df)。"""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 0, 0, 0, 0, 0, 0
    ma, mb = statistics.mean(a), statistics.mean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return ma, mb, mb - ma, 0, 0, 0
    t = (mb - ma) / se
    num = (va / na + vb / nb) ** 2
    den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = num / den if den > 0 else 0
    return ma, mb, mb - ma, se, t, df


def mde(se_diff, power_z=0.8416, alpha_z=1.96):
    """最小可检出效应（80% 功效，alpha=0.05 双尾）：(z_{1-a/2}+z_{1-b})*SE = 2.80*SE。"""
    return (alpha_z + power_z) * se_diff if se_diff > 0 else 0


def decompose(m_base, m_tier):
    """对比两档的交易集合差异。返回 (被拦票列表, 新买票列表)。"""
    base_t, tier_t = m_base["trades"], m_tier["trades"]
    blocked = [(k[0], k[1], v) for k, v in base_t.items() if k not in tier_t]
    added = [(k[0], k[1], v) for k, v in tier_t.items() if k not in base_t]
    return blocked, added


def collect():
    data = {}
    for y in YEARS:
        data[y] = {}
        for t in TIERS + REJECTED:
            p = BT / f"bt_p38_{t}_{y}.db"
            if db_complete(p, y):
                r = analyze(p)
                if r:
                    data[y][t] = r
    return data


def n_done(data):
    """只统计在跑档位的完成数（已否决档位不计入分母）。"""
    return sum(1 for v in data.values() for t in v if t in TIERS)


def render_md(data):
    total = len(TIERS) * len(YEARS)
    done = n_done(data)
    L = []
    L.append("# P3-8 升势 / 最高 Box 回测进度报告\n")
    L.append(f"> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
             f"进度 **{done}/{total}** 库（{done/total*100:.1f}%）\n")
    L.append("> 判据（框架 §五）：`on` 档剔除前三净利 ≥ `off` 档，且改善年数 ≥ 3 才值得采纳；"
             "胜率差需 |z| ≥ 1.96 才称显著。\n")

    for y in YEARS:
        if not data[y]:
            L.append(f"\n## {YEAR_LABEL[y]}\n\n尚无完成库。\n")
            continue
        L.append(f"\n## {YEAR_LABEL[y]}\n")
        L.append("| 档位 | n | 胜率 | 保本率 | edge | PF | 每笔净利 | 已实现 | 集中度 | 剔除前三 | 年末持仓 | 未实现 | 合计损益 | 终值权益 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for t in data[y]:
            if t not in data[y]:
                continue
            r = data[y][t]
            L.append(f"| {TIER_LABEL[t]} | {r['n']} | {r['win_rate']*100:.1f}% | "
                     f"{r['be_rate']*100:.1f}% | {(r['win_rate']-r['be_rate'])*100:+.1f}pp | "
                     f"{r['pf']:.2f} | {r['per_trade']:,.0f} | {r['realized']:,.0f} | "
                     f"{r['conc']*100:.1f}% | {r['ex_top3']:,.0f} | {r['open_n']} | "
                     f"{r['unrl']:+,.0f} | {r['total_pnl']:,.0f} | {r['equity']:,.0f} |")
        # 出口分布
        L.append("\n**出口分布**\n")
        for t in data[y]:
            if t not in data[y]:
                continue
            rs = " · ".join(f"{k} {v}" for k, v in sorted(data[y][t]["reasons"].items(), key=lambda x: -x[1]))
            L.append(f"- `{t}`：{rs}")

        # 与 off 对比 + 显著性
        if "off" in data[y] and len(data[y]) > 1:
            base = data[y]["off"]
            k_base = round(base["win_rate"] * base["n"])
            L.append("\n**对比基线（off）与显著性**\n")
            L.append("| 档位 | Δ笔数 | Δ胜率 | z | p(z) | Fisher p | 显著? | Δ已实现 | Δ剔除前三 | 被拦票净利 | 新买票净利 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for t in data[y]:
                if t == "off" or t not in data[y]:
                    continue
                r = data[y][t]
                k = round(r["win_rate"] * r["n"])
                p1, p2, diff, se, z, pval = ztest(base["n"], k_base, r["n"], k)
                fp = fisher_exact_two_sided(k_base, base["n"] - k_base, k, r["n"] - k)
                sig = "**是**" if abs(z) >= 1.96 else "否"
                blocked, added = decompose(base, r)
                b_sum = sum(x[2] for x in blocked)
                a_sum = sum(x[2] for x in added)
                L.append(f"| {TIER_LABEL[t]} | {r['n']-base['n']:+d} | {diff:+.1f}pp | {z:+.2f} | "
                         f"{pval:.3f} | {fp:.3f} | {sig} | {r['realized']-base['realized']:+,.0f} | "
                         f"{r['ex_top3']-base['ex_top3']:+,.0f} | {b_sum:+,.0f}（{len(blocked)}笔） | "
                         f"{a_sum:+,.0f}（{len(added)}笔） |")

            # 每笔净利 Welch t + MDE
            bp = base["pnls"]
            sd_base = statistics.stdev(bp) if len(bp) > 1 else 0
            cv = sd_base / base["per_trade"] if base["per_trade"] else 0
            L.append("\n**每笔净利分布检验（Welch t，不假设等方差）**\n")
            L.append("| 档位 | n | 每笔均值 | 中位数 | 标准差 | Δ均值 | SE_diff | t | t 双尾 p | 最小可检出效应 | 占基线均值 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for t in data[y]:
                if t == "off" or t not in data[y]:
                    continue
                r = data[y][t]
                _ma, _mb, _d, se, tv, df = welch(bp, r["pnls"])
                med = statistics.median(r["pnls"])
                sd = statistics.stdev(r["pnls"]) if len(r["pnls"]) > 1 else 0
                m = mde(se)
                L.append(f"| {TIER_LABEL[t]} | {r['n']} | {r['per_trade']:,.0f} | {med:,.0f} | {sd:,.0f} | "
                         f"{_d:+,.0f} | {se:,.0f} | {tv:+.2f} | {t_two_sided_p(tv, df):.3f} | "
                         f"{m:,.0f} | {m/base['per_trade']*100:.0f}% |")
            L.append(f"\n> 基线 off：中位数 **{statistics.median(bp):,.0f}**、标准差 {sd_base:,.0f}、"
                     f"变异系数 CV = **{cv:.2f}**"
                     + ("（中位数 < 0 → 过半交易亏钱，利润全靠右尾大赢家）" if statistics.median(bp) < 0 else "")
                     + f"。|t| < 1.96 即不显著。\n"
                     f"> 最小可检出效应 = 2.80×SE_diff（80% 功效）：n≈100 量级下只能检出 "
                     f"约 {mde(math.sqrt(sd_base**2/base['n'] + sd_base**2/base['n']))/base['per_trade']*100:.0f}% "
                     f"以上幅度的每笔净利差异。")
    return "\n".join(L) + "\n"


def render_html(data):
    total = len(TIERS) * len(YEARS)
    done = n_done(data)
    css = """
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f5f1;color:#2c2c2a;line-height:1.6}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;font-weight:600;margin:0 0 6px}
h2{font-size:17px;font-weight:600;margin:30px 0 10px;padding-bottom:6px;border-bottom:2px solid #e2e0d8}
.sub{color:#5f5e5a;font-size:13px;margin:0 0 4px}
.pbar{height:10px;background:#e2e0d8;border-radius:5px;overflow:hidden;margin:12px 0 6px}
.pbar i{display:block;height:100%;background:#3b6d11}
table{width:100%;border-collapse:collapse;font-size:12.5px;background:#fff;border-radius:8px;overflow:hidden;margin:8px 0}
th,td{padding:7px 9px;text-align:right;border-bottom:1px solid #e2e0d8;white-space:nowrap}
th{background:#f6f5f1;font-weight:600;color:#5f5e5a;font-size:11.5px}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:none}
.neg{color:#a32d2d}.pos{color:#3b6d11}
.sig{background:#faeeda;color:#854f0b;font-weight:600;padding:1px 6px;border-radius:8px;font-size:11px}
.nosig{color:#888780;font-size:11px}
.reasons{font-size:12px;color:#5f5e5a;margin:6px 0 14px}
.card{background:#fff;border:1px solid #e2e0d8;border-radius:12px;padding:16px 18px;margin-bottom:18px}
"""
    h = [f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>P3-8 进度报告</title><style>{css}</style></head><body><div class='wrap'>"]
    h.append("<h1>P3-8 升势 / 最高 Box 回测进度报告</h1>")
    h.append(f"<p class='sub'>自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>")
    h.append(f"<p class='sub'>进度 <b>{done}/{total}</b> 库（{done/total*100:.1f}%）</p>")
    h.append(f"<div class='pbar'><i style='width:{done/total*100:.1f}%'></i></div>")
    h.append("<p class='sub'>判据：<code>on</code> 档剔除前三净利 ≥ <code>off</code> 档且改善年数 ≥ 3 才采纳；"
             "胜率差需 |z| ≥ 1.96 才称显著。</p>")

    for y in YEARS:
        h.append(f"<h2>{YEAR_LABEL[y]}</h2>")
        if not data[y]:
            h.append("<p class='sub'>尚无完成库。</p>")
            continue
        h.append("<div class='card'><table><tr><th>档位</th><th>n</th><th>胜率</th><th>保本率</th>"
                 "<th>edge</th><th>PF</th><th>每笔净利</th><th>已实现</th><th>集中度</th>"
                 "<th>剔除前三</th><th>年末持仓</th><th>未实现</th><th>合计损益</th><th>终值权益</th></tr>")
        for t in data[y]:
            if t not in data[y]:
                continue
            r = data[y][t]
            edge = (r["win_rate"] - r["be_rate"]) * 100
            h.append(f"<tr><td>{TIER_LABEL[t]}</td><td>{r['n']}</td><td>{r['win_rate']*100:.1f}%</td>"
                     f"<td>{r['be_rate']*100:.1f}%</td><td class='pos'>{edge:+.1f}pp</td>"
                     f"<td>{r['pf']:.2f}</td><td>{r['per_trade']:,.0f}</td><td>{r['realized']:,.0f}</td>"
                     f"<td>{r['conc']*100:.1f}%</td><td>{r['ex_top3']:,.0f}</td>"
                     f"<td>{r['open_n']}</td><td>{r['unrl']:+,.0f}</td>"
                     f"<td>{r['total_pnl']:,.0f}</td><td>{r['equity']:,.0f}</td></tr>")
        h.append("</table>")
        rs_lines = []
        for t in data[y]:
            if t not in data[y]:
                continue
            rs = " · ".join(f"{k} {v}" for k, v in sorted(data[y][t]["reasons"].items(), key=lambda x: -x[1]))
            rs_lines.append(f"<b>{t}</b>：{rs}")
        h.append("<div class='reasons'>" + "<br>".join(rs_lines) + "</div>")

        if "off" in data[y] and len(data[y]) > 1:
            base = data[y]["off"]
            k_base = round(base["win_rate"] * base["n"])
            h.append("<table><tr><th>档位</th><th>Δ笔数</th><th>Δ胜率</th><th>z</th><th>p(z)</th>"
                     "<th>Fisher p</th><th>显著</th><th>Δ已实现</th><th>Δ剔除前三</th><th>被拦票</th><th>新买票</th></tr>")
            for t in data[y]:
                if t == "off" or t not in data[y]:
                    continue
                r = data[y][t]
                k = round(r["win_rate"] * r["n"])
                p1, p2, diff, se, z, pval = ztest(base["n"], k_base, r["n"], k)
                fp = fisher_exact_two_sided(k_base, base["n"] - k_base, k, r["n"] - k)
                sig = f"<span class='sig'>是</span>" if abs(z) >= 1.96 else "<span class='nosig'>否</span>"
                blocked, added = decompose(base, r)
                b_sum = sum(x[2] for x in blocked)
                a_sum = sum(x[2] for x in added)
                d_re = r["realized"] - base["realized"]
                d_ex = r["ex_top3"] - base["ex_top3"]
                h.append(f"<tr><td>{TIER_LABEL[t]}</td><td>{r['n']-base['n']:+d}</td>"
                         f"<td class='{'pos' if diff>0 else 'neg'}'>{diff:+.1f}pp</td>"
                         f"<td>{z:+.2f}</td><td>{pval:.3f}</td><td>{fp:.3f}</td><td>{sig}</td>"
                         f"<td class='{'pos' if d_re>0 else 'neg'}'>{d_re:+,.0f}</td>"
                         f"<td class='{'pos' if d_ex>0 else 'neg'}'>{d_ex:+,.0f}</td>"
                         f"<td>{b_sum:+,.0f}（{len(blocked)}笔）</td>"
                         f"<td>{a_sum:+,.0f}（{len(added)}笔）</td></tr>")
            h.append("</table>")

            bp = base["pnls"]
            sd_base = statistics.stdev(bp) if len(bp) > 1 else 0
            cv = sd_base / base["per_trade"] if base["per_trade"] else 0
            h.append("<p class='sub' style='margin-top:14px'>每笔净利分布检验（Welch t）</p>"
                     "<table><tr><th>档位</th><th>n</th><th>每笔均值</th><th>中位数</th><th>标准差</th>"
                     "<th>Δ均值</th><th>SE_diff</th><th>t</th><th>t 双尾 p</th><th>最小可检出效应</th><th>占基线均值</th></tr>")
            for t in data[y]:
                if t == "off" or t not in data[y]:
                    continue
                r = data[y][t]
                _ma, _mb, _d, se, tv, df = welch(bp, r["pnls"])
                m = mde(se)
                med = statistics.median(r["pnls"])
                sd = statistics.stdev(r["pnls"]) if len(r["pnls"]) > 1 else 0
                h.append(f"<tr><td>{TIER_LABEL[t]}</td><td>{r['n']}</td><td>{r['per_trade']:,.0f}</td>"
                         f"<td class='neg'>{med:,.0f}</td><td>{sd:,.0f}</td>"
                         f"<td class='{'pos' if _d>0 else 'neg'}'>{_d:+,.0f}</td>"
                         f"<td>{se:,.0f}</td><td>{tv:+.2f}</td><td>{t_two_sided_p(tv, df):.3f}</td>"
                         f"<td>{m:,.0f}</td><td>{m/base['per_trade']*100:.0f}%</td></tr>")
            h.append("</table>")
            h.append(f"<p class='sub'>基线 off 中位数 <b>{statistics.median(bp):,.0f}</b>、标准差 {sd_base:,.0f}、"
                     f"CV = <b>{cv:.2f}</b>"
                     + ("；中位数 &lt; 0 → 过半交易亏钱，利润全靠右尾大赢家" if statistics.median(bp) < 0 else "")
                     + "。|t| &lt; 1.96 即不显著；最小可检出效应 = 2.80×SE_diff（80% 功效）。</p>")
            h.append("</div>")
        else:
            h.append("</div>")
    h.append("</div></body></html>")
    return "".join(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md")
    ap.add_argument("--html")
    a = ap.parse_args()
    data = collect()
    if a.md:
        Path(a.md).write_text(render_md(data), encoding="utf-8")
    if a.html:
        Path(a.html).write_text(render_html(data), encoding="utf-8")
    if not a.md and not a.html:
        print(render_md(data))


if __name__ == "__main__":
    main()
