#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 lifespan 建库：月/周两套波库（66 框架 §3/§4 落地）。

数据口径：
  源 = var/bt/full_hist_adj.db 的 daily_bar_adj（后复权，data_meta adj_ready=1），
  只读；绝不写回 darvas-box 库。
  产出 = var/lifespan/lifespan.db（独立库，与 darvas-box 库分离，用户 2026-11-07 拍板）。

每只票、每个周期跑同一个对称状态机（66 §2）：
  盘整(BOX) → 收盘突破箱顶 → UP 波 → 收盘跌破前 M 根低点轨 → 破位 → BOX
  盘整(BOX) → 收盘跌破箱底 → DN 波 → 收盘升破前 M 根高点轨 → 破位 → BOX
周/月 bar 由日线聚合（周 = 本周最后一交易日收盘定价，月 = 本月最后一交易日收盘定价，
标签日期即该根 bar 可知日期，天然防前视）。

周波打「顺势段/回调段」标签（66 §4.1 防前视口径）：
  月线方向 = 月线波机器在该周波诞生日**已收盘确定**的状态（mdir 数组 = 月 bar 收盘后状态），
  只用 date <= 周波诞生日的月 bar；不用事后结果、不用均线、不用指数。
    月涨 + 周 UP → 顺势多（唯一开多池）    月涨 + 周 DN → 回调跌（66 §4.2 回调段池）
    月跌 + 周 DN → 顺势空（A 股观望）      月跌 + 周 UP → 回调弹（反弹，不碰）

统计口径（66 §6 铁律）：
  三库不混池（月/周独立建表）；顺势/回调分池独立排名；
  衰老度严格小于（<），样本 N<30 时留空不猜。

用法：
  python scripts/lifespan_build.py --sample 300          # 小样本，只出分池概率表（不建库）
  python scripts/lifespan_build.py --all --write-db      # 全市场，写 var/lifespan/lifespan.db
