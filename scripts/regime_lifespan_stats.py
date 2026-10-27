# -*- coding: utf-8 -*-
"""牛熊猴三态寿命统计 —— A 股版「雷亚归类」。

判据变体（都是达瓦斯箱体语言：N 日箱体上下轨的抬升/下移）：
  A) 60日低点（docs/00 §5.6.8 回测同源）：
     升势 = 当日60日最低 > 前一日60日最低 × 1.002
     降势 = 当日60日最低 < 前一日60日最低 × 0.998
  B) 20日高低点双确认（道氏"更高高点+更高低点"原义，2026-10-22 用户拍板）：
     升势 = 20日最低抬升 × 1.002 且 20日最高抬升 × 1.002
     降势 = 20日最低下移 × 0.998 且 20日最高下移 × 0.998
  猴市 = 其余

体制确认规则：新状态连续 5 交易日才确认翻转（滞回，道氏"确认日"思想）。
不确认则日频标签中位寿命仅 2 天，寿命统计无意义。

输出：var/bt/regime_lifespan_report.md
数据源：var/bt/full_hist_10pct.db 的 sh000001（1990-12-19 ~ 2026-10-16，8743 个交易日）。
"""
import sqlite3
import statistics as st
from collections import defaultdict

DB = r'I:\Darvas-box-trading\var\bt\full_hist_10pct.db'
UP_BIAS = 1.002
DOWN_BIAS = 0.998
K_CONFIRM = 5  # 体制确认天数：新状态连续 5 天才确认翻转

STATE_NAME = {'up': '升势(牛)', 'down': '降势(熊)', 'range': '猴市(震荡)'}


def load_bars(con):
    rows = con.execute(
        "SELECT date, low, high, close FROM daily_bar WHERE code='sh000001' ORDER BY date"
    ).fetchall()
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


def label_low(dates, lows, lookback):
    """判据 A：仅低点。"""
    labels = {}
    for i in range(lookback, len(dates)):
        cur = min(lows[i - lookback + 1:i + 1])
        prev = min(lows[i - lookback:i])
        if cur > prev * UP_BIAS:
            labels[dates[i]] = 'up'
        elif cur < prev * DOWN_BIAS:
            labels[dates[i]] = 'down'
        else:
            labels[dates[i]] = 'range'
    return labels


def label_hl(dates, lows, highs, lookback):
    """判据 B：低点与高点同时抬升/下移（道氏"更高高点+更高低点"）。"""
    labels = {}
    for i in range(lookback, len(dates)):
        cur_lo = min(lows[i - lookback + 1:i + 1])
        prev_lo = min(lows[i - lookback:i])
        cur_hi = max(highs[i - lookback + 1:i + 1])
        prev_hi = max(highs[i - lookback:i])
        if cur_lo > prev_lo * UP_BIAS and cur_hi > prev_hi * UP_BIAS:
            labels[dates[i]] = 'up'
        elif cur_lo < prev_lo * DOWN_BIAS and cur_hi < prev_hi * DOWN_BIAS:
            labels[dates[i]] = 'down'
        else:
            labels[dates[i]] = 'range'
    return labels


def label_boxstep(dates, lows, highs, lookback, step):
    """判据 C：箱体对箱体——今日20日箱体 vs step 日前的20日箱体（达瓦斯"箱体抬升"原义）。

    与 A/B 的本质区别：A/B 比相邻两天（量"下轨移动速度"，只能抓到 >0.2%/日 的井喷）；
    C 比间隔 step 天的两个箱体（量"箱体有没有换层"），慢趋势也能识别。
    """
    labels = {}
    for i in range(lookback + step, len(dates)):
        cur_lo = min(lows[i - lookback + 1:i + 1])
        cur_hi = max(highs[i - lookback + 1:i + 1])
        prev_lo = min(lows[i - lookback - step + 1:i - step + 1])
        prev_hi = max(highs[i - lookback - step + 1:i - step + 1])
        if cur_lo > prev_lo * UP_BIAS and cur_hi > prev_hi * UP_BIAS:
            labels[dates[i]] = 'up'
        elif cur_lo < prev_lo * DOWN_BIAS and cur_hi < prev_hi * DOWN_BIAS:
            labels[dates[i]] = 'down'
        else:
            labels[dates[i]] = 'range'
    return labels


def smooth(labels_seq, k):
    out = []
    regime = labels_seq[0]
    pending = None
    run = 0
    for s in labels_seq:
        if s == regime:
            pending, run = None, 0
            out.append(regime)
            continue
        if s == pending:
            run += 1
        else:
            pending, run = s, 1
        if run >= k:
            regime = pending
            pending, run = None, 0
        out.append(regime)
    return out


