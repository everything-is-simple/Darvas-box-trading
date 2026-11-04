"""逐笔交易过程剖析（只读，不改引擎/参数）。

回答：出血在「买错」还是「卖错」？
对每笔已平仓交易还原全过程：
  买入上下文（阶梯档位 t1-t4 / 箱体上轨溢价 / 箱体高度）
  → 持有期最大浮盈 MFE、最大浮亏 MAE（**已按 dividend_event 复权**，避免送股造成假暴跌）
  → 出场原因、出场价、回吐幅度（MFE − 实际收益）
  → 出场后 5/10/20 个交易日走势（卖早了还是卖对了）

输出：
  var/reports/trade_anatomy.csv   全量逐笔明细
  stdout                          分组诊断

用法：python scripts/trade_anatomy.py [--db ...] [--src ...] [--out ...]
"""
import argparse
import bisect
import os
import sqlite3
import statistics as st
from collections import defaultdict


def conn(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="var/bt/base/bt_baseA_cont.db")
    ap.add_argument("--src", default="var/bt/full_hist_10pct.db")
    ap.add_argument("--out", default="var/reports/trade_anatomy.csv")
    a = ap.parse_args()

    c = conn(a.db)
    pos = c.execute("""
        SELECT id, code, opened_date, COALESCE(closed_date,''),
               qty, avg_cost, realized_pnl, COALESCE(close_reason,''),
               entry_box_id, stop_price, status
        FROM position ORDER BY opened_date, id""").fetchall()
    legs = c.execute("SELECT position_id, date, qty, price FROM position_leg").fetchall()
    sells = c.execute("""SELECT code, date, SUM(price*qty)/NULLIF(SUM(qty),0)
                         FROM trade_fill WHERE side='sell' GROUP BY code, date""").fetchall()
    boxmap = {r[0]: (r[1], r[2]) for r in c.execute("SELECT id, top, bottom FROM box")}
    c.close()

    buy = defaultdict(list)
    for pid, d, n, p in legs:
        buy[pid].append((d, n, p))
    sell = {(cc, d): px for cc, d, px in sells}

    src = conn(a.src)
    bars, divs = {}, {}
    for code in sorted({r[1] for r in pos}):
        bars[code] = src.execute(
            "SELECT date, high, low, close FROM daily_bar "
            "WHERE code=? AND date BETWEEN '2002-06-01' AND '2014-12-31' ORDER BY date",
            (code,)).fetchall()
        ev = src.execute(
            "SELECT date, bonus_10, share_bonus_10, allot_10, allot_price "
            "FROM dividend_event WHERE code=? ORDER BY date", (code,)).fetchall()
        # 除权比率 r = 除权参考价/前收；累计因子 F(t)=Π(1/r)
        ds, fs, f = [], [], 1.0
        for d, b10, s10, a10, ap_ in ev:
            if not bars[code]:
                continue
            idx = bisect.bisect_left([x[0] for x in bars[code]], d)
            if idx <= 0:
                continue
            prev = bars[code][idx - 1][2]
            if not prev or prev <= 0:
                continue
            num = prev - (b10 or 0) / 10.0 + (a10 or 0) / 10.0 * (ap_ or 0)
            den = prev * (1 + ((s10 or 0) + (a10 or 0)) / 10.0)
            r = num / den if den > 0 else 1.0
            if not (0.05 < r < 1.0):
                continue
            f *= 1.0 / r
            ds.append(d)
            fs.append(f)
        divs[code] = (ds, fs)
    src.close()

    def F(code, d):
        ds, fs = divs.get(code, ([], []))
        i = bisect.bisect_right(ds, d) - 1
        return fs[i] if i >= 0 else 1.0

    rows = []
    for pid, code, od, cd, qty, avgc, pnl, reason, bid, stop, status in pos:
        bl = bars.get(code) or []
        dates = [b[0] for b in bl]
        if not bl or od not in dates:
            continue
        ie = dates.index(od)
        ep = None
        if buy.get(pid):
            tq = sum(n for _, n, _ in buy[pid])
            ep = sum(n * p for _, n, p in buy[pid]) / tq if tq else None
        ep = ep or avgc
        if not ep:
            continue
        if cd and cd in dates:
            ix = dates.index(cd)
        elif cd:
            cand = [i for i, d in enumerate(dates) if d <= cd]
            ix = cand[-1] if cand else len(dates) - 1
        else:
            ix = len(dates) - 1
        if ix < ie:
            continue

        f0 = F(code, od)
        seg = bl[ie:ix + 1]
        adj = [(b[0], b[1] * F(code, b[0]) / f0, b[2] * F(code, b[0]) / f0) for b in seg]
        mfe = max(x[1] for x in adj) / ep - 1
        mae = min(x[2] for x in adj) / ep - 1

        xp = sell.get((code, cd))
        if xp is None:
            xp = seg[-1][2]
        ret = xp / ep - 1

        post = {}
        for k in (5, 10, 20):
            j = ix + k
            if j < len(bl):
                post[k] = bl[j][2] * F(code, bl[j][0]) / F(code, cd or bl[j][0]) / xp - 1
            else:
                post[k] = None

        top = bot = None
        if bid and bid in boxmap:
            top, bot = boxmap[bid]
        prem = (ep / top - 1) if top else None
        hgt = ((top - bot) / top) if (top and bot and top > 0) else None
        lo = max(0, ie - 252)
        h252 = max(b[1] for b in bl[lo:ie + 1]) if bl[lo:ie + 1] else None
        dist = (h252 - ep) / h252 * 100 if h252 else None

        tier = None
        if dist is not None:
            tier = 1 if dist < 5 else 2 if dist < 10 else 3 if dist < 20 else 4 if dist <= 40 else 0

        rows.append(dict(
            id=pid, code=code, open=od, close=cd,
            entry=round(ep, 3), exit=round(xp, 3), qty=qty,
            pnl=round(pnl or 0, 0), reason=reason, tdays=len(seg),
            mfe=round(mfe * 100, 2), mae=round(mae * 100, 2),
            ret=round(ret * 100, 2),
            giveback=round((mfe - ret) * 100, 2),
            tier=tier,
            prem=round(prem * 100, 2) if prem is not None else None,
            height=round(hgt * 100, 2) if hgt is not None else None,
            dist=round(dist, 2) if dist is not None else None,
            post5=round(post[5] * 100, 2) if post[5] is not None else None,
            post10=round(post[10] * 100, 2) if post[10] is not None else None,
            post20=round(post[20] * 100, 2) if post[20] is not None else None,
        ))

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    cols = list(rows[0].keys())
    with open(a.out, "w", encoding="utf-8-sig") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join("" if r[k] is None else str(r[k]) for k in cols) + "\n")

    cl = [r for r in rows if r["close"]]
    N = len(cl)
    print(f"逐笔明细 {N} 笔 → {a.out}（MFE/MAE 已按除权复权）\n")

    def m(key, rs=None, nd=2):
        v = [r[key] for r in (rs or cl) if r[key] is not None]
        return round(st.median(v), nd) if v else 0

    print("=== 一、过程全景 ===")
    print(f"  MFE 中位 {m('mfe'):+.2f}%   MAE 中位 {m('mae'):+.2f}%   "
          f"实际收益中位 {m('ret'):+.2f}%   回吐中位 {m('giveback'):+.1f}pp")
    print(f"  持有中位 {st.median([r['tdays'] for r in cl]):.0f} 交易日")
    print(f"  MFE≥10%: {sum(1 for r in cl if r['mfe']>=10)}/{N} "
          f"({sum(1 for r in cl if r['mfe']>=10)/N*100:.1f}%)   "
          f"MAE≤-10%: {sum(1 for r in cl if r['mae']<=-10)}/{N} "
          f"({sum(1 for r in cl if r['mae']<=-10)/N*100:.1f}%)")

    print("\n=== 二、曾浮盈却亏着走（出口漏 vs 没给机会）===")
    for th in (3, 5, 10, 15):
        g = [r for r in cl if r["mfe"] >= th and r["pnl"] <= 0]
        print(f"  MFE≥{th:>2}% 仍亏损: {len(g):3d} 笔 ({len(g)/N*100:4.1f}%) "
              f"合计 {sum(r['pnl'] for r in g):>10,.0f}  回吐中位 {m('giveback', g):.1f}pp")

    print("\n=== 三、出场后走势 ===")
    for k in ("post5", "post20"):
        v = [r[k] for r in cl if r[k] is not None]
        up = sum(1 for x in v if x > 0)
        print(f"  {k:>6} 中位 {st.median(v):+6.2f}%  上涨占比 {up/len(v)*100:5.1f}%")

    print("\n=== 四、按出场原因 ===")
    print(f"  {'reason':<16}{'笔':>4}{'MFE':>8}{'MAE':>8}{'实际':>8}{'持有':>5}{'合计':>11}{'post20':>8}")
    by = defaultdict(list)
    for r in cl:
        by[r["reason"]].append(r)
    for k, g in sorted(by.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k:<16}{len(g):>4}{m('mfe',g):>+8.1f}{m('mae',g):>+8.1f}"
              f"{m('ret',g):>+8.1f}{st.median([r['tdays'] for r in g]):>5.0f}"
              f"{sum(r['pnl'] for r in g):>11,.0f}{m('post20',g):>+8.1f}")

    print("\n=== 五、按阶梯档位（t1 最重 3 单位 → t4 最轻 0.5）===")
    print(f"  {'档':<18}{'笔':>4}{'胜率':>7}{'合计':>11}{'均值':>9}{'MFE':>8}{'实际':>8}{'持有':>5}{'post20':>8}")
    for t, name in [(1, "t1 距高<5% (3u)"), (2, "t2 5-10% (2u)"),
                    (3, "t3 10-20% (1u)"), (4, "t4 20-40% (0.5u)")]:
        g = [r for r in cl if r["tier"] == t]
        if not g:
            continue
        w = sum(1 for r in g if r["pnl"] > 0)
        print(f"  {name:<18}{len(g):>4}{w/len(g)*100:>6.1f}%{sum(r['pnl'] for r in g):>11,.0f}"
              f"{sum(r['pnl'] for r in g)/len(g):>9,.0f}{m('mfe',g):>+8.1f}"
              f"{m('ret',g):>+8.1f}{st.median([r['tdays'] for r in g]):>5.0f}{m('post20',g):>+8.1f}")

    print("\n=== 六、最差 12 笔全过程 ===")
    hdr = (f"  {'code':<9}{'买入':<11}{'卖出':<11}{'MFE':>7}{'MAE':>7}{'实际':>7}"
           f"{'持有':>5}{'盈亏':>9}{'原因':<14}{'post20':>8}")
    print(hdr)
    for r in sorted(cl, key=lambda r: r["pnl"])[:12]:
        print(f"  {r['code']:<9}{r['open']:<11}{r['close']:<11}{r['mfe']:>+7.1f}"
              f"{r['mae']:>+7.1f}{r['ret']:>+7.1f}{r['tdays']:>5}{r['pnl']:>9,.0f}"
              f"{r['reason']:<14}{(r['post20'] or 0):>+8.1f}")

    print("\n=== 七、最好 10 笔全过程（对照）===")
    print(hdr)
    for r in sorted(cl, key=lambda r: -r["pnl"])[:10]:
        print(f"  {r['code']:<9}{r['open']:<11}{r['close']:<11}{r['mfe']:>+7.1f}"
              f"{r['mae']:>+7.1f}{r['ret']:>+7.1f}{r['tdays']:>5}{r['pnl']:>9,.0f}"
              f"{r['reason']:<14}{(r['post20'] or 0):>+8.1f}")

    print("\n=== 八、买入溢价 / 箱体高度 ===")
    for key, label, edges in [
        ("prem", "买入价相对箱体上轨溢价", [(-100, 0), (0, 2), (2, 5), (5, 100)]),
        ("height", "箱体高度", [(0, 5), (5, 10), (10, 20), (20, 100)]),
    ]:
        print(f"  {label}")
        for lo, hi in edges:
            g = [r for r in cl if r[key] is not None and lo <= r[key] < hi]
            if len(g) < 5:
                continue
            w = sum(1 for r in g if r["pnl"] > 0)
            print(f"    [{lo:>4},{hi:>4}) {len(g):>3}笔 胜率{w/len(g)*100:5.1f}% "
                  f"合计{sum(r['pnl'] for r in g):>10,.0f} MFE{m('mfe',g):+.1f}%")


if __name__ == "__main__":
    main()
