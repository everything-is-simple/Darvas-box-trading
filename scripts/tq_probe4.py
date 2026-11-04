#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tqcenter back 模式可用性探针：量能是否同步复权 + 全市场吞吐估算。

结论用于决定「用 tqcenter 重灌复权日线」还是「lday 原价 + 本地合成」。
用法：python scripts/tq_probe4.py
"""
import sys
import time
import warnings

import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins\sys")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins")

from tqcenter import tq  # noqa: E402


def main():
    tq.initialize(__file__)

    # ---- 1. 量能是否随复权调整（600008 2016-05-23 10送10）----
    print("===== Volume / Amount 在 back 模式下是否调整 =====")
    res = {}
    for dt in ("none", "back"):
        df = tq.get_market_data(
            field_list=["Close", "Volume", "Amount"], stock_list=["600008.SH"],
            start_time="20160518", end_time="20160526", count=-1,
            dividend_type=dt, period="1d", fill_data=False,
        )
        res[dt] = pd.DataFrame({k: df[k]["600008.SH"] for k in ("Close", "Volume", "Amount")})
    j = res["none"].join(res["back"], lsuffix="_none", rsuffix="_back")
    j["Vol比"] = j["Volume_back"] / j["Volume_none"]
    j["Amt比"] = j["Amount_back"] / j["Amount_none"]
    print(j.to_string())

    # ---- 2. 全市场吞吐估算 ----
    print("\n===== 吞吐估算（20 只 × 全历史 1d）=====")
    codes = tq.get_stock_list("5")  # 5 = 全部A股
    print("A股代码总数:", len(codes) if codes else 0)
    sample = codes[:20] if codes else []
    t0 = time.time()
    df = tq.get_market_data(
        field_list=["Close"], stock_list=sample,
        start_time="20050101", end_time="20181231", count=-1,
        dividend_type="back", period="1d", fill_data=False,
    )
    el = time.time() - t0
    if isinstance(df, dict) and "Close" in df:
        n = len(df["Close"])
        print(f"{len(sample)} 只 → {el:.1f}s（{n} 个交易日） 单只 {el/len(sample):.2f}s")
        print(f"预估 5000 只全历史 ≈ {el/len(sample)*5000/60:.0f} 分钟")
        print("样本首末:", df["Close"].index[0].date(), "~", df["Close"].index[-1].date())

    tq.close()


if __name__ == "__main__":
    main()
