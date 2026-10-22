#!/usr/bin/env python3
"""
方向3 精确回测：不砍开仓，只在「单笔头寸」上随大盘收缩/放大。

判据（达瓦斯箱体语言，60日低点逐级抬升）：
  升势 = 60日最低价 > 前一日60日最低价 × 1.002
  降势 = 60日最低价 < 前一日60日最低价 × 0.998
  震荡 = 其余

仓位规则（方向3，用户拍板）：
  升势 -> 单笔 2 单位（相对现状1单位 ×2）
  震荡 -> 单笔 1 单位（×1，持平）
  降势 -> 不砍开仓（保留逆势强势股），单笔收缩

对比策略：
  S0   现状        = 全部 1 单位（realized_pnl 原值）
  V1   升势×2 其余×1 = 只放大升势
  V2   升势×2 震荡×1 降势×0.5 = 升势放大 + 降势半仓
  V3   升势×2 震荡×1 降势×0.25 = 升势放大 + 降势1/4仓

实现：realized_pnl 与 qty 成正比（avg_cost 固定），按开仓日标签乘缩放系数。
（不含费率，与现状同口径对比，相对结论不变。）
"""
import sqlite3
from collections import defaultdict

YEARS = ['2014', '2015', '2016', '2017', '2018']
LOOKBACK = 60
UP_BIAS = 1.002
DOWN_BIAS = 0.998


def load_index_labels(db):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    rows = con.execute(
        "SELECT date, low FROM daily_bar WHERE code='sh000001' ORDER BY date"
    ).fetchall()
    con.close()
    dates = [r[0] for r in rows]
    lows = [r[1] for r in rows]
    labels = {}
    for i in range(len(rows)):
        if i < LOOKBACK:
            labels[dates[i]] = 0
            continue
        cur = min(lows[i - LOOKBACK + 1:i + 1])
        prev = min(lows[i - LOOKBACK:i])
        if cur > prev * UP_BIAS:
            labels[dates[i]] = 1
        elif cur < prev * DOWN_BIAS:
            labels[dates[i]] = -1
        else:
            labels[dates[i]] = 0
    return labels, dates


def load_positions(db):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    positions = con.execute(
        "SELECT code, opened_date, closed_date, avg_cost, qty, realized_pnl "
        "FROM position WHERE status='closed'"
    ).fetchall()
    return con, positions


def simulate(con, positions, labels, mode):
    """mode 定义缩放表 {label: scale}，返回该策略净利。"""
    scales = {
        'S0': {1: 1.0, 0: 1.0, -1: 1.0},
        'V1': {1: 2.0, 0: 1.0, -1: 1.0},
        'V2': {1: 2.0, 0: 1.0, -1: 0.5},
        'V3': {1: 2.0, 0: 1.0, -1: 0.25},
    }[mode]
    total = 0.0
    for p in positions:
        lbl = labels.get(p['opened_date'], 0)
        total += (p['realized_pnl'] or 0.0) * scales[lbl]
    return total


def main():
    src = 'var/bt/full_hist_10pct.db'
    labels, dates = load_index_labels(src)

    modes = ['S0', 'V1', 'V2', 'V3']
    results = {m: {} for m in modes}
    # 同时统计每年各标签的交易笔数与净利（诊断用）
    diag = {y: defaultdict(lambda: [0, 0.0]) for y in YEARS}

    for y in YEARS:
        db = f'var/bt/bt_p05_{y}.db'
        con, positions = load_positions(db)
        for p in positions:
            lbl = labels.get(p['opened_date'], 0)
            diag[y][lbl][0] += 1
            diag[y][lbl][1] += (p['realized_pnl'] or 0.0)
        for m in modes:
            results[m][y] = simulate(con, positions, labels, m)
        con.close()

    print('=== 方向3 仓位收缩/放大 精确回测（60日低点抬升判据）===')
    print('判据：升势(60日低点↑)/降势(60日低点↓)/震荡(其余)\n')
    header = f'{"年份":<6}' + ''.join(
        f'{m:>14}' for m in ['S0现状', 'V1升×2', 'V2升×2降×.5', 'V3升×2降×.25'])
    print(header)
    print('-' * len(header))
    for y in YEARS:
        row = f'{y:<6}'
        for m in modes:
            row += f'{results[m][y]:>14,.0f}'
        print(row)
    print('-' * len(header))
    row = f'{"合计":<6}'
    for m in modes:
        row += f'{sum(results[m].values()):>14,.0f}'
    print(row)

    print()
    print('=== 相对现状(S0)的提升 ===')
    s0 = sum(results['S0'].values())
    for m in ['V1', 'V2', 'V3']:
        tot = sum(results[m].values())
        print(f'  {m}: {tot:+,.0f}  提升 {tot-s0:+,.0f}')

    print()
    print('=== 诊断：每年开仓日趋势标签分布（笔数 / 净利）===')
    for y in YEARS:
        parts = []
        for lbl, name in [(1, '升'), (0, '震'), (-1, '降')]:
            n, pnl = diag[y][lbl]
            parts.append(f'{name}{n}笔/{pnl:+,.0f}')
        print(f'  {y}: ' + '  '.join(parts))


if __name__ == '__main__':
    main()
