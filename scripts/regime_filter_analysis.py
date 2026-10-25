#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""牛熊开关 + 仓位规则 叠加五年回测交易的分析报告。

规则（用户 2026-10-22 拍板）：
  牛熊判据：sh000001 收盘 > 200日均线 且 200日均线向上 = 牛，否则 = 熊。
  仓位规则：
    熊市（防守）：只用试验仓，单只 1 单位，最多 5 只，总仓位 5 单位截止。
                切换熊市当天立即清空全部持仓（达瓦斯式）。
    牛市（进攻）：试验+主力合一，单只头寸按「当前实际持仓数反推」：
                总仓位 ≤70% 权益，单只单位 = 目标总仓位 / 当前持仓数。
                名义参考：6-7 只 → 单只 5 单位；10 只 → 单只 3 单位。

本脚本是【分析报告】，不动交易逻辑，只在已平仓交易上叠加牛熊标签做情景对比。
输出：五年牛熊分段 + 三情景对比（现状 / 熊市清仓 / 熊市清仓+牛市2单位）。
"""
import sqlite3
from collections import defaultdict

YEARS = ['2014', '2015', '2016', '2017', '2018']
SRC = 'var/bt/full_hist_10pct.db'


def load_index_bull():
    """返回 date -> bool(是否牛) 的字典（sh000001 200日均线）。"""
    con = sqlite3.connect(f'file:{SRC}?mode=ro', uri=True)
    rows = con.execute(
        "SELECT date, close FROM daily_bar WHERE code='sh000001' "
        "AND date>='2013-06-01' AND date<='2018-12-31' ORDER BY date").fetchall()
    con.close()
    closes = [(r[0], r[1]) for r in rows]
    n = len(closes)
    out = {}
    for i in range(n):
        if i < 199:
            continue
        ma200 = sum(closes[i-199+j][1] for j in range(200)) / 200.0
        ma200_prev = sum(closes[i-200+j][1] for j in range(200)) / 200.0
        price = closes[i][1]
        out[closes[i][0]] = (price > ma200) and (ma200 > ma200_prev)
    return out


def load_trades():
    """返回 [(year, opened_date, realized_pnl)] 五年已平仓交易。"""
    trades = []
    for y in YEARS:
        con = sqlite3.connect(f'file:var/bt/bt_p05_{y}.db?mode=ro', uri=True)
        for r in con.execute(
                "SELECT opened_date, realized_pnl FROM position WHERE status='closed'"):
            trades.append((y, r[0], r[1] or 0.0))
        con.close()
    return trades


def main():
    bull = load_index_bull()

    # 每年牛熊天数
    print('=' * 70)
    print('一、sh000001 200日均线 牛熊分段（2014-2018）')
    print('=' * 70)
    yearly = defaultdict(lambda: [0, 0])
    for d, is_bull in bull.items():
        y = d[:4]
        if y in YEARS:
            yearly[y][0 if is_bull else 1] += 1
    for y in YEARS:
        b, br = yearly[y]
        tot = b + br
        print(f'  {y}: 牛 {b:3d}天({b/tot*100:3.0f}%)  熊 {br:3d}天({br/tot*100:3.0f}%)')

    trades = load_trades()

    # 叠加标签：每笔交易看它的 opened_date 当天是牛是熊
    print()
    print('=' * 70)
    print('二、每笔交易按「开仓日牛熊」归类')
    print('=' * 70)
    by_regime = defaultdict(lambda: {'n': 0, 'wins': 0, 'pnl': 0.0})
    unlabeled = 0
    for y, od, pnl in trades:
        if od not in bull:
            unlabeled += 1
            continue
        r = '牛' if bull[od] else '熊'
        by_regime[r]['n'] += 1
        by_regime[r]['pnl'] += pnl
        if pnl > 0:
            by_regime[r]['wins'] += 1
    for r in ['牛', '熊']:
        t = by_regime[r]
        if t['n'] == 0:
            continue
        wr = t['wins'] / t['n'] * 100
        print(f'  {r}市开仓: {t["n"]:3d}笔  胜率{wr:4.1f}%  净利{t["pnl"]:+,.0f}')
    if unlabeled:
        print(f'  (无标签 {unlabeled} 笔，多为2014年初 MA200 预热期)')

    # 情景对比
    print()
    print('=' * 70)
    print('三、情景对比（净利口径，单位：元）')
    print('=' * 70)

    # 情景1：现状（无过滤）
    s1 = sum(p for _, _, p in trades)

    # 情景2：熊市开仓的交易全部剔除（近似"熊市空仓"）
    s2 = sum(p for y, od, p in trades
             if od in bull and bull[od])

    # 情景3：熊市只 1 单位、牛市 2 单位（现状是 1 单位基准）
    #   熊市 = 原样 1 单位；牛市 = 2 单位（翻倍）
    s3 = 0.0
    for y, od, p in trades:
        if od not in bull:
            s3 += p  # 无标签按原样
        elif bull[od]:
            s3 += p * 2.0  # 牛市翻倍
        else:
            s3 += p  # 熊市 1 单位不变

    print(f'  现状（无过滤，1单位）              : {s1:+,.0f}')
    print(f'  情景A 熊市空仓（牛单1单位）        : {s2:+,.0f}   ({s2-s1:+,.0f})')
    print(f'  情景B 熊1单位+牛2单位              : {s3:+,.0f}   ({s3-s1:+,.0f})')

    # 分年明细
    print()
    print('=' * 70)
    print('四、分年净利对比')
    print('=' * 70)
    print(f'  {"年份":<6}{"现状":>12}{"熊市空仓":>12}{"熊1牛2":>12}')
    for y in YEARS:
        a = sum(p for yy, od, p in trades if yy == y)
        b = sum(p for yy, od, p in trades if yy == y and od in bull and bull[od])
        c = 0.0
        for yy, od, p in trades:
            if yy != y:
                continue
            if od not in bull:
                c += p
            elif bull[od]:
                c += p * 2.0
            else:
                c += p
        print(f'  {y:<6}{a:>+12,.0f}{b:>+12,.0f}{c:>+12,.0f}')


if __name__ == '__main__':
    main()
