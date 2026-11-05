#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E19-30 零回测：滚动振幅闸门的可实施性预判。

问题：年振幅与年度盈亏 rho=+0.857（docs/55 §13.5），但年振幅要到年底才知道，不可实施。
本脚本用**滚动 N 日振幅**（当日可算）替代，检验：
  1) 滚动振幅与年振幅的相关性（信息损失多少）
  2) 按入场日的滚动振幅分档，各档交易的胜率/均笔/总账梯度是否单调
  3) 若在滚动振幅低于阈值时停买，反事实总账如何变化

口径：指数用 sh000001（daily_bar，raw 指数无复权问题）；个股盈亏取已平仓 realized_pnl。
只读，不写库。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_DB = Path("var/bt/base/bt_baseA_cont_adj.db")


def load(con):
    c = con.cursor()
    idx = c.execute(
        "select date, high, low, close from daily_bar where code='sh000001' order by date"
    ).fetchall()
    trades = c.execute(
        "select id, code, cast(opened_date as text), cast(closed_date as text), "
        "coalesce(realized_pnl,0) from position where status='closed'"
    ).fetchall()
    return idx, trades


def rolling_amp(idx, win: int) -> dict[str, float]:
    """滚动 win 日振幅 = (win 日内最高 high / 最低 low − 1)。当日即可算。"""
    out: dict[str, float] = {}
    hs: list[float] = []
    ls: list[float] = []
    from collections import deque
    hq: deque = deque()
    lq: deque = deque()
    for d, h, l, _c in idx:
        hq.append((d, h))
        lq.append((d, l))
        if len(hq) > win:
            hq.popleft()
            lq.popleft()
        if len(hq) == win:
            hi = max(x[1] for x in hq)
            lo = min(x[1] for x in lq)
            if lo > 0:
                out[d] = 100.0 * (hi / lo - 1)
    return out


