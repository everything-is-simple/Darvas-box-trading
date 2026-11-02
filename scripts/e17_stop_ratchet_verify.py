#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""raiseStops 事件研究 · 独立校验器（不复用生成器函数）。

独立代码路径重算 3 笔抽查 + 关键结论复核，验证生成器口径正确。

用法：python scripts/e17_stop_ratchet_verify.py
"""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt" / "base"
DATA = ROOT / "docs" / "experiments" / "e17_stop_ratchet_data.json"


def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    trades = data["trades"]

    # 抽查 3 笔：独立从库重算 ratchet = stop_price / stop_after
    print("=== 抽查 3 笔（独立 SQL 重算）===")
    sample = trades[:3]
    ok = True
    for t in sample:
        db = BT / f"bt_baseA_{t['year']}.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        r = con.execute(
            "SELECT p.stop_price, l.stop_after FROM position p "
            "JOIN position_leg l ON l.position_id=p.id AND l.leg_no=1 "
            "WHERE p.code=? AND p.opened_date=? AND p.closed_date=?",
            (t["code"], t["opened"], t["closed"])).fetchone()
        con.close()
        if not r:
            print(f"  ✗ {t['year']} {t['code']}: 库内查不到")
            ok = False
            continue
        ratchet = r[0] / r[1]
        match = abs(ratchet - t["ratchet"]) < 1e-6
        print(f"  {t['year']} {t['code']}: 库内 ratchet={ratchet:.6f} vs "
              f"生成器 {t['ratchet']:.6f} → {'✓' if match else '✗'}")
        if not match:
            ok = False

    # 关键结论复核：受害者计数（未上移且 MFE>30% 且亏损）
    print("\n=== 关键结论复核：受害者计数 ===")
    unmoved = [t for t in trades if t["ratchet"] <= 1.0001]
    victim = [t for t in unmoved if t["mfe"] is not None and t["mfe"] > 0.30
              and t["pnl"] < 0]
    print(f"  未上移 {len(unmoved)} 笔，MFE>30% 且亏 {len(victim)} 笔")
    print(f"  生成器记录 victim_n = {data.get('victim_n')}")
    if len(victim) == data.get("victim_n"):
        print("  ✓ 一致")
    else:
        print("  ✗ 不一致")
        ok = False

    # H1 单调性复核
    print("\n=== H1 单调性复核 ===")
    bands = data["bands"]
    pus = [b["pu"] for b in bands]
    ups = sum(1 for i in range(3) if pus[i] is not None and pus[i+1] is not None
              and pus[i+1] > pus[i])
    print(f"  四带每单位: {pus}")
    print(f"  递增相邻对: {ups}/3（生成器记 {data['h1']['ups']}）")
    if ups == data["h1"]["ups"]:
        print("  ✓ 一致")
    else:
        print("  ✗ 不一致")
        ok = False

    print(f"\n{'全部通过' if ok else '存在不一致'}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
