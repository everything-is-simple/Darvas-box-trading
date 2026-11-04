#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tqcenter 复权因子逐值核对（一次性探针）。

把同一批日期上 none / front / back / ForwardFactor 四个数排成一张表，
直接看比例关系，不再靠推测。

用法：python scripts/tq_probe3.py
"""
import sys
import warnings

import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins\sys")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins")

from tqcenter import tq  # noqa: E402

CODE = "600008.SH"
DATES = [
    "2000-04-27", "2002-08-22", "2010-07-15", "2016-05-19", "2016-05-20",
    "2016-05-23", "2020-09-28", "2020-09-29", "2026-11-03", "2026-11-04",
]


def grab(dt):
    df = tq.get_market_data(
        field_list=["Close", "ForwardFactor"], stock_list=[CODE],
        start_time="19900101", end_time="20301231", count=-1,
        dividend_type=dt, period="1d", fill_data=False,
    )
    return df["Close"][CODE], df.get("ForwardFactor", pd.DataFrame())


def main():
    tq.initialize(__file__)

    none_c, fdf = grab("none")
    front_c, _ = grab("front")
    back_c, _ = grab("back")
    fac = fdf[CODE] if not fdf.empty else None

    t = pd.DataFrame({"none": none_c, "front": front_c, "back": back_c})
    if fac is not None:
        t["F"] = fac

    print("全序列长度 none/front/back:", len(none_c), len(front_c), len(back_c))
    print("\n===== 定点取样（四列原始值） =====")
    idx = pd.to_datetime([d for d in DATES])
    sub = t.reindex(idx)
    print(sub.to_string())

    print("\n===== 比例关系 =====")
    sub2 = sub.copy()
    sub2["front/none"] = sub2["front"] / sub2["none"]
    sub2["back/none"] = sub2["back"] / sub2["none"]
    sub2["back/front"] = sub2["back"] / sub2["front"]
    if "F" in sub2:
        sub2["F"] = sub2["F"]
        sub2["front/none ÷ F"] = sub2["front/none"] / sub2["F"]
    print(sub2[["front/none", "back/none", "back/front", "F", "front/none ÷ F"]].to_string())

    print("\n===== 首末日 =====")
    for c in ["none", "front", "back", "F"]:
        if c in t:
            s = t[c].dropna()
            print(f"{c}: 首 {s.iloc[0]:.6f} ({s.index[0].date()})  末 {s.iloc[-1]:.6f} ({s.index[-1].date()})")

    print("\n===== front 是否 = none × F/F(末)，逐值比对 =====")
    if fac is not None:
        calc = none_c * fac / fac.iloc[-1]
        j = pd.DataFrame({"calc": calc, "actual": front_c}).dropna()
        j["diff"] = (j["calc"] - j["actual"]).abs()
        print("最大绝对差 %.6f  超过1e-4的点数 %d / %d" % (
            j["diff"].max(), (j["diff"] > 1e-4).sum(), len(j)))
        print(j[j["diff"] > 1e-4].head(8).to_string())

    print("\n===== back 是否 = none × F / 常数，逐值比对 =====")
    if fac is not None:
        ratio = (back_c / none_c).dropna()
        print("back/none 唯一值个数:", ratio.round(6).nunique(), " 首:", round(ratio.iloc[0], 6), " 末:", round(ratio.iloc[-1], 6))

    tq.close()


if __name__ == "__main__":
    main()
