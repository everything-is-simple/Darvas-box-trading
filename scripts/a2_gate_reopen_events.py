#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A2 零回测：闸门重开事件表（E19-33）。

问题：MA60 闸门（gate.ma60.mode=stop）「由下穿上」重开后的首波建仓，
胜率是否显著低于常态？若是 → 才立「重开冷却」回测臂。

口径（与引擎一致）：
- 指数 = gate.ma60.index(sh000001)，close vs SMA60(含当日)（regime.CloseVsMA）。
- 事件 E = 首日 close>=MA60 且前一日 close<MA60；gap = 事件前连续在 MA60 下方天数。
- 建仓归属：position.opened_date ∈ (E, E+k] 交易日（E 日收盘判断 → E+1 执行）。
- 胜率 = realized_pnl>0；每单位净利 = Σpnl / Σunits，units = 首腿名义/(entry 日权益×0.02)
  （与 scripts/base_ladder_band.py 同口径，R28 三列：总账 / 剔前3 / 每单位）。

用法：python scripts/a2_gate_reopen_events.py [--db PATH] [--index sh000001] [--min-gap 21]
"""
import argparse
import math
import os
import sqlite3
import sys
from bisect import bisect_left

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "var", "bt", "base", "bt_baseA_cont_adj.db")
WINDOWS = (1, 3, 5, 10)
UNIT_PCT = 0.02


def load_index(db, code):
    rows = db.execute(
        "SELECT date, close FROM daily_bar WHERE code=? AND close>0 ORDER BY date", (code,)
    ).fetchall()
    return [r[0] for r in rows], [float(r[1]) for r in rows]


def ma_series(closes, n):
    out = [None] * len(closes)
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n:
            s -= closes[i - n]
        if i + 1 >= n:
            out[i] = s / n
    return out


def find_reopen_events(dates, closes, ma):
    ev = []
    below = 0
    for i in range(len(closes)):
        if ma[i] is None:
            below = 0
            continue
        if closes[i] < ma[i]:
            below += 1
            continue
        if below > 0 and i > 0 and ma[i - 1] is not None and closes[i - 1] < ma[i - 1]:
            ev.append((i, below))
        below = 0
    return ev


def load_positions(db):
    """返回 rows: (pid, code, opened, closed, pnl, notional, eq)"""
    eq = {r[0]: r[1] for r in db.execute("SELECT date, total FROM equity_snapshot").fetchall()}
    leg = {r[0]: r[1] for r in db.execute(
        "SELECT position_id, qty*price FROM position_leg WHERE leg_no=1").fetchall()}
    rows = []
    for pid, code, od, cd, pnl in db.execute(
            """SELECT id, code, cast(opened_date as text), cast(closed_date as text),
                      coalesce(realized_pnl,0) FROM position ORDER BY opened_date, id"""):
        n = leg.get(pid, 0.0)
        e = eq.get(od[:10], 0.0)
        rows.append((pid, code, od, od, float(pnl), n, e))
    return rows


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def fisher_less(a, n1, c, n2):
    """单侧精确检验 p 值：H1 = 组1胜率 < 组2胜率。"""
    from math import comb
    tw, N = a + c, n1 + n2
    lo, hi = max(0, tw - n2), min(n1, tw)
    p = 0.0
    for x in range(lo, a + 1):
        p += comb(n1, x) * comb(n2, tw - x) / comb(N, tw)
    return min(1.0, p)


def blk(label, rows, all_n=None):
    n = len(rows)
    if n == 0:
        return dict(label=label, n=0, w=0, wr=0.0, lo=0.0, hi=0.0, pnl=0.0, avg=0.0,
                    unit=0.0, ex3=0.0, n_ex3=0)
    w = sum(1 for r in rows if r[4] > 0)
    pnl = sum(r[4] for r in rows)
    units = sum((r[5] / (r[6] * UNIT_PCT)) for r in rows if r[6] > 0)
    ps = sorted(r[4] for r in rows)
    ex3 = sum(ps[:-3]) if n > 3 else 0.0
    lo, hi = wilson(w, n)
    return dict(label=label, n=n, w=w, wr=w / n, lo=lo, hi=hi, pnl=pnl, avg=pnl / n,
                unit=(pnl / units if units else 0.0), ex3=ex3, n_ex3=max(0, n - 3))


def fmt(s):
    return (f"{s['n']:>4} {s['wr']*100:>6.1f}% [{s['lo']*100:>4.1f},{s['hi']*100:>4.1f}] "
            f"{s['pnl']:>12,.0f} {s['avg']:>9,.0f} {s['unit']:>9,.0f} {s['ex3']:>12,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--index", default="sh000001")
    ap.add_argument("--ma", type=int, default=60)
    ap.add_argument("--min-gap", type=int, default=21)
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    dates, closes = load_index(db, a.index)
    if not dates:
        sys.exit(f"指数 {a.index} 无日线")
    ma = ma_series(closes, a.ma)
    events = find_reopen_events(dates, closes, ma)
    ev_idx = [e[0] for e in events]
    pos = load_positions(db)
    allst = blk("全样本", pos)
    first_open = pos[0][2]

    # 建仓 → 交易日索引 → 距最近事件位置
    rel, epos = {}, {}
    for r in pos:
        i = bisect_left(dates, r[2])
        if i >= len(dates) or dates[i] != r[2]:
            rel[r[0]] = None
            continue
        epos[r[0]] = i
        j = bisect_left(ev_idx, i)
        rel[r[0]] = (i - ev_idx[j - 1]) if j > 0 else None

    print(f"# A2 闸门重开事件表｜{a.index} MA{a.ma}｜库 {os.path.basename(a.db)}")
    print(f"指数 {dates[0]}~{dates[-1]}（{len(dates)} 交易日）；建仓 {len(pos)} 笔 "
          f"（{first_open}~{pos[-1][2]}）；重开事件 {len(events)} 次")
    live = [e for e in events if dates[e[0]] >= first_open]
    print(f"其中落在交易期内的事件 {len(live)} 次\n")

    # ---------- 1 聚合窗口 ----------
    print("## 1 重开窗口 vs 对照（R28 三列）")
    print(f"{'组':<22}{'n':>5}{'胜率':>9}{'95%CI':>16}{'合计':>13}{'均笔':>9}{'每单位':>9}{'剔前3':>13}")
    stat = {}
    for k in WINDOWS:
        rows = [r for r in pos if rel.get(r[0]) is not None and 1 <= rel[r[0]] <= k]
        stat[k] = blk(f"T+1..{k}", rows)
        print(f"{'重开后 '+str(k)+' 日内':<22}{fmt(stat[k])}")
    kmax = max(WINDOWS)
    ctrl = [r for r in pos if rel.get(r[0]) is None or rel[r[0]] > kmax]
    cs = blk("对照", ctrl)
    print(f"{'对照（窗口外）':<22}{fmt(cs)}")
    print(f"{'全样本（基线）':<22}{fmt(allst)}\n")

    # ---------- 2 逐日位置 ----------
    print("## 2 距最近重开日的逐档位置（位置≈市场状态代理，慎读）")
    print(f"{'位置':<22}{'n':>5}{'胜率':>9}{'95%CI':>16}{'合计':>13}{'均笔':>9}{'每单位':>9}{'剔前3':>13}")
    bands = [("T+1", lambda x: x == 1), ("T+2", lambda x: x == 2), ("T+3", lambda x: x == 3),
             ("T+4~5", lambda x: x is not None and 4 <= x <= 5),
             ("T+6~10", lambda x: x is not None and 6 <= x <= 10),
             ("T+11~20", lambda x: x is not None and 11 <= x <= 20),
             ("T+21~60", lambda x: x is not None and 21 <= x <= 60),
             ("T+61+", lambda x: x is not None and x > 60)]
    for name, fn in bands:
        s = blk(name, [r for r in pos if fn(rel.get(r[0]))])
        print(f"{name:<22}{fmt(s)}")
    print()

    # ---------- 3 前关闸时长分层 ----------
    print("## 3 重开前关闸时长分层（窗口 = T+1..T+10）")
    print(f"{'前关闸天数':<22}{'n':>5}{'胜率':>9}{'95%CI':>16}{'合计':>13}{'均笔':>9}{'每单位':>9}{'剔前3':>13}")
    gap_bands = [(1, 3), (4, 10), (11, 20), (21, 40), (41, 10 ** 6)]
    gap_stat = {}
    for lo, hi in gap_bands:
        sel = []
        for r in pos:
            i = epos.get(r[0])
            if i is None:
                continue
            j = bisect_left(ev_idx, i)
            if j == 0:
                continue
            ei, g = events[j - 1]
            if lo <= g <= hi and 1 <= i - ei <= kmax:
                sel.append(r)
        tag = f"{lo}~{hi}" if hi < 10 ** 6 else f"{lo}+"
        gap_stat[tag] = blk(tag, sel)
        print(f"{'前关闸 '+tag+' 日':<22}{fmt(gap_stat[tag])}")
    print()

    # ---------- 4 主判据检验 ----------
    print("## 4 判伪检验（预注册阈值 = 基线胜率 −1.96SE）")
    thr = allst['wr'] - 1.96 * math.sqrt(allst['wr'] * (1 - allst['wr']) / allst['n'])
    print(f"阈值 = {allst['wr']*100:.2f}% − 1.96×SE = **{thr*100:.2f}%**（n={allst['n']}）")
    for k in WINDOWS:
        s = stat[k]
        p = fisher_less(s['w'], s['n'], cs['w'], cs['n'])
        verdict = "触发" if s['wr'] < thr else "不触发"
        print(f"- T+1..T+{k}: {s['w']}/{s['n']}={s['wr']*100:.1f}% vs 对照 "
              f"{cs['w']}/{cs['n']}={cs['wr']*100:.1f}% ｜ p={p:.4f} ｜ **{verdict}**")
    long_sel = []
    for r in pos:
        i = epos.get(r[0])
        if i is None:
            continue
        j = bisect_left(ev_idx, i)
        if j == 0:
            continue
        ei, g = events[j - 1]
        if g >= a.min_gap and 1 <= i - ei <= kmax:
            long_sel.append(r)
    ls = blk(f"gap>={a.min_gap}", long_sel)
    pl = fisher_less(ls['w'], ls['n'], cs['w'], cs['n'])
    p_all = fisher_less(ls['w'], ls['n'], stat[kmax]['w'] - ls['w'], stat[kmax]['n'] - ls['n'])
    print(f"- **长关闸(≥{a.min_gap}日) 窗口**: {ls['w']}/{ls['n']}={ls['wr']*100:.1f}% vs 对照 "
          f"{cs['wr']*100:.1f}% ｜ p={pl:.4f} ｜ "
          f"{'触发' if ls['wr'] < thr else '不触发'}")
    print(f"  同窗口内短关闸组 {stat[kmax]['n']-ls['n']} 笔 {stat[kmax]['wr']*100:.1f}% → "
          f"长 vs 短 p={p_all:.4f}\n")

    # ---------- 5 稳健性 ----------
    print("## 5 稳健性：长关闸窗口组")
    r2018 = [r for r in long_sel if r[2].startswith("2018")]
    s18 = blk("2018", r2018)
    rest = [r for r in long_sel if not r[2].startswith("2018")]
    sr = blk("非2018", rest)
    print(f"{'长关闸窗口 全':<22}{fmt(ls)}")
    print(f"{'  其中 2018':<22}{fmt(s18)}")
    print(f"{'  剔除 2018':<22}{fmt(sr)}")
    yrs = {}
    for r in long_sel:
        yrs.setdefault(r[2][:4], []).append(r)
    print("  年度分布：", " ｜ ".join(
        f"{y}: {len(v)}笔 {sum(1 for x in v if x[4]>0)}胜 {sum(x[4] for x in v):+,.0f}"
        for y, v in sorted(yrs.items())))
    print()

    # ---------- 6 长关闸事件明细 ----------
    print(f"## 6 长关闸(≥{a.min_gap}日)重开事件明细（窗口 T+1..T+10）")
    print("| 重开日 | 关闸日 | 偏离 | 后20日指数 | 窗内笔数 | 胜 | 胜率 | 合计 |")
    print("|---|---|---|---|---|---|---|---|")
    for k, (i, g) in enumerate(events, 1):
        if g < a.min_gap or dates[i] < first_open:
            continue
        sel = [r for r in pos if epos.get(r[0]) is not None
               and 0 < epos[r[0]] - i <= kmax and epos[r[0]] > i
               and bisect_left(ev_idx, epos[r[0]]) > 0
               and events[bisect_left(ev_idx, epos[r[0]]) - 1][0] == i]
        if not sel:
            continue
        w = sum(1 for r in sel if r[4] > 0)
        fwd = (f"{(closes[i+20]/closes[i]-1)*100:+.2f}%" if i + 20 < len(closes) else "")
        print(f"| {dates[i]} | {g} | {(closes[i]/ma[i]-1)*100:+.2f}% | {fwd} | {len(sel)} | {w} | "
              f"{w/len(sel)*100:.0f}% | {sum(r[4] for r in sel):+,.0f} |")

    # ---------- 6b 年份内对照（排除「坏年份」混杂） ----------
    print(f"\n## 6b 年份内对照：同一年内 长关闸窗口 vs 该年其他建仓（排除坏年份混杂）")
    print("| 年 | 窗口n | 窗口胜 | 窗口胜率 | 该年其他n | 其他胜 | 其他胜率 | 胜率差 |")
    print("|---|---|---|---|---|---|---|---|")
    by_year = {}
    for r in pos:
        by_year.setdefault(r[2][:4], []).append(r)
    mh_num = mh_den = mh_var = 0.0
    for y in sorted(by_year):
        rows = by_year[y]
        inw = [r for r in rows if r in long_sel]
        out = [r for r in rows if r not in long_sel]
        if not inw or not out:
            continue
        a1, n1 = sum(1 for r in inw if r[4] > 0), len(inw)
        a0, n0 = sum(1 for r in out if r[4] > 0), len(out)
        N = n1 + n0
        m1 = a1 + a0
        e = n1 * m1 / N
        v = n1 * n0 * m1 * (N - m1) / (N * N * (N - 1)) if N > 1 else 0
        mh_num += a1 - e
        mh_var += v
        print(f"| {y} | {n1} | {a1} | {a1/n1*100:.0f}% | {n0} | {a0} | {a0/n0*100:.0f}% | "
              f"{(a1/n1-a0/n0)*100:+.0f}pp |")
    z = mh_num / math.sqrt(mh_var) if mh_var > 0 else 0.0
    p_mh = 0.5 * math.erfc(z / math.sqrt(2))  # 单侧
    print(f"\nMantel-Haenszel 合并（按年分层）：z={z:.3f}，**单侧 p={p_mh:.5f}**"
          f" → {'窗口组在同一年内仍显著更差' if p_mh < 0.05 else '差异可由年份构成解释'}")

    # ---------- 7 2018 核对 ----------
    print("\n## 7 2018-11 脉冲核对（R27 原始证据）")
    for i, g in events:
        if dates[i].startswith("2018-1"):
            sel = [r for r in pos if epos.get(r[0]) is not None and 0 < epos[r[0]] - i <= kmax
                   and bisect_left(ev_idx, epos[r[0]]) > 0
                   and events[bisect_left(ev_idx, epos[r[0]]) - 1][0] == i]
            if sel:
                print(f"- 事件 {dates[i]}（关闸 {g} 日）→ 窗内 {len(sel)} 笔，"
                      f"{sum(1 for r in sel if r[4]>0)} 胜，{sum(r[4] for r in sel):+,.0f}")


if __name__ == "__main__":
    main()
