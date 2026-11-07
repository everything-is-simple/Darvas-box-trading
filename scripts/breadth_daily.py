#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘广度三指标：涨跌家数比 / MA10-20 / z-score（零机时验证，只读 full_hist_adj.db）。

指标（全市场每天一个数，用 daily_bar.close 后复权价算涨跌）：
  1. breadth = (涨家数 - 跌家数) / 总家数       —— 涨跌家数比，[-1,1]
  2. MA10/MA20 = breadth 的 10/20 日均线，金叉死叉
  3. z-score = (breadth - 滚动均值) / 滚动标准差（60 日窗口）

验证目标：三样指标能否在三个明确熊市前给出「背离/转弱」信号：
  熊市① 2008 全年；熊市② 2015-06 股灾；熊市③ 2018 全年阴跌。

只读，不写库。输出 CSV 到 var/bt/。
"""
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "full_hist_adj.db"
OUT = ROOT / "var" / "bt" / "breadth_daily.csv"


def load_breadth():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = """
    SELECT date,
      SUM(CASE WHEN r>0 THEN 1 ELSE 0 END) AS up,
      SUM(CASE WHEN r<0 THEN 1 ELSE 0 END) AS dn,
      COUNT(*) AS tot
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
    for date, up, dn, tot in rows:
        if up + dn == 0:  # 首日或无有效涨跌，跳过
            continue
        breadth = (up - dn) / tot
        out.append((date, breadth, up, dn, tot))
    return out


def rolling_ma(vals, n):
    out = []
    for i in range(len(vals)):
        if i + 1 < n:
            out.append(None)
        else:
            out.append(sum(vals[i + 1 - n:i + 1]) / n)
    return out


def rolling_z(vals, n):
    out = []
    for i in range(len(vals)):
        if i + 1 < n:
            out.append(None)
        else:
            w = vals[i + 1 - n:i + 1]
            m = statistics.mean(w)
            s = statistics.pstdev(w)
            out.append((vals[i] - m) / s if s > 0 else 0.0)
    return out


def main():
    data = load_breadth()
    print(f"有效交易日 {len(data)} 天，{data[0][0]} ~ {data[-1][0]}")

    dates = [d[0] for d in data]
    bvals = [d[1] for d in data]
    ma10 = rolling_ma(bvals, 10)
    ma20 = rolling_ma(bvals, 20)
    z60 = rolling_z(bvals, 60)

    # 写 CSV
    with OUT.open("w", encoding="utf-8") as f:
        f.write("date,breadth,up,dn,tot,ma10,ma20,z60\n")
        for i, d in enumerate(data):
            f.write(f"{d[0]},{d[1]:.4f},{d[2]},{d[3]},{d[4]},"
                    f"{'' if ma10[i] is None else round(ma10[i],4)},"
                    f"{'' if ma20[i] is None else round(ma20[i],4)},"
                    f"{'' if z60[i] is None else round(z60[i],4)}\n")
    print(f"已写 {OUT}")

    # 三个熊市前的信号快照
    print("\n=== 熊市① 2008（全年阴跌，取 2007-10 顶部→2008 底） ===")
    snap(dates, bvals, ma10, ma20, z60, "2007-10-15", "2008-11-01")
    print("\n=== 熊市② 2015-06 股灾（前 1 个月到股灾） ===")
    snap(dates, bvals, ma10, ma20, z60, "2015-05-01", "2015-07-01")
    print("\n=== 熊市③ 2018 全年阴跌 ===")
    snap(dates, bvals, ma10, ma20, z60, "2018-01-01", "2018-12-01")


def snap(dates, bvals, ma10, ma20, z60, s, e):
    idx = [i for i, d in enumerate(dates) if s <= d <= e]
    # 每 ~10 天打一个点，看趋势
    step = max(1, len(idx) // 12)
    print(f"  {'日期':<12}{'breadth':>9}{'ma10':>9}{'ma20':>9}{'z60':>8}")
    for j in idx[::step]:
        print(f"  {dates[j]:<12}{bvals[j]:>9.3f}"
              f"{'' if ma10[j] is None else f'{ma10[j]:>9.3f}'}"
              f"{'' if ma20[j] is None else f'{ma20[j]:>9.3f}'}"
              f"{'' if z60[j] is None else f'{z60[j]:>8.2f}'}")


if __name__ == "__main__":
    main()
