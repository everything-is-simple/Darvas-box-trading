#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三条底层序列评估：B 打底 / C 确认 / A 对照。

对每条序列（统一转成「越大越健康」）套四组双均线，算方向/速度/加速度，输出：
  1. 顶部风险分档（方向分+速度分 0~6）的后 20 日表现
  2. 年度信号分布（判死判活）
  3. 信号集中度：熊市窗口内占比 vs 随机基准
  4. 三次顶部的领先/滞后交易日

只读 var/bt/series_abc.csv，不写库。
"""
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "var" / "bt" / "series_abc.csv"
DB = ROOT / "var" / "bt" / "full_hist_adj.db"

GROUPS = [("g1", 5, 20), ("g2", 10, 30), ("g3", 30, 50), ("g4", 50, 200)]
DIR = {(1, 1): "同↑", (-1, -1): "同↓", (-1, 1): "顶背离", (1, -1): "底背离"}
VEL = {(1, 1): "同升", (-1, -1): "同降", (-1, 1): "快降慢升", (1, -1): "快升慢降"}
RSK_D = {"同↑": 0, "顶背离": 2, "同↓": 3, "底背离": 0}
RSK_V = {"同升": 0, "快降慢升": 2, "同降": 3, "快升慢降": 0}

TOPS = [("2007-10-16", "2008-10-28"), ("2015-06-12", "2015-08-26"),
        ("2018-01-24", "2018-12-28")]


def sg(v):
    if v is None:
        return None
    return 1 if v > 0 else (-1 if v < 0 else None)


def ma(seq, n):
    out = [None] * len(seq)
    run = 0.0
    for i, v in enumerate(seq):
        run += v
        if i >= n:
            run -= seq[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def diff(seq, k):
    out = [None] * len(seq)
    for i in range(k, len(seq)):
        if seq[i] is not None and seq[i - k] is not None:
            out[i] = seq[i] - seq[i - k]
    return out


def sh_index():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    d = dict(c.execute(
        "SELECT date, close FROM daily_bar WHERE code='sh000001' "
        "AND date BETWEEN '2004-01-01' AND '2018-12-31'").fetchall())
    c.close()
    return d


def fwd(px, dates, i, n):
    j = min(i + n, len(dates) - 1)
    if j <= i:
        return None
    a, b = px.get(dates[i]), px.get(dates[j])
    return (b - a) / a if a and b else None


def load_series():
    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    dates = [r["date"] for r in rows]
    out = {}
    out["B 站上MA20占比"] = [float(r["b_pct"]) for r in rows]
    out["C 箱体下轨健康度(100-破位%)"] = [100 - float(r["c20_pct"]) for r in rows]
    out["C60 长箱健康度"] = [100 - float(r["c60_pct"]) for r in rows]
    a, run = [], 100.0
    for r in rows:
        run *= (1 + float(r["a_ret"]))
        a.append(run)
    out["A 等权指数"] = a
    return dates, out


def eval_series(name, vals, dates, px):
    print("\n" + "#" * 96)
    print(f"### {name}")
    print("#" * 96)
    n = len(vals)
    lo = sorted(v for v in vals if v is not None)
    print(f"范围 {lo[0]:.2f} ~ {lo[-1]:.2f}   中位 {lo[len(lo)//2]:.2f}   {n} 天")

    # 熊市窗口（顶前 20 到顶后 120）
    win = set()
    for top, bot in TOPS:
        ti = next((i for i, d in enumerate(dates) if d >= top), None)
        if ti is None:
            continue
        for k in range(max(0, ti - 20), min(n, ti + 121)):
            win.add(k)
    base_rate = len(win) / n * 100

    for gid, na, nb in GROUPS:
        maa, mab = ma(vals, na), ma(vals, nb)
        da, db = sg_seq(diff(maa, 1)), sg_seq(diff(mab, 1))
        va, vb = diff(maa, 5), diff(mab, 5)
        aa, ab = diff(va, 5), diff(vb, 5)
        states = []
        for i in range(n):
            if None in (da[i], db[i], va[i], vb[i], aa[i], ab[i]):
                continue
            d = DIR[(da[i], db[i])]
            v = VEL[(sg(va[i]), sg(vb[i]))]
            a3 = (sg(aa[i]), sg(ab[i]))
            states.append((i, d, v, a3))
        if not states:
            continue
        st = {i: (d, v, a3) for i, d, v, a3 in states}

        # 分档
        buckets = defaultdict(list)
        for i, d, v, a3 in states:
            buckets[RSK_D[d] + RSK_V[v]].append(i)
        print(f"\n[{gid}] MA{na}/{nb}")
        print(f"  档  天数   占比   后20均   跌率  信号在熊市窗口占比")
        for s in range(4, 7):
            idx = buckets.get(s, [])
            if not idx:
                continue
            r20 = [x for x in (fwd(px, dates, i, 20) for i in idx) if x is not None]
            a20 = sum(r20) / len(r20) * 100 if r20 else float("nan")
            dn = sum(1 for x in r20 if x < 0) / len(r20) * 100 if r20 else float("nan")
            conc = sum(1 for i in idx if i in win) / len(idx) * 100
            print(f"  {s}  {len(idx):>5} {len(idx)/len(states)*100:>6.1f}% "
                  f"{a20:>7.2f}% {dn:>6.1f}%   {conc:>5.1f}%  "
                  f"{'★集中' if conc > base_rate * 1.8 else ''}")

        # 三维 C 格
        C = [i for i, d, v, a3 in states
             if d == "顶背离" and v == "快降慢升" and a3 == (-1, 1)]
        if C:
            r20 = [x for x in (fwd(px, dates, i, 20) for i in C) if x is not None]
            a20 = sum(r20) / len(r20) * 100
            dn = sum(1 for x in r20 if x < 0) / len(r20) * 100
            conc = sum(1 for i in C if i in win) / len(C) * 100
            print(f"  C格 {len(C):>5} {len(C)/len(states)*100:>6.1f}% "
                  f"{a20:>7.2f}% {dn:>6.1f}%   {conc:>5.1f}%  "
                  f"{'★集中' if conc > base_rate * 1.8 else ''}")

        # 年度分布（分≥4）
        yr = Counter(dates[i][:4] for i, (d, v, _a) in st.items()
                     if RSK_D[d] + RSK_V[v] >= 4)
        if yr:
            ys = sorted(yr)
            print("  年度分≥4： " + " ".join(f"{y[2:]}:{yr[y]}" for y in ys))

        # 三顶领先/滞后
        lead = []
        for top, bot in TOPS:
            ti = next((i for i, d in enumerate(dates) if d >= top), None)
            if ti is None:
                continue
            hits = [i for i in range(max(0, ti - 60), min(n, ti + 60))
                    if i in st and RSK_D[st[i][0]] + RSK_V[st[i][1]] >= 4]
            if hits:
                lead.append(f"{top[:7]}:{hits[0]-ti:+d}日")
            else:
                lead.append(f"{top[:7]}:无")
        print("  三顶领先： " + "  ".join(lead))
    print(f"\n  [基准] 熊市窗口占全窗 {base_rate:.1f}%")


def sg_seq(seq):
    out = [None] * len(seq)
    for i, v in enumerate(seq):
        if v is not None:
            out[i] = 1 if v > 0 else (-1 if v < 0 else None)
    return out


def main():
    dates, series = load_series()
    px = sh_index()
    for name, vals in series.items():
        eval_series(name, vals, dates, px)


if __name__ == "__main__":
    main()
