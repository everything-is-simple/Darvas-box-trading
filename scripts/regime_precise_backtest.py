#!/usr/bin/env python3
"""
精确回测：趋势三元门槛 + 卖点门（大盘破就走）

判据（达瓦斯箱体语言，N日低点逐级抬升）：
  升势 = 60日最低价 > 前一日60日最低价 × 1.002
  降势 = 60日最低价 < 前一日60日最低价 × 0.998
  震荡 = 其余

三道门对比：
  S0 现状        = 全部交易照单全收
  S1 买点门      = 降势不开仓（震荡/升势开仓）
  S2 买点门+卖点门 = S1 + 持仓期内指数转降势当日用该个股收盘价强平
  S3 只做升势     = 仅升势开仓（震荡/降势都停）

卖点门精确盈亏：对每笔 position，若持仓期(opened_date, closed_date)内指数首次转降势，
用该降势首日该个股的 close 价重算盈亏 = (close_强平 - avg_cost) × qty，替代原 realized_pnl。
（realized_pnl 未含费率，这里与现状同口径对比，不影响相对结论。）

用法：python regime_precise_backtest.py
"""
import sqlite3
from collections import defaultdict

YEARS = ['2014', '2015', '2016', '2017', '2018']
LOOKBACK = 60
UP_BIAS = 1.002
DOWN_BIAS = 0.998


def load_index_labels(db):
    """从源库读 sh000001 逐日价，算趋势三元标签 {date: 1升势/-1降势/0震荡}。"""
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
    return labels, dates, lows


def load_positions(db):
    """读某年回测库的已平仓持仓，逐日收盘价按需查询（不整库载入）。"""
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    positions = con.execute(
        "SELECT code, opened_date, closed_date, avg_cost, qty, realized_pnl "
        "FROM position WHERE status='closed'"
    ).fetchall()
    return con, positions


def close_at(con, code, date):
    """按需查某 code 某日的收盘价。"""
    r = con.execute(
        "SELECT close FROM daily_bar WHERE code=? AND date=?",
        (code, date),
    ).fetchone()
    return r[0] if r else None


def simulate(con, positions, labels, dates, mode):
    """
    mode:
      'S0' 现状全收
      'S1' 买点门(降势不开仓)
      'S2' 买点门+卖点门(持仓期指数转降势强平)
      'S3' 只做升势
    """
    total = 0.0
    for p in positions:
        code, od, cd, cost, qty, rpnl = (p['code'], p['opened_date'],
                                          p['closed_date'], p['avg_cost'],
                                          p['qty'], p['realized_pnl'] or 0.0)
        open_label = labels.get(od, 0)

        # 买点门
        if mode == 'S1' or mode == 'S2':
            if open_label == -1:  # 降势不开仓
                continue
        elif mode == 'S3':
            if open_label != 1:  # 只做升势
                continue

        # 卖点门：持仓期内首次转降势 -> 强平
        if mode == 'S2':
            if od in dates and cd in dates:
                oi = dates.index(od)
                ci = dates.index(cd)
                if ci > oi:
                    bear_date = None
                    for i in range(oi, ci + 1):
                        d = dates[i]
                        if labels.get(d, 0) == -1:
                            bear_date = d
                            break
                    if bear_date and cost and qty:
                        c = close_at(con, code, bear_date)
                        if c:
                            total += (c - cost) * qty
                            continue
        total += rpnl
    return total


def main():
    src = 'var/bt/full_hist_10pct.db'
    labels, dates, lows = load_index_labels(src)

    # 逐年 + 五年汇总
    results = {m: {} for m in ['S0', 'S1', 'S2', 'S3']}
    for y in YEARS:
        db = f'var/bt/bt_p05_{y}.db'
        con, positions = load_positions(db)
        for m in results:
            results[m][y] = simulate(con, positions, labels, dates, m)
        con.close()

    # 打印
    print(f'=== 趋势三元门槛 + 卖点门 精确回测（60日低点抬升判据）===')
    print(f'判据：升势(60日低点↑) / 降势(60日低点↓) / 震荡(其余)\n')
    header = f'{"年份":<6}' + ''.join(f'{m:>14}' for m in ['S0现状', 'S1买点门', 'S2+卖点门', 'S3只升势'])
    print(header)
    print('-' * len(header))
    for y in YEARS:
        row = f'{y:<6}'
        for m in ['S0', 'S1', 'S2', 'S3']:
            row += f'{results[m][y]:>14,.0f}'
        print(row)
    print('-' * len(header))
    row = f'{"合计":<6}'
    for m in ['S0', 'S1', 'S2', 'S3']:
        row += f'{sum(results[m].values()):>14,.0f}'
    print(row)

    # 相对现状的提升
    print()
    print('=== 相对现状(S0)的提升 ===')
    s0 = sum(results['S0'].values())
    for m in ['S1', 'S2', 'S3']:
        tot = sum(results[m].values())
        print(f'  {m}: {tot:+,.0f}  提升 {tot-s0:+,.0f}')


if __name__ == '__main__':
    main()
