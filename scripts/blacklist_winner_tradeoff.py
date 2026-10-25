# -*- coding: utf-8 -*-
import sqlite3, json
from collections import defaultdict
from datetime import date

DBS = {2014:'I:/Darvas-box-trading/var/bt/bt_p05_2014.db',
       2015:'I:/Darvas-box-trading/var/bt/bt_p05_2015.db',
       2016:'I:/Darvas-box-trading/var/bt/bt_p05_2016.db'}

all_pos = []
for y, db in DBS.items():
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.realized_pnl, p.close_reason,
               po.reason_json, s.name
        FROM position p
        LEFT JOIN position_leg pl ON pl.position_id = p.id AND pl.leg_no = 1
        LEFT JOIN plan_order po ON po.id = pl.plan_order_id
        LEFT JOIN security s ON s.code = p.code
        WHERE p.closed_date IS NOT NULL
        ORDER BY p.opened_date, p.closed_date, p.id
    """)
    for r in cur.fetchall():
        d = dict(r)
        try:
            js = json.loads(d['reason_json'] or '{}')
            d['units'] = int(js.get('units', 1))
        except: d['units'] = 1
        all_pos.append(d)
    conn.close()

actual_total = sum(p['realized_pnl'] for p in all_pos)
flat1_total = sum(p['realized_pnl']/p['units'] for p in all_pos)

by_code = defaultdict(list)
for p in all_pos:
    by_code[p['code']].append(p)

post_loss_pnl = 0.0; post_loss_count = 0
post_win_pnl = 0.0; post_win_count = 0
winner_extra = 0.0

for code, ps in by_code.items():
    if len(ps) <= 1: continue
    ps.sort(key=lambda x: x['opened_date'])
    for i in range(len(ps)-1):
        cur = ps[i]; nxt = ps[i+1]
        d0 = date.fromisoformat(cur['closed_date'])
        d1 = date.fromisoformat(nxt['opened_date'])
        days = (d1 - d0).days
        if days <= 365:
            if cur['realized_pnl'] <= 0:
                post_loss_pnl += nxt['realized_pnl']; post_loss_count += 1
            else:
                post_win_pnl += nxt['realized_pnl']; post_win_count += 1
            if nxt['units'] == 5:
                winner_extra += nxt['realized_pnl'] - (nxt['realized_pnl']/5)

# 统计：亏损后 12 月内"没有"再交易的股票数
no_repeat_after_loss = 0
for code, ps in by_code.items():
    ps.sort(key=lambda x: x['opened_date'])
    for i, p in enumerate(ps):
        if p['realized_pnl'] <= 0:
            had_repeat = False
            for j in range(i+1, len(ps)):
                d0 = date.fromisoformat(p['closed_date'])
                d1 = date.fromisoformat(ps[j]['opened_date'])
                if (d1-d0).days <= 365:
                    had_repeat = True; break
            if not had_repeat:
                no_repeat_after_loss += 1

print('=== 赢家 vs 黑名单 权衡 (2014-2016 实际交易) ===')
print(f'实际 0017 总盈亏:          {actual_total:>12,.0f}')
print(f'若全部 1 单位 (无赢家):    {flat1_total:>12,.0f}  (差异 {flat1_total-actual_total:+,.0f})')
print(f'赢家 5x 机制额外盈亏:      {winner_extra:>12,.0f}')
print(f'亏损后 12 月内再交易:      {post_loss_count} 笔, 合计 {post_loss_pnl:+,.0f}')
print(f'盈利后 12 月内再交易:      {post_win_count} 笔, 合计 {post_win_pnl:+,.0f}')
print(f'亏损后 12 月内无再交易:    {no_repeat_after_loss} 只股票')
print('  （无再交易 = 可能黑名单成功拦截，也可能真的没有信号；从 position 表无法区分）')

print('\n=== 情景汇总（仅基于已观测到的交易）===')
print(f'维持现状 (0017):            {actual_total:>12,.0f}')
print(f'无赢家 5x、保留黑名单:      {flat1_total:>12,.0f}')
print(f'保留赢家、移除黑名单:       {actual_total + post_loss_pnl:>12,.0f}')
print(f'无赢家、移除黑名单:         {flat1_total + post_loss_pnl:>12,.0f}')
print(f'  注：移除黑名单的收益仅统计了实际发生的 7 笔再交易；若黑名单还拦截了')
print(f'  其他未发生的亏损信号，则实际收益可能不同。')
