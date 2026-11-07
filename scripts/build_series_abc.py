#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建三条底层序列（B 打底 / C 确认 / A 对照），只读 full_hist_adj.db。

B = 站上 MA20 的股票占比（%），有界 0~100，衡量「盛不盛」
C = 跌破自己箱体下轨的股票占比（%）：
      c20 = 收盘 < 前 20 日最低价（不含当日）
      c60 = 收盘 < 前 60 日最低价（不含当日）
    衡量「破没破」
A = 等权指数日收益（对照组）

过滤：volume>0（停牌剔除）、|r|<50%（异常剔除）。
预热期从 2004-06-01 起，输出从 2005-01-01 起。
"""
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "full_hist_adj.db"
OUT = ROOT / "var" / "bt" / "series_abc.csv"

START = "2004-06-01"
END = "2018-12-31"


def q_conn():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def build_b():
    """站上 MA20 占比。"""
    sql = """
    WITH t AS (
      SELECT code, date, close,
        AVG(close) OVER (PARTITION BY code ORDER BY date
                         ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
        COUNT(*) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS n
      FROM daily_bar
      WHERE date BETWEEN ? AND ? AND volume > 0
    )
    SELECT date,
           SUM(CASE WHEN close > ma20 THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
           COUNT(*)
    FROM t WHERE n = 20 GROUP BY date ORDER BY date
    """
    c = q_conn()
    rows = c.execute(sql, (START, END)).fetchall()
    c.close()
    return {d: (b, n) for d, b, n in rows}


def build_c():
    """跌破前 N 日最低价占比。"""
    sql = """
    WITH t AS (
      SELECT code, date, close,
        MIN(low) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS lo20,
        MIN(low) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS lo60,
        COUNT(*) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING) AS n60
      FROM daily_bar
      WHERE date BETWEEN ? AND ? AND volume > 0
    )
    SELECT date,
           SUM(CASE WHEN close < lo20 THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
           SUM(CASE WHEN close < lo60 THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
           COUNT(*)
    FROM t WHERE n60 = 60 GROUP BY date ORDER BY date
    """
    c = q_conn()
    rows = c.execute(sql, (START, END)).fetchall()
    c.close()
    return {d: (c20, c60, n) for d, c20, c60, n in rows}


def build_a():
    """等权指数日收益。"""
    sql = """
    WITH t AS (
      SELECT code, date,
        close / LAG(close) OVER (PARTITION BY code ORDER BY date) - 1 AS r
      FROM daily_bar
      WHERE date BETWEEN ? AND ? AND volume > 0
    )
    SELECT date, AVG(r), COUNT(*)
    FROM t WHERE r IS NOT NULL AND r > -0.5 AND r < 0.5
    GROUP BY date ORDER BY date
    """
    c = q_conn()
    rows = c.execute(sql, (START, END)).fetchall()
    c.close()
    return {d: (r, n) for d, r, n in rows}


def main():
    print("B 站上MA20占比 ...", flush=True)
    b = build_b()
    print(f"  {len(b)} 天", flush=True)
    print("C 跌破箱体下轨占比 ...", flush=True)
    cmap = build_c()
    print(f"  {len(cmap)} 天", flush=True)
    print("A 等权收益 ...", flush=True)
    amap = build_a()
    print(f"  {len(amap)} 天", flush=True)

    dates = sorted(set(b) & set(cmap) & set(amap))
    dates = [d for d in dates if d >= "2005-01-01"]
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "b_pct", "c20_pct", "c60_pct", "a_ret", "n_stock"])
        for d in dates:
            bb, bn = b[d]
            c20, c60, cn = cmap[d]
            ar, an = amap[d]
            w.writerow([d, f"{bb:.4f}", f"{c20:.4f}", f"{c60:.4f}", f"{ar:.6f}", cn])
    print(f"已写 {OUT}  共 {len(dates)} 天  {dates[0]} ~ {dates[-1]}", flush=True)


if __name__ == "__main__":
    main()
