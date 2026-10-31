#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LB-5 · Box 谱系深度梯度实验 · 独立校验器。

独立代码路径重算 3 笔抽查（浅层 / 中层 / 深层各 1 笔）的谱系深度 + 前驱链完整性，
不复用生成器 e17_lb5_lineagedepth.py 的任何函数。pnl/units 已由生成器 HARD⑧ 全局对账覆盖，
本校验器聚焦「谱系深度」这一本实验的核心新口径。

用法：python scripts/e17_lb5_lineagedepth_verify.py
"""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
DATA = ROOT / "docs" / "experiments" / "e17_lb5_lineagedepth_data.json"


def pick_samples():
    d = json.load(open(DATA, encoding="utf-8"))
    trades = [t for t in d["trades"] if t["recomputable"]]
    shallow = min((t for t in trades if t["lineage_depth"] <= 5),
                  key=lambda t: abs(t["pnl"] - 0))  # 浅层
    mid = max((t for t in trades if 13 <= t["lineage_depth"] <= 20),
              key=lambda t: abs(t["pnl"]))  # 中层（取 pnl 绝对值最大）
    deep = max((t for t in trades if t["lineage_depth"] >= 21),
               key=lambda t: abs(t["pnl"]))  # 深层
    return d, [shallow, mid, deep]


def recompute_depth(t):
    """独立重算：按 (code, box_top, box_bottom) 重新匹配 box，沿 prev_box_id 链回溯算深度。"""
    db = BT / "base" / f"bt_baseA_{t['year']}.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    bx = con.execute(
        """SELECT id, prev_box_id FROM box
           WHERE code=? AND ABS(top-?)<1e-6 AND ABS(bottom-?)<1e-6 AND status='broken_up'
           ORDER BY broken_date DESC""",
        (t["code"], t["box_top"], t["box_bottom"])).fetchall()
    b = bx[0]
    cur = b["id"]
    depth = 1
    seen = {cur}
    while True:
        row = con.execute("SELECT prev_box_id FROM box WHERE id=?", (cur,)).fetchone()
        if row is None:
            break
        nxt = row["prev_box_id"]
        if nxt is None or nxt in seen:
            break
        seen.add(nxt)
        cur = nxt
        depth += 1
    con.close()
    return depth, b["id"]


def main():
    d, samples = pick_samples()
    print("LB-5 谱系深度独立校验（3 笔抽查）")
    print("=" * 60)
    all_ok = True
    for t in samples:
        depth, box_id = recompute_depth(t)
        exp = t["lineage_depth"]
        ok = (depth == exp)
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {t['year']} {t['code']} "
              f"深度 重算={depth} vs 报告={exp}  box_id={box_id}")
    print("=" * 60)
    print(f"结果：{'全部通过' if all_ok else '存在不一致'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
