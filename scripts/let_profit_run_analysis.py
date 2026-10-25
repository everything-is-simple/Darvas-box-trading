#!/usr/bin/env python3
"""分析五年盈利票是否"让利润奔跑"，还是过早止盈。"""
import sqlite3
import statistics
from datetime import date

YEARS = ['2014', '2015', '2016', '2017', '2018']

all_rows = []
for y in YEARS:
    con = sqlite3.connect(f'file:var/bt/bt_p05_{y}.db?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    for p in con.execute(
        "SELECT code, opened_date, closed_date, avg_cost, realized_pnl, qty, close_reason "
        "FROM position WHERE status='closed'"
    ):
        all_rows.append((y, p))
    con.close()

win_days, loss_days = [], []
win_pnl, loss_pnl = [], []
win_ret, loss_ret = [], []
for y, p in all_rows:
    od, cd = p['opened_date'], p['closed_date']
    if not od or not cd:
        continue
    try:
        d0 = date(int(od[:4]), int(od[5:7]), int(od[8:10]))
        d1 = date(int(cd[:4]), int(cd[5:7]), int(cd[8:10]))
        days = (d1 - d0).days
    except Exception:
        continue
    pnl = p['realized_pnl'] or 0.0
    if pnl > 0:
        win_days.append(days)
        win_pnl.append(pnl)
    else:
        loss_days.append(days)
        loss_pnl.append(pnl)

print('=== 五年平仓交易：盈利/亏损票持仓天数 ===')
print(f'盈利票 {len(win_days)} 笔: 平均持仓 {statistics.mean(win_days):.1f} 天, '
      f'中位 {statistics.median(win_days):.0f} 天, 最长 {max(win_days)} 天')
print(f'亏损票 {len(loss_days)} 笔: 平均持仓 {statistics.mean(loss_days):.1f} 天, '
      f'中位 {statistics.median(loss_days):.0f} 天, 最长 {max(loss_days)} 天')

print()
print('盈利票持仓天数分布:')
bins = [(0, 5), (6, 10), (11, 20), (21, 40), (41, 9999)]
labels = ['0-5天', '6-10天', '11-20天', '21-40天', '40天+']
for (lo, hi), lbl in zip(bins, labels):
    cnt = sum(1 for d in win_days if lo <= d <= hi)
    print(f'  {lbl}: {cnt} 笔')

print()
print('盈利票盈亏金额 TOP10 的持仓天数（看大盈利是否吃满趋势）:')
# 需要 code/name，重查
top = []
for y, p in all_rows:
    if (p['realized_pnl'] or 0) > 0:
        top.append((p['code'], p['realized_pnl'], p['opened_date'], p['closed_date']))
top.sort(key=lambda x: -x[1])
for code, pnl, od, cd in top[:10]:
    d0 = date(int(od[:4]), int(od[5:7]), int(od[8:10]))
    d1 = date(int(cd[:4]), int(cd[5:7]), int(cd[8:10]))
    days = (d1 - d0).days
    print(f'  {code} +{pnl:,.0f}  持仓{days}天  ({od}~{cd})')
