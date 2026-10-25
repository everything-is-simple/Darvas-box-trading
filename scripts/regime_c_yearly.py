# -*- coding: utf-8 -*-
"""变体 C 判据（20日箱体 vs 20日前箱体·高低点双确认）在 2014-2018 各年的样子。

输出：var/bt/regime_c_yearly.html —— 5 个 SVG 时间线面板（指数叠加三态色带）+ 逐年统计表。

色带（A股惯例）：升势=红、降势=绿、猴市=灰。
"""
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, r'I:\Darvas-box-trading\scripts')
import regime_lifespan_stats as R

DB = R.DB
YEARS = (2014, 2015, 2016, 2017, 2018)
COLOR = {'up': '#e74c3c', 'down': '#27ae60', 'range': '#bdc3c7'}
NAME = {'up': '升势(牛)', 'down': '降势(熊)', 'range': '猴市'}


def c_labels_2014_2018():
    con = sqlite3.connect(DB)
    dates, lows, highs, closes = R.load_bars(con)
    labels = R.label_boxstep(dates, lows, highs, 20, 20)
    # 5日确认平滑
    d = [x for x in dates if x in labels]
    c = [x for dd, x in zip(dates, closes) if dd in labels]
    raw = [labels[x] for x in d]
    seq = R.smooth(raw, R.K_CONFIRM)
    return d, c, seq


def year_data(dates, closes, seq, year):
    items = [(dt, cl, s) for dt, cl, s in zip(dates, closes, seq) if dt.startswith(str(year))]
    return items


def episodes_in(items):
    eps = []
    cur = items[0][2]
    start = prev = items[0][0]
    s_close = items[0][1]
    for dt, cl, s in items[1:]:
        if s != cur:
            eps.append((cur, start, prev, prev_close, cl / s_close - 1.0))
            cur, start, s_close = s, dt, cl
        prev, prev_close = dt, cl
    eps.append((cur, start, prev, prev_close, cl / s_close - 1.0))
    return eps