"""
import argparse
import csv
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DB = ROOT / "var" / "bt" / "full_hist_adj.db"
OUT_DIR = ROOT / "var" / "lifespan"
OUT_DB = OUT_DIR / "lifespan.db"

MIN_POOL = 30  # 66 §6 铁律4


# ---------------------------------------------------------------- 数据加载

def load_adj_bars(codes=None, chunk_all=False):
    """读 daily_bar_adj（后复权），按 code 分组升序返回 {code: [(date, o, h, l, c)]}。"""
    c = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    c.execute("PRAGMA temp_store=MEMORY")
    if chunk_all:
        by = {}
        # 按首字母分段查询，控内存峰值（长跑期跑全量时的保守做法）
        for lo in "0123456789abcdefghij":
            rows = c.execute(
                "SELECT code, date, adj_open, adj_high, adj_low, adj_close "
                "FROM daily_bar_adj WHERE substr(code,3,1) BETWEEN ? AND ? "
                "AND adj_volume > 0 ORDER BY code, date", (lo, lo)).fetchall()
            for code, d, o, h, l, cl in rows:
                by.setdefault(code, []).append((d, o, h, l, cl))
    else:
        ph = ",".join("?" * len(codes))
        rows = c.execute(
            f"SELECT code, date, adj_open, adj_high, adj_low, adj_close "
            f"FROM daily_bar_adj WHERE code IN ({ph}) AND adj_volume > 0 "
            f"ORDER BY code, date", codes).fetchall()
        by = defaultdict(list)
        for code, d, o, h, l, cl in rows:
            by[code].append((d, o, h, l, cl))
    c.close()
    return dict(by)


# ---------------------------------------------------------------- bar 聚合

def resample(bars, mode):
    """日线 → 周/月 bar。bar 日期 = 期内最后一交易日（该根 bar 收盘可知日，防前视）。
    周键 = ISO 年-周；月键 = 年-月。"""
    def key(d):
        y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
        if mode == "month":
            return (y, m)
        iso = _iso_week(y, m, dd)
        return iso

    out = []
    cur_k, o, h, l, cl, last_d = None, None, None, None, None, None
    for d, op, hi, lo, cl_ in bars:
        k = key(d)
        if k != cur_k:
            if cur_k is not None:
                out.append((last_d, o, h, l, cl))
            cur_k, o, h, l, cl = k, op, hi, lo, cl_
        else:
            h = max(h, hi)
            l = min(l, lo)
            cl = cl_
        last_d = d
    if cur_k is not None:
        out.append((last_d, o, h, l, cl))
    return out


def _iso_week(y, m, d):
    import datetime
    return datetime.date(y, m, d).isocalendar()[:2]


# ---------------------------------------------------------------- 状态机

def detect_waves_sym(bars, N, M):
    """对称状态机（66 §2）：返回已终止波列表 + 未闭合波。
    wave = dict(dir, start_i, end_i, start, end, entry, exit, whigh, wlow, span, gain, ret)
    """
    dates = [b[0] for b in bars]
    high = [b[2] for b in bars]
    low = [b[3] for b in bars]
    close = [b[4] for b in bars]
    n = len(bars)
    waves = []
    state = "BOX"
    w = None
    for i in range(N, n):
        top = max(high[i - N:i])
        bot = min(low[i - N:i])
        if state == "BOX":
            if top > 0 and close[i] > top:
                state, w = "UP", {"dir": "UP", "start_i": i, "entry": close[i],
                                  "whigh": high[i], "wlow": low[i]}
            elif bot > 0 and close[i] < bot:
                state, w = "DN", {"dir": "DN", "start_i": i, "entry": close[i],
                                  "whigh": high[i], "wlow": low[i]}
        elif state == "UP":
            w["whigh"] = max(w["whigh"], high[i])
            w["wlow"] = min(w["wlow"], low[i])
            if close[i] < min(low[i - M:i]):
                _close_wave(waves, w, dates, i, close[i])
                state, w = "BOX", None
        elif state == "DN":
            w["whigh"] = max(w["whigh"], high[i])
            w["wlow"] = min(w["wlow"], low[i])
            if close[i] > max(high[i - M:i]):
                _close_wave(waves, w, dates, i, close[i])
                state, w = "BOX", None
    cur = None
    if w is not None:
        cur = dict(w)
        cur["start"] = dates[w["start_i"]]
        cur["span"] = n - 1 - w["start_i"]
        ref = w["whigh"] if w["dir"] == "UP" else w["wlow"]
        cur["gain"] = (ref / w["entry"] - 1) * 100
        cur["velocity"] = (cur["gain"] / cur["span"]) if cur["span"] > 0 else 0.0
    return waves, cur


def _close_wave(waves, w, dates, i, exit_close):
    w["end_i"] = i
    w["exit"] = exit_close
    w["span"] = i - w["start_i"]
    ref = w["whigh"] if w["dir"] == "UP" else w["wlow"]
    w["gain"] = (ref / w["entry"] - 1) * 100
    w["ret"] = (exit_close / w["entry"] - 1) * 100
    w["velocity"] = (w["gain"] / w["span"]) if w["span"] > 0 else 0.0
    w["start"] = dates[w["start_i"]]
    w["end"] = dates[i]
    waves.append(w)


# ---------------------------------------------------------------- 月方向时间线（防前视标签的依据）

def month_dir_timeline(mbars, N, M):
    """mdir[j] = 第 j 根月 bar **收盘后**的月线方向：+1 UP 波存活 / -1 DN 波存活 / 0 盘整。
    66 §4.1：月线方向纯靠月线箱体突破/破位，周标签只用 date<=周波诞生日的月 bar。"""
    dates = [b[0] for b in mbars]
    high = [b[2] for b in mbars]
    low = [b[3] for b in mbars]
    close = [b[4] for b in mbars]
    n = len(mbars)
    mdir = [0] * n
    state = "BOX"
    for i in range(N, n):
        top = max(high[i - N:i])
        bot = min(low[i - N:i])
        if state == "BOX":
            if top > 0 and close[i] > top:
                state = "UP"
            elif bot > 0 and close[i] < bot:
                state = "DN"
        elif state == "UP":
            if close[i] < min(low[i - M:i]):
                state = "BOX"
        elif state == "DN":
            if close[i] > max(high[i - M:i]):
                state = "BOX"
        mdir[i] = {"UP": 1, "DN": -1, "BOX": 0}[state]
    return dates, mdir


def month_dir_at(mdates, mdir, t):
    """周波诞生日期 t 时点已确定的月线方向（防前视：只看 date<=t 的月 bar 收盘态）。"""
    import bisect
    j = bisect.bisect_right(mdates, t) - 1
    if j < 0:
        return None
    return mdir[j]


def pool_label(mdir_at, wave_dir):
    if mdir_at is None:
        return "未定"
    if mdir_at == 1:
        return "顺势多" if wave_dir == "UP" else "回调跌"
    if mdir_at == -1:
        return "顺势空" if wave_dir == "DN" else "回调弹"
    return "未定"  # 月线盘整期诞生的周波，方向未确立，不归池


def is_grinding(w, vel_p25, min_span_p75):
    """磨 = 过滤判据（66 §1.4 / 铁律7）：wavespan 长 + 涨速低 → 无效标的，过滤。
    定量口径（66 §7.1 待填空，此处给可复算实现）：涨速低于**同池** p25 且 span 高于**同池** p75。
    阈值未回测前仅作标记（列 `grinding`），不删除记录。"""
    return w["velocity"] < vel_p25 and w["span"] > min_span_p75


# ---------------------------------------------------------------- 分池统计

def pct(sorted_v, q):
    if not sorted_v:
        return None
    return sorted_v[min(int(len(sorted_v) * q), len(sorted_v) - 1)]


def pool_stats(pool, name):
    """pool: list of (span, gain)。输出分布摘要（66 §4.2）。"""
    if len(pool) < MIN_POOL:
        return f"  {name}: n={len(pool)} < {MIN_POOL}（铁律4：不出分布）"
    spans = sorted(s for s, _ in pool)
    gains = sorted(g for _, g in pool)
    return (f"  {name}: n={len(pool)}  span p10={pct(spans,.1)} p25={pct(spans,.25)} "
            f"p50={pct(spans,.5)} p75={pct(spans,.75)} p90={pct(spans,.9)} "
            f"| 波幅p50={pct(gains,.5):+.1f}% p25={pct(gains,.25):+.1f}% p75={pct(gains,.75):+.1f}%")


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="随机抽 N 只（小样本，不建库）")
    ap.add_argument("--all", action="store_true", help="全市场")
    ap.add_argument("--codes", default="")
    ap.add_argument("--write-db", action="store_true", help="写 var/lifespan/lifespan.db")
    ap.add_argument("--out", default="", help="CSV 前缀（默认 var/lifespan/l0_sample）")
    # 待填空参数（66 §7）：结构规则同一天机（N 根箱体窗口 / M 根出场轨），按周期给默认
    ap.add_argument("--entry-day", type=int, default=20)
    ap.add_argument("--exit-day", type=int, default=3)
    ap.add_argument("--entry-week", type=int, default=10)
    ap.add_argument("--exit-week", type=int, default=3)
    ap.add_argument("--entry-month", type=int, default=6)
    ap.add_argument("--exit-month", type=int, default=3)
    ap.add_argument("--min-bars", type=int, default=0, help="日线最少根数（默认按周期内部门槛）")
    a = ap.parse_args()

    if not (a.sample or a.all or a.codes):
        print("需要 --sample / --all / --codes 之一")
        sys.exit(1)

    if a.all:
        by = load_adj_bars(chunk_all=True)
    elif a.sample:
        c = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
        allc = [r[0] for r in c.execute(
            "SELECT code FROM daily_bar_adj GROUP BY code HAVING COUNT(*) >= 1000")]
        c.close()
        random.seed(42)
        by = load_adj_bars(random.sample(allc, min(a.sample, len(allc))))
    else:
        by = load_adj_bars(a.codes.split(","))

    min_day = a.min_bars or 500
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = a.out or ("var/lifespan/l0_sample" if a.sample else "var/lifespan/l0_full")

    mrows, wrows = [], []
    pools = defaultdict(list)  # pool_label -> [(span, gain)]（周库）
    month_pools = defaultdict(list)
    n_codes = 0

    for code, bars in by.items():
        if len(bars) < min_day:
            continue
        n_codes += 1
        mbars = resample(bars, "month")
        wbars = resample(bars, "week")
        if len(mbars) < 36 or len(wbars) < 104:
            continue

        mwaves, mcur = detect_waves_sym(mbars, a.entry_month, a.exit_month)
        mdates, mdir = month_dir_timeline(mbars, a.entry_month, a.exit_month)
        for k, w in enumerate(mwaves, 1):
            mrows.append([code, k, w["dir"], w["start"], w["end"], w["span"],
                          f"{w['entry']:.3f}", f"{w['exit']:.3f}",
                          f"{w['gain']:.1f}", f"{w['ret']:.1f}", f"{w['velocity']:.2f}"])
            month_pools[("月" + w["dir"])].append((w["span"], w["gain"]))

        wwaves, wcur = detect_waves_sym(wbars, a.entry_week, a.exit_week)
        # 先打池标签，再按「同池」算涨速 p25 / span p75（防前视：只用已闭合周波，且池内同向可比）
        wlab = []
        for w in wwaves:
            wlab.append(pool_label(month_dir_at(mdates, mdir, w["start"]), w["dir"]))
        _pool_vel = defaultdict(list)
        _pool_span = defaultdict(list)
        for w, lab in zip(wwaves, wlab):
            if lab != "未定":
                _pool_vel[lab].append(w["velocity"])
                _pool_span[lab].append(w["span"])
        vel_p25 = {lab: sorted(v)[int(len(v) * 0.25)] for lab, v in _pool_vel.items() if v}
        span_p75 = {lab: sorted(v)[int(len(v) * 0.75)] for lab, v in _pool_span.items() if v}
        for k, (w, lab) in enumerate(zip(wwaves, wlab), 1):
            grind = False
            if lab != "未定":
                grind = (w["velocity"] < vel_p25.get(lab, 0.0)
                         and w["span"] > span_p75.get(lab, 0))
            wrows.append([code, k, w["dir"], lab, w["start"], w["end"], w["span"],
                          f"{w['entry']:.3f}", f"{w['exit']:.3f}",
                          f"{w['gain']:.1f}", f"{w['ret']:.1f}",
                          f"{w['velocity']:.2f}", 1 if grind else 0])
            if lab != "未定":
                pools[lab].append((w["span"], w["gain"]))

    # ---- CSV ----
    mf = ROOT / f"{tag}_month_waves.csv"
    wf = ROOT / f"{tag}_week_waves.csv"
    with mf.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "seq", "dir", "start", "end", "span_bars",
                    "entry", "exit", "gain_pct", "ret_pct", "velocity_pct_d"])
        w.writerows(mrows)
    with wf.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "seq", "dir", "pool", "start", "end", "span_bars",
                    "entry", "exit", "gain_pct", "ret_pct", "velocity_pct_d",
                    "grinding"])
        w.writerows(wrows)

    # ---- 可选建库（独立 lifespan 库，与 darvas-box 库分离）----
    if a.write_db:
        con = sqlite3.connect(OUT_DB)
        con.executescript("""
        DROP TABLE IF EXISTS wave_month; DROP TABLE IF EXISTS wave_week; DROP TABLE IF EXISTS meta;
        CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE wave_month(
          code TEXT, seq INTEGER, dir TEXT, start TEXT, end TEXT, span_bars INTEGER,
          entry REAL, exit REAL, gain_pct REAL, ret_pct REAL, velocity_pct_d REAL);
        CREATE TABLE wave_week(
          code TEXT, seq INTEGER, dir TEXT, pool TEXT, start TEXT, end TEXT, span_bars INTEGER,
          entry REAL, exit REAL, gain_pct REAL, ret_pct REAL, velocity_pct_d REAL, grinding INTEGER);
        CREATE INDEX idx_wm_code ON wave_month(code);
        CREATE INDEX idx_ww_code ON wave_week(code);
        CREATE INDEX idx_ww_pool ON wave_week(pool);
        """)
        con.executemany("INSERT INTO wave_month VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        [(r[0], r[1], r[2], r[3], r[4], r[5], float(r[6]), float(r[7]),
                          float(r[8]), float(r[9]), float(r[10])) for r in mrows])
        con.executemany("INSERT INTO wave_week VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], float(r[7]), float(r[8]),
                          float(r[9]), float(r[10]), float(r[11]), int(r[12])) for r in wrows])
        con.executemany("INSERT INTO meta VALUES(?,?)", [
            ("built_at", __import__("datetime").datetime.now().isoformat(timespec="seconds")),
            ("source", "full_hist_adj.db daily_bar_adj (后复权, adj_ready=1)"),
            ("price_basis", "后复权"),
            ("entry_day/exit_day", f"{a.entry_day}/{a.exit_day}"),
            ("entry_week/exit_week", f"{a.entry_week}/{a.exit_week}"),
            ("entry_month/exit_month", f"{a.entry_month}/{a.exit_month}"),
            ("pool_rule", "66 §4.1 防前视：月方向=周波诞生日已收盘确定的月线状态"),
            ("grind_rule", "磨=涨速<p25 且 span>p75（66 §7.1 待填空，未回测仅标记）"),
            ("n_codes", str(n_codes)), ("n_month_waves", str(len(mrows))),
            ("n_week_waves", str(len(wrows))),
        ])
        con.commit()
        con.close()
        print(f"已写独立库: {OUT_DB}")

    # ---- 汇总 ----
    print(f"标的数={n_codes}  月波={len(mrows)}  周波={len(wrows)}  "
          f"参数: 月 {a.entry_month}/{a.exit_month} 周 {a.entry_week}/{a.exit_week}（66 §7 待填空）")
    print("\n== 周库分池（66 §4.2，防前视标签）==")
    for lab in ("顺势多", "回调跌", "顺势空", "回调弹"):
        print(pool_stats(pools.get(lab, []), lab))
    print("\n== 月库（主趋势参考）==")
    for lab in ("月UP", "月DN"):
        print(pool_stats(month_pools.get(lab, []), lab))
    print(f"\n明细: {mf}\n      {wf}")


if __name__ == "__main__":
    main()
