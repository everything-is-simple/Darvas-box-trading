# -*- coding: utf-8 -*-
"""
甜点 2-4% 在新基线（baseA）下的复验 · 零成本 A 类。

背景（2026-11-03）：
  - 历史结论「甜点 2-4%」来自旧 E17 基线（e17_newhigh_distance_report.md, M9.0）：
    2-4% 净利 +640,260 / 122 笔 / 胜率 39.3% / 每单位 +1,749 / PF 4.02 / 占总量 76%。
  - E17 库已归档消失，用户拍板改用现行生产基线 baseA（bt_baseA_<year>.db）复验。
  - 本脚本复刻旧报告口径（FINE_BANDS 2% 步进 / COARSE_BANDS 5% / TIERS / agg 六项），
    仅两处差异：
      (1) 数据源 bt_e17 -> bt_baseA；
      (2) regime 用库内 reason_json.regime（bull/range/bear），不再复算 A 判据。

口径（严格对齐 e17_newhigh_distance_report.py）：
  - 交易 = position_leg.leg_no=1 建仓（baseA 阶梯制只首买，全 leg_no=1）
  - newhigh_distance_pct = plan_order.reason_json.newhigh_distance_pct（触发日）
  - units = reason_json.units（t1=3/t2=2/t3=1/t4=0.5）
  - 净利 = position.realized_pnl（status='closed'）
  - 每单位净利 = Sum(pnl)/Sum(units)；PF = 毛盈/|毛亏|；集中度 = 前三净利/毛盈
"""
import os
import sys
import json
import glob
import sqlite3
import statistics
from collections import OrderedDict
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BT = os.path.join(BASE, "var", "bt", "base")
OUT = os.path.join(BASE, "docs", "experiments")

# 细粒度分档（2% 步进，与旧报告一致）
FINE_BANDS = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 12), (12, 14),
              (14, 16), (16, 18), (18, 20), (20, 25), (25, 30), (30, 35), (35, 40)]
COARSE_BANDS = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40)]

TIERS = OrderedDict([
    ("newhigh_t1", ("t1", "0–5%", 3.0)),
    ("newhigh_t2", ("t2", "5–10%", 2.0)),
    ("newhigh_t3", ("t3", "10–20%", 1.0)),
    ("newhigh_t4", ("t4", "20–40%", 0.5)),
])

REGIME_CN = {"bull": "升势（牛）", "range": "震荡（猴）", "bear": "降势（熊）"}


def years_list():
    return sorted(int(os.path.basename(p).split('_')[2].split('.')[0])
                  for p in glob.glob(os.path.join(BT, "bt_baseA_*.db")))


def load_trades():
    trades = []
    for y in years_list():
        db = os.path.join(BT, f"bt_baseA_{y}.db")
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
            try:
                units = float(rj.get("units", 1.0))
            except (TypeError, ValueError):
                units = 1.0
            trades.append({
                "year": y, "dist": float(dist),
                "units": units, "pnl": r["realized_pnl"],
                "status": r["status"], "opened": r["opened_date"],
                "closed": r["closed_date"],
                "basis": rj.get("sizing_basis"),
                "regime": rj.get("regime", "range"),
            })
        con.close()
    return trades


def agg(rows):
    rows = [r for r in rows if r["pnl"] is not None]
    n = len(rows)
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] < 0]
    pnl = sum(r["pnl"] for r in rows)
    units = sum(r["units"] for r in rows)
    gw = sum(r["pnl"] for r in wins)
    gl = sum(r["pnl"] for r in losses)
    # 保本胜率：pnl > 0 的笔数占比（与旧报告一致，wr 即胜率；保本用 pnl>=0 另算）
    be = sum(1 for r in rows if r["pnl"] >= 0)
    wr = len(wins) / n if n else None
    be_r = be / n if n else None
    pu = pnl / units if units else None
    pf = (gw / abs(gl)) if gl else None
    top3 = sum(sorted((r["pnl"] for r in rows), reverse=True)[:3])
    conc = top3 / gw if gw else None
    med = statistics.median([r["pnl"] for r in rows]) if rows else None
    return {
        "n": n, "pnl": pnl, "units": units,
        "wr": wr, "be": be_r, "pu": pu, "pf": pf, "conc": conc,
        "top3": top3, "med": med,
    }


def band_rows(trades, lo, hi):
    return [t for t in trades if lo <= t["dist"] < hi]


def fnum(x, d=0):
    return f"{x:,.{d}f}" if x is not None else "—"


def fsign(x, d=0):
    if x is None:
        return "—"
    return f"{x:+,.{d}f}"


