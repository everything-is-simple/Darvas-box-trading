#!/usr/bin/env python3
"""双层组合回测：A 标签做开仓日放大（M2 升2 震1 降1）+ C 标签做熊市闸门。

熊市闸门：开仓日的 C 判据（20日箱体对箱体·5日确认）smoothed state = 'down' → 拒新仓
（已挂止损单不受影响，仅拒绝当日新买单）。这等价于"C 进入降势期段后不开新仓"。

对照：
  S0        现状（全 1 单位，无闸门）              = +41,501
  A_M2      A 标签缩放 升2 震1 降1，无闸门          = +106,840
  gate_only 现状 1 单位 + 熊市闸门（方案2 兜底）
  double    A M2 + 熊市闸门（方案1 双层）          ← 待验证目标
"""
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, r'I:\Darvas-box-trading\scripts')
import regime_lifespan_stats as R

YEARS = ['2014', '2015', '2016', '2017', '2018']
SRC = r'var\bt\full_hist_10pct.db'


def load_a_labels():
    """A 判据：60日低点相邻日比（docs/00 §5.6.8 同源）。"""
    con = sqlite3.connect(f'file:{SRC}?mode=ro', uri=True)
    rows = con.execute(
        "SELECT date, low FROM daily_bar WHERE code='sh000001' ORDER BY date"
    ).fetchall()
    con.close()
    dates = [r[0] for r in rows]
    lows = [r[1] for r in rows]
    labels = {}
    for i in range(60, len(rows)):
        cur = min(lows[i - 59:i + 1])
        prev = min(lows[i - 60:i])
        labels[dates[i]] = 1 if cur > prev * 1.002 else (-1 if cur < prev * 0.998 else 0)
    return labels


def load_c_state():
    """C 判据：20日箱体对箱体·5日确认平滑后的状态（'up'/'down'/'range'）。"""
    con = sqlite3.connect(f'file:{SRC}?mode=ro', uri=True)
    dates, lows, highs, closes = R.load_bars(con)
    con.close()
    raw = R.label_boxstep(dates, lows, highs, 20, 20)
    d = [x for x in dates if x in raw]
    raw_seq = [raw[x] for x in d]
    seq = R.smooth(raw_seq, 5)
    return {date: s for date, s in zip(d, seq)}


def load_positions(db):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT code, opened_date, closed_date, avg_cost, qty, realized_pnl "
        "FROM position WHERE status='closed'"
    ).fetchall()
    return con, rows


def main():
    a_labels = load_a_labels()
    c_state = load_c_state()
    a_scale = {1: 2.0, 0: 1.0, -1: 1.0}  # M2 升2 震1 降1

    results = {m: {} for m in ('S0', 'A_M2', 'gate_only', 'double')}
    diag = {y: {'total': 0, 'skipped': 0, 'skipped_pnl': 0.0,
                'skipped_by_state': defaultdict(lambda: [0, 0.0])}
            for y in YEARS}

    for y in YEARS:
        db = rf'var\bt\bt_p05_{y}.db'
        con, positions = load_positions(db)
        s0 = am2 = go = dbl = 0.0
        for p in positions:
            pnl = p['realized_pnl'] or 0.0
            odate = p['opened_date']
            a_lbl = a_labels.get(odate, 0)
            c_st = c_state.get(odate, 'range')
            gate = (c_st == 'down')
            s0 += pnl
            am2 += pnl * a_scale[a_lbl]
            if not gate:
                go += pnl
                dbl += pnl * a_scale[a_lbl]
            diag[y]['total'] += 1
            if gate:
                diag[y]['skipped'] += 1
                diag[y]['skipped_pnl'] += pnl * a_scale[a_lbl]
                diag[y]['skipped_by_state'][c_st][0] += 1
                diag[y]['skipped_by_state'][c_st][1] += pnl * a_scale[a_lbl]
        results['S0'][y] = s0
        results['A_M2'][y] = am2
        results['gate_only'][y] = go
        results['double'][y] = dbl
        con.close()

    print('=== 双层组合回测：A M2（升2 震1 降1）+ C 熊市闸门（C state=down 拒新仓）===\n')
    header = f'{"年份":<6}{"S0现状":>14}{"A_M2无闸":>14}{"仅闸门(方案2)":>16}{"双层(A+闸)":>14}'
    print(header)
    print('-' * len(header))
    for y in YEARS:
        row = f'{y:<6}'
        for m in ('S0', 'A_M2', 'gate_only', 'double'):
            row += f'{results[m][y]:>14,.0f}' if m != 'gate_only' else f'{results[m][y]:>16,.0f}'
        print(row)
    print('-' * len(header))
    row = f'{"合计":<6}'
    for m in ('S0', 'A_M2', 'gate_only', 'double'):
        row += f'{sum(results[m].values()):>14,.0f}' if m != 'gate_only' else f'{sum(results[m].values()):>16,.0f}'
    print(row)

    print('\n=== 相对基线 ===')
    s0 = sum(results['S0'].values())
    am2 = sum(results['A_M2'].values())
    go = sum(results['gate_only'].values())
    dbl = sum(results['double'].values())
    print(f'  S0 现状               = {s0:+,.0f}')
    print(f'  A M2 无闸门           = {am2:+,.0f}  vs S0 = {am2-s0:+,.0f}')
    print(f'  仅闸门（方案2 兜底）   = {go:+,.0f}  vs S0 = {go-s0:+,.0f}')
    print(f'  双层 A M2 + 闸门       = {dbl:+,.0f}  vs S0 = {dbl-s0:+,.0f}  vs A_M2 = {dbl-am2:+,.0f}')

    print('\n=== 诊断：每年熊市闸门拦截情况 ===')
    print(f'{"年份":<6}{"总笔":>6}{"拦截":>6}{"拦截率":>8}{"拦截笔A_M2净利":>18}{"留下":>6}')
    print('-' * 50)
    for y in YEARS:
        d = diag[y]
        rate = d['skipped'] / d['total'] * 100 if d['total'] else 0
        print(f'{y:<6}{d["total"]:>6}{d["skipped"]:>6}{rate:>7.0f}%'
              f'{d["skipped_pnl"]:>18,.0f}{d["total"]-d["skipped"]:>6}')

    print('\n=== 双层逐年 vs A_M2 逐年（看闸门救回了多少）===')
    print(f'{"年份":<6}{"A_M2":>14}{"双层":>14}{"救回":>14}')
    for y in YEARS:
        a = results['A_M2'][y]
        d = results['double'][y]
        print(f'{y:<6}{a:>14,.0f}{d:>14,.0f}{d-a:>+14,.0f}')


if __name__ == '__main__':
    main()