def segment_labeled(dates, closes, seq):
    idx = {d: i for i, d in enumerate(dates)}
    episodes = []
    cur_state = seq[0]
    start = prev = dates[0]
    for d, s in zip(dates[1:], seq[1:]):
        if s != cur_state:
            i0, i1 = idx[start], idx[prev]
            episodes.append((cur_state, start, prev, i1 - i0 + 1, closes[i1] / closes[i0] - 1.0))
            cur_state, start = s, d
        prev = d
    i0, i1 = idx[start], idx[prev]
    episodes.append((cur_state, start, prev, i1 - i0 + 1, closes[i1] / closes[i0] - 1.0))
    return episodes


def pctiles(xs, ps=(10, 25, 50, 75, 90)):
    xs = sorted(xs)
    n = len(xs)
    out = {}
    for p in ps:
        k = (n - 1) * p / 100.0
        lo, hi = int(k), min(int(k) + 1, n - 1)
        frac = k - lo
        out[p] = xs[lo] * (1 - frac) + xs[hi] * frac
    return out


def build_episodes(dates, lows, highs, closes, mode, lookback, step=20):
    if mode == 'low':
        labels = label_low(dates, lows, lookback)
    elif mode == 'hl':
        labels = label_hl(dates, lows, highs, lookback)
    else:
        labels = label_boxstep(dates, lows, highs, lookback, step)
    c = [x for d, x in zip(dates, closes) if d in labels]
    d = [x for x in dates if x in labels]
    raw_seq = [labels[x] for x in d]
    raw_n = len(segment_labeled(d, c, raw_seq))
    seq = smooth(raw_seq, K_CONFIRM)
    return d, c, segment_labeled(d, c, seq), raw_n


def stats_lines(episodes):
    """期段统计五表（时间占比/持续期/幅度/存活/剩余寿命）。"""
    by_state = defaultdict(list)
    for ep in episodes:
        by_state[ep[0]].append(ep)
    total_days = sum(e[3] for e in episodes)
    lines = []
    lines.append('| 状态 | 期段数 | 总交易日 | 时间占比 |')
    lines.append('|---|---|---|---|')
    for s in ('up', 'down', 'range'):
        eps = by_state[s]
        td = sum(e[3] for e in eps)
        lines.append(f'| {STATE_NAME[s]} | {len(eps)} | {td} | {td/total_days*100:.1f}% |')

    lines.append('\n**期段持续期分布（交易日 / 近似自然年=÷245）**\n')
    lines.append('| 状态 | 期段数 | P10 | P25 | 中位数 | P75 | P90 | 最大 | 平均 |')
    lines.append('|---|---|---|---|---|---|---|---|---|')
    for s in ('up', 'down', 'range'):
        ds = [e[3] for e in by_state[s]]
        p = pctiles(ds)
        lines.append(
            f"| {STATE_NAME[s]} | {len(ds)} | {p[10]:.0f} | {p[25]:.0f} | "
            f"**{p[50]:.0f}** (~{p[50]/245:.2f}年) | {p[75]:.0f} (~{p[75]/245:.2f}年) | "
            f"{p[90]:.0f} | {max(ds)} | {st.mean(ds):.0f} |")

    lines.append('\n**期段幅度分布（期段内指数涨跌幅 %）**\n')
    lines.append('| 状态 | P10 | P25 | 中位数 | P75 | P90 |')
    lines.append('|---|---|---|---|---|---|')
    for s in ('up', 'down', 'range'):
        ms = [e[4] * 100 for e in by_state[s]]
        p = pctiles(ms)
        lines.append(
            f"| {STATE_NAME[s]} | {p[10]:+.1f}% | {p[25]:+.1f}% | **{p[50]:+.1f}%** | "
            f"{p[75]:+.1f}% | {p[90]:+.1f}% |")

    horizons = [20, 60, 120, 250, 500]
    lines.append('\n**存活函数：P(期段持续 > N 交易日)**\n')
    lines.append('| 状态 | >20日 | >60日 | >120日 | >250日(1年) | >500日(2年) |')
    lines.append('|---|---|---|---|---|---|')
    for s in ('up', 'down', 'range'):
        ds = [e[3] for e in by_state[s]]
        cells = [f"{sum(1 for d in ds if d > h)/len(ds)*100:.0f}%" for h in horizons]
        lines.append(f"| {STATE_NAME[s]} | " + ' | '.join(cells) + ' |')

    lines.append('\n**剩余寿命表：期段已持续 d 天后，再存活 ≥X 交易日的经验概率**\n')
    lines.append('| 状态 | 已持续 | 样本数 | 再活≥20日 | 再活≥60日 | 再活≥120日 | 中位剩余寿命 |')
    lines.append('|---|---|---|---|---|---|---|')
    for s in ('up', 'down', 'range'):
        ds = sorted(e[3] for e in by_state[s])
        for d in (20, 60, 120, 250):
            alive = [x for x in ds if x >= d]
            if len(alive) < 4:
                continue
            rem = [x - d for x in alive]
            p20 = sum(1 for r in rem if r >= 20) / len(alive) * 100
            p60 = sum(1 for r in rem if r >= 60) / len(alive) * 100
            p120 = sum(1 for r in rem if r >= 120) / len(alive) * 100
            med_rem = pctiles(rem)[50]
            lines.append(
                f"| {STATE_NAME[s]} | {d}日 | {len(alive)} | {p20:.0f}% | {p60:.0f}% | "
                f"{p120:.0f}% | {med_rem:.0f}日 |")
    return lines


