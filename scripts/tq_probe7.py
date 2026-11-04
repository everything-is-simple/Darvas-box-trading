#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""退市股数据缺口定性与替代路径验证（一次性探针）。

1. 退市股的 get_divid_factors 是否可取（可否自算复权因子）
2. 修正代码转换后，tqcenter none 价 vs lday 原始价同源性
3. 退市股在现有库中的样本量级

用法：python scripts/tq_probe7.py
"""
import sqlite3
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins\sys")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins")

from tqcenter import tq  # noqa: E402

DELISTED = ["600001.SH", "600002.SH", "600005.SH", "600065.SH", "600087.SH", "000003.SZ"]
ALIVE = ["600008.SH", "600519.SH", "000001.SZ", "002251.SZ"]


def to_local(t):
    num, mk = t.split(".")
    return mk.lower() + num


def main():
    tq.initialize(__file__)

    print("===== Q1 退市股 get_divid_factors =====")
    for c in DELISTED:
        try:
            dv = tq.get_divid_factors(c, "", "")
            n = len(dv) if dv is not None else 0
            print(f"  {c}: {'✅ ' + str(n) + ' 条' if n else '空'}")
        except Exception as e:  # noqa: BLE001
            print(f"  {c}: 异常 {e}")

    print("\n===== Q2 同源价格校验（现存股 2016-05-20，修正代码后）=====")
    codes = [c for c in ALIVE]
    dfn = tq.get_market_data(
        field_list=["Close"], stock_list=codes,
        start_time="20160520", end_time="20160520", count=-1,
        dividend_type="none", period="1d", fill_data=False,
    )
    db = sqlite3.connect("file:var/bt/full_hist_10pct.db?mode=ro", uri=True)
    ok = 0
    for t in codes:
        lc = to_local(t)
        row = db.execute(
            "SELECT close FROM daily_bar WHERE code=? AND date='2016-05-20'", (lc,)
        ).fetchone()
        s = dfn["Close"][t].dropna() if "Close" in dfn else []
        tv = float(s.iloc[0]) if len(s) else None
        same = "✓" if (row and tv is not None and abs(row[0] - tv) < 0.011) else "✗"
        if same == "✓":
            ok += 1
        print(f"  {lc}: lday={row[0] if row else None}  tq={tv}  {same}")
    print(f"  一致 {ok}/{len(codes)}")

    print("\n===== Q3 退市股在现有库中的样本量 =====")
    # 用 lday 有、tqcenter 无的 339 只，看它们在 2005-2018 里的 bar 数
    tot = db.execute(
        "SELECT COUNT(*) FROM daily_bar WHERE date BETWEEN '2005-01-01' AND '2018-12-31'"
    ).fetchone()[0]
    print(f"  全库 2005-2018 bar 数: {tot:,}")
    for lc in ["sh600001", "sh600002", "sh600005", "sh600087"]:
        n = db.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM daily_bar WHERE code=? AND date BETWEEN '2005-01-01' AND '2018-12-31'",
            (lc,),
        ).fetchone()
        print(f"  {lc}: {n[0]} 根  {n[1]} ~ {n[2]}")

    print("\n===== Q4 退市股是否在 dividend_event 里有记录 =====")
    for lc in ["sh600001", "sh600002", "sh600005", "sh600087"]:
        n = db.execute("SELECT COUNT(*) FROM dividend_event WHERE code=?", (lc,)).fetchone()[0]
        print(f"  {lc}: {n} 条")

    db.close()
    tq.close()


if __name__ == "__main__":
    main()
