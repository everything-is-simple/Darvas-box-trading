#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场宽度（breadth）序列构建 —— 攻防转换的状态输入候选。

为什么需要它（2026-11-03 诊断）：
    现有 regime 只有「指数 vs MA60」和「指数距 252 日高点」。两者在两类年份都失灵：
      · 脱钩年（2013 沪弱深强 / 2017 指数强小盘弱）—— 指数好看，内部在坏；
      · 洗耳光年（2018）—— 指数每次反弹站回均线就开门，买完续跌。
    市场宽度 = 当日收盘在 MA50 上方的股票占比，直接看「内部有多少票在涨」，
    理论上能区分「指数涨而多数票跌」的假牛。

产物：CSV（date, breadth50, n），缓存到 var/tmp/breadth50.csv，后续后验复用。

用法：
    python scripts/e17_breadth_build.py
    python scripts/e17_breadth_build.py --db var/bt/base/bt_baseA_2018.db --out var/tmp/breadth50.csv
"""
import argparse
import csv
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "var" / "bt" / "base" / "bt_baseA_2018.db"
DEFAULT_OUT = ROOT / "var" / "tmp" / "breadth50.csv"
START = "2003-01-01"   # 留 50 个交易日预热，2004 起可用
END = "2018-12-31"

SQL = f"""
SELECT date,
       ROUND(SUM(CASE WHEN close > ma50 THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS b50,
       COUNT(*) AS n
FROM (
    SELECT code, date, close,
           AVG(close) OVER (PARTITION BY code ORDER BY date
                            ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS ma50,
           ROW_NUMBER() OVER (PARTITION BY code ORDER BY date) AS rn
    FROM daily_bar
    WHERE date BETWEEN '{START}' AND '{END}' AND close > 0
)
WHERE rn >= 50
GROUP BY date
ORDER BY date
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.execute("PRAGMA temp_store=FILE")
    cur = con.execute(SQL)
    rows = cur.fetchall()
    con.close()

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "breadth50", "n"])
        for d, b, n in rows:
            w.writerow([d[:10] if isinstance(d, str) else d, b, n])

    print(f"宽度序列 {len(rows)} 行 → {out}  耗时 {time.time()-t0:.0f}s")
    if rows:
        print(f"日期范围 {rows[0][0]} ~ {rows[-1][0]}")
        vals = sorted(r[1] for r in rows)
        q = lambda p: vals[int(len(vals) * p)]
        print(f"宽度分位：p10={q(.1):.2f} p25={q(.25):.2f} 中位={q(.5):.2f} "
              f"p75={q(.75):.2f} p90={q(.9):.2f}")


if __name__ == "__main__":
    main()
