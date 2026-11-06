#!/usr/bin/env python3
"""
Step 0 零回测：行业轮动/行业动量 正式探针（docs/57 §6 预注册版）

只用基线库账本 + daily_bar_adj，不跑回测、不改引擎、0 机时。

回答一个问题：基线 1,142 笔的盈亏，是否随「入场日所属行业的相对动量排名」单调变化？
若单调 → 行业动量可作选股过滤器（veto 最热组 / 或 selector 追热组）。
若跨窗口翻符号 / 年内配对不稳健 → 判「不可用」，行业层关闭。

口径（与 docs/57 §5.6a 一致）：
- 行业强度(t) = 行业等权价格在 [t-W, t] 的收益率 − 上证指数同期收益率，W ∈ {20,60,120}
- 等权：库内 daily_bar_adj 按 tdxhy.cfg 行业分组，逐日算术平均
- 排名：132 个行业按超额收益从高到低，取该笔所属行业名次 → 分带
- 年内配对差（R29）：逐年算「最强带 vs 最弱带」的盈亏/胜率差，看同向年份数
- 欧尼尔口径并列：近 6 个月(≈120 交易日)行业组排名，前 15% vs 后 15%

输出：
- 每个窗口的分带表（笔数/胜率/盈亏/R28 三列）
- 年内配对差表（逐年最强带 vs 最弱带）
- 欧尼尔 6 个月口径
- 结论判定（对齐 §6 门槛：同向 ≥10/14 年 且 剔前3 不转负）

用法：python scripts/industry_rotation_step0.py
"""
import sqlite3
import bisect
from collections import defaultdict, Counter

BASE = "file:var/bt/base/bt_baseA_cont_adj.db?mode=ro"
HY_CFG = "D:/new_tdx64/T0002/hq_cache/tdxhy.cfg"
INDEX = "sh000001"


def load_map():
    """tdxhy.cfg → {code: T2行业代码}"""
    mp = {}
    for l in open(HY_CFG, "rb").read().decode("gbk", errors="replace").splitlines():
        p = l.split("|")
        if len(p) > 2 and p[2].startswith("T"):
            pre = {"0": "sz", "1": "sh", "2": "bj"}.get(p[0])
            if pre:
                mp[pre + p[1]] = p[2]
    return mp


def build_cum(db, mp):
    """行业等权累计净值序列 {ind: [(date, cum)]}，交易日历用上证"""
    cal = [r[0] for r in db.execute(
        f"select date from daily_bar where code='{INDEX}' and close>0 order by date")]
    px = defaultdict(dict)
    for code, d, c in db.execute("select code,date,adj_close from daily_bar_adj where adj_close>0"):
        ind = mp.get(code)
        if ind:
            px[ind][d] = px[ind].get(d, []) + [c]
    cum = {}
    for ind, d in px.items():
        series = []
        prev = None
        for dt in cal:
            v = d.get(dt)
            if v:
                m = sum(v) / len(v)
                if prev is None:
                    series.append((dt, 1.0))
                else:
                    series.append((dt, series[-1][1] * m / prev))
                prev = m
            elif series:
                series.append((dt, series[-1][1]))
        cum[ind] = series
    return cal, cum


def build_idx(db, cal):
    ic = {d: c for d, c in db.execute(
        f"select date,close from daily_bar where code='{INDEX}' and close>0")}
    return [ic[d] for d in cal if d in ic]


def rank_positions(cal, cum, idxvals, mp, rows, W):
    """每笔交易返回 (排名位置, pnl, opened_year)。位置 None=未映射/数据不足。"""
    dates_by_ind = {ind: [x[0] for x in s] for ind, s in cum.items()}
    icdates = [d for d in cal if d in idxvals]

    def r(ind, dt):
        ds = dates_by_ind[ind]
        s = cum[ind]
        i = bisect.bisect_left(ds, dt)
        if i < W:
            return None
        return s[i - 1][1] / s[i - W][1] - 1

    def ridx(dt):
        i = bisect.bisect_left(icdates, dt)
        if i < W:
            return None
        return idxvals[i - 1] / idxvals[i - W] - 1

    out = []
    for code, od, pnl in rows:
        dt = od[:10]
        year = od[:4]
        if dt not in dates_by_ind.get("__x__", []) and False:
            pass
        ir = {ind: r(ind, dt) for ind in cum}
        valid = {k: v for k, v in ir.items() if v is not None}
        if len(valid) < 60:
            out.append((None, pnl, year))
            continue
        base = ridx(dt) or 0
        excess = {k: v - base for k, v in valid.items()}
        rk = sorted(excess, key=excess.get, reverse=True)
        myind = mp.get(code)
        pos = rk.index(myind) + 1 if myind in excess else None
        out.append((pos, pnl, year))
    return out


