#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2.5 · 双镜 AND 门零成本推演（docs/29 §六之二，2026-10-29）

原理（不跑回测，只读 A/B 两库）：
- A 组闸门：上证 < MA60 → 停买；B 组闸门：中证500 < MA60 → 停买。
- **AND 门**：两个都 < MA60 才停买 → 放行条件 = 上证 ≥ MA60 **或** 中证500 ≥ MA60
  → 放行的买单集合 = A 组买单 ∪ B 组买单（并集）。
- **OR 门**（对照）：任一 < MA60 就停 → 放行 = A ∩ B（交集）。
- 因此可用「逐笔并集/交集」直接推演 AND/OR 门能拿到哪些交易、合计盈亏多少。

⚠️ 口径限制（必须诚实标注）：
1. 这是**逐笔层面的并集近似**，不是组合级回测——真实 14 只上限与资金约束会改变可成交量与顺序，
   故结果为「方向性估计」，不是可采纳的终值；
2. 盈亏用 `position.realized_pnl` 累加，年均收益以「Σpnl / 100 万起始权益」为代理（与加法账同源），
   四年再链式复利；与真回测的复利账有差异，只用于比较四种门控的相对高低；
3. 交易同一性以 (code, opened_date) 判定。

产物：docs/experiments/e17_and_gate_projection.{md,json}
"""
import json
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BT = BASE / "var" / "bt" / "base"
OUT = BASE / "docs" / "experiments"
YEARS = (2007, 2010, 2013, 2018)
START_EQUITY = 1_000_000.0


def load(tag, year):
    db = BT / f"bt_base{tag}_{year}.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT code, opened_date, closed_date, realized_pnl, close_reason "
        "FROM position WHERE status='closed'"
    ).fetchall()
    con.close()
    return {(r["code"], r["opened_date"]): dict(r) for r in rows}


def agg(d):
    pnls = [v["realized_pnl"] for v in d.values()]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": n,
        "pnl": sum(pnls),
        "wr": (wins / n) if n else 0.0,
        "wins": wins,
    }


def main():
    result = {"years": {}, "note": "逐笔并集近似，非组合回测"}
    for y in YEARS:
        a, b = load("A", y), load("B", y)
        if a is None or b is None:
            continue
        ka, kb = set(a), set(b)
        only_a, only_b = ka - kb, kb - ka
        both = ka & kb
        union = ka | kb
        row = {
            "A_单上证": agg({k: a[k] for k in ka}),
            "B_单中证500": agg({k: b[k] for k in kb}),
            "AND_并集": agg({**{k: a[k] for k in ka}, **{k: b[k] for k in kb}}),
            "OR_交集": agg({k: a[k] for k in both}),
            "仅A有": {"n": len(only_a), "pnl": sum(a[k]["realized_pnl"] for k in only_a)},
            "仅B有": {"n": len(only_b), "pnl": sum(b[k]["realized_pnl"] for k in only_b)},
        }
        result["years"][y] = row

    # 四年链式复利（Σpnl/起始权益 作年收益代理）
    comp = {}
    for key in ("A_单上证", "B_单中证500", "AND_并集", "OR_交集"):
        lvl = 1.0
        for y, r in result["years"].items():
            lvl *= 1 + r[key]["pnl"] / START_EQUITY
        comp[key] = lvl
    result["compound"] = comp

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e17_and_gate_projection.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = ["# 阶段 2.5 · 双镜 AND 门零成本推演", "",
             "> 口径：逐笔并集近似（非组合回测）；年收益代理 = Σpnl / 100 万起始权益；四年链式复利。", ""]
    hdr = "| 年 | 门控 | 笔数 | 胜率 | Σpnl | 年收益代理 |"
    lines += [hdr, "|---|---|---:|---:|---:|---:|"]
    for y, r in result["years"].items():
        for key in ("A_单上证", "B_单中证500", "AND_并集", "OR_交集"):
            g = r[key]
            lines.append(
                f"| {y} | {key} | {g['n']} | {g['wr']*100:.1f}% | {g['pnl']:,.0f} | {g['pnl']/START_EQUITY*100:+.1f}% |"
            )
    lines += ["", "## 四年复利（链式）", "", "| 门控 | 四年终值 |", "|---|---:|"]
    for key, v in comp.items():
        lines.append(f"| {key} | **{v:.3f}×** |")
    lines += ["", "## 只在单侧出现的交易（AND 门相对单镜的增量）", "",
              "| 年 | 仅A有(笔/盈亏) | 仅B有(笔/盈亏) |", "|---|---|---|"]
    for y, r in result["years"].items():
        lines.append(
            f"| {y} | {r['仅A有']['n']} / {r['仅A有']['pnl']:,.0f} | {r['仅B有']['n']} / {r['仅B有']['pnl']:,.0f} |"
        )
    (OUT / "e17_and_gate_projection.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
