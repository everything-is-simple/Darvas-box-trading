#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 打底 + C 确认：用绝对水平 × 变化率检验「盛极必衰」。

B = 站上 MA20 占比（0~100），有界刻度 → 「盛」可以直接用水平定义，无需均线。
C = 跌破箱体下轨占比（%），衡量「破没破」。

检验：
  1. B 水平档 × B 五日变化档 = 20 格，看后 20 日收益 / 跌率 / 最大回撤
  2. 「盛极转衰」样本里，C 破位率 ≥15% 与 <15% 两组对比（C 确认的增量）
  3. B≥90 之后 60 日内的最大回撤分布

只读 var/bt/series_abc.csv，不写库。
"""
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "var" / "bt" / "series_abc.csv"
DB = ROOT / "var" / "bt" / "full_hist_adj.db"

LEVELS = [(90, 101, "≥90 盛极"), (75, 90, "75-90 盛"), (55, 75, "55-75 中上"),
          (35, 55, "35-55 中下"), (0, 35, "<35 弱")]
CHGS = [(5, 999, ">+5pp 快升"), (1, 5, "+1~5 升"), (-1, 1, "±1 平"),
        (-5, -1, "-1~5 降"), (-999, -5, "<-5pp 快降")]


def sh_index():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    d = dict(c.execute(
        "SELECT date, close FROM daily_bar WHERE code='sh000001' "
        "AND date BETWEEN '2004-01-01' AND '2019-06-30'").fetchall())
    c.close()
    return d


def fwd(px, dates, i, n):
    j = min(i + n, len(dates) - 1)
    if j <= i:
        return None
    a, b = px.get(dates[i]), px.get(dates[j])
    return (b - a) / a if a and b else None


def fwd_mdd(px, dates, i, n):
    a = px.get(dates[i])
    if not a:
        return None
    j = min(i + n, len(dates) - 1)
    lo = None
    for k in range(i + 1, j + 1):
        p = px.get(dates[k])
        if p is not None and (lo is None or p < lo):
            lo = p
    return (lo - a) / a if lo else None


def lvl(v):
    for a, b, name in LEVELS:
        if a <= v < b:
            return name
    return LEVELS[-1][2]


def chgf(v):
    for a, b, name in CHGS:
        if a <= v < b:
            return name
    return CHGS[-1][2]


def stat(px, dates, idx, n=20):
    r = [x for x in (fwd(px, dates, i, n) for i in idx) if x is not None]
    m = [x for x in (fwd_mdd(px, dates, i, n) for i in idx) if x is not None]
    if not r:
        return None
    return (len(idx), sum(r) / len(r) * 100,
            sum(1 for x in r if x < 0) / len(r) * 100,
            sum(1 for x in r if x <= -0.05) / len(r) * 100,
            sorted(m)[len(m) // 2] * 100)


def main():
    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    dates = [r["date"] for r in rows]
    b = [float(r["b_pct"]) for r in rows]
    c20 = [float(r["c20_pct"]) for r in rows]
    c60 = [float(r["c60_pct"]) for r in rows]
    px = sh_index()
    n = len(rows)

    print("=" * 100)
    print("一、B 水平 × B 五日变化（20 格）：盛极必衰在这里找")
    print("=" * 100)
    cell = defaultdict(list)
    for i in range(5, n):
        cell[(lvl(b[i]), chgf(b[i] - b[i - 5]))].append(i)

    print(f"{'水平':<12}{'变化':<14}{'天数':>6}{'占比':>7}{'后20均':>9}"
          f"{'跌率':>7}{'深跌5%':>8}{'MDD中位':>9}")
    for _, _, ln in LEVELS:
        for _, _, cn in CHGS:
            idx = cell.get((ln, cn), [])
            s = stat(px, dates, idx)
            if not s:
                continue
            cnt, a20, dn, dp, md = s
            mark = ""
            if ln.startswith("≥90") and cn == "<-5pp 快降":
                mark = "  ← 盛极转衰"
            print(f"{ln:<12}{cn:<14}{cnt:>6}{cnt/n*100:>6.1f}%{a20:>8.2f}%"
                  f"{dn:>6.1f}%{dp:>7.1f}%{md:>8.2f}%{mark}")

    print("\n" + "=" * 100)
    print("二、C 确认的增量：在「B 从盛极回落」的样本里，跌破箱体下轨是否加强")
    print("=" * 100)
    # 盛极回落 = B 前 10 日内曾 ≥90，且当前 5 日变化 < 0
    cand = [i for i in range(15, n)
            if max(b[i - 10:i + 1]) >= 90 and b[i] - b[i - 5] < 0]
    print(f"「B 曾≥90 且当前在回落」样本 {len(cand)} 天 "
          f"（占 {len(cand)/n*100:.1f}%）\n")
    print(f"{'C 条件':<28}{'天数':>6}{'后20均':>9}{'跌率':>7}{'深跌5%':>8}{'MDD中位':>9}")
    groups = [
        ("c20 ≥ 15%（破位确认）", lambda i: c20[i] >= 15),
        ("10 ≤ c20 < 15", lambda i: 10 <= c20[i] < 15),
        ("5 ≤ c20 < 10", lambda i: 5 <= c20[i] < 10),
        ("c20 < 5（仅广度回落）", lambda i: c20[i] < 5),
    ]
    for name, f in groups:
        idx = [i for i in cand if f(i)]
        s = stat(px, dates, idx)
        if not s:
            print(f"{name:<28}{0:>6}")
            continue
        cnt, a20, dn, dp, md = s
        print(f"{name:<28}{cnt:>6}{a20:>8.2f}%{dn:>6.1f}%{dp:>7.1f}%{md:>8.2f}%")

    print("\n" + "=" * 100)
    print("三、C 单独用：破位率突增是否可当扳机")
    print("=" * 100)
    print(f"{'条件':<30}{'天数':>6}{'后5均':>8}{'后20均':>9}{'跌率':>7}{'深跌5%':>8}")
    cg = [
        ("c20 ≥ 30%（大面积破位）", lambda i: c20[i] >= 30),
        ("c20 从<5 跳到 ≥15", lambda i: c20[i] >= 15 and min(c20[max(0,i-5):i] or [0]) < 5),
        ("c60 ≥ 30%（长箱破位）", lambda i: c60[i] >= 30),
        ("c20 ≥ 15% 且 B<30", lambda i: c20[i] >= 15 and b[i] < 30),
    ]
    for name, f in cg:
        idx = [i for i in range(1, n) if f(i)]
        s5 = stat(px, dates, idx, 5)
        s = stat(px, dates, idx, 20)
        if not s:
            continue
        print(f"{name:<30}{s[0]:>6}{s5[1]:>7.2f}%{s[1]:>8.2f}%{s[2]:>6.1f}%{s[3]:>7.1f}%")

    print("\n" + "=" * 100)
    print("四、B 绝对水平单变量：盛极之后必衰吗")
    print("=" * 100)
    print(f"{'水平':<14}{'天数':>6}{'后20均':>9}{'跌率':>7}{'后60均':>9}{'60日MDD中位':>12}")
    for _, _, ln in LEVELS:
        idx = [i for i in range(n) if lvl(b[i]) == ln]
        s = stat(px, dates, idx, 20)
        s60 = stat(px, dates, idx, 60)
        if not s:
            continue
        print(f"{ln:<14}{s[0]:>6}{s[1]:>8.2f}%{s[2]:>6.1f}%{s60[1]:>8.2f}%{s60[4]:>11.2f}%")


if __name__ == "__main__":
    main()
