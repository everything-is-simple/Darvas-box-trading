#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""base_snowball.py —— 新基线（单镜上证）14 年滚雪球复利总账

为什么需要另写一个（而不是直接用 e17_snowball.py）
--------------------------------------------------
`e17_snowball.py` 硬编码读 `bt_e17_<year>.db` + `bt_b12_stop_<year>.db` 共 28 个库，
那是老机的产物，未随移植包带来（见 DEPLOY-PLAN §10）。本脚本用**同一套链式算法**
读新基线库 `var/bt/base/bt_baseA_<year>.db`，并把两个历史锚作为固定对照列写在报告里：

  账本基线（E17 + gate=off，旧仓位语义）      终值 2,610,550 / CAGR 7.09% / MDD −21.10%
  擂主（E17 + gate=stop，旧仓位语义）          终值 2,961,561 / CAGR 8.06% / MDD −8.03% / Calmar 1.005
  新基线（本脚本，单镜上证 + max_hold=14 + 闸门 off）   ← 待判定

⚠️ 三者语义不完全相同（仓位上限与权益闸门不同），对照只作**量级与形状**参考，
   不得当作同参数 A/B（docs/29 §〇 四层基线不得混用）。

算法（与 e17_snowball.py 逐行等价）
    level_0 = 1,000,000
    年内第 d 日：level_d = level_{d-1} × (total_d / total_{d-1})
    跨年：上一年末 level 直接作为下一年起点（每年首日权益恒 = 1,000,000）

用法
    python scripts/base_snowball.py              # 14 年齐全才出报告
    python scripts/base_snowball.py --allow-partial   # 缺年也出（标注不完整，禁止引用为结论）
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BT = ROOT / "var" / "bt"
BASE = BT / "base"
DOCS = ROOT / "docs" / "experiments"
YEARS = list(range(2005, 2019))
START = 1_000_000.0
PATTERN = "bt_baseA_{y}.db"

# 历史锚（老机产物，docs/29 §〇；本机无对应库，只能引用数值）
ANCHORS = {
    "账本基线": dict(pattern="bt_e17_{y}.db", final=2_610_550, cagr=0.0709, mdd=-0.2110,
                     note="E17 + gate.mode=off，旧仓位语义（max_hold=7、无权益闸门）"),
    "擂主": dict(pattern="bt_b12_stop_{y}.db", final=2_961_561, cagr=0.0806, mdd=-0.0803,
                 note="E17 + gate.mode=stop（B-2），旧仓位语义"),
}


def daily_totals(db: Path):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT date, total FROM equity_snapshot ORDER BY date").fetchall()
    finally:
        con.close()


def chain(years):
    level = START
    curve, yearly = [], []
    for y in years:
        db = BASE / PATTERN.format(y=y)
        rows = daily_totals(db)
        if not rows:
            raise SystemExit(f"{db.name} 无 equity_snapshot，拒绝出报告")
        y_start = level
        prev = START
        for d, total in rows:
            level *= total / prev
            prev = total
            curve.append((d, level))
        yearly.append({
            "year": y, "days": len(rows), "y_start": y_start, "y_end": level,
            "comp_ret": level / y_start - 1.0,
            "chain_factor": level / y_start, "db_factor": rows[-1][1] / START,
            "last_date": rows[-1][0],
        })
    return curve, yearly


def max_drawdown(curve):
    peak, peak_d = -1.0, ""
    mdd, mdd_from, mdd_to, cur, longest = 0.0, "", "", 0, 0
    for d, v in curve:
        if v >= peak:
            peak, peak_d, cur = v, d, 0
        else:
            cur += 1
            longest = max(longest, cur)
            dd = v / peak - 1.0
            if dd < mdd:
                mdd, mdd_from, mdd_to = dd, peak_d, d
    return mdd, mdd_from, mdd_to, longest


def stats(curve, yearly):
    final = curve[-1][1]
    n_y = len(yearly)
    cagr = (final / START) ** (1.0 / n_y) - 1.0
    rets = [curve[i][1] / curve[i - 1][1] - 1.0 for i in range(1, len(curve))]
    mean_d = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean_d) ** 2 for r in rets) / (len(rets) - 1))
    mdd, dd_from, dd_to, dd_days = max_drawdown(curve)
    y_ret = [x["comp_ret"] for x in yearly]
    am = sum(y_ret) / n_y
    gm = math.exp(sum(math.log1p(r) for r in y_ret) / n_y) - 1.0
    return dict(
        final=final, total_ret=final / START - 1.0, cagr=cagr, years=n_y,
        vol=sd * math.sqrt(252), sharpe=(mean_d / sd * math.sqrt(252)) if sd else float("nan"),
        mdd=mdd, mdd_from=dd_from, mdd_to=dd_to, mdd_days=dd_days,
        calmar=cagr / abs(mdd) if mdd else float("nan"),
        arith_mean=am, geo_mean=gm, drag=am - gm,
        best=max(yearly, key=lambda x: x["comp_ret"]),
        worst=min(yearly, key=lambda x: x["comp_ret"]),
        daily_points=len(curve),
    )


