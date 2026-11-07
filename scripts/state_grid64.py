#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三维状态格 64 组合实测：方向 / 速度 / 加速度 各 4 态，真实数据里到底出现过几格。

只读 var/bt/breadth_curve_groups.csv + full_hist_adj.db(sh000001)，不写库。

四组均线：MA5/20、MA10/30、MA30/50、MA50/200
三维各 4 态（快线 f，慢线 s，符号对按 (f, s)）：
  dir : (+,+) 同↑ | (-,-) 同↓ | (-,+) 顶背离 | (+,-) 底背离
  vel : (+,+) 同升 | (-,-) 同降 | (-,+) 快降慢升 | (+,-) 快升慢降
  acc : (+,+) 同增 | (-,-) 同减 | (-,+) 快减慢增 | (+,-) 快增慢减

核心问题：4*4*4=64 格，实测可达几格？哪些格是「盛极/衰极」的指纹？
"""
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "var" / "bt" / "breadth_curve_groups.csv"
DB = ROOT / "var" / "bt" / "full_hist_adj.db"

GROUPS = [("g1", 5, 20), ("g2", 10, 30), ("g3", 30, 50), ("g4", 50, 200)]

DIR = {(1, 1): "同↑", (-1, -1): "同↓", (-1, 1): "顶背离", (1, -1): "底背离"}
VEL = {(1, 1): "同升", (-1, -1): "同降", (-1, 1): "快降慢升", (1, -1): "快升慢降"}
ACC = {(1, 1): "同增", (-1, -1): "同减", (-1, 1): "快减慢增", (1, -1): "快增慢减"}


def sg(x):
    if x is None or x == "":
        return None
    v = float(x)
    if v > 0:
        return 1
    if v < 0:
        return -1
    return None  # 0 视为中性，剔除


def load():
    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    return rows


def sh_index():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    d = dict(c.execute(
        "SELECT date, close FROM daily_bar WHERE code='sh000001' "
        "AND date BETWEEN '2005-01-01' AND '2018-12-31'").fetchall())
    c.close()
    return d


def fwd_ret(px, dates, i, n):
    """第 i 天之后 n 个交易日的指数收益。"""
    j = min(i + n, len(dates) - 1)
    if j <= i:
        return None
    a, b = px.get(dates[i]), px.get(dates[j])
    if not a or not b:
        return None
    return (b - a) / a


def main():
    rows = load()
    dates = [r["date"] for r in rows]
    px = sh_index()

    per_group = {}
    for gid, na, nb in GROUPS:
        cnt = Counter()
        cells = defaultdict(list)   # cell -> [idx]
        for i, r in enumerate(rows):
            df = sg(r[f"{gid}_dir{na}"]); ds = sg(r[f"{gid}_dir{nb}"])
            vf = sg(r[f"{gid}_vel{na}"]); vs = sg(r[f"{gid}_vel{nb}"])
            af = sg(r[f"{gid}_acc{na}"]); as_ = sg(r[f"{gid}_acc{nb}"])
            if None in (df, ds, vf, vs, af, as_):
                continue
            key = (DIR[(df, ds)], VEL[(vf, vs)], ACC[(af, as_)])
            cnt[key] += 1
            cells[key].append(i)
        per_group[gid] = (cnt, cells)

    # ---- 1. 每组 64 格实测可达数 ----
    print("=" * 88)
    print("一、64 格实测可达数（2005-01 ~ 2018-12，全窗 3402 交易日）")
    print("=" * 88)
    print(f"{'组':<6}{'均线':<12}{'有效日':>8}{'出现格数':>10}{'未出现':>8}{'覆盖率':>9}")
    all_cells = set()
    for gid, na, nb in GROUPS:
        cnt, _ = per_group[gid]
        n = sum(cnt.values())
        print(f"{gid:<6}{f'MA{na}/{nb}':<12}{n:>8}{len(cnt):>10}{64-len(cnt):>8}"
              f"{len(cnt)/64*100:>8.1f}%")
        all_cells |= set(cnt)

    print(f"\n四组合并后出现过的格：{len(all_cells)} / 64")
    never = [k for k in
             [(d, v, a) for d in DIR.values() for v in VEL.values() for a in ACC.values()]
             if k not in all_cells]
    print(f"从未出现的格：{len(never)}")
    for k in never:
        print("   ✗", " / ".join(k))

    # ---- 2. 各组前 12 高频格 ----
    print("\n" + "=" * 88)
    print("二、各组高频格 Top12（天数 / 占比）")
    print("=" * 88)
    for gid, na, nb in GROUPS:
        cnt, _ = per_group[gid]
        n = sum(cnt.values())
        print(f"\n[{gid}] MA{na}/MA{nb}  有效 {n} 天")
        print(f"  {'方向':<8}{'速度':<10}{'加速度':<10}{'天数':>7}{'占比':>8}")
        for (d, v, a), c in cnt.most_common(12):
            print(f"  {d:<8}{v:<10}{a:<10}{c:>7}{c/n*100:>7.1f}%")

    # ---- 3. 顶部/底部指纹：三阶同向 (快线全负 / 慢线全正) ----
    print("\n" + "=" * 88)
    print("三、拐点指纹格：(快,慢) 符号在三个维度上一致")
    print("=" * 88)
    TOP = ("顶背离", "快降慢升", "快减慢增")
    BOT = ("底背离", "快升慢降", "快增慢减")
    for gid, na, nb in GROUPS:
        cnt, cells = per_group[gid]
        nt = cnt.get(TOP, 0)
        nbp = cnt.get(BOT, 0)
        print(f"\n[{gid}] MA{na}/MA{nb}")
        print(f"  顶部指纹 {TOP} : {nt} 天")
        if nt:
            idx = cells[TOP]
            for h in (20, 60):
                rs = [fwd_ret(px, dates, i, h) for i in idx]
                rs = [x for x in rs if x is not None]
                if rs:
                    avg = sum(rs) / len(rs)
                    win = sum(1 for x in rs if x < 0) / len(rs)
                    print(f"     后 {h} 日：均收益 {avg*100:+.2f}%  "
                          f"下跌概率 {win*100:.1f}%  (n={len(rs)})")
        print(f"  底部指纹 {BOT} : {nbp} 天")
        if nbp:
            idx = cells[BOT]
            for h in (20, 60):
                rs = [fwd_ret(px, dates, i, h) for i in idx]
                rs = [x for x in rs if x is not None]
                if rs:
                    avg = sum(rs) / len(rs)
                    win = sum(1 for x in rs if x > 0) / len(rs)
                    print(f"     后 {h} 日：均收益 {avg*100:+.2f}%  "
                          f"上涨概率 {win*100:.1f}%  (n={len(rs)})")

    # ---- 4. 全窗基准 ----
    rs = [fwd_ret(px, dates, i, 20) for i in range(len(dates))]
    rs = [x for x in rs if x is not None]
    print(f"\n[基准] 全窗任一日后 20 日均收益 {sum(rs)/len(rs)*100:+.2f}%  "
          f"下跌概率 {sum(1 for x in rs if x<0)/len(rs)*100:.1f}%  (n={len(rs)})")
    rs = [fwd_ret(px, dates, i, 60) for i in range(len(dates))]
    rs = [x for x in rs if x is not None]
    print(f"[基准] 全窗任一日后 60 日均收益 {sum(rs)/len(rs)*100:+.2f}%  "
          f"下跌概率 {sum(1 for x in rs if x<0)/len(rs)*100:.1f}%  (n={len(rs)})")


if __name__ == "__main__":
    main()


def chain():
    """盛极必衰的传导链：三阶导数在 (快,慢) 上逐级转负。
    A 初警 = dir(+,+) vel(+,+) acc(-,+)   曲率先掉头
    B 传导 = dir(+,+) vel(-,+) acc(-,+)   速度转负
    C 成立 = dir(-,+) vel(-,+) acc(-,+)   方向背离
    D 走完 = dir(-,-) vel(-,-) acc(-,-)   全线下行
    """
    rows = load()
    dates = [r["date"] for r in rows]
    px = sh_index()
    STAGES = [
        ("A 初警", ("同↑", "同升", "快减慢增")),
        ("B 传导", ("同↑", "快降慢升", "快减慢增")),
        ("C 成立", ("顶背离", "快降慢升", "快减慢增")),
        ("D 走完", ("同↓", "同降", "同减")),
    ]
    print("\n" + "=" * 88)
    print("四、盛极必衰传导链：逐级转负，各阶段的后续表现")
    print("=" * 88)
    for gid, na, nb in GROUPS:
        print(f"\n[{gid}] MA{na}/MA{nb}")
        print(f"  {'阶段':<8}{'天数':>7}{'占比':>8}{'后20日均':>11}{'跌概率':>9}"
              f"{'后60日均':>11}{'跌概率':>9}")
        n_all = 0
        recs = []
        for i, r in enumerate(rows):
            df = sg(r[f"{gid}_dir{na}"]); ds = sg(r[f"{gid}_dir{nb}"])
            vf = sg(r[f"{gid}_vel{na}"]); vs = sg(r[f"{gid}_vel{nb}"])
            af = sg(r[f"{gid}_acc{na}"]); as_ = sg(r[f"{gid}_acc{nb}"])
            if None in (df, ds, vf, vs, af, as_):
                continue
            n_all += 1
            recs.append((i, (DIR[(df, ds)], VEL[(vf, vs)], ACC[(af, as_)])))
        for name, key in STAGES:
            idx = [i for i, k in recs if k == key]
            if not idx:
                print(f"  {name:<8}{0:>7}")
                continue
            r20 = [x for x in (fwd_ret(px, dates, i, 20) for i in idx) if x is not None]
            r60 = [x for x in (fwd_ret(px, dates, i, 60) for i in idx) if x is not None]
            a20 = sum(r20)/len(r20)*100 if r20 else float("nan")
            d20 = sum(1 for x in r20 if x < 0)/len(r20)*100 if r20 else float("nan")
            a60 = sum(r60)/len(r60)*100 if r60 else float("nan")
            d60 = sum(1 for x in r60 if x < 0)/len(r60)*100 if r60 else float("nan")
            print(f"  {name:<8}{len(idx):>7}{len(idx)/n_all*100:>7.1f}%"
                  f"{a20:>10.2f}%{d20:>8.1f}%{a60:>10.2f}%{d60:>8.1f}%")

        # A->C 的时滞
        idxA = [i for i, k in recs if k == STAGES[0][1]]
        idxC = [i for i, k in recs if k == STAGES[2][1]]
        gaps = []
        for i in idxA:
            later = [j for j in idxC if j >= i]
            if later:
                g = later[0] - i
                if g <= 120:
                    gaps.append(g)
        if gaps:
            gaps.sort()
            print(f"  A→C 时滞（中位/均值）：{gaps[len(gaps)//2]} / {sum(gaps)/len(gaps):.1f} 交易日  (n={len(gaps)})")
