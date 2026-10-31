#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LB-4 · Box 成长速度梯度实验 · 独立校验器（不复用生成器函数）。

独立代码路径重算 3 笔抽查的成长速度（top_confirmed → bottom_confirmed 交易日计数），
验证与 data.json 里生成器记录的一致性。这是本实验唯一需要独立复核的核心新口径
（pnl 已由 HARD⑧ 全局对账覆盖）。

用法：python scripts/e17_lb4_growthspeed_verify.py
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
DATA = ROOT / "docs" / "experiments" / "e17_lb4_growthspeed_data.json"


def pick_samples(data):
    trades = [t for t in data["trades"] if t["recomputable"]]
    # 快 / 中 / 慢 各 1 笔（取每带 pnl 绝对值最大的一笔，保证非平凡）
    fast = max((t for t in trades if t["growth_speed"] < 6), key=lambda t: abs(t["pnl"]))
    mid = max((t for t in trades if 6 <= t["growth_speed"] < 8), key=lambda t: abs(t["pnl"]))
    slow = max((t for t in trades if t["growth_speed"] >= 11), key=lambda t: abs(t["pnl"]))
    return [fast, mid, slow]


def recompute_growth_speed(t):
    """独立重算：按 (code, box_top, box_bottom) 重新匹配 box，重数 top_confirmed→bottom_confirmed 交易日数。"""
    db = BT / "base" / f"bt_baseA_{t['year']}.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    bx = con.execute(
        """SELECT id, top_confirmed, bottom_confirmed, broken_date
           FROM box WHERE code=? AND ABS(top-?)<1e-6 AND ABS(bottom-?)<1e-6
           AND status='broken_up'""",
        (t["code"], t["box_top"], t["box_bottom"])).fetchall()
    if not bx:
        con.close()
        return None, "box 匹配失败"
    bx = sorted(bx, key=lambda b: b["broken_date"] or "", reverse=True)
    b = bx[0]
    tc, bc = b["top_confirmed"], b["bottom_confirmed"]
    if tc is None or bc is None:
        con.close()
        return None, "缺 top/bottom_confirmed"
    if tc > bc:
        con.close()
        return None, "顺序异常"
    gs = con.execute(
        "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
        (t["code"], tc, bc)).fetchone()[0]
    con.close()
    return gs, None


def main():
    data = json.load(open(DATA, encoding="utf-8"))
    samples = pick_samples(data)

    print(f"独立校验：从 {len(data['trades'])} 笔可复算样本中抽查 {len(samples)} 笔，"
          f"重算成长速度（top_confirmed→bottom_confirmed 交易日计数）。\n")
    n_ok = 0
    for t in samples:
        gs, err = recompute_growth_speed(t)
        rec = t["growth_speed"]
        ok = (err is None) and (gs == rec)
        if ok:
            n_ok += 1
        print(f"  {t['year']} {t['code']}  top={t['box_top']} bottom={t['box_bottom']}  "
              f"pnl={t['pnl']:+,.0f}  重算成长速度={gs}  记录={rec}  "
              f"{'✓ 一致' if ok else ('✗ 不一致: ' + (err or ''))}")
    print(f"\n抽查 {n_ok}/{len(samples)} 通过。")
    return 0 if n_ok == len(samples) else 1


if __name__ == "__main__":
    sys.exit(main())
