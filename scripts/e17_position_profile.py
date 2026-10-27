#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""e17_position_profile.py —— 14 年真实仓位画像（只读，零回测）

回答三个问题：牛市进攻够不够狠、熊市防守够不够硬、猴市参与度是否合适。

数据来源（全部只读）
- equity_snapshot：逐日 cash / market_value / total → 仓位占用率 = market_value/total
- position：opened_date / closed_date → 重建逐日持仓只数
- plan_order + trade_plan：买入 qty × trigger_price → 单笔建仓占权益比例（建仓日权益）
- daily_bar(sh000001, low)：60 日低点相邻日比 → 大盘三态（升势/震荡/降势），与生产 A 判据同口径

产物
- var/bt/position_profile.json
- docs/experiments/e17_position_profile_report.md
- docs/experiments/e17_position_profile.svg
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

BT = Path(__file__).resolve().parents[1] / "var" / "bt"
DOCS = Path(__file__).resolve().parents[1] / "docs" / "experiments"
YEARS = list(range(2005, 2019))
VARIANTS = {
    "baseline": "bt_e17_{y}.db",
    "baseline+": "bt_b12_stop_{y}.db",
}


def idx_regime() -> dict[str, str]:
    """sh000001 的 60 日低点相邻日比 → 升势/震荡/降势（生产 A 判据同口径）"""
    con = sqlite3.connect(f"file:{BT / 'full_hist_10pct.db'}?mode=ro", uri=True)
    rows = con.execute("SELECT date, low FROM daily_bar WHERE code='sh000001' ORDER BY date").fetchall()
    con.close()
    out, win = {}, []
    prev = None
    for d, low in rows:
        win.append(low)
        if len(win) > 60:
            win.pop(0)
        cur = min(win)
        if prev is None or len(win) < 60:
            out[d] = "震荡"
        elif cur > prev * 1.002:
            out[d] = "升势"
        elif cur < prev * 0.998:
            out[d] = "降势"
        else:
            out[d] = "震荡"
        prev = cur
    return out


def daily(con, regime) -> list[dict]:
    eq = con.execute("SELECT date, market_value, total FROM equity_snapshot ORDER BY date").fetchall()
    pos = con.execute("SELECT opened_date, closed_date FROM position").fetchall()
    out = []
    for d, mv, tot in eq:
        n = sum(1 for o, c in pos if o <= d and (c is None or c > d))
        out.append({"date": d, "inv": (mv / tot) if tot else 0.0, "n": n,
                    "regime": regime.get(d, "震荡")})
    return out


def entries(con) -> list[dict]:
    rows = con.execute("""
        SELECT o.code, t.plan_date, o.qty, o.trigger_price, o.reason_json FROM plan_order o
        JOIN trade_plan t ON t.id = o.trade_plan_id
        WHERE o.side='buy' ORDER BY t.plan_date""").fetchall()
    eq = dict(con.execute("SELECT date, total FROM equity_snapshot").fetchall())
    out = []
    for code, d, qty, px, rj in rows:
        tot = eq.get(d)
        if not tot or not qty or not px:
            continue
        try:
            units = json.loads(rj or "{}").get("units", 1.0)
        except Exception:
            units = 1.0
        out.append({"date": d, "pct": qty * px / tot, "units": units})
    return out


