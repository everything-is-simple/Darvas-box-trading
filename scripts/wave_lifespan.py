#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 寿命引擎（Darvas 版，斯波朗迪「平均剩余寿命」方法的全市场实现）。

思想（源自《专业投机原理》第 26 章 + MALF v2.1 Lifespan 设计）：
  盈利的本质 = 离开此岸 box，借一段 wave 抵达彼岸 box。
  对每段 wave 建户口（寿命、幅度），在历史同向 wave 池中排名，
  用条件生存分布回答「这段 wave 还能延续多久」。

结构定义（全部只用截至昨日的窗口，天然防前视）：
  此岸 box  = 前 N 日（默认 20）最高价 ~ 最低价区间
  wave 诞生 = 收盘突破此岸 box 上轨（close > max(high, N)）
  wave 终结 = 收盘跌破前 M 日最低价（close < min(low, M)，默认 M=3，
              即斯波朗迪「三天高-低价准则」的出场版）
  wave 寿命 = 诞生日 → 终结日的交易日数
  wave 幅度 = 期间最高价 / 突破日收盘 − 1

排名（MALF L1c 口径）：
  池 = 本标的全部已终止 UP wave（termination_date <= 今天，防前视）
  percentile_rank(x) = count(x_i < x) / N_pool，严格小于，不含等于
  最小样本 N_pool >= 30 才给排名，否则 None（诚实退化）

剩余寿命（斯波朗迪条件生存）：
  S = { span_i : span_i >= T_current }（活得比当前 wave 久的历史 wave）
  剩余分布 = { span_i - T_current }
  P(再活 >= x 日) = |{span_i >= T + x}| / |S|

只读 var/bt/full_hist_adj.db，不写库，不碰引擎。

用法：
  python scripts/wave_lifespan.py --codes sh600000,sz000002
  python scripts/wave_lifespan.py --sample 100
  python scripts/wave_lifespan.py --all
