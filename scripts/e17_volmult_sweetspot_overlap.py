#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验二 · 重叠度预检（docs/47，2026-11-03）。

问题：组合「量比 ≥1.5 × 甜点 2–4%」是否有 1+1>2 的价值，还是两个条件互相重叠？

方法（零成本，只读 base/bt_baseA_<year>.db，不重算不回测）：
1. 读 14 库 filled 买单，每笔现算：
   volratio = volume(plan_date) / avg(volume, 前 50 交易日不含当日)   ← 复刻 e17_volmult
   ndist    = reason_json.newhigh_distance_pct                        ← 甜点口径
2. 统计交集占比：P(量比≥1.5 | 甜点2-4%) 和 P(甜点2-4% | 量比≥1.5)，双向看。
3. 判定（docs/47 §执行步骤1）：
   >70% 交集 → 组合≈单参数，降级关闭；30–70% → 值得回测；<30% → 正交，慎立项。

输出：docs/experiments/e17_volmult_sweetspot_overlap.md
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt" / "base"
EXP = ROOT / "docs" / "experiments"
OUT_MD = EXP / "e17_volmult_sweetspot_overlap.md"
OUT_JSON = EXP / "e17_volmult_sweetspot_overlap_data.json"

VOL_WINDOW = 50
VOLRATIO_THRESHOLD = 1.5      # 量比 ≥1.5
SWEET_LO, SWEET_HI = 2.0, 4.0  # 甜点 2–4%（含 2，不含 4，与 newhigh 报告一致）


def years_list():
    return sorted(int(p.stem.split("_")[2].split(".")[0])
                  for p in BT.glob("bt_baseA_*.db"))


def load_all():
    """14 库 filled 买单，每笔算 volratio + ndist。"""
    rows = []
    funnel = {"dbs": 0, "filled": 0, "no_bar": 0, "no_vol": 0, "no_ndist": 0}
    for y in years_list():
        db = BT / f"bt_baseA_{y}.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        funnel["dbs"] += 1
        q = """
        SELECT o.code, o.reason_json, t.plan_date, b.volume
        FROM plan_order o
        JOIN trade_plan t ON t.id = o.trade_plan_id
        JOIN daily_bar b ON b.code = o.code AND b.date = t.plan_date
        WHERE o.side='buy' AND o.status='filled'
        """
        for r in con.execute(q):
            funnel["filled"] += 1
            rj = json.loads(r["reason_json"]) if r["reason_json"] else {}
            ndist = rj.get("newhigh_distance_pct")
            if ndist is None:
                funnel["no_ndist"] += 1
                continue
            # 量比：前 50 交易日（不含当日）均量
            vs = con.execute(
                "SELECT volume FROM daily_bar WHERE code=? AND date<? ORDER BY date DESC LIMIT ?",
                (r["code"], r["plan_date"], VOL_WINDOW)).fetchall()
            if not vs:
                funnel["no_bar"] += 1
                continue
            avg = sum(v[0] for v in vs) / len(vs)
            vr = (r["volume"] / avg) if avg > 0 else None
            if vr is None:
                funnel["no_vol"] += 1
                continue
            rows.append({
                "year": y, "code": r["code"], "plan_date": r["plan_date"],
                "volratio": vr, "ndist": ndist,
            })
        con.close()
    return rows, funnel


def pct_of(subset, total):
    return (len(subset) / total * 100) if total else 0.0


def main():
    rows, funnel = load_all()

    vol_hi = [r for r in rows if r["volratio"] >= VOLRATIO_THRESHOLD]
    sweet = [r for r in rows if SWEET_LO <= r["ndist"] < SWEET_HI]
    both = [r for r in rows if r["volratio"] >= VOLRATIO_THRESHOLD
            and SWEET_LO <= r["ndist"] < SWEET_HI]

    n = len(rows)
    n_vol = len(vol_hi)
    n_sweet = len(sweet)
    n_both = len(both)

    # 双向条件概率
    p_vol_given_sweet = len([r for r in sweet if r["volratio"] >= VOLRATIO_THRESHOLD]) / n_sweet * 100 if n_sweet else 0
    p_sweet_given_vol = n_both / n_vol * 100 if n_vol else 0

    # 独立假设下的期望交集（若两条件独立）
    exp_both = n * (n_vol / n) * (n_sweet / n)
    ratio_obs_exp = (n_both / exp_both) if exp_both > 0 else None

    # 判定
    if p_vol_given_sweet > 70 or p_sweet_given_vol > 70:
        verdict = "降级关闭：两条件高度重叠（>70%），组合≈单参数，无新增信息"
    elif p_vol_given_sweet < 30 and p_sweet_given_vol < 30:
        verdict = "慎立项：两条件近乎正交（<30%），组合筛选会砍掉过多样本"
    else:
        verdict = "值得回测：交集 30–70%，组合可能产生新子集"

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "funnel": funnel,
        "n_total": n, "n_vol": n_vol, "n_sweet": n_sweet, "n_both": n_both,
        "p_vol_given_sweet": round(p_vol_given_sweet, 2),
        "p_sweet_given_vol": round(p_sweet_given_vol, 2),
        "expected_both_if_independent": round(exp_both, 2),
        "ratio_obs_exp": round(ratio_obs_exp, 3) if ratio_obs_exp else None,
        "verdict": verdict,
        "volratio_threshold": VOLRATIO_THRESHOLD,
        "sweet_band": [SWEET_LO, SWEET_HI],
    }
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    lines.append("# 实验二 · 重叠度预检：量比≥1.5 × 甜点2–4%\n")
    lines.append(f"> 生成于 {data['generated_at']} · 数据源 base/bt_baseA_<year>.db（新基线 14 年）\n")
    lines.append("## 漏斗\n")
    for k, v in funnel.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 结果\n")
    lines.append(f"- 总 filled 买单（可算两值）: **{n}**")
    lines.append(f"- 量比≥1.5: {n_vol}（占 {n_vol/n*100:.1f}%）")
    lines.append(f"- 甜点2–4%: {n_sweet}（占 {n_sweet/n*100:.1f}%）")
    lines.append(f"- **交集（同时满足）: {n_both}（占 {n_both/n*100:.1f}%）**")
    lines.append("")
    lines.append("## 双向条件概率\n")
    lines.append(f"- P(量比≥1.5 | 甜点2–4%) = **{p_vol_given_sweet:.1f}%**")
    lines.append(f"- P(甜点2–4% | 量比≥1.5) = **{p_sweet_given_vol:.1f}%**")
    lines.append(f"- 若两条件独立，期望交集 = {exp_both:.1f} 笔；实际 {n_both} 笔，"
                 f"比值 = {data['ratio_obs_exp']}")
    lines.append("")
    lines.append(f"## 判定：**{verdict}**")
    lines.append("")
    lines.append("---\n*本报告由 scripts/e17_volmult_sweetspot_overlap.py 自动生成。*")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    # 控制台
    print(f"funnel: {funnel}")
    print(f"总 {n} 笔；量比≥1.5 {n_vol} 笔；甜点 {n_sweet} 笔；交集 {n_both} 笔")
    print(f"P(量比≥1.5|甜点)={p_vol_given_sweet:.1f}%  P(甜点|量比≥1.5)={p_sweet_given_vol:.1f}%")
    print(f"独立期望交集={exp_both:.1f}，实际={n_both}，比值={data['ratio_obs_exp']}")
    print(f"判定: {verdict}")


if __name__ == "__main__":
    main()