def agg(rows, key):
    v = sorted(r[key] for r in rows)
    if not v:
        return {"n": 0}
    m = len(v)
    med = v[m // 2] if m % 2 else (v[m // 2 - 1] + v[m // 2]) / 2
    return {"n": m, "mean": sum(v) / m, "median": med, "max": v[-1], "min": v[0]}


def main() -> int:
    regime = idx_regime()
    data = {}
    for label, pat in VARIANTS.items():
        days, ents, per_year = [], [], []
        for y in YEARS:
            con = sqlite3.connect(f"file:{BT / pat.format(y=y)}?mode=ro", uri=True)
            dl = daily(con, regime)
            ents += entries(con)
            con.close()
            days += dl
            per_year.append({"year": y, **{k: v for k, v in agg(dl, "inv").items()},
                             "n_mean": agg(dl, "n")["mean"], "n_max": agg(dl, "n")["max"],
                             "n_median": agg(dl, "n")["median"]})
        buckets = {}
        for st in ("升势", "震荡", "降势"):
            sub = [r for r in days if r["regime"] == st]
            buckets[st] = {"days": len(sub), **{("inv_" + k): v for k, v in agg(sub, "inv").items()},
                           "n_mean": agg(sub, "n")["mean"], "n_median": agg(sub, "n")["median"],
                           "n_max": agg(sub, "n")["max"],
                           "zero_share": (sum(1 for r in sub if r["n"] == 0) / len(sub)) if sub else 0.0,
                           "full_share": (sum(1 for r in sub if r["n"] == 7) / len(sub)) if sub else 0.0}
        data[label] = {
            "overall": {"days": len(days),
                        **{("inv_" + k): v for k, v in agg(days, "inv").items()},
                        "n_mean": agg(days, "n")["mean"], "n_median": agg(days, "n")["median"],
                        "n_max": agg(days, "n")["max"],
                        "zero_share": sum(1 for r in days if r["n"] == 0) / len(days),
                        "full_share": sum(1 for r in days if r["n"] == 7) / len(days)},
            "buckets": buckets,
            "per_year": per_year,
            "entries": {"count": len(ents), **{k: v for k, v in agg(ents, "pct").items()},
                        "units_mean": sum(e["units"] for e in ents) / len(ents),
                        "u1": sum(1 for e in ents if e["units"] <= 1.0),
                        "u2": sum(1 for e in ents if e["units"] >= 2.0)},
        }
    (BT / "position_profile.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    b, p = data["baseline"], data["baseline+"]

    def pc(x, d=1):
        return f"{x * 100:.{d}f}%"

    L = ["# E17 14 年真实仓位画像（2005–2018）\n",
         "> 生成器：`scripts/e17_position_profile.py`（只读 28 库的 equity_snapshot / position / plan_order，零回测）",
         "> 仓位占用率 = 每日 `market_value / total`；持仓只数按 `position` 开关日期重建；单笔建仓比例 = `qty × trigger_price / 当日权益`",
         "> 大盘三态 = sh000001 60 日低点相邻日比（生产 A 判据同口径）\n", "---\n",
         "## 一、总体：从来没满过仓，也从来没空过仓\n",
         "| 指标 | baseline | baseline+ |", "|---|---:|---:|",
         f"| 样本交易日 | {b['overall']['days']} | {p['overall']['days']} |",
         f"| 仓位占用 平均 / 中位 | **{pc(b['overall']['inv_mean'])} / {pc(b['overall']['inv_median'])}** | {pc(p['overall']['inv_mean'])} / {pc(p['overall']['inv_median'])} |",
         f"| 仓位占用 最高 | **{pc(b['overall']['inv_max'])}** | {pc(p['overall']['inv_max'])} |",
         f"| 持仓只数 平均 / 中位 / 最高 | **{b['overall']['n_mean']:.2f} / {b['overall']['n_median']:.0f} / {b['overall']['n_max']:.0f}** | {p['overall']['n_mean']:.2f} / {p['overall']['n_median']:.0f} / {p['overall']['n_max']:.0f} |",
         f"| 空仓日占比 | {pc(b['overall']['zero_share'])} | {pc(p['overall']['zero_share'])} |",
         f"| 满仓（7 只）日占比 | {pc(b['overall']['full_share'])} | {pc(p['overall']['full_share'])} |",
         f"| 单笔建仓占权益 平均 / 中位 / 最大 | {pc(b['entries']['mean'])} / {pc(b['entries']['median'])} / {pc(b['entries']['max'])} | {pc(p['entries']['mean'])} / {pc(p['entries']['median'])} / {pc(p['entries']['max'])} |",
         f"| 建仓笔数（1 单位 / 2 单位） | {b['entries']['u1']} / {b['entries']['u2']} | {p['entries']['u1']} / {p['entries']['u2']} |", "",
         "---\n", "## 二、按大盘三态分桶（进攻/防守/参与度）\n",
         "| 大盘状态 | 交易日 | baseline 平均仓位 | baseline 平均只数 | baseline+ 平均仓位 | baseline+ 平均只数 | baseline 空仓日 | baseline+ 空仓日 |",
         "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for st in ("升势", "震荡", "降势"):
        bb, pp = b["buckets"][st], p["buckets"][st]
        L.append(f"| **{st}** | {bb['days']} | {pc(bb['inv_mean'])} | {bb['n_mean']:.2f} | "
                 f"{pc(pp['inv_mean'])} | {pp['n_mean']:.2f} | {pc(bb['zero_share'])} | {pc(pp['zero_share'])} |")
    L += ["", "---\n", "## 三、逐年\n",
          "| 年 | baseline 平均仓位 | baseline 最高仓位 | baseline 平均只数 | baseline 最高只数 | baseline+ 平均仓位 | baseline+ 最高仓位 |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for i, y in enumerate(b["per_year"]):
        q = p["per_year"][i]
        L.append(f"| {y['year']} | {pc(y['mean'])} | {pc(y['max'])} | {y['n_mean']:.2f} | {y['n_max']:.0f} | "
                 f"{pc(q['mean'])} | {pc(q['max'])} |")
    L += ["", "---\n", "## 四、诚实边界\n",
          "1. 仓位占用率含未平仓浮盈浮亏（`market_value/total`），是个股涨跌与仓位共同作用的结果，不是纯「下注比例」。",
          "2. 单笔建仓比例按 `qty × trigger_price / 当日权益` 近似（未含费用与滑点）；整手取整使实际值略低于理论值。",
          "3. 大盘三态用 A 判据（60 日低点），与 docs/00 §5.6.8 同口径；它描述指数结构，不代表个股行情（2013 教训）。",
          "4. 7 只是配置上限硬约束，「满仓日占比」低可能是信号少而非保守。\n"]
    (DOCS / "e17_position_profile_report.md").write_text("\n".join(L), encoding="utf-8")

    # ---- SVG：逐年平均仓位 + 三态对照 ----
    W, H = 680, 400
    L_, R_, T_, B_ = 60, 20, 50, 60
    pw, ph = W - L_ - R_, H - T_ - B_
    barw = pw / (len(YEARS) * 2.4)
    parts = [f'<?xml version="1.0" encoding="UTF-8"?>',
             f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="PingFang SC, Microsoft YaHei, sans-serif">',
             f'<rect width="{W}" height="{H}" rx="10" fill="#f8fafc"/>',
             f'<text x="{W/2}" y="24" text-anchor="middle" font-size="14" font-weight="700" fill="#0f172a">逐年平均仓位占用（灰=baseline 红=baseline+）</text>']
    for g in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        y = T_ + ph * (1 - g)
        parts.append(f'<line x1="{L_}" y1="{y:.1f}" x2="{W-R_}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        parts.append(f'<text x="{L_-8}" y="{y+4:.1f}" text-anchor="end" font-size="9.5" fill="#64748b">{int(g*100)}%</text>')
    for i, y in enumerate(b["per_year"]):
        x0 = L_ + pw * i / len(YEARS) + barw * 0.2
        h1 = ph * min(1.0, y["mean"])
        h2 = ph * min(1.0, p["per_year"][i]["mean"])
        parts.append(f'<rect x="{x0:.1f}" y="{T_+ph-h1:.1f}" width="{barw:.1f}" height="{h1:.1f}" fill="#94a3b8"/>')
        parts.append(f'<rect x="{x0+barw:.1f}" y="{T_+ph-h2:.1f}" width="{barw:.1f}" height="{h2:.1f}" fill="#dc2626"/>')
        parts.append(f'<text x="{x0+barw:.1f}" y="{H-B_+16}" text-anchor="middle" font-size="9.5" fill="#334155">{y["year"]}</text>')
    parts.append(f'<text x="{L_}" y="{H-8}" font-size="9.5" fill="#94a3b8">14 年平均仓位 baseline {pc(b["overall"]["inv_mean"])} / baseline+ {pc(p["overall"]["inv_mean"])}；7 只上限从未长期打满</text>')
    parts.append("</svg>")
    (DOCS / "e17_position_profile.svg").write_text("\n".join(parts), encoding="utf-8")

    print(f"{'指标':<20}{'baseline':>12}{'baseline+':>12}")
    for k, f in (("平均仓位", lambda x: pc(x["inv_mean"])), ("中位仓位", lambda x: pc(x["inv_median"])),
                 ("最高仓位", lambda x: pc(x["inv_max"])), ("平均只数", lambda x: f"{x['n_mean']:.2f}"),
                 ("中位只数", lambda x: f"{x['n_median']:.0f}"), ("最高只数", lambda x: f"{x['n_max']:.0f}")):
        print(f"{k:<20}{f(b['overall']):>12}{f(p['overall']):>12}")
    print("\n三态：")
    for st in ("升势", "震荡", "降势"):
        bb, pp = b["buckets"][st], p["buckets"][st]
        print(f"  {st} {bb['days']:>4}天  仓位 {pc(bb['inv_mean'])}/{pc(pp['inv_mean'])}  只数 {bb['n_mean']:.2f}/{pp['n_mean']:.2f}  空仓 {pc(bb['zero_share'])}/{pc(pp['zero_share'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