def bands_from(res):
    """分带：1-20 / 21-40 / 41-66 / 67-132 / NA"""
    b = defaultdict(lambda: [0, 0.0, 0])  # n, pnl, wins
    for pos, pnl, _ in res:
        if pos is None:
            key = "NA"
        elif pos <= 20:
            key = "1-20"
        elif pos <= 40:
            key = "21-40"
        elif pos <= 66:
            key = "41-66"
        else:
            key = "67-132"
        b[key][0] += 1
        b[key][1] += pnl
        b[key][2] += 1 if pnl > 0 else 0
    return b


def within_year_pairs(res, W):
    """年内配对差：逐年 最强带(1-20) vs 最弱带(67-132) 的 盈亏差 + 胜率差。
    返回 (同向年份数, 明细列表)"""
    by_year = defaultdict(lambda: {"hot": [0, 0.0, 0], "cold": [0, 0.0, 0]})
    for pos, pnl, year in res:
        if pos is None:
            continue
        key = "hot" if pos <= 20 else "cold" if pos > 66 else None
        if key is None:
            continue
        by_year[year][key][0] += 1
        by_year[year][key][1] += pnl
        by_year[year][key][2] += 1 if pnl > 0 else 0

    detail = []
    same_sign = 0
    for y in sorted(by_year):
        h, c = by_year[y]["hot"], by_year[y]["cold"]
        dp = h[1] - c[1]  # 热-冷盈亏差
        detail.append((y, h[0], c[0], h[1], c[1], dp))
        if dp > 0:
            same_sign += 1  # 热>冷
    return same_sign, detail


def main():
    db = sqlite3.connect(BASE, uri=True)
    mp = load_map()
    cal, cum = build_cum(db, mp)
    # idxvals 需与 cal 对齐长度一致（build_idx 已过滤 close>0 的日期）
    ic = {d: c for d, c in db.execute(
        f"select date,close from daily_bar where code='{INDEX}' and close>0")}
    idxvals = [ic[d] for d in cal if d in ic]
    rows = db.execute(
        "select code,cast(opened_date as text),realized_pnl from position where status='closed'").fetchall()
    print(f"== 数据规模：交易日 {len(cal)}｜行业数 {len(cum)}｜建仓 {len(rows)} 笔｜映射率 "
          f"{sum(1 for c,_,_ in rows if c in mp)/len(rows)*100:.1f}%\n")

    for W in (20, 60, 120):
        res = rank_positions(cal, cum, idxvals, mp, rows, W)
        b = bands_from(res)
        same, detail = within_year_pairs(res, W)
        print(f"===== 窗口 W={W} 交易日 =====")
        print(f"{'带':<9}{'笔数':>6}{'胜率':>8}{'盈亏':>14}{'均笔':>10}")
        for k in ["1-20", "21-40", "41-66", "67-132", "NA"]:
            n, pnl, w = b[k]
            wr = f"{w/n*100:.1f}%" if n else "—"
            avg = pnl / n if n else 0
            print(f"{k:<9}{n:>6}{wr:>8}{pnl:>14,.0f}{avg:>10,.0f}")
        print(f"  年内配对差（热1-20 vs 冷67-132）：同向(热>冷) {same}/14 年")
        for y, hn, cn, hp, cp, dp in detail:
            flag = "  ◀热更优" if dp > 0 else ""
            print(f"    {y}  热{hn:>3}笔{hp:>+12,.0f}  冷{cn:>3}笔{cp:>+12,.0f}  差{dp:>+12,.0f}{flag}")
        print()

    # 欧尼尔 6 个月口径（120 日即 6 个月近似）
    res = rank_positions(cal, cum, idxvals, mp, rows, 120)
    print("===== 欧尼尔口径：近 6 个月(120日) 排名，前15% vs 后15% =====")
    n_ranked = 0
    hot15 = [0, 0.0, 0]; cold15 = [0, 0.0, 0]
    ntotal = sum(len(v) for v in cum.values())
    # 行业总数
    nind = len(cum)
    top_cut = max(1, int(nind * 0.15))
    for pos, pnl, _ in res:
        if pos is None:
            continue
        n_ranked += 1
        if pos <= top_cut:
            hot15[0] += 1; hot15[1] += pnl; hot15[2] += 1 if pnl > 0 else 0
        elif pos > nind - top_cut:
            cold15[0] += 1; cold15[1] += pnl; cold15[2] += 1 if pnl > 0 else 0
    print(f"  行业总数 {nind}，前15%=前{top_cut}组")
    for name, v in (("前15%(该买)", hot15), ("后15%(该避)", cold15)):
        wr = f"{v[2]/v[0]*100:.1f}%" if v[0] else "—"
        print(f"    {name:<12} {v[0]:>4}笔  {wr:>7}  {v[1]:>+12,.0f}")


if __name__ == "__main__":
    main()