def main():
    trades = load_trades()
    closed = [t for t in trades if t["status"] == "closed" and t["pnl"] is not None]
    total_pnl = sum(t["pnl"] for t in closed)
    total_units = sum(t["units"] for t in closed)

    fine = OrderedDict()
    for lo, hi in FINE_BANDS:
        fine[(lo, hi)] = agg(band_rows(closed, lo, hi))
    coarse = OrderedDict()
    for lo, hi in COARSE_BANDS:
        coarse[(lo, hi)] = agg(band_rows(closed, lo, hi))
    tiers = OrderedDict()
    for key, (short, rng, units) in TIERS.items():
        sub = [t for t in closed if t["basis"] == key]
        tiers[key] = agg(sub)

    # regime 分层（用库内 regime 字段）
    regime_fine = OrderedDict()
    for rkey in ["bull", "range", "bear"]:
        rc = [t for t in closed if t["regime"] == rkey]
        if not rc:
            continue
        subs = OrderedDict()
        for lo, hi in FINE_BANDS:
            subs[(lo, hi)] = agg(band_rows(rc, lo, hi))
        regime_fine[rkey] = {"overall": agg(rc), "fine": subs}

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "var/bt/base/bt_baseA_<year>.db（新基线 baseA）",
        "total_closed_n": len(closed),
        "total_pnl": total_pnl,
        "total_units": total_units,
        "fine": {f"{lo}-{hi}": v for (lo, hi), v in fine.items()},
        "coarse": {f"{lo}-{hi}": v for (lo, hi), v in coarse.items()},
        "tiers": tiers,
        "regime": {k: {"overall": v["overall"],
                       "fine": {f"{lo}-{hi}": a for (lo, hi), a in v["fine"].items()}}
                   for k, v in regime_fine.items()},
    }
    os.makedirs(OUT, exist_ok=True)
    json_path = os.path.join(OUT, "e17_newhigh_sweetspot_baseA_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- markdown 报告 ----
    L = []
    L.append("# 甜点 2–4% 在新基线（baseA）复验 · A 类\n")
    L.append(f"> 生成于 {data['generated_at']} · 数据源 {data['source']}\n")
    L.append("> 目的：旧 E17 基线的「甜点 2–4%」结论，在现行生产基线 baseA（阶梯制+MA60 闸门）\n")
    L.append("> 下是否仍成立（甜点是否漂移、是否仍是 2–4% 窄带）。\n")
    L.append(f"## 总体（已平仓 {data['total_closed_n']} 笔，净利 {fsign(total_pnl)}，单位 {total_units:.1f}）\n")

    L.append("## ① 细粒度 2% 步进（14 年合并）\n")
    L.append("| 分档 | 笔数 | 胜率 | 每单位净利 | 保本胜率 | PF | 集中度 | 净利 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for (lo, hi), a in fine.items():
        L.append(f"| {lo}–{hi}% | {a['n']} | {a['wr']*100:.1f}% | {fsign(a['pu'])} | {a['be']*100:.1f}% | "
                 f"{a['pf']:.2f} | {a['conc']*100:.1f}% | {fsign(a['pnl'])} |")
    L.append("")

    L.append("## ② 5% 粗粒度对照\n")
    L.append("| 分档 | 笔数 | 胜率 | 每单位净利 | 保本胜率 | PF | 集中度 | 净利 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for (lo, hi), a in coarse.items():
        L.append(f"| {lo}–{hi}% | {a['n']} | {a['wr']*100:.1f}% | {fsign(a['pu'])} | {a['be']*100:.1f}% | "
                 f"{a['pf']:.2f} | {a['conc']*100:.1f}% | {fsign(a['pnl'])} |")
    L.append("")

    L.append("## ③ 四档 t1–t4（按 sizing_basis）\n")
    L.append("| 档位 | 单位 | 笔数 | 胜率 | 每单位净利 | PF | 净利 |")
    L.append("|---|---|---|---|---|---|---|")
    for key, (short, rng, units) in TIERS.items():
        a = tiers[key]
        L.append(f"| {short} {rng} | {units} | {a['n']} | {a['wr']*100:.1f}% | {fsign(a['pu'])} | "
                 f"{a['pf']:.2f} | {fsign(a['pnl'])} |")
    L.append("")

    L.append("## ④ regime 归因（库内 regime 字段：bull/range/bear）\n")
    for rkey in ["bull", "range", "bear"]:
        if rkey not in regime_fine:
            continue
        o = regime_fine[rkey]["overall"]
        L.append(f"### {REGIME_CN[rkey]}（{o['n']} 笔，净利 {fsign(o['pnl'])}，胜率 {o['wr']*100:.1f}%）\n")
        L.append("| 分档 | 笔数 | 胜率 | 每单位净利 | 净利 |")
        L.append("|---|---|---|---|---|")
        for (lo, hi), a in regime_fine[rkey]["fine"].items():
            if a["n"] == 0:
                continue
            L.append(f"| {lo}–{hi}% | {a['n']} | {a['wr']*100:.1f}% | {fsign(a['pu'])} | {fsign(a['pnl'])} |")
        L.append("")

    L.append("---\n*本报告由 scripts/e17_newhigh_sweetspot_baseA.py 自动生成。*")
    md_path = os.path.join(OUT, "e17_newhigh_sweetspot_baseA_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    # ---- 控制台摘要 ----
    print(f"已平仓 {len(closed)} 笔，总净利 {total_pnl:+,.0f}，总单位 {total_units:.1f}")
    print(f"甜点候选 2-4%：{fine[(2,4)]}")
    print(f"陷阱候选 4-6%：{fine[(4,6)]}")
    print(f"0-2%：{fine[(0,2)]}")
    print(f"t1(0-5%)：{tiers['newhigh_t1']}")
    print(f"产出：{os.path.basename(md_path)}, {os.path.basename(json_path)}")


if __name__ == "__main__":
    main()
