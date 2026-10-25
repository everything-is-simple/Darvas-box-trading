# -*- coding: utf-8 -*-
import sqlite3, json, datetime
from collections import defaultdict, Counter

DBS = {
    2014: 'I:/Darvas-box-trading/var/bt/bt_p05_2014.db',
    2015: 'I:/Darvas-box-trading/var/bt/bt_p05_2015.db',
    2016: 'I:/Darvas-box-trading/var/bt/bt_p05_2016.db',
}

def load_year(year):
    conn = sqlite3.connect(DBS[year])
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.realized_pnl, po.reason_json
        FROM position p
        LEFT JOIN position_leg pl ON pl.position_id = p.id AND pl.leg_no = 1
        LEFT JOIN plan_order po ON po.id = pl.plan_order_id
        WHERE p.closed_date IS NOT NULL
        ORDER BY p.opened_date, p.closed_date, p.id
    """)
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        units = 1
        try:
            js = json.loads(d['reason_json'] or '{}')
            units = int(js.get('units', 1))
        except Exception:
            pass
        d['actual_units'] = units
        d['year'] = year
        rows.append(d)
    conn.close()
    return rows

all_rows = []
for y in (2014,2015,2016):
    rs = load_year(y)
    print(f'{y}: 平仓 {len(rs)} 笔, units 分布 {Counter(r["actual_units"] for r in rs)}')
    all_rows.extend(rs)

all_rows.sort(key=lambda r: (r['opened_date'], r['closed_date'], r['id']))
print(f'\n三年合计: {len(all_rows)} 笔')

LADDERS = {
    '0017': [1, 5],
    'D': [2, 2, 3, 3, 5],
}

def simulate(ladder, rows, use_blacklist=False):
    level = defaultdict(int)
    blacklist = defaultdict(lambda: datetime.date.min)
    total = 0.0
    wins = losses = skipped = 0
    win_pnls = []
    loss_pnls = []
    sizes = []
    eq = [0.0]
    peak = max_dd = 0.0
    level_counts = Counter()
    for r in rows:
        code = r['code']
        dt = datetime.date.fromisoformat(r['opened_date'])
        if use_blacklist and dt < blacklist[code]:
            skipped += 1
            continue
        sz = ladder[level[code]]
        sizes.append(sz)
        level_counts[level[code]] += 1
        per_unit = r['realized_pnl'] / r['actual_units']
        pnl = per_unit * sz
        total += pnl
        eq.append(eq[-1] + pnl)
        peak = max(peak, eq[-1])
        max_dd = max(max_dd, peak - eq[-1])
        if pnl > 0:
            wins += 1; win_pnls.append(pnl)
        else:
            losses += 1; loss_pnls.append(pnl)
        if pnl > 0:
            level[code] = min(level[code] + 1, len(ladder)-1)
        else:
            level[code] = max(level[code] - 1, 0)
            if use_blacklist:
                blacklist[code] = datetime.date.fromisoformat(r['closed_date']) + datetime.timedelta(days=365)
    return {
        'total': total, 'wins': wins, 'losses': losses, 'skipped': skipped,
        'win_rate': wins/(wins+losses) if (wins+losses) else 0,
        'profit_factor': abs(sum(win_pnls)/sum(loss_pnls)) if loss_pnls and sum(loss_pnls)!=0 else float('inf'),
        'avg_win': sum(win_pnls)/len(win_pnls) if win_pnls else 0,
        'avg_loss': sum(loss_pnls)/len(loss_pnls) if loss_pnls else 0,
        'max_dd': max_dd, 'avg_size': sum(sizes)/len(sizes) if sizes else 0,
        'level_counts': level_counts,
    }

print('\n=== 分年 + 三年连算 ===')
print(f'{"数据集":<10}{"方案":<10}{"总盈亏":>12}{"胜率":>8}{"盈亏比":>8}{"均盈":>10}{"均亏":>10}{"最大回撤":>10}{"均仓位":>8}{"跳过":>6}')
for label, subset in [('2014', [r for r in all_rows if r['year']==2014]),
                      ('2015', [r for r in all_rows if r['year']==2015]),
                      ('2016', [r for r in all_rows if r['year']==2016]),
                      ('2014-16', all_rows)]:
    for name, lad in LADDERS.items():
        for bl in (False, True):
            s = simulate(lad, subset, bl)
            tag = f'{name}{"+BL" if bl else ""}'
            print(f'{label:<10}{tag:<10}{s["total"]:>12,.0f}{s["win_rate"]:>8.1%}{s["profit_factor"]:>8.2f}{s["avg_win"]:>10,.0f}{s["avg_loss"]:>10,.0f}{s["max_dd"]:>10,.0f}{s["avg_size"]:>8.2f}{s["skipped"]:>6}')

# 三年连算下 D 的等级分布
sD = simulate([2,2,3,3,5], all_rows, False)
print('\nD 梯子 2014-16 等级使用次数:', dict(sorted(sD['level_counts'].items())))
s17 = simulate([1,5], all_rows, False)
print('0017 梯子 2014-16 等级使用次数:', dict(sorted(s17['level_counts'].items())))
