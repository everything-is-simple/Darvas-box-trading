#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tqcenter 复权口径探针（一次性核实，非生产代码）。

目的：确认 get_market_data 的 dividend_type 三态（none/front/back）
      与 ForwardFactor 字段的真实语义，用于钉死新基线数据口径。

用法：python scripts/tq_probe.py
"""
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins\sys")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins")

from tqcenter import tq  # noqa: E402

CODE = "600008.SH"          # 2016-05-23 每10股送10股（不复权 8.12 → 4.07）
START = "20160510"
END = "20160610"


def main():
    tq.initialize(__file__)

    out = {}
    for dt in ("none", "front", "back"):
        df = tq.get_market_data(
            field_list=[], stock_list=[CODE],
            start_time=START, end_time=END, count=-1,
            dividend_type=dt, period="1d", fill_data=False,
        )
        print(f"\n===== dividend_type={dt} =====")
        print("返回字段:", sorted(df.keys()) if isinstance(df, dict) else type(df))
        if isinstance(df, dict) and "Close" in df:
            close = df["Close"][CODE]
            print("Close:")
            print(close.to_string())
            out[dt] = close
        if isinstance(df, dict) and "ForwardFactor" in df:
            print("ForwardFactor:")
            print(df["ForwardFactor"][CODE].to_string())

    if {"none", "front", "back"} <= set(out):
        print("\n===== 三态对比（除权日前后各 2 日） =====")
        cmp = out["none"].rename("none").to_frame()
        cmp["front"] = out["front"]
        cmp["back"] = out["back"]
        cmp["front/none"] = cmp["front"] / cmp["none"]
        cmp["back/none"] = cmp["back"] / cmp["none"]
        print(cmp.tail(15).to_string())

    # 除权除息明细
    print("\n===== get_divid_factors =====")
    try:
        dv = tq.get_divid_factors(CODE, "", "")
        print(dv.tail(10).to_string())
        print("列:", list(dv.columns), "index:", dv.index.name)
    except Exception as e:  # noqa: BLE001
        print("失败:", e)

    tq.close()


if __name__ == "__main__":
    main()
