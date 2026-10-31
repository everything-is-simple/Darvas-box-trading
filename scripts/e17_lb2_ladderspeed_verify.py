#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LB-2 阶梯速度 · 独立校验器。

独立代码路径重算 3 笔样本的阶梯速度（ladder_gap）+ 前驱链，不复用生成器函数。
校验项：每笔 (ladder_gap, prev_broken_date, broken_date) 三项，与 data.json 逐项比对。
pnl 已由生成器 HARD⑧（Σrealized_pnl=+1,461,228.2）全局对账覆盖，本校验器不再逐笔重验。

用法：python scripts/e17_lb2_ladderspeed_verify.py
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
DATA = ROOT / "docs" / "experiments" / "e17_lb2_ladderspeed_data.json"


def pick_samples(d):
    trades = d["trades"]
    rc = [t for t in trades if t["recomputable"]]
    fast = max((t for t in rc if t["ladder_gap"] < 15), key=lambda t: abs(t["pnl"]))
    mid = max((t for t in rc if 15 <= t["ladder_gap"] < 30), key=lambda t: abs(t["pnl"]))
    slow = max((t for t in rc if t["ladder_gap"] >= 60), key=lambda t: abs(t["pnl"]))
    return [fast, mid, slow]


def recompute(t):
    """独立重算：按 (year, code, box_top, box_bottom) 重新匹配 box → prev_box_id → 前驱 broken_date → 阶梯速度。"""
    db = BT / "base" / f"bt_baseA_{t['year']}.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    bx = con.execute(
        "SELECT id, prev_box_id, broken_date FROM box "
        "WHERE code=? AND ABS(top-?)<1e-6 AND ABS(bottom-?)<1e-6 AND status='broken_up' "
        "ORDER BY broken_date DESC",
        (t["code"], t["box_top"], t["box_bottom"])).fetchall()
    b = bx[0]
    prev = con.execute("SELECT broken_date FROM box WHERE id=?",
                       (b["prev_box_id"],)).fetchone()
    gap = con.execute(
        "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
        (t["code"], prev["broken_date"], b["broken_date"])).fetchone()[0]
    con.close()
    return {"ladder_gap": gap, "prev_broken_date": prev["broken_date"],
            "broken_date": b["broken_date"]}


def main():
    d = json.load(open(DATA, encoding="utf-8"))
    samples = pick_samples(d)
    print("独立校验 3 笔（快梯/中梯/慢梯），每笔 3 项（ladder_gap / prev_broken_date / broken_date）")
    print()
    fail = 0
    checks = 0
    for t in samples:
        r = recompute(t)
        ok_gap = r["ladder_gap"] == t["ladder_gap"]
        ok_prev = r["prev_broken_date"] == t["prev_broken_date"]
        ok_broken = r["broken_date"] == t["broken_date"]
        print(f"[{t['year']} {t['code']}] gap 生成器={t['ladder_gap']} 重算={r['ladder_gap']}  "
              f"prev={t['prev_broken_date']} broken={t['broken_date']} pnl={t['pnl']:+.0f}")
        for name, ok in [("ladder_gap", ok_gap), ("prev_broken_date", ok_prev),
                         ("broken_date", ok_broken)]:
            checks += 1
            if not ok:
                fail += 1
                print(f"  ✗ {name} 不一致：生成器 {t[name] if name!='ladder_gap' else t['ladder_gap']} vs 重算 {r[name] if name!='ladder_gap' else r['ladder_gap']}")
        print()
    print(f"结果：{checks - fail}/{checks} 项一致，{fail} 项不一致")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
