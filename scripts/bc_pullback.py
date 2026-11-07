#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 打底 + C 确认（修正口径）：
B = 站上 MA20 占比。顶部不是「当前还高」，而是「从高位掉下来了多少」。
   → 用 60 日峰值回落幅度 BD = max(B5, 60日) - B5 定义「盛极转衰」
C = 跌破箱体下轨占比，做结构确认。

只读 var/bt/series_abc.csv。
"""
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "var" / "bt" / "series_abc.csv"
DB = ROOT / "var" / "bt" / "full_hist_adj.db"
TOPS = [("2007-10-16", "2008 熊"), ("2015-06-12", "2015 股灾"), ("2018-01-24", "2018 阴跌")]


def sh_index():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    d = dict(c.execute(
        "SELECT date, close FROM daily_bar WHERE code='sh000001' "
        "AND date BETWEEN '2004-01-01' AND '2019-12-31'").fetchall())
    c.close()
    return d


def ma(v, n):
    out = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def fwd(px, dates, i, n):
    j = min(i + n, len(dates) - 1)
    if j <= i:
        return None
    a, b = px.get(dates[i]), px.get(dates[j])
    return (b - a) / a if a and b else None


def mdd(px, dates, i, n):
    a = px.get(dates[i])
    if not a:
        return None
    j = min(i + n, len(dates) - 1)
    lo = min((px.get(dates[k]) for k in range(i + 1, j + 1)
              if px.get(dates[k]) is not None), default=None)
    return (lo - a) / a if lo else None


def main():
    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    dates = [r["date"] for r in rows]
    b = [float(r["b_pct"]) for r in rows]
    c20 = [float(r["c20_pct"]) for r in rows]
    c60 = [float(r["c60_pct"]) for r in rows]
    px = sh_index()
    n = len(rows)
    b5 = ma(b, 5)
    b10 = ma(b, 10)

    # 60 日峰值回落
    bd = [None] * n
    for i in range(60, n):
        w = [x for x in b5[i - 60:i + 1] if x is not None]
        bd[i] = max(w) - b5[i]

    def show(name, idx, horizons=(20, 60)):
        if not idx:
            print(f"{name:<34}{0:>6}")
            return
        line = f"{name:<34}{len(idx):>6}"
        for h in horizons:
            r = [x for x in (fwd(px, dates, i, h) for i in idx) if x is not None]
            a = sum(r) / len(r) * 100
            dn = sum(1 for x in r if x < 0) / len(r) * 100
            line += f"{a:>9.2f}%{dn:>7.1f}%"
        m = [x for x in (mdd(px, dates, i, 60) for i in idx) if x is not None]
        line += f"{sorted(m)[len(m)//2]*100:>9.2f}%"
        print(line)

    print("=" * 96)
    print("一、B 峰值回落（60日）：从盛极掉下来多少")
    print("=" * 96)
    print(f"{'条件':<34}{'天数':>6}{'后20均':>9}{'跌率':>7}{'后60均':>9}{'跌率':>7}{'60MDD中':>9}")
    for th in (15, 20, 25, 30, 40, 50):
        idx = [i for i in range(n) if bd[i] is not None and bd[i] >= th and b5[i] < 75]
        show(f"回落 ≥{th}pp 且 B<75", idx)

    print("\n二、B 回落 + C 破位确认")
    print(f"{'条件':<34}{'天数':>6}{'后20均':>9}{'跌率':>7}{'后60均':>9}{'跌率':>7}{'60MDD中':>9}")
    for th in (20, 30):
        for cth in (5, 10, 15, 25):
            idx = [i for i in range(n)
                   if bd[i] is not None and bd[i] >= th and b5[i] < 75 and c20[i] >= cth]
            show(f"回落≥{th}pp + c20≥{cth}%", idx)

    print("\n三、反向：B 回落但 C 没破位（对照）")
    print(f"{'条件':<34}{'天数':>6}{'后20均':>9}{'跌率':>7}{'后60均':>9}{'跌率':>7}{'60MDD中':>9}")
    for th in (20, 30):
        idx = [i for i in range(n)
               if bd[i] is not None and bd[i] >= th and b5[i] < 75 and c20[i] < 3]
        show(f"回落≥{th}pp + c20<3%", idx)

    print("\n" + "=" * 96)
    print("四、三顶覆盖：各信号首次触发距顶几个交易日（搜索窗 -250 ~ +60）")
    print("=" * 96)
    sigs = {
        "B 回落 ≥20pp": lambda i: bd[i] is not None and bd[i] >= 20 and b5[i] < 75,
        "B 回落 ≥30pp": lambda i: bd[i] is not None and bd[i] >= 30 and b5[i] < 75,
        "B < 50 且回落≥15": lambda i: bd[i] is not None and bd[i] >= 15 and b5[i] < 50,
        "c20 ≥ 15%": lambda i: c20[i] >= 15,
        "c20 ≥ 25%": lambda i: c20[i] >= 25,
        "B回落≥20 + c20≥10": lambda i: (bd[i] is not None and bd[i] >= 20
                                        and b5[i] < 75 and c20[i] >= 10),
        "B的MA10加速度<0": lambda i: (i >= 15 and b10[i] is not None
                                      and b10[i - 5] is not None and b10[i - 10] is not None
                                      and i - 15 >= 0
                                      and (b10[i] - b10[i - 5]) - (b10[i - 5] - b10[i - 10]) < 0),
    }
    print(f"{'信号':<24}" + "".join(f"{nm:>16}" for _, nm in TOPS))
    for sname, f in sigs.items():
        cells = []
        for top, _ in TOPS:
            ti = next((i for i, d in enumerate(dates) if d >= top), None)
            hits = [i for i in range(max(0, ti - 250), min(n, ti + 60)) if f(i)]
            if not hits:
                cells.append("无")
            else:
                cells.append(f"{hits[0]-ti:+d}日")
        print(f"{sname:<24}" + "".join(f"{c:>16}" for c in cells))

    print("\n五、三顶当日的 B / 60日峰值 / 回落 / c20")
    for top, nm in TOPS:
        i = next((i for i, d in enumerate(dates) if d >= top), None)
        if i is None or b5[i] is None:
            continue
        peak = max(x for x in b5[max(0, i - 60):i + 1] if x is not None)
        print(f"  {top} {nm:<10} B={b5[i]:>5.1f}  60日峰={peak:>5.1f}  "
              f"回落={peak-b5[i]:>5.1f}pp  c20={c20[i]:>5.1f}%  c60={c60[i]:>5.1f}%")


if __name__ == "__main__":
    main()
