#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘趋势识别：涨跌家数差 z-score 曲线 + 双均线组的方向/速度/加速度。

用户规格（2026-11-07）：
  - 底层序列 = 全市场每日 (涨家数 - 跌家数) 的原始差，再对该序列做 z-score（滚动）。
  - 在 z 曲线上套「两组均线」，不是四组。
  - 试 4 种均线组合，找哪组在三个熊市前最灵敏：
      组1 MA5/MA20    组2 MA10/MA30    组3 MA30/MA50    组4 MA50/MA200
  - 每组算两条均线的三阶量：
      方向(direction) = sign(MA_t - MA_{t-1})            —— 同向/异向
      速度(velocity)  = MA_t - MA_{t-k}  (k=5 日斜率)    —— 切线
      加速度(accel)   = velocity_t - velocity_{t-k}      —— 曲率

只读 full_hist_adj.db，输出 CSV + 三个熊市前后对比表。
"""
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "full_hist_adj.db"
OUT = ROOT / "var" / "bt" / "breadth_curve_groups.csv"

Z_WIN = 60        # z-score 滚动窗口（日）
VEL_K = 5         # 速度/斜率的差分跨度（日）

GROUPS = [
    ("g1", 5, 20),
    ("g2", 10, 30),
    ("g3", 30, 50),
    ("g4", 50, 200),
]

BEARS = [
    ("2007-10-15", "2008-12-01", "熊市① 2008 阴跌"),
    ("2015-04-15", "2015-07-15", "熊市② 2015-06 股灾"),
    ("2018-01-01", "2018-12-31", "熊市③ 2018 阴跌"),
]


def load_advdecl():
    """返回 [(date, adv_decl)]，adv_decl = 涨家数 - 跌家数。"""
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = """
    SELECT date,
      SUM(CASE WHEN r>0 THEN 1 ELSE 0 END) AS up,
      SUM(CASE WHEN r<0 THEN 1 ELSE 0 END) AS dn
    FROM (
      SELECT date, close,
        close - LAG(close) OVER (PARTITION BY code ORDER BY date) AS r
      FROM daily_bar
      WHERE date BETWEEN '2005-01-01' AND '2018-12-31'
    )
    GROUP BY date ORDER BY date
    """
    rows = c.execute(q).fetchall()
    c.close()
    out = []
    for date, up, dn in rows:
        if up + dn == 0:
            continue
        out.append((date, up - dn))
    return out


def rolling_z(vals, n):
    """对 vals 序列做滚动 z-score（当前值相对过去 n 日的均值/标准差）。"""
    out = [None] * len(vals)
    for i in range(len(vals)):
        if i + 1 < n:
            continue
        w = vals[i + 1 - n:i + 1]
        m = statistics.mean(w)
        s = statistics.pstdev(w)
        out[i] = (vals[i] - m) / s if s > 0 else 0.0
    return out


def rolling_ma(vals, n):
    out = [None] * len(vals)
    for i in range(len(vals)):
        w = vals[i + 1 - n:i + 1] if i + 1 >= n else []
        if len(w) < n or any(v is None for v in w):
            continue
        out[i] = sum(w) / n
    return out


def diff(seq, k):
    """seq_t - seq_{t-k}，前 k 位为 None。"""
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
    data = load_advdecl()
    dates = [d[0] for d in data]
    adv = [d[1] for d in data]
    print(f"有效交易日 {len(dates)} 天，{dates[0]} ~ {dates[-1]}")

    # 底层：A/D 累计线 = 涨跌家数差的累计和（cumsum）
    ad = []
    run = 0
    for v in adv:
        run += v
        ad.append(run)
    z = ad

    # 写 CSV 头
    header = ["date", "adv_decl", "adline"]
    cols = {"date": dates, "adv_decl": adv, "adline": ad}

    # 每个组算两条均线 + 三阶量
    for gid, na, nb in GROUPS:
        ma_a = rolling_ma(z, na)
        ma_b = rolling_ma(z, nb)
        # 方向：日级 sign（用 1 日差分）
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
                    row.append(f"{v:.4f}")
                else:
                    row.append(str(v))
            f.write(",".join(row) + "\n")
    print(f"已写 {OUT}")

    # 三个熊市对比：每组，熊市前快慢两线是否「同向向下 + 速度负 + 加速度负」
    print("\n" + "=" * 100)
    for s, e, name in BEARS:
        idx = [i for i, d in enumerate(dates) if s <= d <= e]
        if not idx:
            continue
        print(f"\n### {name}  [{s} ~ {e}]  {len(idx)} 天")
        # 用每 1/8 采样点打表，看转折
        step = max(1, len(idx) // 8)
        picks = idx[::step]
        for gid, na, nb in GROUPS:
            ma_a = cols[f"{gid}_ma{na}"]
            ma_b = cols[f"{gid}_ma{nb}"]
            vel_b = cols[f"{gid}_vel{nb}"]   # 慢线速度（最稳的趋势量）
            acc_b = cols[f"{gid}_acc{nb}"]   # 慢线加速度
            print(f"\n  [{gid}] MA{na}/MA{nb}")
            print(f"    {'日期':<12}{f'MA{na}':>8}{f'MA{nb}':>8}{f'vel{nb}':>9}{f'acc{nb}':>9}")
            for j in picks:
                va = ma_a[j] if ma_a[j] is not None else float("nan")
                vb = ma_b[j] if ma_b[j] is not None else float("nan")
                vv = vel_b[j] if vel_b[j] is not None else float("nan")
                aa = acc_b[j] if acc_b[j] is not None else float("nan")
                print(f"    {dates[j]:<12}{va:>8.3f}{vb:>8.3f}{vv:>9.4f}{aa:>9.4f}")


if __name__ == "__main__":
    main()