def spearman(a: list[float], b: list[float]) -> float:
    def rank(x):
        s = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        i = 0
        while i < len(s):
            j = i
            while j + 1 < len(s) and x[s[j + 1]] == x[s[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[s[k]] = avg
            i = j + 1
        return r

    ra, rb = rank(a), rank(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((x - mb) ** 2 for x in rb) ** 0.5
    return num / (da * db) if da * db else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"库不存在: {db}", file=sys.stderr)
        return 1
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    idx, trades = load(con)
    if not idx or not trades:
        print("数据不足", file=sys.stderr)
        return 1

    # --- 年振幅（后验基准，用于对照） ---
    byy = defaultdict(list)
    for d, h, l, _c in idx:
        byy[int(d[:4])].append((h, l))
    yr_amp = {}
    for y, rows in byy.items():
        hi = max(r[0] for r in rows)
        lo = min(r[1] for r in rows)
        if lo > 0 and len(rows) >= 60:
            yr_amp[y] = 100.0 * (hi / lo - 1)

    # --- 滚动振幅 ---
    ramp = rolling_amp(idx, args.window)
    idx_dates = sorted(ramp)

    import bisect

    def ramp_at(d: str) -> float | None:
        i = bisect.bisect_right(idx_dates, d) - 1
        return ramp[idx_dates[i]] if i >= 0 else None

    # 每个交易日的滚动振幅，与其"未来 250 日"的盈亏无关，直接归到入场日
    rows = []
    for tid, code, o, cl, pnl in trades:
        a = ramp_at(o)
        if a is None:
            continue
        rows.append((o, a, pnl))

    print(f"=== 滚动 {args.window} 日振幅（当日可算） vs 年振幅（后验）===")
    ys = sorted(yr_amp)
    yv = [yr_amp[y] for y in ys]
    # 每年用"该年 12-31 的滚动值"近似比较
    rv = []
    keep = []
    for y in ys:
        d = f"{y}-12-31"
        near = [x for x in idx_dates if x.startswith(str(y))]
        if not near:
            continue
        v = ramp_at(near[-1])
        if v is None:
            continue
        rv.append(v)
        keep.append(y)
    yv2 = [yr_amp[y] for y in keep]
    rho = spearman(yv2, rv)
    print(f"  年末滚动振幅 vs 年振幅  Spearman rho = {rho:+.3f}  (n={len(keep)} 年)")
    print(f"  读数：滚动值普遍小于年振幅（窗口短），但秩相关 {rho:+.3f}，"
          f"{'可替代' if rho > 0.7 else '信息损失较大，需谨慎'}")

    # --- 按滚动振幅分档 ---
    print()
    print(f"=== 按【入场日滚动振幅】分档（n={len(rows)} 笔）===")
    bands = [(0, 15, "<15%"), (15, 20, "15–20%"), (20, 25, "20–25%"),
             (25, 30, "25–30%"), (30, 40, "30–40%"), (40, 999, ">40%")]
    print(f"{'档':<10}{'笔数':>6}{'占比%':>7}{'胜率%':>7}{'总账':>12}{'均笔':>9}{'PF':>6}")
    for lo, hi, lab in bands:
        sub = [p for _d, a, p in rows if lo <= a < hi]
        if not sub:
            print(f"{lab:<10}{0:>6}")
            continue
        w = [p for p in sub if p > 0]
        gw = sum(w)
        gl = -sum(p for p in sub if p <= 0)
        pf = gw / gl if gl > 0 else float("inf")
        print(f"{lab:<10}{len(sub):>6}{100*len(sub)/len(rows):>7.1f}"
              f"{100*len(w)/len(sub):>7.1f}{sum(sub):>12,.0f}"
              f"{sum(sub)/len(sub):>9,.0f}{pf:>6.2f}")

    # --- 反事实：低于阈值停买 ---
    print()
    print("=== 反事实：滚动振幅 < 阈值 时停买（仅算术，不含槽位再利用）===")
    base = sum(p for _d, _a, p in rows)
    print(f"  基线（全部成交）总账 = {base:,.0f}")
    print(f"{'阈值':>8}{'删笔数':>8}{'删掉盈亏':>12}{'新总账':>12}{'变化':>10}")
    for th in [15, 18, 20, 22, 25, 28, 30]:
        cut = [p for _d, a, p in rows if a < th]
        newb = base - sum(cut)
        print(f"{th:>7}%{len(cut):>8}{sum(cut):>12,.0f}{newb:>12,.0f}{newb-base:>10,.0f}")

    # --- 单调性检查 ---
    print()
    means = []
    for lo, hi, lab in bands:
        sub = [p for _d, a, p in rows if lo <= a < hi]
        if len(sub) >= 30:
            means.append((lab, sum(sub) / len(sub), len(sub)))
    print("=== 梯度单调性（仅取 n≥30 的档）===")
    ok = True
    for i in range(1, len(means)):
        arrow = "↑" if means[i][1] > means[i - 1][1] else "↓"
        if means[i][1] < means[i - 1][1]:
            ok = False
        print(f"  {means[i-1][0]:>8} → {means[i][0]:<8} {arrow}  "
              f"({means[i-1][1]:,.0f} → {means[i][1]:,.0f})")
    print(f"  → {'单调递增，E19-30 立项成立' if ok else '存在反向跳，闸门不可直接单调使用，须改二值（停/不停）'}")

    if args.json:
        import json
        out = {
            "window": args.window,
            "rho_year_end_rolling_vs_year_amp": round(rho, 3),
            "bands": [{"label": lab, "n": len([p for _d, a, p in rows if lo <= a < hi]),
                       "sum": round(sum(p for _d, a, p in rows if lo <= a < hi))}
                      for lo, hi, lab in bands],
            "cumulative": [{"th": th,
                            "n_cut": len([p for _d, a, p in rows if a < th]),
                            "sum_cut": round(sum(p for _d, a, p in rows if a < th))}
                           for th in [15, 18, 20, 22, 25, 28, 30]],
        }
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nJSON → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
