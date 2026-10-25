#!/usr/bin/env python3
"""方向3 五年回测 —— 用 C 判据（20日箱体 vs 20日前箱体·高低点双确认·5日确认）替代 A 判据。

仓位规则同方向3：不砍开仓，只在单笔头寸随大盘收缩/放大，realized_pnl 与 qty 成正比（avg_cost 固定），
按开仓日的 C 标签乘缩放系数。

模式（对照之前 A 判据跑过的同口径）：
  S0  {升1, 震1, 降1}          = 现状
  M1  {升2, 震1, 降1}          = 只放大升势
  M2  {升2, 震0.5, 降1}        = 升放大 + 震荡收缩 (A下=+106,840)
  M3  {升3, 震0.5, 降1}        = 升3 + 震荡收缩 (A下=+159,696)
  M4  {升3, 震0.25, 降1}       = 升3 + 震荡更紧 (A下=+165,937)

A 判据五年合计对照（之前结果）：S0=+41,501 / M2=+106,840 / M3=+159,696 / M4=+165,937。
"""
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, r'I:\Darvas-box-trading\scripts')
import regime_lifespan_stats as R

YEARS = ['2014', '2015', '2016', '2017', '2018']
SRC = r'var\bt\full_hist_10pct.db'  # 指数日线
LBL_MAP = {'up': 1, 'down': -1, 'range': 0}

MODES = {
    'S0': {1: 1.0, 0: 1.0, -1: 1.0},
    'M1': {1: 2.0, 0: 1.0, -1: 1.0},
    'M2': {1: 2.0, 0: 0.5, -1: 1.0},
    'M3': {1: 3.0, 0: 0.5, -1: 1.0},
    'M4': {1: 3.0, 0: 0.25, -1: 1.0},
}


def load_c_labels():
    """用 C 判据（boxstep 20日/step20日/5日确认）给每个交易日打标签。"""
    con = sqlite3.connect(f'file:{SRC}?mode=ro', uri=True)
    dates, lows, highs, closes = R.load_bars(con)
    con.close()
    raw = R.label_boxstep(dates, lows, highs, 20, 20)
    d = [x for x in dates if x in raw]
    raw_seq = [raw[x] for x in d]
    seq = R.smooth(raw_seq, R.K_CONFIRM)
    return {date: LBL_MAP[s] for date, s in zip(d, seq)}


def load_positions(db):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT code, opened_date, closed_date, avg_cost, qty, realized_pnl "
        "FROM position WHERE status='closed'"
    ).fetchall()
    return con, rows


def simulate(positions, labels, mode):
    scales = MODES[mode]
    return sum((p['realized_pnl'] or 0.0) * scales[labels.get(p['opened_date'], 0)]
               for p in positions)


def main():
    labels = load_c_labels()
    print('=== 方向3 五年回测 —— C 判据（20日箱体对箱体·高低点双确认·5日确认）===\n')
    modes = list(MODES.keys())
    results = {m: {} for m in modes}
    diag = {y: defaultdict(lambda: [0, 0.0]) for y in YEARS}

    for y in YEARS:
        db = rf'var\bt\bt_p05_{y}.db'
        con, positions = load_positions(db)
        for p in positions:
            lbl = labels.get(p['opened_date'], 0)
            diag[y][lbl][0] += 1
            diag[y][lbl][1] += (p['realized_pnl'] or 0.0)
        for m in modes:
            results[m][y] = simulate(positions, labels, m)
        con.close()

    header = f'{"年份":<6}' + ''.join(f'{m:>16}' for m in
            ['S0现状', 'M1升2震1降1', 'M2升2震.5降1', 'M3升3震.5降1', 'M4升3震.25降1'])
    print(header)
    print('-' * len(header))
    for y in YEARS:
        row = f'{y:<6}'
        for m in modes:
            row += f'{results[m][y]:>16,.0f}'
        print(row)
    print('-' * len(header))
    row = f'{"合计":<6}'
    for m in modes:
        row += f'{sum(results[m].values()):>16,.0f}'
    print(row)

    print()
    print('=== 相对现状(S0)的提升 ===')
    s0 = sum(results['S0'].values())
    print(f'  S0 (C判据现状) = {s0:+,.0f}    [对照 A判据 S0 = +41,501]')
    for m in ['M1', 'M2', 'M3', 'M4']:
        tot = sum(results[m].values())
        print(f'  {m}: {tot:+,.0f}  提升 {tot - s0:+,.0f}')

    a_baseline = {
        'S0': 41501, 'M2': 106840, 'M3': 159696, 'M4': 165937,
    }
    print()
    print('=== A 判据 vs C 判据 五年合计对照 ===')
    print(f'{"模式":<22}{"A判据":>14}{"C判据":>14}{"差异":>14}')
    print('-' * 64)
    for m in ['S0', 'M2', 'M3', 'M4']:
        a = a_baseline.get(m, 0)
        c = int(round(sum(results[m].values())))
        print(f'{m:<22}{a:>14,}{c:>14,}{c-a:>+14,}')

    print()
    print('=== 诊断：每年开仓日 C 标签分布（笔数 / 净利 / 平均单笔）===')
    for y in YEARS:
        parts = []
        for lbl, name in [(1, '升'), (0, '震'), (-1, '降')]:
            n, pnl = diag[y][lbl]
            avg = pnl / n if n else 0
            parts.append(f'{name}{n}笔/{pnl:+,.0f}/均{avg:+,.0f}')
        print(f'  {y}: ' + '  '.join(parts))

    # 逐年差异表（C vs A 的 M2 升2震.5降1）
    print()
    print('=== M2（升2震.5降1）逐年 A vs C 对照 ===')
    # A 判据 M2 逐年（来自上一轮 results 没存，重算需要 A 标签）—— 这里仅列合计
    print(f'  A 判据五年合计 M2 = +106,840')
    print(f'  C 判据五年合计 M2 = {sum(results["M2"].values()):+,.0f}')
    print(f'  C 逐年：' + '  '.join(f'{y}={results["M2"][y]:+,.0f}' for y in YEARS))


if __name__ == '__main__':
    main()
