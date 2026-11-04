#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tqcenter 退市股可拉性 + 同源价格校验（一次性探针）。

Q1 已退市股（不在 get_stock_list("5") 里）能否按代码直接拉？
Q2 现存股 tqcenter none 价 vs lday 原始价是否一致？

用法：python scripts/tq_probe6.py
"""
import sqlite3
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins\sys")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins")

from tqcenter import tq  # noqa: E402

# 已退市样本：邯郸钢铁(2009退)、齐鲁石化(2006退)、武钢股份(2016退)、
# 上海家化前身? 用确定的：600001 邯郸钢铁 / 600002 齐鲁石化 / 600005 武钢股份
DELISTED = ["600001.SH", "600002.SH", "600005.SH", "600065.SH"]
ALIVE = ["600008.SH", "600519.SH", "000001.SZ", "002251.SZ", "300750.SZ"]


def pull(codes, dt, start="20050101", end="20181231"):
    df = tq.get_market_data(
        field_list=[], stock_list=codes,
        start_time=start, end_time=end, count=-1,
        dividend_type=dt, period="1d", fill_data=False,
    )
    return df if isinstance(df, dict) else {}


def main():
    tq.initialize(__file__)

    print("===== Q1 退市股可拉性 =====")
    for c in DELISTED:
        df = pull([c], "none")
        if "Close" in df and c in df["Close"]:
            s = df["Close"][c].dropna()
            if len(s):
                print(f"  {c}: ✅ {len(s)} 根  {s.index[0].date()} ~ {s.index[-1].date()}  末值 {s.iloc[-1]}")
            else:
                print(f"  {c}: ⚠️ 返回空序列")
        else:
            print(f"  {c}: ❌ 无数据  keys={list(df.keys())[:6]}")

    print("\n===== Q1b 退市股 back 模式 =====")
    dfb = pull(DELISTED, "back")
    if "Close" in dfb:
        for c in DELISTED:
            if c in dfb["Close"]:
                s = dfb["Close"][c].dropna()
                print(f"  {c}: {'✅ ' + str(len(s)) + ' 根' if len(s) else '⚠️ 空'}")

    print("\n===== Q2 同源价格校验（现存股，2016-05-20）=====")
    db = sqlite3.connect("file:var/bt/full_hist_10pct.db?mode=ro", uri=True)
    dfn = pull(ALIVE, "none", "20160520", "20160520")
    for t in ALIVE:
        code = t[:2].lower() + t[3:]
        row = db.execute(
            "SELECT close FROM daily_bar WHERE code=? AND date='2016-05-20'", (code,)
        ).fetchone()
        tv = None
        if "Close" in dfn and t in dfn["Close"]:
            s = dfn["Close"][t].dropna()
            tv = float(s.iloc[0]) if len(s) else None
        print(f"  {code}: lday={row[0] if row else None}  tq={tv}")

    print("\n===== Q2b 全历史根数对比（存续 5 年以上的股）=====")
    dfn2 = pull(["600519.SH", "000001.SZ"], "none", "20050101", "20181231")
    for t in ["600519.SH", "000001.SZ"]:
        code = t[:2].lower() + t[3:]
        n = db.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date BETWEEN '2005-01-01' AND '2018-12-31'",
            (code,),
        ).fetchone()[0]
        tv = len(dfn2["Close"][t].dropna()) if "Close" in dfn2 and t in dfn2["Close"] else 0
        print(f"  {code}: lday={n} 根  tq={tv} 根")

    db.close()
    tq.close()


if __name__ == "__main__":
    main()
