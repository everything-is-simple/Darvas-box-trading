#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tqcenter 复权因子语义确认（一次性探针，非生产代码）。

待确认 4 件事：
  Q1 ForwardFactor 的归一化端点（首发日? 最新日?）
  Q2 front 是否等于 none × F / F(最新)
  Q3 back  是否等于 none × F / F(首日)
  Q4 F 是否随未来除权改写（决定能否直接当后复权因子用）

用法：python scripts/tq_probe2.py
"""
import sys
import warnings

import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins\sys")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins")

from tqcenter import tq  # noqa: E402

CODE = "600008.SH"


def grab(dividend_type, start, end):
    df = tq.get_market_data(
        field_list=["Close", "ForwardFactor"], stock_list=[CODE],
        start_time=start, end_time=end, count=-1,
        dividend_type=dividend_type, period="1d", fill_data=False,
    )
    if not isinstance(df, dict) or "Close" not in df:
        return None, None
    return df["Close"][CODE].dropna(), df.get("ForwardFactor", pd.DataFrame())


def main():
    tq.initialize(__file__)

    none_all, f_all = grab("none", "19900101", "20301231")
    if none_all is None:
        print("拉取失败")
        return
    fac = f_all[CODE].dropna() if not f_all.empty else None

    print("===== Q1 ForwardFactor 端点 =====")
    print("样本数:", len(none_all), " 首日:", none_all.index[0].date(), " 末日:", none_all.index[-1].date())
    if fac is not None:
        print("F 首值: %.6f   F 末值: %.6f" % (fac.iloc[0], fac.iloc[-1]))
        print("F 唯一值个数:", fac.nunique())

    # Q2/Q3 端点核对
    front_all, _ = grab("front", "19900101", "20301231")
    back_all, _ = grab("back", "19900101", "20301231")
    print("\n===== Q2/Q3 端点算式核对 =====")
    if fac is not None and front_all is not None and back_all is not None:
        f_last, f_first = fac.iloc[-1], fac.iloc[0]
        calc_front = none_all * fac / f_last
        calc_back = none_all * fac / f_first
        for name, calc, actual in [
            ("front = none*F/F(末)", calc_front, front_all),
            ("back  = none*F/F(首)", calc_back, back_all),
        ]:
            j = calc.align(actual, join="inner")
            diff = (j[0] - j[1]).abs()
            rel = (diff / j[1].abs().replace(0, pd.NA)).dropna()
            print(f"{name}: 样本{len(j[0])} 最大绝对差 {diff.max():.6f} 最大相对差 {rel.max():.6f}")

    print("\n===== 除权日前后连续性（600008 2016-05-23 10送10） =====")
    for label, s in [("none", none_all), ("front", front_all), ("back", back_all)]:
        if s is None:
            continue
        w = s.loc["2016-05-19":"2016-05-25"]
        print(f"\n{label}:")
        print(w.to_string())

    print("\n===== Q4 F 是否单调 & 跳变点 =====")
    if fac is not None:
        jumps = fac[fac.diff().fillna(0) != 0]
        print("F 跳变次数:", len(jumps))
        print(jumps.tail(8).to_string())

    print("\n===== get_divid_factors =====")
    try:
        dv = tq.get_divid_factors(CODE, "", "")
        print("行数:", len(dv), "列:", list(dv.columns))
        print(dv.tail(6).to_string())
    except Exception as e:  # noqa: BLE001
        print("失败:", e)

    tq.close()


if __name__ == "__main__":
    main()