def svg_panel(items, year):
    n = len(items)
    prices = [it[1] for it in items]
    pmin, pmax = min(prices), max(prices)
    span = pmax - pmin or 1
    W, H = 720, 220
    L, Rm, T, Bm = 40, 12, 14, 26
    pw, ph = W - L - Rm, H - T - Bm
    dx = pw / max(n - 1, 1)

    def x(i): return L + i * dx
    def y(p): return T + (pmax - p) / span * ph

    parts = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" class="panel">'
             f'<text x="{L}" y="13" class="title">{year} 上证指数 + C判据三态色带</text>']
    # 色带：每个交易日一条 1px 宽的竖条
    for i, (dt, cl, s) in enumerate(items):
        x0 = x(i) - dx / 2
        parts.append(f'<rect x="{x0:.1f}" y="{T}" width="{dx+0.6:.1f}" height="{ph}" fill="{COLOR[s]}" opacity="0.45"/>')
    # 指数折线
    pts = ' '.join(f'{x(i):.1f},{y(it[1]):.1f}' for i, it in enumerate(items))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#2c3e50" stroke-width="1.2"/>')
    # 边框 + 起止价标
    parts.append(f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="none" stroke="#888"/>')
    parts.append(f'<text x="{L}" y="{H-8}" class="axis">{pmin:.0f}</text>')
    parts.append(f'<text x="{W-Rm-40}" y="{H-8}" class="axis">{pmax:.0f}</text>')
    parts.append(f'<text x="{L}" y="{T-2}" class="axis">起 {items[0][0]}</text>')
    parts.append(f'<text x="{W-Rm-50}" y="{T-2}" class="axis">止 {items[-1][0]}</text>')
    parts.append('</svg>')
    return '\n'.join(parts)


def main():
    dates, closes, seq = c_labels_2014_2018()
    html = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
            '<title>变体C三态 2014-2018</title>',
            '<style>body{font-family:-apple-system,"PingFang SC",sans-serif;background:#fafafa;'
            'margin:20px;color:#2c3e50}h1{font-size:18px}'
            '.panel{display:block;margin:8px auto}'
            '.title{font-size:11px;fill:#2c3e50;font-weight:600}'
            '.axis{font-size:9px;fill:#666}'
            'table{border-collapse:collapse;margin:14px auto;font-size:12px}'
            'th,td{border:1px solid #ccc;padding:4px 8px}th{background:#ecf0f1}'
            '.legend span{display:inline-block;width:14px;height:10px;margin:0 4px -1px 0}'
            '</style></head><body>']
    html.append('<h1>变体 C（20日箱体 vs 20日前箱体·双确认·5日确认）2014-2018 各年样子</h1>')
    html.append('<div class="legend">'
                '<span style="background:#e74c3c"></span>升势(牛) '
                '<span style="background:#27ae60"></span>降势(熊) '
                '<span style="background:#bdc3c7"></span>猴市(震荡) '
                '<span style="border-top:2px solid #2c3e50;display:inline-block;width:18px;height:0;margin:0 4px 4px"></span>上证指数')
    html.append('</div>')

    # 逐年统计
    html.append('<h2>逐年统计</h2><table><tr><th>年份</th><th>交易日</th>'
                '<th>升势天数/占比</th><th>降势天数/占比</th><th>猴市天数/占比</th>'
                '<th>升势段数/中位寿命</th><th>降势段数/中位寿命</th><th>猴市段数/中位寿命</th>'
                '<th>指数涨跌</th></tr>')
    for y in YEARS:
        items = year_data(dates, closes, seq, y)
        if not items:
            continue
        n = len(items)
        cnt = defaultdict(int)
        for it in items:
            cnt[it[2]] += 1
        eps = episodes_in(items)
        bs = defaultdict(list)
        for e in eps:
            bs[e[0]].append(e)
        def med(lst):
            return R.pctiles(lst)[50] if lst else 0
        idx_ret = items[-1][1] / items[0][1] - 1
        html.append(
            f"<tr><td><b>{y}</b></td><td>{n}</td>"
            f"<td>{cnt['up']}/{cnt['up']/n*100:.0f}%</td>"
            f"<td>{cnt['down']}/{cnt['down']/n*100:.0f}%</td>"
            f"<td>{cnt['range']}/{cnt['range']/n*100:.0f}%</td>"
            f"<td>{len(bs['up'])}/{med([e[3] for e in bs['up']]):.0f}日</td>"
            f"<td>{len(bs['down'])}/{med([e[3] for e in bs['down']]):.0f}日</td>"
            f"<td>{len(bs['range'])}/{med([e[3] for e in bs['range']]):.0f}日</td>"
            f"<td>{idx_ret*100:+.1f}%</td></tr>")
    html.append('</table>')

    # 逐年面板
    html.append('<h2>逐年时间线</h2>')
    for y in YEARS:
        items = year_data(dates, closes, seq, y)
        if not items:
            continue
        html.append(svg_panel(items, y))

    # 逐年大期段明细
    html.append('<h2>逐年期段明细（持续 ≥10 交易日）</h2>')
    for y in YEARS:
        items = year_data(dates, closes, seq, y)
        if not items:
            continue
        eps = [e for e in episodes_in(items) if e[3] >= 10]
        html.append(f'<h3>{y}</h3><table><tr><th>状态</th><th>起</th><th>止</th>'
                    f'<th>交易日</th><th>幅度</th></tr>')
        for e in eps:
            html.append(f"<tr><td><span style='color:{COLOR[e[0]]}'>■</span> {NAME[e[0]]}</td>"
                        f"<td>{e[1]}</td><td>{e[2]}</td><td>{e[3]}</td><td>{e[4]*100:+.1f}%</td></tr>")
        html.append('</table>')

    html.append('</body></html>')
    out = r'I:\Darvas-box-trading\var\bt\regime_c_yearly.html'
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(html))
    print('written ->', out)
    # 控制台同步打表
    for y in YEARS:
        items = year_data(dates, closes, seq, y)
        if not items:
            continue
        n = len(items)
        cnt = defaultdict(int)
        for it in items:
            cnt[it[2]] += 1
        print(f"{y}: {n}日  升{cnt['up']}/{cnt['up']/n*100:.0f}%  "
              f"降{cnt['down']}/{cnt['down']/n*100:.0f}%  "
              f"猴{cnt['range']}/{cnt['range']/n*100:.0f}%  "
              f"指数 {items[-1][1]/items[0][1]-1:+.1%}")


if __name__ == '__main__':
    main()