def pct(x, d=2):
    return f"{x * 100:+.{d}f}%"


def money(x):
    return f"{x:,.0f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-partial", action="store_true",
                    help="缺年也出报告（标注不完整；禁止引用为结论）")
    a = ap.parse_args()

    have = [y for y in YEARS if (BASE / PATTERN.format(y=y)).exists()]
    missing = [y for y in YEARS if y not in have]

    # 完整性：最后交易日 ≥ 当年 12-25（A 股年度最后交易日恒在 12-28~12-31）
    def done(y):
        try:
            rows = daily_totals(BASE / PATTERN.format(y=y))
            return bool(rows) and rows[-1][0] >= f"{y}-12-25"
        except Exception:
            return False

    partial = [y for y in have if not done(y)]
    if missing and not a.allow_partial:
        raise SystemExit(f"缺 {len(missing)} 年（{missing}），用 --allow-partial 可出残缺版")
    years = [y for y in have if y not in partial]
    if not years:
        raise SystemExit("没有一个完整年，拒绝出报告")

    # 连续性检查：年份不连续时，链式复利与 CAGR 无经济意义（中间年缺失=跳跃）
    contiguous = years == list(range(years[0], years[-1] + 1))
    if not contiguous:
        print("⚠️  年份不连续（", years, "），链式复利/CAGR 不可引用——仅供调试")

    curve, yearly = chain(years)

    # HARD 1：逐年链式因子 ≡ 该库年末权益/100万
    for x in yearly:
        assert abs(x["chain_factor"] - x["db_factor"]) < 1e-6, f"{x['year']} 链式因子不符"
    # HARD 2：每年必须跑到 12-25 之后（完整性判据）
    for x in yearly:
        assert x["last_date"] >= f"{x['year']}-12-25", f"{x['year']} 未跑完（截至 {x['last_date']}）"

    st = stats(curve, yearly)
    out = {
        "years": years, "missing": missing, "partial": partial, "start_capital": START,
        "variant": "新基线 · 单镜上证（max_hold=14, equity_gate=off, ma60=stop@sh000001）",
        "stats": st, "yearly": yearly,
        "anchors": {k: {kk: vv for kk, vv in v.items() if kk != "pattern"}
                    for k, v in ANCHORS.items()},
    }
    (DOCS / "e17_baseline_single_index_data.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    L = []
    L.append("# 新基线 · 单镜上证：14 年滚雪球复利总账（2005–2018）\n")
    L.append("> 生成器：`scripts/base_snowball.py`（只读 `var/bt/base/bt_baseA_<year>.db` 的 `equity_snapshot`，逐日链式复利）")
    L.append("> 参数：`newhigh_ladder=on lookback=252 cutoff=40 units=3/2/1/0.5 pool.max_hold=14 "
             "pos.equity_gate=off gate.ma60=stop@sh000001 staledays=2`")
    L.append(f"> 数据：{len(years)} 年齐全" + (f"；**缺 {missing}，本报告不完整**" if missing else "（2005–2018 全段）"))
    if partial:
        L.append(f"> 已排除未跑完的年份：{partial}（`equity_snapshot` 未到 12-25，不参与复利链）")
    L.append("> 算法：`level_d = level_{d-1} × (total_d / total_{d-1})`；跨年直接衔接（每年首日权益恒 = 1,000,000）\n")
    if not contiguous:
        L.append("> ## ⚠️ 本年表年份不连续，链式复利/CAGR/MDD 全部不可引用\n"
                 "> （缺年等于资金跳跃，不是真实路径）。仅当 2005–2018 连续时本报告才有结论效力。\n")
    L.append("---\n")

    L.append("## 一、一句话结论\n")
    best_anchor = max(ANCHORS.values(), key=lambda x: x["final"])
    rel = st["final"] / best_anchor["final"] - 1.0
    L.append(f"- 新基线 14 年复利终值 **{money(st['final'])}**（累计 {pct(st['total_ret'],1)}，"
             f"CAGR **{pct(st['cagr'])}**，日频链式 MDD **{pct(st['mdd'])}**，Calmar **{st['calmar']:.3f}**）。")
    L.append(f"- 与历史最高锚（{[(k,v['final']) for k,v in ANCHORS.items() if v is best_anchor][0][0]} "
             f"{money(best_anchor['final'])}）相比 **{rel*100:+.1f}%**。")
    L.append(f"- 极端年：最好 {st['best']['year']} {pct(st['best']['comp_ret'])}，"
             f"最差 {st['worst']['year']} {pct(st['worst']['comp_ret'])}。\n")
    L.append("> ⚠️ 三个锚的参数语义不同（仓位上限 / 权益闸门），本表只做**量级与形状**对照，"
             "不是同参数 A/B（docs/29 §〇 四层基线不得混用）。\n")
    L.append("---\n")

    L.append("## 二、与两个历史锚并列\n")
    L.append("| 口径 | 终值 | 累计收益 | CAGR | MDD | Calmar | 说明 |")
    L.append("|---|---:|---:|---:|---:|---:|---|")
    L.append(f"| **新基线（单镜上证）** | **{money(st['final'])}** | **{pct(st['total_ret'],1)}** | "
             f"**{pct(st['cagr'])}** | **{pct(st['mdd'])}** | **{st['calmar']:.3f}** | 本机 14 年，`max_hold=14`+闸门 off |")
    for k, v in ANCHORS.items():
        L.append(f"| {k}（老机） | {money(v['final'])} | {pct(v['final']/START-1,1)} | {pct(v['cagr'])} | "
                 f"{pct(v['mdd'])} | {'1.005' if k == '擂主' else '0.336'} | {v['note']} |")
    L.append("")
    L.append(f"> 资金体感：1 万元起，14 年后 → **{st['final']/START*10000:,.0f} 元**。\n")
    L.append("---\n")

    L.append("## 三、逐年复利明细\n")
    L.append("| 年 | 交易日 | 年初 | 收益率 | 年末 | 相对 100 万 |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for x in yearly:
        L.append(f"| {x['year']} | {x['days']} | {money(x['y_start'])} | {pct(x['comp_ret'])} | "
                 f"{money(x['y_end'])} | {pct(x['db_factor']-1)} |")
    L.append("")
    L.append("> 「相对 100 万」= 该年库自身首末比（每年独立起跑的真实收益），"
             "「收益率」是链式口径；两者应完全相同——这是 HARD 校验项。\n")
    L.append("---\n")

    L.append("## 四、风险指标\n")
    L.append("| 指标 | 值 |")
    L.append("|---|---:|")
    L.append(f"| 累计收益率 | {pct(st['total_ret'],1)} |")
    L.append(f"| CAGR | {pct(st['cagr'])} |")
    L.append(f"| 最大回撤（日频链式） | {pct(st['mdd'])} |")
    L.append(f"| 回撤区间 | {st['mdd_from']} → {st['mdd_to']} |")
    L.append(f"| 水下最长交易日 | {st['mdd_days']} |")
    L.append(f"| 年化波动率 | {pct(st['vol'])} |")
    L.append(f"| 夏普（rf=0） | {st['sharpe']:.3f} |")
    L.append(f"| Calmar | {st['calmar']:.3f} |")
    L.append(f"| 算术平均年收益 | {pct(st['arith_mean'])} |")
    L.append(f"| 几何平均年收益 | {pct(st['geo_mean'])} |")
    L.append(f"| 波动拖累（算术−几何） | {pct(st['drag'])} |")
    L.append("")
    L.append("---\n")

    L.append("## 五、诚实边界\n")
    L.append("1. **这是「按年收益率的复利推演」，不是连续交易路径的重跑**：真连续跑时下单金额基于复利后权益，"
             "整手取整、持仓上限、黑名单冷启动的边界行为都会变。")
    L.append("2. **跨年强制衔接**：年末未平仓头寸按市价结算带入下一年起点（`equity_snapshot` 已含浮盈）。")
    L.append("3. **容量未计**：14 年复利到数百万仍属小资金，冲击成本未建模。")
    L.append("4. **无风险利率取 0**：夏普仅作相对指标。")
    L.append("5. **历史锚不可重算**：账本基线与擂主的逐笔库在老机、未随包带来，表中数值只能引用，"
             "本机无法复现其曲线。\n")

    (DOCS / "e17_baseline_single_index_report.md").write_text("\n".join(L), encoding="utf-8")

    print(f"{'指标':<20}{'新基线':>16}")
    for k, f in (("终值", money), ("累计收益", lambda x: pct(x, 1)), ("CAGR", pct),
                 ("最大回撤", pct), ("水下天数", lambda x: f"{x}"),
                 ("年化波动", pct), ("夏普", lambda x: f"{x:.3f}"), ("Calmar", lambda x: f"{x:.3f}")):
        key = {"终值": "final", "累计收益": "total_ret", "CAGR": "cagr", "最大回撤": "mdd",
               "水下天数": "mdd_days", "年化波动": "vol", "夏普": "sharpe", "Calmar": "calmar"}[k]
        print(f"{k:<20}{f(st[key]):>16}")
    print(f"\n产物 → {DOCS/'e17_baseline_single_index_report.md'}")
    print(f"       {DOCS/'e17_baseline_single_index_data.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
