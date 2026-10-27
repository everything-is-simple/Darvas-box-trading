#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HARD ④：独立代码路径抽查 3 个交易日的等权中位数，与主流程结果比对。

刻意不复用 e17_market_index.py 的任何函数——用 SQL 子查询取 prev_close，
按框架文定义重算一遍中位数。不一致即主流程有 bug。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
BASE_DB = ROOT / "var" / "bt" / "full_hist_10pct.db"
DATA = ROOT / "docs" / "experiments" / "e17_market_index_data.json"
SPOT = ["2008-11-04", "2013-06-25", "2017-06-01"]
WINSOR, MIN_LIST, MAX_GAP = 0.11, 30, 30


def dd(s: str) -> date:
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def main():
    p = json.loads(DATA.read_text(encoding="utf-8"))
    series = dict(zip(p["series"]["dates"], p["series"]["a"]))
    c = sqlite3.connect(f"file:{BASE_DB}?mode=ro", uri=True)
    stocks = {code: (name or "", dd(ld)) for code, name, ld in c.execute(
        "select code,name,list_date from security where kind='stock' and list_date is not null "
        "and code not like 'sh900%' and code not like 'sz200%'")}

    print(f"{'日期':<12}{'脚本值':>10}{'独立值':>10}{'差':>10}{'样本数':>8}  校验")
    ok_all = True
    for ds in SPOT:
        vals = []
        rows = c.execute(
            "select b.code,b.close,b.high,b.low,b.volume,"
            "(select close from daily_bar x where x.code=b.code and x.date<b.date "
            " order by x.date desc limit 1),"
            "(select date from daily_bar x where x.code=b.code and x.date<b.date "
            " order by x.date desc limit 1) "
            "from daily_bar b where b.date=?", (ds,)).fetchall()
        for code, cl, hi, lo, vol, pc, pd_ in rows:
            if code not in stocks:          # 排除指数/B 股
                continue
            if not vol or vol <= 0 or pc is None or pc <= 0:
                continue
            if (dd(ds) - dd(pd_)).days > MAX_GAP:
                continue
            name, ldate = stocks[code]
            if ("ST" in name) or ("退" in name):
                continue
            if (dd(ds) - ldate).days < MIN_LIST:
                continue
            if hi is None or lo is None or hi <= lo:
                continue
            r = cl / pc - 1.0
            vals.append(max(-WINSOR, min(WINSOR, r)))
        ind = median(vals)
        main_v = series.get(ds)
        diff = abs(ind - main_v) if main_v is not None else None
        ok = diff is not None and diff < 1e-6
        ok_all &= ok
        print(f"{ds:<12}{main_v*100:>9.4f}%{ind*100:>9.4f}%{(diff or 0)*100:>9.6f}%{len(vals):>8}  {'OK' if ok else 'MISMATCH'}")
    print("HARD④:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
