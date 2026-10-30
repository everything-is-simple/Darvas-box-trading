#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-6a staledays 梯度 · 中途预览（只读，不写库、不出正式报告）。

复用 scripts/base_year_summary.load_year 的口径（与 14 年基线年报表 100% 一致），
再补框架 §4.2 要求的三个结构指标：
  ts_share    time_stop_noprofit 出口占比（reason_json 口径，不用 close_reason）
  short10     持有 ≤10 个交易日的已平仓合计（最大失血块是否被止住）
  big50       MFE>50% 的笔数与合计（防切碎：46 笔曾贡献 +1,133,049）

用法：
  python scripts/p26_preview.py                 # 全部 p26 库
  python scripts/p26_preview.py --year 2012
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from base_year_summary import load_year  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
P26 = ROOT / "var" / "bt" / "p26"

TS_SQL = """
select count(distinct p.id)
from plan_order o
join trade_plan t on t.id = o.trade_plan_id
join position p on p.code = o.code and p.closed_date = t.execute_date
where o.side = 'sell'
  and json_extract(o.reason_json, '$.signal') = 'time_stop_noprofit'
"""


def structure(db: Path):
    """ts 占比 / 短持块 / 大赢家保留率。"""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        closed = c.execute(
            "select count(*) from position where status='closed'"
        ).fetchone()[0]
        ts = c.execute(TS_SQL).fetchone()[0] if closed else 0

        # 逐笔：持有交易日数（含平仓日、不含开仓日）+ MFE
        rows = c.execute(
            """select p.id, p.avg_cost, p.realized_pnl,
                      (select count(*) from daily_bar b
                        where b.code=p.code and b.date>p.opened_date
                          and b.date<=p.closed_date) hb,
                      (select max(b.high) from daily_bar b
                        where b.code=p.code and b.date>=p.opened_date
                          and b.date<=p.closed_date) mx
               from position p where p.status='closed'"""
        ).fetchall()
    finally:
        c.close()

    short_n = short_pnl = 0
    big_n = big_pnl = 0
    big_n50 = 0
    for _id, cost, pnl, hb, mx in rows:
        if pnl is None:
            continue
        if hb is not None and hb <= 10:
            short_n += 1
            short_pnl += pnl
        if cost and mx:
            mfe = (mx - cost) / cost
            if mfe > 0.50:
                big_n += 1
                big_pnl += pnl
                if mfe > 0.50:
                    big_n50 += 1
    return dict(
        closed=closed, ts=ts, ts_share=(ts / closed if closed else 0.0),
        short_n=short_n, short_pnl=short_pnl,
        big_n=big_n, big_pnl=big_pnl,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", default="2012")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    files = sorted(P26.glob(f"p26_sd*_{a.year}.db"))
    if not files:
        print(f"未找到 {a.year} 的 p26 库")
        return 1

    print(f"\n=== P2-6a staledays 梯度 · {a.year} 年（预览口径，正式以 p26_report.py 为准）===\n")
    hdr = (f"{'档':<7}{'笔数':>5}{'胜率':>7}{'保本':>7}{'缺口':>7}{'PF':>6}"
           f"{'每单位':>9}{'集中度':>7}{'剔前三':>10}{'持中位':>7}"
           f"{'权益变动':>11}{'收益率':>8}{'已实现':>10}{'MDD':>7}")
    print(hdr)
    print("-" * len(hdr))
    out = []
    for f in files:
        r = load_year(f)
        if not r:
            print(f"{f.name} 读取失败")
            continue
        s = structure(f)
        tag = f.stem.replace("p26_", "").replace(f"_{a.year}", "")
        pf = "   inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        conc = r["top3_share_gw"]
        print(f"{tag:<7}{r['trades']:>5}{r['win_rate']*100:>6.1f}%{r['breakeven']*100:>6.1f}%"
              f"{r['gap']*100:>+6.1f}{pf:>6}{r['per_unit_realized']:>9,.0f}"
              f"{conc*100:>6.1f}%{r['realized_ex_top3']:>10,.0f}{r['hold_median'] or 0:>7.0f}"
              f"{r['net']:>11,.0f}{r['ret_pct']*100:>+7.2f}%{r['realized']:>10,.0f}"
              f"{r['mdd']*100:>6.2f}%")
        out.append(dict(tag=tag, **r, **s))

    print("\n=== 结构指标（框架 §4.2）===\n")
    h2 = (f"{'档':<7}{'平仓':>5}{'ts笔':>5}{'ts占比':>7}{'短持≤10日':>9}{'短持净利':>10}"
          f"{'大赢MFE>50%':>11}{'大赢净利':>10}")
    print(h2)
    print("-" * len(h2))
    for o in out:
        print(f"{o['tag']:<7}{o['closed']:>5}{o['ts']:>5}{o['ts_share']*100:>6.1f}%"
              f"{o['short_n']:>9}{o['short_pnl']:>10,.0f}{o['big_n']:>11}{o['big_pnl']:>10,.0f}")

    # 噪声门槛（框架 §5.2）：以基线 sd2 为参照的收益率差
    base = next((o for o in out if o["tag"] == "sd2"), None)
    if base:
        print("\n=== 相对基线 sd2 的收益率差（框架 §5.2：<3% 视为噪声）===\n")
        for o in out:
            if o["tag"] == "sd2":
                continue
            d = (o["ret_pct"] - base["ret_pct"]) * 100
            verdict = "噪声" if abs(d) < 3.0 else ("改善" if d > 0 else "恶化")
            print(f"  {o['tag']:<7}{d:>+7.2f}pp   {verdict}")

    if a.json:
        import json
        print("\n" + json.dumps(out, ensure_ascii=False, default=str, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
