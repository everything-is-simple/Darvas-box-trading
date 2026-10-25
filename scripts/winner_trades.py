# -*- coding: utf-8 -*-
import sqlite3, json
from collections import defaultdict

DBS = {
    2014: 'I:/Darvas-box-trading/var/bt/bt_p05_2014.db',
    2015: 'I:/Darvas-box-trading/var/bt/bt_p05_2015.db',
    2016: 'I:/Darvas-box-trading/var/bt/bt_p05_2016.db',
}

all_positions = []
for year, db in DBS.items():
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.realized_pnl,
               p.close_reason, p.status, po.reason_json, s.name
        FROM position p
        LEFT JOIN position_leg pl ON pl.position_id = p.id AND pl.leg_no = 1
        LEFT JOIN plan_order po ON po.id = pl.plan_order_id
        LEFT JOIN security s ON s.code = p.code
        WHERE p.closed_date IS NOT NULL
        ORDER BY p.opened_date, p.closed_date, p.id
    """)
    for r in cur.fetchall():
        d = dict(r)
        d['year'] = year
        try:
            js = json.loads(d['reason_json'] or '{}')
            d['units'] = int(js.get('units', 1))
            d['track_level'] = js.get('track_level', '?')
            d['budget_bucket'] = js.get('budget_bucket', '?')
        except Exception:
            d['units'] = 1
            d['track_level'] = '?'
            d['budget_bucket'] = '?'
        all_positions.append(d)
    conn.close()

# 按 code 分组，按 opened_date 排序，找出 5 单位交易及其前后文
by_code = defaultdict(list)
for p in all_positions:
    by_code[p['code']].append(p)

print('=== 三年中所有 5 单位（winner 档）交易及前后文 ===\n')
print(f'{"年份":<6}{"代码":<10}{"名称":<10}{"开仓":<12}{"平仓":<12}{"天数":>5}{"单位":>5}{"track":<8}{"close_reason":<20}{"盈亏":>12}{"结果":>6}')
for code, ps in by_code.items():
    ps.sort(key=lambda x: x['opened_date'])
    for i, p in enumerate(ps):
        if p['units'] == 5:
            days = (json.loads('"'+p['closed_date']+'"') and (datetime.date.fromisoformat(p['closed_date']) - datetime.date.fromisoformat(p['opened_date'])).days) if False else 0
            # compute days
            from datetime import date
            days = (date.fromisoformat(p['closed_date']) - date.fromisoformat(p['opened_date'])).days
            result = 'WIN' if p['realized_pnl'] > 0 else 'LOSS'
            print(f"{p['year']:<6}{p['code']:<10}{p['name'] or '':<10}{p['opened_date']:<12}{p['closed_date']:<12}{days:>5}{p['units']:>5}{p['track_level']:<8}{p['close_reason'] or '':<20}{p['realized_pnl']:>12,.0f}{result:>6}")
            # print prior context
            if i > 0:
                prev = ps[i-1]
                print(f"  -> 前一笔: {prev['opened_date']}~{prev['closed_date']} units={prev['units']} pnl={prev['realized_pnl']:,.0f} ({'WIN' if prev['realized_pnl']>0 else 'LOSS'})")
            if i < len(ps)-1:
                nxt = ps[i+1]
                print(f"  -> 后一笔: {nxt['opened_date']}~{nxt['closed_date']} units={nxt['units']} pnl={nxt['realized_pnl']:,.0f} ({'WIN' if nxt['realized_pnl']>0 else 'LOSS'})")
            print()

# summary
winner5 = [p for p in all_positions if p['units']==5]
print(f'\n5 单位交易共 {len(winner5)} 笔: 赢 {sum(1 for p in winner5 if p["realized_pnl"]>0)} 笔 / 输 {sum(1 for p in winner5 if p["realized_pnl"]<=0)} 笔')
print(f'总盈亏: {sum(p["realized_pnl"] for p in winner5):,.0f}')
print(f'单笔平均: {sum(p["realized_pnl"] for p in winner5)/len(winner5):,.0f}')
