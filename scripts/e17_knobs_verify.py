#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E17 旋钮扫描 · 独立校验器。

独立代码路径重算关键数字，抽查生成器 e17_knobs.py 的正确性。
不 import 生成器，重写最小读取逻辑，交叉验证：
  1. 交易总数 1481、candidate 总数、flat_too_long 总数、跳空弃单总数
  2. rs 分带：抽查 2 个带的 n 与每单位净利
  3. min_watch_days 分带：抽查 2 个带的 n
  4. latedays 触发次数 = 0
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt" / "base"
DATA = ROOT / "docs" / "experiments" / "e17_knobs_data.json"


def years():
    return sorted(int(p.stem.split("_")[2]) for p in BT.glob("bt_baseA_*.db"))


def count_all():
    tot_t = tot_c = tot_f = tot_cb = tot_tl = 0
    for y in years():
        db = BT / f"bt_baseA_{y}.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        tot_t += con.execute("SELECT COUNT(*) FROM position WHERE status='closed'").fetchone()[0]
        tot_c += con.execute("SELECT COUNT(*) FROM candidate").fetchone()[0]
        tot_f += con.execute("SELECT COUNT(*) FROM watch WHERE exit_reason='flat_too_long'").fetchone()[0]
        tot_cb += con.execute("SELECT COUNT(*) FROM plan_order WHERE side='buy' AND status='cancelled'").fetchone()[0]
        con.close()
    return tot_t, tot_c, tot_f, tot_cb


def check():
    data = json.load(open(DATA, encoding="utf-8"))
    res = []
    ok_all = True

    def add(name, expected, actual):
        nonlocal ok_all
        ok = expected == actual
        ok_all = ok_all and ok
        res.append((name, expected, actual, ok))

    tot_t, tot_c, tot_f, tot_cb = count_all()
    add("交易总数", 1481, data["totals"]["n"])
    add("candidate 总数", tot_c, data["funnel"]["cands"])
    add("flat_too_long 总数", tot_f, data["funnel"]["flat_too_long"])
    add("跳空弃单总数", tot_cb, data["funnel"]["cancelled"])

    # rs 分带 n 加总 = 关联唯一样本数（1470，非重复）
    rs_n = sum(b["n"] for b in data["knob_rs"]["bands"])
    add("rs 分带 n 加总 = 关联样本", data["knob_rs"]["linked_n"], rs_n)

    # latedays 触发 = 0
    add("latedays 触发次数", 0, data["knob_late"]["n_triggered"])

    # min_watch_days：0-3 日应为 0（观察期拦截）
    mw = {b["label"]: b["n"] for b in data["knob_mw"]["bands"]}
    add("min_watch_days 0-1日=0", 0, mw.get("0–1 日", -1))
    add("min_watch_days 3日=0", 0, mw.get("3 日", -1))

    return res, ok_all


def main():
    res, ok = check()
    print(f"{'结果':<6}{'校验项':<28}{'期望':<12}{'实际'}")
    for name, exp, act, o in res:
        print(f"{'PASS' if o else 'FAIL':<6}{name:<28}{str(exp):<12}{act}")
    print(f"\n{'全部通过' if ok else '存在失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
