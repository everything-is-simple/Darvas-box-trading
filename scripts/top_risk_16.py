#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""顶部风险：方向 × 速度 16 格 + 盛衰转换（只看顶，不看底）。

用户规格（2026-11-07 02:00）：
  - 只识别顶部风险，不做底部择时（底是涨出来的，不买不亏钱）。
  - 看加速度先行（曲率先弯），方向+速度两维做确认。
  - 四组均线：MA5/20、MA10/30、MA30/50、MA50/200。

两维各 4 态（快线 f / 慢线 s）：
  dir : (+,+) 同↑ | (-,-) 同↓ | (-,+) 顶背离 | (+,-) 底背离
  vel : (+,+) 同升 | (-,-) 同降 | (-,+) 快降慢升 | (+,-) 快升慢降
→ 16 格。顶部风险分 = 方向分 + 速度分（0~6）。

只读 CSV + full_hist_adj.db(sh000001)，不写库。
"""
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "full_hist_adj.db"
GROUPS = [("g1", 5, 20), ("g2", 10, 30), ("g3", 30, 50), ("g4", 50, 200)]

DIR = {(1, 1): "同↑", (-1, -1): "同↓", (-1, 1): "顶背离", (1, -1): "底背离"}
VEL = {(1, 1): "同升", (-1, -1): "同降", (-1, 1): "快降慢升", (1, -1): "快升慢降"}

# 顶部风险分：方向 / 速度各自的贡献
RSK_D = {"同↑": 0, "顶背离": 2, "同↓": 3, "底背离": 0}
RSK_V = {"同升": 0, "快降慢升": 2, "同降": 3, "快升慢降": 0}

BEARS = [
    ("2007-10-16", "2008-11-04", "2008 熊"),
    ("2015-06-12", "2015-08-26", "2015 股灾"),
    ("2018-01-24", "2018-12-28", "2018 阴跌"),
]


def sg(x):
    if x is None or x == "":
        return None
    v = float(x)
    return 1 if v > 0 else (-1 if v < 0 else None)


def load(path):
    return list(csv.DictReader(Path(path).open(encoding="utf-8")))


def sh_index():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    d = dict(c.execute(
        "SELECT date, close FROM daily_bar WHERE code='sh000001' "
        "AND date BETWEEN '2005-01-01' AND '2018-12-31'").fetchall())
    c.close()
    return d


def fwd(px, dates, i, n):
    j = min(i + n, len(dates) - 1)
    if j <= i:
        return None
    a, b = px.get(dates[i]), px.get(dates[j])
    return (b - a) / a if a and b else None


def fwd_mdd(px, dates, i, n):
    """后 n 个交易日内，相对当日收盘的最大回撤（负数）。"""
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


def build(rows, gid, na, nb, dates):
    """返回 [(idx, dir_lab, vel_lab, acc_sign_f, acc_sign_s)]"""
    out = []
    for i, r in enumerate(rows):
        df = sg(r[f"{gid}_dir{na}"]); ds = sg(r[f"{gid}_dir{nb}"])
        vf = sg(r[f"{gid}_vel{na}"]); vs = sg(r[f"{gid}_vel{nb}"])
        af = sg(r[f"{gid}_acc{na}"]); as_ = sg(r[f"{gid}_acc{nb}"])
        if None in (df, ds, vf, vs):
            continue
        out.append((i, DIR[(df, ds)], VEL[(vf, vs)], af, as_))
    return out


def report16(rows, px, dates, title):
    print("\n" + "=" * 92)
    print(f"【{title}】 方向 × 速度 16 格")
    print("=" * 92)
    for gid, na, nb in GROUPS:
        st = build(rows, gid, na, nb, dates)
        n = len(st)
        cnt = Counter((d, v) for _, d, v, _, _ in st)
        print(f"\n[{gid}] MA{na}/MA{nb}   有效 {n} 天")
        print(f"  {'方向':<8}{'速度':<10}{'分':>3}{'天数':>7}{'占比':>7}"
              f"{'后20均':>9}{'跌率':>7}{'深跌':>7}{'20日MDD中位':>12}")
        order_d = ["同↑", "顶背离", "同↓", "底背离"]
        order_v = ["同升", "快降慢升", "同降", "快升慢降"]
        for d in order_d:
            for v in order_v:
                k = cnt.get((d, v), 0)
                if k == 0:
                    continue
                idx = [i for i, dd, vv, _, _ in st if (dd, vv) == (d, v)]
                r20 = [x for x in (fwd(px, dates, i, 20) for i in idx) if x is not None]
                md = [x for x in (fwd_mdd(px, dates, i, 20) for i in idx) if x is not None]
                a20 = sum(r20) / len(r20) * 100 if r20 else float("nan")
                dn = sum(1 for x in r20 if x < 0) / len(r20) * 100 if r20 else float("nan")
                dp = sum(1 for x in r20 if x <= -0.05) / len(r20) * 100 if r20 else float("nan")
                mm = sorted(md)[len(md) // 2] * 100 if md else float("nan")
                sc = RSK_D[d] + RSK_V[v]
                print(f"  {d:<8}{v:<10}{sc:>3}{k:>7}{k/n*100:>6.1f}%"
                      f"{a20:>8.2f}%{dn:>6.1f}%{dp:>6.1f}%{mm:>11.2f}%")


def report_score(rows, px, dates, title):
    """风险分档 0~6：只看顶。"""
    print("\n" + "=" * 92)
    print(f"【{title}】 顶部风险分档（方向分 + 速度分 = 0~6）")
    print("=" * 92)
    for gid, na, nb in GROUPS:
        st = build(rows, gid, na, nb, dates)
        buckets = defaultdict(list)
        for i, d, v, _, _ in st:
            buckets[RSK_D[d] + RSK_V[v]].append(i)
        print(f"\n[{gid}] MA{na}/MA{nb}")
        print(f"  {'分':>3}{'含义':<12}{'天数':>7}{'占比':>7}{'后20均':>9}"
              f"{'跌率':>7}{'深跌≥5%':>9}{'20日MDD中位':>12}")
        LAB = {0: "全盛", 1: "盛-偏", 2: "初衰", 3: "转弱",
               4: "警戒", 5: "危险", 6: "全衰"}
        for s in range(7):
            idx = buckets.get(s, [])
            if not idx:
                continue
            r20 = [x for x in (fwd(px, dates, i, 20) for i in idx) if x is not None]
            md = [x for x in (fwd_mdd(px, dates, i, 20) for i in idx) if x is not None]
            a20 = sum(r20) / len(r20) * 100
            dn = sum(1 for x in r20 if x < 0) / len(r20) * 100
            dp = sum(1 for x in r20 if x <= -0.05) / len(r20) * 100
            mm = sorted(md)[len(md) // 2] * 100
            print(f"  {s:>3}{LAB[s]:<12}{len(idx):>7}{len(idx)/len(st)*100:>6.1f}%"
                  f"{a20:>8.2f}%{dn:>6.1f}%{dp:>8.1f}%{mm:>11.2f}%")


def report_switch(rows, px, dates, title):
    """盛衰转换：只看「速度首次转弱」的那一天。"""
    print("\n" + "=" * 92)
    print(f"【{title}】 盛衰转换点（首次进入该格，非存量）")
    print("=" * 92)
    SEV = [("① 同↑同升 → 同↑快降慢升", ("同↑", "同升"), ("同↑", "快降慢升")),
           ("② 同↑快降慢升 → 顶背离快降慢升", ("同↑", "快降慢升"), ("顶背离", "快降慢升")),
           ("③ 顶背离 → 同↓", ("顶背离", "快降慢升"), ("同↓", "同降"))]
    for gid, na, nb in GROUPS:
        st = build(rows, gid, na, nb, dates)
        seq = {i: (d, v) for i, d, v, _, _ in st}
        print(f"\n[{gid}] MA{na}/MA{nb}")
        print(f"  {'转换':<32}{'次数':>6}{'后5均':>8}{'后20均':>9}{'跌率':>7}"
              f"{'深跌':>7}{'20日MDD中位':>12}")
        for name, frm, to in SEV:
            hits = [i for i, dv in seq.items() if dv == to and seq.get(i - 1) == frm]
            if not hits:
                print(f"  {name:<32}{0:>6}")
                continue
            r5 = [x for x in (fwd(px, dates, i, 5) for i in hits) if x is not None]
            r20 = [x for x in (fwd(px, dates, i, 20) for i in hits) if x is not None]
            md = [x for x in (fwd_mdd(px, dates, i, 20) for i in hits) if x is not None]
            a5 = sum(r5) / len(r5) * 100 if r5 else float("nan")
            a20 = sum(r20) / len(r20) * 100 if r20 else float("nan")
            dn = sum(1 for x in r20 if x < 0) / len(r20) * 100
            dp = sum(1 for x in r20 if x <= -0.05) / len(r20) * 100
            mm = sorted(md)[len(md) // 2] * 100
            print(f"  {name:<32}{len(hits):>6}{a5:>7.2f}%{a20:>8.2f}%{dn:>6.1f}%"
                  f"{dp:>6.1f}%{mm:>11.2f}%")


def report_bears(rows, px, dates, title):
    """三次熊市：风险分≥4 的首次触发日，距顶部多少天。"""
    print("\n" + "=" * 92)
    print(f"【{title}】 三次熊市的顶部风险触发（分≥4 首次触发 vs 实际顶）")
    print("=" * 92)
    for gid, na, nb in GROUPS:
        st = build(rows, gid, na, nb, dates)
        print(f"\n[{gid}] MA{na}/MA{nb}")
        for top, bot, name in BEARS:
            ti = next((i for i, d in enumerate(dates) if d >= top), None)
            if ti is None:
                continue
            hits = [i for i, d, v, _, _ in st
                    if RSK_D[d] + RSK_V[v] >= 4 and ti - 40 <= i <= ti + 60]
            if not hits:
                print(f"  {name:<10} 顶前后 100 日内无≥4 触发")
                continue
            first = min(hits)
            lag = first - ti
            print(f"  {name:<10} 首次触发 {dates[first]}  "
                  f"{'顶前' if lag < 0 else '顶后'} {abs(lag)} 个交易日  "
                  f"（窗口内共 {len(hits)} 天 ≥4）")


def main():
    px = sh_index()
    for path, title in [(ROOT / "var" / "bt" / "breadth_curve_groups.csv", "A/D 累计线（广度）"),
                        (ROOT / "var" / "bt" / "shindex_curve_groups.csv", "上证指数涨幅")]:
        rows = load(path)
        dates = [r["date"] for r in rows]
        report16(rows, px, dates, title)
        report_score(rows, px, dates, title)
        report_switch(rows, px, dates, title)
        report_bears(rows, px, dates, title)


if __name__ == "__main__":
    main()
