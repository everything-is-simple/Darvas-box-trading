#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双序列 MA10/30 组合测试：日线 / 周线。

底层两条序列（用户《市场情绪表》口径）：
  SH  = 上证指数当日涨跌幅（%）
  AVG = 平均股价当日涨跌幅（%）= 全市场等权日收益

均线：MA10 / MA30（非标参数）
三层量（快 f=MA10，慢 s=MA30）：
  dir = sign(MA_t - MA_{t-1})          方向
  vel = sign(MA_t - MA_{t-VK})         速度
  acc = sign(Δvel)                     加速度

用法：python scripts/dual_ma_ab.py [daily|weekly]
只读，不写库。
"""
import csv
import sys
import sqlite3
import datetime
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "full_hist_adj.db"
CSV = ROOT / "var" / "bt" / "series_abc.csv"

FA, FB = 10, 30
FREQ = sys.argv[1] if len(sys.argv) > 1 else "daily"
VK = 5 if FREQ == "daily" else 4
H1, H2 = (20, 60) if FREQ == "daily" else (4, 13)

DIR = {(1, 1): "同↑", (-1, -1): "同↓", (-1, 1): "顶背离", (1, -1): "底背离"}
VEL = {(1, 1): "同升", (-1, -1): "同降", (-1, 1): "快降慢升", (1, -1): "快升慢降"}
ACC = {(1, 1): "同增", (-1, -1): "同减", (-1, 1): "快减慢增", (1, -1): "快增慢减"}

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
            avg[r["date"]] = float(r["a_ret"]) * 100
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    sh = dict(c.execute(
        "SELECT date, close FROM daily_bar WHERE code='sh000001' "
        "AND date BETWEEN '2005-01-01' AND '2018-12-31'").fetchall())
    c.close()
    return avg, sh


def to_daily(avg, sh):
    dates = sorted(sh)
    out = []
    prev = None
    for d in dates:
        r = (sh[d] / prev - 1) * 100 if prev else 0.0
        prev = sh[d]
        out.append({"end": d, "px": sh[d], "sh": r, "avg": avg.get(d, 0.0)})
    return out


def to_weekly(avg, sh):
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
        m = 1.0
        for x in b["avg"]:
            m *= x
        out.append({"end": b["end"], "px": b["px"],
                    "sh": (b["px"] / prev_px - 1) * 100, "avg": (m - 1) * 100})
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


def df(seq, k):
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
    mf, ms = ma(series, FA), ma(series, FB)
    dfm, dsm = df(mf, 1), df(ms, 1)
    vfm, vsm = df(mf, VK), df(ms, VK)
    return list(zip(sg(dfm), sg(dsm), sg(vfm), sg(vsm),
                    sg(df(vfm, VK)), sg(df(vsm, VK))))


def nm(t):
    if None in t:
        return None
    return (DIR[(t[0], t[1])], VEL[(t[2], t[3])], ACC[(t[4], t[5])])


def fwd(px, i, k):
    j = i + k
    return (px[j] / px[i] - 1) * 100 if j < len(px) else None


def stat(idx, px, k):
    r = [x for x in (fwd(px, i, k) for i in idx) if x is not None]
    if not r:
        return (0, 0, 0)
    return (len(r), sum(r) / len(r), sum(1 for x in r if x < 0) / len(r) * 100)


def main():
    avg, sh = load_daily()
    W = to_daily(avg, sh) if FREQ == "daily" else to_weekly(avg, sh)
    n = len(W)
    px = [x["px"] for x in W]
    ends = [x["end"] for x in W]
    unit = "日" if FREQ == "daily" else "周"
    print(f"频率 {FREQ}  样本 {n} {unit}  {ends[0]} ~ {ends[-1]}")

    sh_s = [x["sh"] for x in W]
    av_s = [x["avg"] for x in W]
    dv_s = [a - b for a, b in zip(av_s, sh_s)]  # 分化度

    st = {k: [nm(t) for t in triple(s)]
          for k, s in (("SH", sh_s), ("AVG", av_s), ("DIV", dv_s))}
    valid = [i for i in range(n) if st["SH"][i] and st["AVG"][i] and st["DIV"][i]]
    print(f"有效 {len(valid)} {unit}（MA{FB} 预热后）\n")

    NAMES = [("SH", "上证指数当日涨跌幅"), ("AVG", "平均股价当日涨跌幅"),
             ("DIV", "分化度（平均股价−上证）")]
    if FREQ == "weekly":
        NAMES = [(k, v.replace("当日", "周")) for k, v in NAMES]

    # ============ 一、单线方向 4 态 ============
    b1, b2 = stat(valid, px, H1), stat(valid, px, H2)
    for k, label in NAMES:
        print("=" * 100)
        print(f"一、{label} — 方向 4 态（MA{FA}/{FB}）")
        print("=" * 100)
        print(f"  {'状态':<10}{'数量':>7}{'占比':>7}"
              f"{'后%d均' % H1:>10}{'跌率':>8}{'后%d均' % H2:>11}{'跌率':>8}")
        print(f"  {'【基准】':<10}{b1[0]:>7}{100:>6.1f}%{b1[1]:>9.2f}%"
              f"{b1[2]:>7.1f}%{b2[1]:>10.2f}%{b2[2]:>7.1f}%")
        for dn in ["同↑", "同↓", "顶背离", "底背离"]:
            idx = [i for i in valid if st[k][i][0] == dn]
            if not idx:
                continue
            s1, s2 = stat(idx, px, H1), stat(idx, px, H2)
            dd = s1[2] - b1[2]
            mark = "  <<<" if dd >= 10 else ""
            print(f"  {dn:<10}{len(idx):>7}{len(idx)/len(valid)*100:>6.1f}%"
                  f"{s1[1]:>9.2f}%{s1[2]:>7.1f}%{s2[1]:>10.2f}%{s2[2]:>7.1f}%"
                  f"   Δ跌率{dd:+.1f}pp{mark}")
        print()

    # ============ 二、双线方向 4×4 ============
    print("=" * 100)
    print(f"二、上证方向 × 平均股价方向 = 16 格（后{H1}{unit}，格式 格内数量/均收益/跌率）")
    print("=" * 100)
    dnames = ["同↑", "同↓", "顶背离", "底背离"]
    print(f"  {'上证＼平均':<12}" + "".join(f"{x:>21}" for x in dnames))
    cell = defaultdict(list)
    for i in valid:
        cell[(st["SH"][i][0], st["AVG"][i][0])].append(i)
    for a in dnames:
        line = f"  {a:<12}"
        for b in dnames:
            idx = cell.get((a, b), [])
            if len(idx) < 20:
                line += f"{'n=%d 样本不足' % len(idx):>21}"
            else:
                s = stat(idx, px, H1)
                line += f"{len(idx):>5} {s[1]:>6.2f}%/{s[2]:>5.1f}%   "
        print(line)
    print()

    # ============ 三、方向×速度 单线 ============
    print("=" * 100)
    print(f"三、方向 × 速度（剔除加速度），只列 n≥30 且跌率偏离基准 ≥8pp 的格")
    print("=" * 100)
    for k, label in NAMES:
        print(f"\n[{label}]  基准跌率 {b1[2]:.1f}%  基准后{H1}均 {b1[1]:.2f}%")
        print(f"  {'方向 / 速度':<22}{'数量':>7}{'后%d均' % H1:>10}{'跌率':>8}"
              f"{'Δ':>7}{'后%d均' % H2:>11}{'跌率':>8}")
        cnt = defaultdict(list)
        for i in valid:
            cnt[(st[k][i][0], st[k][i][1])].append(i)
        rows = []
        for key, idx in cnt.items():
            if len(idx) < 30:
                continue
            s1, s2 = stat(idx, px, H1), stat(idx, px, H2)
            rows.append((s1[2] - b1[2], key, len(idx), s1, s2))
        for dd, key, ln, s1, s2 in sorted(rows, reverse=True):
            if abs(dd) < 8:
                continue
            print(f"  {key[0]+' / '+key[1]:<22}{ln:>7}{s1[1]:>9.2f}%{s1[2]:>7.1f}%"
                  f"{dd:>+6.1f}{s2[1]:>10.2f}%{s2[2]:>7.1f}%")
    print()

    # ============ 四、三顶前瞻 ============
    print("=" * 100)
    print("四、三个顶：特定形态在顶前多久最后一次出现（往回最多 250" + unit + "）")
    print("=" * 100)
    SIGS = [("方向背离", lambda s: s[0] == "顶背离"),
            ("方向+速度", lambda s: s[0] == "顶背离" and s[1] == "快降慢升"),
            ("方向+速度+加速度", lambda s: s[0] == "顶背离" and s[1] == "快降慢升"
             and s[2] == "快减慢增"),
            ("双线全空", lambda s: s[0] == "同↓" and s[1] == "同降")]
    WIN = 250 if FREQ == "daily" else 52
    for label, cond in SIGS:
        print(f"\n[{label}]")
        print(f"  {'顶部':<22}" + "".join(f"{k:>12}" for k in ("SH", "AVG", "DIV")))
        for top, note in TOPS:
            ti = next((i for i, e in enumerate(ends) if e >= top), None)
            if ti is None:
                continue
            cells = []
            for k in ("SH", "AVG", "DIV"):
                found = None
                for j in range(ti - 1, max(-1, ti - WIN), -1):
                    if st[k][j] and cond(st[k][j]):
                        found = ti - j
                        break
                cells.append(f"{found}{unit}前" if found is not None else "—")
            print(f"  {top + ' ' + note:<22}" + "".join(f"{c:>12}" for c in cells))
    print()

    # ============ 五、按年信号密度 ============
    print("=" * 100)
    print("五、信号按年分布（出现次数）— 检验是不是只在熊市响")
    print("=" * 100)
    years = sorted({e[:4] for e in ends if "2005" <= e <= "2018"})
    for label, cond in SIGS:
        print(f"\n[{label}]")
        print("  年    " + "".join(f"{y[2:]:>5}" for y in years) + "   合计")
        for k in ("SH", "AVG", "DIV"):
            c = Counter(ends[i][:4] for i in valid if st[k][i] and cond(st[k][i]))
            print(f"  {k:<6}" + "".join(f"{c.get(y,0):>5}" for y in years)
                  + f"   {sum(c.values()):>4}")
    print()

    # ============ 六、熊市覆盖 ============
    print("=" * 100)
    print("六、四段熊市内的「双线全空」占比")
    print("=" * 100)
    print(f"  {'熊市段':<16}{'总' + unit:>7}" + "".join(f"{k:>20}" for k in ("SH", "AVG", "DIV")))
    for a, b, note in BEARS:
        idx = [i for i in valid if a <= ends[i] <= b]
        if not idx:
            continue
        line = f"  {note:<16}{len(idx):>7}"
        for k in ("SH", "AVG", "DIV"):
            c = sum(1 for i in idx if st[k][i][0] == "同↓" and st[k][i][1] == "同降")
            line += f"{c:>8}{unit} {c/len(idx)*100:>5.1f}%     "
        print(line)
    print()

    # ============ 七、DIV 恶化格逐条 ============
    print("=" * 100)
    print(f"七、分化度「顶背离 / 快降慢升」全部触发点（{H1}{unit}后上证表现）")
    print("=" * 100)
    idx = [i for i in valid if st["DIV"][i][0] == "顶背离" and st["DIV"][i][1] == "快降慢升"]
    print(f"  合计 {len(idx)} 次，基准跌率 {b1[2]:.1f}%，基准后{H1}均 {b1[1]:.2f}%")
    print(f"  {'日期':<13}{'上证':>9}{'分化度':>9}{'后%d' % H1:>9}{'后%d' % H2:>9}")
    for i in idx:
        f1, f2 = fwd(px, i, H1), fwd(px, i, H2)
        print(f"  {ends[i]:<13}{px[i]:>9.0f}{dv_s[i]:>+9.2f}"
              f"{(f'{f1:+.2f}%' if f1 is not None else '—'):>9}"
              f"{(f'{f2:+.2f}%' if f2 is not None else '—'):>9}")


if __name__ == "__main__":
    main()
