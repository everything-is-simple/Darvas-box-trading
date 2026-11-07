#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周线双序列 MA10/30 组合测试。

底层两条序列（用户指定，来自《市场情绪表》）：
  SH  = 上证指数周涨跌幅（%）
  AVG = 平均股价周涨跌幅（%）= 全市场等权日收益的周复合

均线：MA10 / MA30（周），非标参数，用户习惯。
三层量（快线 f=MA10，慢线 s=MA30）：
  dir = sign(MA_t - MA_{t-1})      方向（1 周变化）
  vel = sign(MA_t - MA_{t-VK})     速度（VK=4 周变化）
  acc = sign((MA_t-MA_{t-VK}) - (MA_{t-VK}-MA_{t-2VK}))   加速度

只读 var/bt/series_abc.csv + full_hist_adj.db，不写库。
"""
import csv
import sqlite3
import datetime
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "full_hist_adj.db"
CSV = ROOT / "var" / "bt" / "series_abc.csv"

FA, FB = 10, 30
VK = 4

DIR = {(1, 1): "同↑", (-1, -1): "同↓", (-1, 1): "顶背离", (1, -1): "底背离"}
VEL = {(1, 1): "同升", (-1, -1): "同降", (-1, 1): "快降慢升", (1, -1): "快升慢降"}
ACC = {(1, 1): "同增", (-1, -1): "同减", (-1, 1): "快减慢增", (1, -1): "快增慢减"}

# 三个顶（上证见顶日）与三段熊
TOPS = [("2007-10-16", "2007 结构分化顶"), ("2015-06-12", "2015 杠杆顶"),
        ("2018-01-24", "2018 白马顶")]
BEARS = [("2008-01-02", "2008-12-31", "2008 慢熊"),
         ("2011-01-01", "2011-12-31", "2011 慢熊"),
         ("2015-06-12", "2015-08-26", "2015 快熊"),
         ("2018-01-24", "2018-12-31", "2018 慢熊")]


def load_daily():
    avg = {}
    for r in csv.DictReader(CSV.open(encoding="utf-8")):
        if r["a_ret"]:
            avg[r["date"]] = float(r["a_ret"])
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    sh = dict(c.execute(
        "SELECT date, close FROM daily_bar WHERE code='sh000001' "
        "AND date BETWEEN '2005-01-01' AND '2018-12-31'").fetchall())
    c.close()
    return avg, sh


def build_weekly(avg, sh):
    dates = sorted(sh)
    order, wk = [], {}
    for d in dates:
        y, w, _ = datetime.date.fromisoformat(d).isocalendar()
        k = (y, w)
        if k not in wk:
            wk[k] = {"end": d, "px": sh[d], "avg": [], "first": sh[d]}
            order.append(k)
        wk[k]["end"] = d
        wk[k]["px"] = sh[d]
        if d in avg:
            wk[k]["avg"].append(1.0 + avg[d])
    out = []
    for i, k in enumerate(order):
        b = wk[k]
        prev_px = wk[order[i - 1]]["px"] if i > 0 else b["first"]
        sh_r = (b["px"] / prev_px - 1) * 100
        m = 1.0
        for x in b["avg"]:
            m *= x
        avg_r = (m - 1) * 100
        out.append({"end": b["end"], "px": b["px"], "sh": sh_r, "avg": avg_r})
    return out


def ma(v, n):
    o = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            o[i] = s / n
    return o


def d(seq, k):
    o = [None] * len(seq)
    for i in range(k, len(seq)):
        if seq[i] is not None and seq[i - k] is not None:
            o[i] = seq[i] - seq[i - k]
    return o


def sg(seq):
    o = [None] * len(seq)
    for i, x in enumerate(seq):
        if x is None:
            continue
        o[i] = 1 if x > 0 else (-1 if x < 0 else None)
    return o


def triple(series):
    """三条均线上的 dir/vel/acc 符号对，返回 (dir_f,dir_s,vel_f,vel_s,acc_f,acc_s) 列表。"""
    mf, ms = ma(series, FA), ma(series, FB)
    df, ds = sg(d(mf, 1)), sg(d(ms, 1))
    vf, vs = d(mf, VK), d(ms, VK)
    af, as_ = sg(d(vf, VK)), sg(d(vs, VK))
    return list(zip(df, ds, sg(vf), sg(vs), af, as_))


def state_names(t):
    df, ds, vf, vs, af, as_ = t
    if None in t:
        return None
    return (DIR[(df, ds)], VEL[(vf, vs)], ACC[(af, as_)])


def fwd(px, i, k):
    j = i + k
    if j >= len(px):
        return None
    return (px[j] / px[i] - 1) * 100


def stat(idx, px, k):
    r = [x for x in (fwd(px, i, k) for i in idx) if x is not None]
    if not r:
        return None
    return len(r), sum(r) / len(r), sum(1 for x in r if x < 0) / len(r) * 100


def show(title, rows, px, keys, base_idx):
    print("\n" + "=" * 96)
    print(title)
    print("=" * 96)
    b4 = stat(base_idx, px, 4)
    b13 = stat(base_idx, px, 13)
    print(f"  {'状态':<20}{'周数':>6}{'占比':>7}"
          f"{'后4周均':>9}{'跌率':>7}{'后13周均':>10}{'跌率':>7}")
    print(f"  {'【全窗基准】':<20}{b4[0]:>6}{100:>6.1f}%"
          f"{b4[1]:>8.2f}%{b4[2]:>6.1f}%{b13[1]:>9.2f}%{b13[2]:>6.1f}%")
    for k in keys:
        idx = [i for i, s in rows if s == k]
        if not idx:
            continue
        s4, s13 = stat(idx, px, 4), stat(idx, px, 13)
        if not s4:
            continue
        print(f"  {k:<20}{len(idx):>6}{len(idx)/len(base_idx)*100:>6.1f}%"
              f"{s4[1]:>8.2f}%{s4[2]:>6.1f}%{s13[1]:>9.2f}%{s13[2]:>6.1f}%")


def main():
    avg, sh = load_daily()
    W = build_weekly(avg, sh)
    n = len(W)
    px = [x["px"] for x in W]
    ends = [x["end"] for x in W]
    print(f"周线样本 {n} 周  {ends[0]} ~ {ends[-1]}")

    sh_s = [x["sh"] for x in W]
    av_s = [x["avg"] for x in W]
    df_s = [a - b for a, b in zip(av_s, sh_s)]  # 分化度：平均股价 − 上证

    tri = {"SH": triple(sh_s), "AVG": triple(av_s), "DIV": triple(df_s)}
    st = {k: [state_names(t) for t in v] for k, v in tri.items()}
    valid = [i for i in range(n) if st["SH"][i] and st["AVG"][i] and st["DIV"][i]]
    print(f"有效周 {len(valid)}  （MA30 预热后）")

    # ---- 一、单线方向 4 态 ----
    for k, nm in [("SH", "上证指数周涨跌幅"), ("AVG", "平均股价周涨跌幅"),
                  ("DIV", "分化度(平均股价−上证)")]:
        rows = [(i, st[k][i][0]) for i in valid]
        show(f"一、{nm} — 方向 4 态（MA10/30，周线）", rows, px,
             ["同↑", "同↓", "顶背离", "底背离"], valid)

    # ---- 二、方向 × 速度 16 格 ----
    print("\n" + "=" * 96)
    print("二、方向 × 速度 16 格（只列 n>=10 的格）")
    print("=" * 96)
    for k, nm in [("SH", "上证"), ("AVG", "平均股价"), ("DIV", "分化度")]:
        print(f"\n[{nm}]")
        print(f"  {'方向/速度':<20}{'周数':>6}{'后4周均':>9}{'跌率':>7}{'后13周均':>10}{'跌率':>7}")
        b4, b13 = stat(valid, px, 4), stat(valid, px, 13)
        print(f"  {'【基准】':<20}{len(valid):>6}{b4[1]:>8.2f}%{b4[2]:>6.1f}%"
              f"{b13[1]:>9.2f}%{b13[2]:>6.1f}%")
        cnt = defaultdict(list)
        for i in valid:
            cnt[(st[k][i][0], st[k][i][1])].append(i)
        for key, idx in sorted(cnt.items(), key=lambda x: -len(x[1])):
            if len(idx) < 10:
                continue
            s4, s13 = stat(idx, px, 4), stat(idx, px, 13)
            mark = " <<<" if s4[2] >= 60 and len(idx) >= 20 else ""
            print(f"  {key[0]+' / '+key[1]:<20}{len(idx):>6}{s4[1]:>8.2f}%{s4[2]:>6.1f}%"
                  f"{s13[1]:>9.2f}%{s13[2]:>6.1f}%{mark}")

    # ---- 三、双线方向组合 4x4 ----
    print("\n" + "=" * 96)
    print("三、上证方向 × 平均股价方向 = 16 格")
    print("=" * 96)
    print(f"  {'上证 \\ 平均股价':<16}{'同↑':>16}{'同↓':>16}{'顶背离':>16}{'底背离':>16}")
    cell = defaultdict(list)
    for i in valid:
        cell[(st["SH"][i][0], st["AVG"][i][0])].append(i)
    dnames = ["同↑", "同↓", "顶背离", "底背离"]
    for a in dnames:
        line = f"  {a:<16}"
        for b in dnames:
            idx = cell.get((a, b), [])
            if len(idx) < 5:
                line += f"{'-':>16}"
            else:
                s4 = stat(idx, px, 4)
                line += f"{len(idx):>4}周 {s4[1]:>5.1f}%/{s4[2]:>4.0f}%"
        print(line)
    print("  （格式：周数 + 后4周均收益 / 跌率）")

    # ---- 四、三顶前瞻 ----
    print("\n" + "=" * 96)
    print("四、三个顶：信号在顶前多少周首次出现")
    print("=" * 96)
    SIGS = {
        "方向背离": lambda s: s[0] == "顶背离",
        "方向+速度": lambda s: s[0] == "顶背离" and s[1] == "快降慢升",
        "三维全负向前": lambda s: s[0] == "顶背离" and s[1] == "快降慢升" and s[2] == "快减慢增",
        "双线同↓": None,  # 特殊处理
    }
    for label, cond in SIGS.items():
        print(f"\n[{label}]")
        print(f"  {'顶部':<20}{'SH':>10}{'AVG':>10}{'DIV':>10}")
        for top, note in TOPS:
            ti = next((i for i, e in enumerate(ends) if e >= top), None)
            if ti is None:
                continue
            cells = []
            for k in ("SH", "AVG", "DIV"):
                found = None
                for j in range(ti, max(-1, ti - 40), -1):
                    s = st[k][j]
                    if not s:
                        continue
                    hit = (s[0] == "同↓" and s[1] == "同降") if cond is None else cond(s)
                    if hit:
                        found = ti - j
                        break
                cells.append(f"{found}周前" if found is not None else "—")
            print(f"  {top+' '+note:<20}{cells[0]:>10}{cells[1]:>10}{cells[2]:>10}")

    # ---- 五、按年信号密度 ----
    print("\n" + "=" * 96)
    print("五、信号按年分布（每年出现周数）—— 检验是否只在熊市响")
    print("=" * 96)
    years = sorted({e[:4] for e in ends if e >= "2005"})
    for label, cond in [("方向背离", lambda s: s[0] == "顶背离"),
                        ("方向+速度", lambda s: s[0] == "顶背离" and s[1] == "快降慢升"),
                        ("三维全负向前", lambda s: s[0] == "顶背离" and s[1] == "快降慢升"
                         and s[2] == "快减慢增"),
                        ("双线同↓", lambda s: s[0] == "同↓" and s[1] == "同降")]:
        print(f"\n[{label}]")
        print("  年    " + " ".join(f"{y[2:]:>4}" for y in years))
        for k in ("SH", "AVG", "DIV"):
            cnt = Counter()
            for i in valid:
                if ends[i] < "2005":
                    continue
                s = st[k][i]
                if s and cond(s):
                    cnt[ends[i][:4]] += 1
            print(f"  {k:<6}" + " ".join(f"{cnt.get(y,0):>4}" for y in years)
                  + f"  合计 {sum(cnt.values())}")

    # ---- 六、熊市覆盖 ----
    print("\n" + "=" * 96)
    print("六、四段熊市内的信号覆盖（占比 = 该段触发周数/总周数）")
    print("=" * 96)
    print(f"  {'熊市段':<18}{'总周':>6}" + "".join(f"{k:>22}" for k in ("SH", "AVG", "DIV")))
    for a, b, nm in BEARS:
        idx = [i for i in valid if a <= ends[i] <= b]
        if not idx:
            continue
        line = f"  {nm:<18}{len(idx):>6}"
        for k in ("SH", "AVG", "DIV"):
            c = sum(1 for i in idx if st[k][i][0] == "同↓" and st[k][i][1] == "同降")
            line += f"{c:>6}周 {c/len(idx)*100:>5.1f}%          "
        print(line)

    # ---- 七、分化度的极值时刻 ----
    print("\n" + "=" * 96)
    print("七、分化度（平均股价 − 上证）周涨幅差最大的 15 周")
    print("=" * 96)
    print(f"  {'周末':<14}{'上证%':>9}{'平均股价%':>11}{'分化pp':>9}{'上证点位':>11}")
    for i in sorted(valid, key=lambda i: -abs(df_s[i]))[:15]:
        print(f"  {ends[i]:<14}{sh_s[i]:>8.2f}%{av_s[i]:>10.2f}%{df_s[i]:>9.2f}"
              f"{px[i]:>11.0f}")


if __name__ == "__main__":
    main()