"""
import argparse
import csv
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "full_hist_adj.db"

MIN_POOL = 30          # MALF L4：样本不足不排名
FLOOR_PCT = 0.20       # 波动地板：box 上轨至少比区间高 20%（近似 min_width，防死箱）


def load_bars(codes=None):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    if codes:
        ph = ",".join("?" * len(codes))
        rows = c.execute(
            f"SELECT code, date, high, low, close, volume FROM daily_bar "
            f"WHERE code IN ({ph}) AND volume > 0 ORDER BY code, date", codes).fetchall()
    else:
        rows = c.execute(
            "SELECT code, date, high, low, close, volume FROM daily_bar "
            "WHERE volume > 0 ORDER BY code, date").fetchall()
    c.close()
    by = defaultdict(list)
    for code, d, h, l, cl, v in rows:
        by[code].append((d, h, l, cl))
    return by


def detect_waves(bars, N=20, M=3):
    """状态机：BOX ⇄ UP_WAVE。返回 (waves, current_wave)。"""
    dates = [b[0] for b in bars]
    high = [b[1] for b in bars]
    low = [b[2] for b in bars]
    close = [b[3] for b in bars]
    n = len(bars)
    waves = []
    state = "BOX"
    w = None
    for i in range(N, n):
        top = max(high[i - N:i])          # 此岸 box 上轨（不含当日）
        bot = min(low[i - N:i])           # 此岸 box 下轨（不含当日）
        if state == "BOX":
            if bot > 0 and close[i] > top and (top / bot - 1) > 0.0:
                state = "UP"
                w = {"start_i": i, "entry": close[i], "whigh": high[i],
                     "wlow_after_high": low[i]}
        elif state == "UP":
            if high[i] > w["whigh"]:
                w["whigh"] = high[i]
            exit_line = min(low[i - M:i])  # 前 M 日最低（不含当日）= 三天低点轨
            if close[i] < exit_line:
                w["end_i"] = i
                w["exit"] = close[i]
                w["span"] = i - w["start_i"]
                w["gain"] = (w["whigh"] / w["entry"] - 1) * 100
                w["ret"] = (close[i] / w["entry"] - 1) * 100
                w["start"] = dates[w["start_i"]]
                w["end"] = dates[i]
                waves.append(w)
                state = "BOX"
                w = None
    cur = None
    if state == "UP" and w is not None:
        cur = {"start_i": w["start_i"], "entry": w["entry"], "whigh": w["whigh"],
               "start": dates[w["start_i"]], "span": n - 1 - w["start_i"],
               "gain": (w["whigh"] / w["entry"] - 1) * 100}
    return waves, cur


def percentile_rank(x, pool):
    if len(pool) < MIN_POOL:
        return None
    return sum(1 for v in pool if v < x) / len(pool)


def wave_regime(gain):
    """按浮动盈亏方向分区：UP=涨 wave，DN=跌 wave。"""
    if gain > 0.01:
        return "UP"
    if gain < -0.01:
        return "DN"
    return "FLAT"


def survival(pool_entries, T,
             horizons=(2, 4, 6, 8, 12, 16, 20, 28, 36, 44, 52, 65, 80, 100)):
    """条件生存：已知当前 wave 已活 T 日，还剩多久。
    pool_entries: list of (span_days, gain_pct)。
    分区：先按 wave 方向(UP/DN/FLAT)拆池，再对存活集算分位数。
    """
    by_regime = {"UP": [], "DN": [], "FLAT": [], "ALL": []}
    for span, gain in pool_entries:
        by_regime["ALL"].append(span)
        by_regime[wave_regime(gain)].append(span)

    out = {}
    for regime, spans in by_regime.items():
        S = sorted(s for s in spans if s >= T)
        if len(S) < 8:
            out[regime] = None
            continue
        rem = sorted(s - T for s in S)
        # 条件生存分位数（下标即百分位：rem_p10 = 10% 分位）
        rec = {
            "n_cond": len(S),
            "rem_p10": rem[int(len(rem) * 0.10)],
            "rem_p25": rem[int(len(rem) * 0.25)],
            "rem_p50": rem[len(rem) // 2],
            "rem_p75": rem[int(len(rem) * 0.75)],
            "rem_p90": rem[int(len(rem) * 0.90)],
        }
        for h in horizons:
            rec[f"P{h}"] = round(
                sum(1 for s in S if s >= T + h) / len(S) * 100)
        out[regime] = rec
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--entry", type=int, default=20)
    ap.add_argument("--exit", type=int, default=3)
    ap.add_argument("--min-bars", type=int, default=500)
    ap.add_argument("--alive-since", default="2026-06-01",
                    help="最后一根 bar 早于此日期的死标的，其未闭合 wave 不算『当前』")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    if a.all:
        by = load_bars()
    elif a.sample:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        allc = [r[0] for r in c.execute(
            "SELECT code FROM daily_bar GROUP BY code HAVING COUNT(*) >= ?",
            (a.min_bars,))]
        c.close()
        random.seed(42)
        by = load_bars(random.sample(allc, min(a.sample, len(allc))))
    elif a.codes:
        by = load_bars(a.codes.split(","))
    else:
        print("需要 --codes / --sample / --all 之一")
        sys.exit(1)

    tag = a.out or f"var/bt/wave_N{a.entry}_M{a.exit}"
    wf = ROOT / f"{tag}_waves.csv"
    sf = ROOT / f"{tag}_summary.csv"

    wrows = []
    srows = []
    all_spans = []
    alive = 0
    for code, bars in by.items():
        if len(bars) < a.min_bars:
            continue
        waves, cur = detect_waves(bars, a.entry, a.exit)
        if not waves and not cur:
            continue
        is_live = bars[-1][0] >= a.alive_since
        if cur and not is_live:
            cur = None  # 死标的的截断 wave：既不入当前、也不入历史池（寿命被截断，失真）
        pool = [(w["span"], w["gain"]) for w in waves]
        all_spans.extend(w["span"] for w in waves)
        for k, w in enumerate(waves, 1):
            wrows.append([code, k, w["start"], w["end"], w["span"],
                          f"{w['entry']:.2f}", f"{w['exit']:.2f}",
                          f"{w['gain']:.1f}", f"{w['ret']:.1f}"])
        rank = pct = None
        surv = None
        if cur:
            alive += 1
            rank = percentile_rank(
                cur["span"], [s for s, _ in pool])
            surv = survival(pool, cur["span"])
        pool_sorted = sorted(s for s, _ in pool)
        srows.append([
            code, len(waves),
            pool_sorted[len(pool) // 2] if pool else None,
            pool_sorted[0] if pool else None,
            pool_sorted[-1] if pool else None,
            cur["start"] if cur else "",
            cur["span"] if cur else None,
            f"{cur['gain']:.1f}" if cur else "",
            "" if rank is None else f"{rank:.3f}",
            surv or {}])

    with wf.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "seq", "start", "end", "span_days", "entry",
                    "exit", "max_gain_pct", "close_ret_pct"])
        w.writerows(wrows)
    with sf.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "n_waves", "life_p50", "life_min", "life_max",
                    "cur_start", "cur_span", "cur_gain_pct",
                    "cur_rank_pct", "survival"])
        for r in srows:
            w.writerow(r[:-1] + [str(r[-1]) if r[-1] else ""])

    # ---- 控制台汇总 ----
    print(f"参数: entry={a.entry} exit={a.exit} floor=0  标的数={len(srows)}  "
          f"UP wave 总数={len(all_spans)}  当前存活 wave={alive}")
    if all_spans:
        s = sorted(all_spans)
        n = len(s)
        print(f"全市场 UP wave 寿命分布(日): "
              f"p10={s[int(n*.1)]} p25={s[int(n*.25)]} p50={s[n//2]} "
              f"p75={s[int(n*.75)]} p90={s[int(n*.9)]} max={s[-1]}")
    print(f"明细: {wf}")
    print(f"汇总: {sf}")

    # ---- 打印有当前 wave 的前 20 个标的的完整判读 ----
    shown = 0
    for r in srows:
        if shown >= 20:
            break
        if not r[5]:
            continue
        code, nw, p50, lmin, lmax, cs, ct, cg, rk, sv = r
        print(f"\n[{code}] 历史 {nw} 段 wave  寿命中位 {p50} 日 "
              f"(min {lmin} / max {lmax})")
        print(f"  当前 wave: {cs} 起 已 {ct} 日  浮盈高点 {cg}%  "
              f"历史排名 {rk if rk else f'样本不足{MIN_POOL}'}")
        if sv:
            cur_regime = wave_regime(float(cg) if cg else 0.0)
            rec = sv.get(cur_regime) or sv.get("ALL")
            if rec:
                print(f"  当前为{cur_regime} wave。条件生存(同方向池 n={rec['n_cond']}): "
                      f"中位 +{rec['rem_p50']} 日 "
                      f"(p25~p75: +{rec['rem_p25']}~+{rec['rem_p75']})")
                line = "  ".join(
                    f">={h}日:{rec[f'P{h}']}%" for h in
                    (2, 4, 6, 8, 12, 16, 20, 28, 36, 44))
                print(f"  {line}")
            dn = sv.get("DN")
            if cur_regime != "DN" and dn:
                print(f"  [对照] 若转为DN wave: 中位 +{dn['rem_p50']} 日, "
                      f"P>=20日:{dn['P20']}%")
        shown += 1


if __name__ == "__main__":
    main()
