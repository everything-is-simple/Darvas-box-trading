#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tqcenter 数据覆盖与一致性校验（一次性探针）。

Q1 tqcenter 的 A 股清单 vs 本地 lday 清单：退市股是否缺失（幸存者偏差）
Q2 tqcenter none 价 vs lday 原始价：是否同源一致
Q3 回测区间（2004-12 ~ 2018-12）覆盖率

用法：python scripts/tq_probe5.py
"""
import sys
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins\sys")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins")

from tqcenter import tq  # noqa: E402

TDX_ROOT = Path(r"D:\new_tdx64\vipdoc")


def lday_codes():
    out = set()
    for mk in ("sh", "sz"):
        d = TDX_ROOT / mk / "lday"
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.suffix == ".day":
                out.add(f.stem)
    return out


def is_a_share(code):
    """A 股：sh60/68/900(B股除外) sz00/30；排除指数 sh000/sz399、基金、债。"""
    if code.startswith("sh"):
        num = code[2:]
        return num[:2] in ("60", "68") or num[:3] in ("605", "603", "601")
    if code.startswith("sz"):
        num = code[2:]
        return num[:2] in ("00", "30")
    return False


def main():
    tq.initialize(__file__)

    loc = lday_codes()
    loc_a = {c for c in loc if is_a_share(c)}
    print("===== Q1 清单覆盖 =====")
    print("lday 文件总数:", len(loc), " 其中 A 股:", len(loc_a))

    tq_codes = tq.get_stock_list("5") or []
    tq_set = {c.lower().replace(".", "") if "." in c else c.lower() for c in tq_codes}
    # 统一为 sh600000 / sz000001 形式
    norm = set()
    for c in tq_codes:
        c = c.strip().upper()
        if "." in c:
            num, mk = c.split(".")
            norm.add(mk.lower() + num)
        else:
            norm.add(c.lower())
    print("tqcenter A 股清单:", len(norm))

    only_loc = loc_a - norm
    only_tq = norm - loc_a
    print(f"只在 lday（疑似退市/停牌老股）: {len(only_loc)}")
    print(f"只在 tqcenter: {len(only_tq)}")
    print("  只在 lday 样本:", sorted(only_loc)[:10])
    print("  只在 tqcenter 样本:", sorted(only_tq)[:10])

    # Q2 抽样对账：lday 原价 vs tqcenter none
    print("\n===== Q2 价格同源校验（抽样 5 只，2016-05-20）=====")
    import sqlite3
    db = sqlite3.connect("file:var/bt/full_hist_10pct.db?mode=ro", uri=True)
    sample = sorted(loc_a)[:5]
    tqc = [c[:2].upper() + "." + c[2:] for c in sample]
    try:
        df = tq.get_market_data(
            field_list=["Close"], stock_list=tqc,
            start_time="20160520", end_time="20160520", count=-1,
            dividend_type="none", period="1d", fill_data=False,
        )
        for c, t in zip(sample, tqc):
            row = db.execute(
                "SELECT close FROM daily_bar WHERE code=? AND date='2016-05-20'", (c,)
            ).fetchone()
            tv = df["Close"][t].dropna()
            tv = float(tv.iloc[0]) if len(tv) else None
            print(f"  {c}: lday={row[0] if row else None}  tq={tv}")
    except Exception as e:  # noqa: BLE001
        print("  失败:", e)
    db.close()

    # Q3 区间覆盖
    print("\n===== Q3 回测区间覆盖（2004-12-01 ~ 2018-12-31）=====")
    db = sqlite3.connect("file:var/bt/full_hist_10pct.db?mode=ro", uri=True)
    n = db.execute(
        "SELECT COUNT(DISTINCT code) FROM daily_bar WHERE date BETWEEN '2004-12-01' AND '2018-12-31'"
    ).fetchone()[0]
    bars = db.execute(
        "SELECT COUNT(*) FROM daily_bar WHERE date BETWEEN '2004-12-01' AND '2018-12-31'"
    ).fetchone()[0]
    print(f"lday 侧该区间：{n} 只 / {bars:,} 根")
    db.close()

    tq.close()


if __name__ == "__main__":
    main()
