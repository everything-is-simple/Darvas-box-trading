#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LB-1 · Box 寿命梯度实验 · 独立校验器。

独立代码路径重算 3 笔抽查（不复用 e17_lb1_lifespan.py 的任何函数），
验证生成器的「反向匹配 box → 寿命交易日计数」是否可信。
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt" / "base"
DATA = ROOT / "docs" / "experiments" / "e17_lb1_lifespan_data.json"


def main():
    d = json.load(open(DATA, encoding="utf-8"))
    trades = [t for t in d["trades"] if t["recomputable"]]

    # 短 / 中 / 长寿命各取 |pnl| 最大的一笔
    short = max((t for t in trades if t["lifespan"] < 3), key=lambda t: abs(t["pnl"]))
    mid = max((t for t in trades if 3 <= t["lifespan"] < 11), key=lambda t: abs(t["pnl"]))
    long_ = max((t for t in trades if t["lifespan"] >= 31), key=lambda t: abs(t["pnl"]))
    samples = [short, mid, long_]

    print(f"抽查 {len(samples)} 笔（短/中/长寿命各 1，取带内 |pnl| 最大者）：\n")
    ok_all = True
    for t in samples:
        db = BT / f"bt_baseA_{t['year']}.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        # 独立路径 1：重匹配 box（不复用生成器的任何 SQL 封装，直接写查询）
        bx = con.execute(
            "SELECT id, bottom_confirmed, broken_date, top, bottom FROM box "
            "WHERE code=? AND status='broken_up' AND top=? AND bottom=?",
            (t["code"], t["box_top"], t["box_bottom"])).fetchall()
        bx = sorted(bx, key=lambda b: b["broken_date"] or "", reverse=True)
        if not bx:
            print(f"[{t['year']} {t['code']}] 独立重匹配失败")
            ok_all = False
            con.close()
            continue
        b = bx[0]

        # 独立路径 2：重数寿命（交易日计数）
        span = con.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
            (t["code"], b["bottom_confirmed"], b["broken_date"])).fetchone()[0]

        # 独立路径 3：直接从 position 取 realized_pnl（按 id 精确定位）
        pnl = con.execute(
            "SELECT realized_pnl FROM position WHERE code=? AND opened_date=? AND closed_date=?",
            (t["code"], t["opened"], t["closed"])).fetchone()[0]
        con.close()

        # 比对
        checks = [
            ("lifespan", span, t["lifespan"]),
            ("bottom_confirmed", b["bottom_confirmed"], t["bottom_confirmed"]),
            ("broken_date", b["broken_date"], t["broken_date"]),
            ("pnl", round(pnl, 2), t["pnl"]),
        ]
        print(f"[{t['year']} {t['code']}] 生成器寿命={t['lifespan']} 独立重算={span}")
        for name, a, bv in checks:
            ok = a == bv
            if not ok:
                ok_all = False
            print(f"    {name}: 独立={a} vs 生成器={bv}  {'OK' if ok else 'MISMATCH'}")
        print()

    print("结果：", "全部通过" if ok_all else "存在不一致")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
