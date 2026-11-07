#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账本解剖：验证「滚动盈亏传感器」是否灵验（零机时，只读 v1 账本）。

假说 H（用户想法）："做一单亏一单就是熊市" —— 用账户最近 N 笔已平仓单的
滚动盈亏，预测下一笔的成败；连续亏 → 缩小仓位，连续赢 → 放大。

本脚本只做第一步「传感器有没有信号」：
  对每笔单，按其 opened_date 排序，取「closed_date < 该笔 opened_date」的
  最近 N 笔已平仓单（严格事前可见，不偷未来），算累计盈亏，
  再看它与该笔 realized_pnl 的相关性 / 分组差异。

判读：
  - 若「前 N 笔累计盈亏」与「下一笔盈亏」显著同号 → 传感器有信号，可继续设计缩放档位。
  - 若无关甚至反向 → 传感器失灵，换方向，别浪费 18 机时跑全窗。

只读，不写库。
"""
import argparse
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "base" / "bt_baseA_cont_adj.db"


def load_positions(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT opened_date, closed_date, realized_pnl, close_reason "
        "FROM position ORDER BY opened_date, id"
    ).fetchall()
    c.close()
    return rows


def analyze(rows, n):
    """对窗口 N，返回 (样本数, 相关系数, 分组对比)。"""
    pairs = []  # (前N笔累计盈亏, 该笔盈亏)
    by_sign = {"prev_loss": [], "prev_win": []}  # 前N笔累亏 / 累赢 → 下一笔盈亏列表

    for i, (opened, closed, pnl, reason) in enumerate(rows):
        if pnl is None:
            continue
        # 取 closed_date < opened 的最近 N 笔（严格事前）
        prev = [
            r[2] for r in rows[:i]
            if r[1] and r[1] < opened and r[2] is not None
        ][-n:]
        if len(prev) < n:
            continue  # 前面平仓单不足 N 笔，跳过（避免冷启动噪声）
        cum = sum(prev)
        pairs.append((cum, pnl))
        key = "prev_loss" if cum < 0 else "prev_win"
        by_sign[key].append(pnl)

    if len(pairs) < 30:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    r = pearson(xs, ys)
    out = {
        "n": n,
        "样本": len(pairs),
        "相关系数": r,
    }
    for k in ("prev_loss", "prev_win"):
        lst = by_sign[k]
        if len(lst) >= 10:
            wins = sum(1 for x in lst if x > 0)
            out[k] = {
                "笔数": len(lst),
                "胜率%": round(wins / len(lst) * 100, 1),
                "均笔盈亏": round(statistics.mean(lst), 0),
                "合计": round(sum(lst), 0),
            }
    return out


def pearson(xs, ys):
    if len(xs) < 2:
        return float("nan")
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--ns", default="3,5,10,20", help="窗口 N 列表，逗号分隔")
    a = ap.parse_args()

    rows = load_positions(a.db)
    print(f"账本总数 {len(rows)} 笔，opened {rows[0][0]} ~ {rows[-1][0]}\n")

    for n in [int(x) for x in a.ns.split(",")]:
        r = analyze(rows, n)
        if r is None:
            print(f"[N={n}] 样本不足，跳过")
            continue
        print(f"=== 窗口 N={n} ===")
        print(f"  样本 {r['样本']} 笔，Pearson 相关系数 = {r['相关系数']:.3f}")
        for k, label in (("prev_loss", "前 N 笔累计亏损"), ("prev_win", "前 N 笔累计盈利")):
            if k in r:
                v = r[k]
                print(f"  {label:14s} → 下一笔：{v['笔数']} 笔 | 胜率 {v['胜率%']}% | 均笔 {v['均笔盈亏']:+,.0f} | 合计 {v['合计']:+,.0f}")
        print()


if __name__ == "__main__":
    main()
