#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘趋势识别：上证指数每日涨幅 套四组双均线的方向/速度/加速度。

用户规格（2026-11-07）：底层 = 上证指数(sh000001)每日涨幅，套四组双均线：
  组1 MA5/MA20  组2 MA10/MA30  组3 MA30/MA50  组4 MA50/MA200
  每组算两线三阶量：方向(sign 日差)、速度(5日斜率)、加速度(速度的5日差分)。

只读 full_hist_adj.db。输出 CSV + 三个熊市前后对比表。
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "full_hist_adj.db"
OUT = ROOT / "var" / "bt" / "shindex_curve_groups.csv"

VEL_K = 5
GROUPS = [("g1", 5, 20), ("g2", 10, 30), ("g3", 30, 50), ("g4", 50, 200)]
BEARS = [
    ("2007-10-15", "2008-12-01", "熊市① 2008 阴跌"),
    ("2015-04-15", "2015-07-15", "熊市② 2015-06 股灾"),
    ("2018-01-01", "2018-12-31", "熊市③ 2018 阴跌"),
]


def load_shindex():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = """
    SELECT date, close FROM daily_bar
    WHERE code='sh000001' AND date BETWEEN '2005-01-01' AND '2018-12-31'
    ORDER BY date
    """
    rows = c.execute(q).fetchall()
    c.close()
    dates = [r[0] for r in rows]
    closes = [r[1] for r in rows]
    ret = [None] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    return dates, ret


def rolling_ma(vals, n):
    out = [None] * len(vals)
    for i in range(len(vals)):
        w = vals[i + 1 - n:i + 1] if i + 1 >= n else []
        if len(w) < n or any(v is None for v in w):
            continue
        out[i] = sum(w) / n
    return out


def diff(seq, k):
    out = [None] * len(seq)
    for i in range(k, len(seq)):
        if seq[i] is None or seq[i - k] is None:
            continue
        out[i] = seq[i] - seq[i - k]
    return out


def sign(seq):
    out = [None] * len(seq)
    for i in range(len(seq)):
        if seq[i] is None:
            continue
        out[i] = 1 if seq[i] > 0 else (-1 if seq[i] < 0 else 0)
    return out


def main():
    dates, ret = load_shindex()
    print(f"上证指数 {len(dates)} 日，{dates[0]} ~ {dates[-1]}")

    header = ["date", "ret"]
    cols = {"date": dates, "ret": ret}
    for gid, na, nb in GROUPS:
        ma_a = rolling_ma(ret, na)
        ma_b = rolling_ma(ret, nb)
        dir_a = sign(diff(ma_a, 1))
        dir_b = sign(diff(ma_b, 1))
        vel_a = diff(ma_a, VEL_K)
        vel_b = diff(ma_b, VEL_K)
        acc_a = diff(vel_a, VEL_K)
        acc_b = diff(vel_b, VEL_K)
        for suf, seq in [
            (f"ma{na}", ma_a), (f"ma{nb}", ma_b),
            (f"dir{na}", dir_a), (f"dir{nb}", dir_b),
            (f"vel{na}", vel_a), (f"vel{nb}", vel_b),
            (f"acc{na}", acc_a), (f"acc{nb}", acc_b),
        ]:
            cols[f"{gid}_{suf}"] = seq
            header.append(f"{gid}_{suf}")

    with OUT.open("w", encoding="utf-8") as f:
        f.write(",".join(header) + "\n")
        for i in range(len(dates)):
            row = []
            for h in header:
                v = cols[h][i]
                if v is None:
                    row.append("")
                elif isinstance(v, float):
                    row.append(f"{v:.6f}")
                else:
                    row.append(str(v))
            f.write(",".join(row) + "\n")
    print(f"已写 {OUT}")

    for s, e, name in BEARS:
        idx = [i for i, d in enumerate(dates) if s <= d <= e]
        if not idx:
            continue
        print(f"\n### {name}  [{s} ~ {e}]  {len(idx)} 天，每5日一行")
        picks = idx[::5]
        # 大宽表：每组 5 列（同异 / vel快 / vel慢 / acc快 / acc慢），4 组共 20 列
        head = f"{'日期':<11}"
        for gid, na, nb in GROUPS:
            head += f"| {gid} 同异 {'vel快':>7} {'vel慢':>7} {'acc快':>7} {'acc慢':>7} "
        print(head)
        sep = "-" * 11 + "|" + "-" * 13 * len(GROUPS)
        print(sep)
        for j in picks:
            row = f"{dates[j]:<11}"
            for gid, na, nb in GROUPS:
                da = cols[f"{gid}_dir{na}"][j]
                db = cols[f"{gid}_dir{nb}"][j]
                va = cols[f"{gid}_vel{na}"][j]
                vb = cols[f"{gid}_vel{nb}"][j]
                aa = cols[f"{gid}_acc{na}"][j]
                ab = cols[f"{gid}_acc{nb}"][j]
                if da is None or db is None:
                    row += "| " + "— " * 5 + " "
                    continue
                same = "同↑" if (da == db == 1) else ("同↓" if (da == db == -1) else ("异" if (da != 0 and db != 0) else "—"))
                def f3(x, mul=100):
                    return f"{x*mul:>7.3f}" if x is not None else "      —"
                row += f"| {same:>4} {f3(va)} {f3(vb)} {f3(aa)} {f3(ab)} "
            print(row)


if __name__ == "__main__":
    main()