def med(episodes, state, col):
    return pctiles([e[col] for e in episodes if e[0] == state])[50]


def main():
    con = sqlite3.connect(DB)
    dates, lows, highs, closes = load_bars(con)

    # 变体 A：60日低点（docs/00 §5.6.8 回测同源）
    dA, cA, epA, rawA = build_episodes(dates, lows, highs, closes, 'low', 60)
    # 变体 B：20日高低点双确认·相邻日比（用户拍板 2026-10-22）
    dB, cB, epB, rawB = build_episodes(dates, lows, highs, closes, 'hl', 20)
    # 变体 C：20日箱体 vs 20日前箱体·高低点双确认（箱体抬升原义，B 的修正版）
    dC, cC, epC, rawC = build_episodes(dates, lows, highs, closes, 'boxstep', 20, 20)

    lines = []
    lines.append('# A股牛熊猴三态寿命统计（1990-12 ~ 2026-10，sh000001，8743 交易日）\n')
    lines.append('- 判据都是达瓦斯箱体语言（N日箱体上下轨的抬升/下移）：')
    lines.append('  - **变体 A** = 60日低点抬升/下移·相邻日比（docs/00 §5.6.8 回测同源）')
    lines.append('  - **变体 B** = 20日低点+高点同时抬升/下移·相邻日比（2026-10-22 用户拍板；')
    lines.append('    实测太严——36年仅4段升势，1992-2014整段归为一个大猴市，统计上不可用）')
    lines.append('  - **变体 C** = 20日箱体 vs 20日前箱体·高低点双确认（箱体抬升原义，B 的修正版）')
    lines.append(f'- **体制确认规则：新状态连续 {K_CONFIRM} 交易日才确认翻转**（滞回，道氏"确认日"思想）。')
    lines.append(f'  原始日频标签碎片：A={rawA} 段 / B={rawB} 段 / C={rawC} 段。\n')

    lines.append(f'## 1. 变体 A：60日低点·相邻日比（确认后期段 {len(epA)} 个）\n')
    lines.extend(stats_lines(epA))
    lines.append(f'\n## 2. 变体 B：20日高低点·相邻日比（确认后期段 {len(epB)} 个，退化仅列概况）\n')
    lines.append('| 状态 | 期段数 | 时间占比 | 中位寿命 | 幅度中位 |')
    lines.append('|---|---|---|---|---|')
    for s in ('up', 'down', 'range'):
        eps = [e for e in epB if e[0] == s]
        if not eps:
            continue
        tot = sum(e[3] for e in epB)
        lines.append(f"| {STATE_NAME[s]} | {len(eps)} | {sum(e[3] for e in eps)/tot*100:.1f}% | "
                     f"{pctiles([e[3] for e in eps])[50]:.0f}日 | {pctiles([e[4] for e in eps])[50]*100:+.1f}% |")
    lines.append('\n> B 退化的原因：要求 20 日低点与高点**同时**以 >0.2%/日 的速度同向移动并持续 5 日，'
                 '只有井喷式行情满足；1992-2014 被归为一段 5579 日的"猴市"。')
    lines.append(f'\n## 3. 变体 C：20日箱体 vs 20日前箱体（确认后期段 {len(epC)} 个）★推荐\n')
    lines.extend(stats_lines(epC))

    # 历史大期段明细（A、C 合并；B 退化略）
    lines.append('\n## 4. 历史大期段明细（持续 ≥250 交易日）\n')
    lines.append('| 判据 | 状态 | 起 | 止 | 交易日 | 幅度 |')
    lines.append('|---|---|---|---|---|---|')
    for tag, eps in (('A:60日低点', epA), ('C:20日箱体对箱体', epC)):
        for e in sorted([e for e in eps if e[3] >= 250], key=lambda x: x[1]):
            lines.append(f'| {tag} | {STATE_NAME[e[0]]} | {e[1]} | {e[2]} | {e[3]} | {e[4]*100:+.1f}% |')

    # 道氏对照（A、C 并列）
    lines.append('\n## 5. 与道氏/斯波朗迪美股统计对照（《专业投机原理》第4章）\n')
    lines.append('| 维度 | 道氏/雷亚/斯波朗迪（美股1896-1989） | A：60日低点 | C：20日箱体对箱体 |')
    lines.append('|---|---|---|---|')
    for state, name, dow_dur, dow_mag in (
            ('up', '牛市', '2.33年（75%超1.8年，67%在1.8-4.1年）', '+77.5%'),
            ('down', '熊市', '1.1年（75%在0.8-2.8年）', '-29.4%（75%在-20.4%~-47.1%）'),
            ('range', '**猴市**', '**道氏无统计（用户洞察属实）**', '—')):
        dA_med, dC_med = med(epA, state, 3), med(epC, state, 3)
        mA, mC = med(epA, state, 4) * 100, med(epC, state, 4) * 100
        lines.append(f'| {name}期限中位 | {dow_dur} | {dA_med:.0f}日≈{dA_med/245:.2f}年 | {dC_med:.0f}日≈{dC_med/245:.2f}年 |')
        if dow_mag != '—':
            lines.append(f'| {name}幅度中位 | {dow_mag} | {mA:+.1f}% | {mC:+.1f}% |')

    lines.append('\n## 6. 窗口稳健性（判据 A 换 120/250 日窗口，5日确认）\n')
    lines.append('| 窗口 | 升势中位寿命 | 降势中位寿命 | 猴市中位寿命 | 猴市时间占比 |')
    lines.append('|---|---|---|---|---|')
    for lb in (60, 120, 250):
        _, _, epX, _ = build_episodes(dates, lows, highs, closes, 'low', lb)
        tot = sum(e[3] for e in epX)
        rg = sum(e[3] for e in epX if e[0] == 'range')
        lines.append(f'| {lb}日 | {med(epX,"up",3):.0f}日 | {med(epX,"down",3):.0f}日 | '
                     f'{med(epX,"range",3):.0f}日 | {rg/tot*100:.1f}% |')

    lines.append('\n## 7. 诚实标注与设计含义\n')
    lines.append('- **A/B 是"速度计"，C 才是"换层计"**：A/B 比相邻两天的箱体（量上下轨移动速度），')
    lines.append('  只能抓到 >0.2%/日 的井喷，慢牛慢熊全漏（A 下 2006-07 +175.6% 大牛市被标猴市）；')
    lines.append('  C 比间隔 20 天的两个箱体（达瓦斯"箱体抬升"原义），慢趋势也能识别。')
    lines.append('- **C 的口径注意**：期段幅度是"整段收盘价涨跌"，而标签说的是"箱体 vs 20日前箱体"——')
    lines.append('  升势期段中位幅度 ~0% 是正常的：标签在涨后平台期仍持续（箱体仍高于20日前）。')
    lines.append('- C 下三态时间占比均衡（升~40%/降~35%/猴~24%），中位寿命 26/23/11 交易日；')
    lines.append('  升势最长一段 259 交易日（~1 年）——接近道氏次级/主要之间的尺度，仍不到道氏的 2.33 年。')
    lines.append('- 要道氏尺度的牛熊寿命（数年），须另起周线箱体阶梯（docs/00 §5.6.8：默认箱体参数扫指数太粗）。')
    lines.append('- 1990 年代早期市场极不成熟，期段幅度失真（大期段明细可甄别）。')
    lines.append('- **设计含义**：若三态门换用 C 判据，"升势放大仓位"窗口从 A 的 ~10 日放宽到 ~26 日，')
    lines.append('  且猴市从"默认态(94%)"变成"过渡态(24%)"——仓位表的三态频率假设要重估。')
    lines.append('  换判据=重跑方向3回测，这是下一步待拍板项。')

    report = '\n'.join(lines)
    out = r'I:\Darvas-box-trading\var\bt\regime_lifespan_report.md'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(report)
    print(report)
    print('\nwritten ->', out)


if __name__ == '__main__':
    main()
