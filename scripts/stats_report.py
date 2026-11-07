#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计报告最低清单生成器（用户 2026-11-07 定的长期标准，docs/60 §4.3）。

六项硬门槛，1→6 顺序逐项输出：
  1) 按年统计：年末权益、当年收益率、年内 MDD（含日期）、当年交易日数、当年成交笔数
  2) 单月极值：最高盈利月 / 最大亏损月（月份、金额、环比），占比双口径
  3) 单笔极值：最高盈利 / 最大亏损（标的、建仓→清仓、持有交易日、盈亏、占比），标明归并方式
  4) 剔除盈利前 3 / 亏损前 3 后重算：累计盈亏、收益率、笔数、胜率
  5) 剔除盈利前 10 / 亏损前 10 后：同上
  6) 与基线 v1 同时间窗对照（R34：已实现按平仓日 ≤ 统计边界截断）

round trip 归并方式：按 position_id 归并（一仓多腿，金字塔加仓同属一笔），
盈亏 = Σ(sell qty×price) − Σ(sell fee) − Σ(buy qty×price) − Σ(buy fee)。
持有交易日 = 建仓首腿日 → 清仓末腿日之间的交易日数（按两库 daily_bar 日期并集）。

用法：
  python scripts/stats_report.py --db var/bt/arms/bt_d3d4_off.db \
      --base var/bt/base/bt_baseA_cont_adj.db [--until 2011-03-17] [--initial 1000000]
"""
import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_d(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def fmt_money(x):
    return f"{x:+,.0f}"


def load_round_trips(db_path, until=None):
    """round trip = position 行（引擎已归并一仓多腿），盈亏直接用 position.realized_pnl。
    until: 只统计平仓日 <= until 的已实现交易（R34 截断）。"""
    db = sqlite3.connect(db_path)
    c = db.cursor()
    c.execute("SELECT id, code, opened_date, closed_date, realized_pnl FROM position")
    trips = []
    for pid, code, od, cd, pnl in c.fetchall():
        if cd is None or pnl is None:
            continue  # 未平仓，不计已实现
        if until and parse_d(cd) > parse_d(until):
            continue
        trips.append({
            "pid": pid, "code": code, "pnl": pnl,
            "open": od, "close": cd,
            "buy_cost": 0.0, "sell_gross": 0.0,
        })
    db.close()
    return trips


def load_equity(db_path):
    db = sqlite3.connect(db_path)
    c = db.cursor()
    c.execute("SELECT date, total, peak_total, drawdown FROM equity_snapshot ORDER BY date")
    rows = [(d, t, p, dd) for d, t, p, dd in c.fetchall()]
    db.close()
    return rows


def load_trade_days(db_path):
    db = sqlite3.connect(db_path)
    c = db.cursor()
    c.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date")
    days = [r[0] for r in c.fetchall()]
    db.close()
    return days


def holding_days(open_d, close_d, day_index):
    o, cl = parse_d(open_d), parse_d(close_d)
    lo, hi = day_index.get(o), day_index.get(cl)
    if lo is None or hi is None:
        return None
    return hi - lo


def yearly_stats(eq, trips, initial):
    """1) 按年统计"""
    out = []
    years = sorted({d[:4] for d, *_ in eq})
    start_equity = initial
    for y in years:
        rows = [(d, t, p, dd) for d, t, p, dd in eq if d[:4] == y]
        end_eq = rows[-1][1]
        # 年内 MDD：取该年 drawdown 最小处
        worst = min(rows, key=lambda r: r[3])
        days = len(rows)
        sells = [t for t in trips if t["close"][:4] == y]
        ret = end_eq / start_equity - 1
        out.append({
            "year": y, "end_eq": end_eq, "ret": ret, "start_eq": start_equity,
            "mdd": worst[3], "mdd_date": worst[0], "days": days, "n_trips": len(sells),
        })
        start_equity = end_eq
    return out


def monthly_pnl(trips):
    """按平仓月归并盈亏"""
    m = defaultdict(float)
    for t in trips:
        m[t["close"][:7]] += t["pnl"]
    return dict(sorted(m.items()))


def pick_extreme_months(mp):
    best_m = max(mp, key=lambda k: mp[k])
    worst_m = min(mp, key=lambda k: mp[k])
    return best_m, worst_m


def win_stats(trips):
    n = len(trips)
    wins = [t for t in trips if t["pnl"] > 0]
    losses = [t for t in trips if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trips)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = sum(t["pnl"] for t in losses)  # 负值
    return {
        "n": n, "win_rate": len(wins) / n if n else 0.0,
        "total": total, "gross_win": gross_win, "gross_loss": gross_loss,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": gross_loss / len(losses) if losses else 0.0,
        "wins": wins, "losses": losses,
    }


def trim_stats(trips, k, initial):
    """剔盈利前 k / 亏损前 k 后重算"""
    by_pnl = sorted(trips, key=lambda t: t["pnl"], reverse=True)
    top_w = by_pnl[:k]
    top_l = sorted(trips, key=lambda t: t["pnl"])[:k]
    res = {}
    for label, excluded in [("剔盈利前%d" % k, top_w), ("剔亏损前%d" % k, top_l)]:
        ex_ids = {t["pid"] for t in excluded}
        kept = [t for t in trips if t["pid"] not in ex_ids]
        total = sum(t["pnl"] for t in kept)
        wins = [t for t in kept if t["pnl"] > 0]
        res[label] = {
            "total": total, "ret": total / initial,
            "n": len(kept), "win_rate": len(wins) / len(kept) if kept else 0.0,
            "excluded_sum": sum(t["pnl"] for t in excluded),
        }
    return res


def cagr(start_eq, end_eq, d0, d1):
    days = (parse_d(d1) - parse_d(d0)).days
    if days <= 0 or start_eq <= 0:
        return 0.0
    return (end_eq / start_eq) ** (365.0 / days) - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--until", default=None, help="统计边界日，默认取臂库最新快照日")
    ap.add_argument("--initial", type=float, default=1_000_000.0)
    args = ap.parse_args()

    db_path = ROOT / args.db if not Path(args.db).is_absolute() else Path(args.db)
    base_path = ROOT / args.base if not Path(args.base).is_absolute() else Path(args.base)

    eq = load_equity(db_path)
    until = args.until or eq[-1][0]
    trips = load_round_trips(db_path, until=until)
    all_days = load_trade_days(db_path)
    day_index = {parse_d(d): i for i, d in enumerate(all_days)}
    for t in trips:
        t["hold"] = holding_days(t["open"], t["close"], day_index)

    print("=" * 72)
    print(f"统计报告（docs/60 §4.3 最低清单口径）")
    print(f"库: {args.db} | 统计边界: {until}（已实现按平仓日 ≤ 边界截断，R34）")
    print(f"期初权益: {args.initial:,.0f} | 期末权益: {eq[-1][1]:,.0f}")
    print("=" * 72)

    ws = win_stats(trips)
    print(f"\n总览：round trip {ws['n']} 笔 | 胜率 {ws['win_rate']*100:.1f}% | "
          f"累计盈亏 {fmt_money(ws['total'])} | 毛盈利 {fmt_money(ws['gross_win'])} | "
          f"毛亏损 {fmt_money(ws['gross_loss'])}")

    # ---- 1) 按年 ----
    print("\n【1】按年统计")
    ys = yearly_stats(eq, trips, args.initial)
    print(f"{'年份':<6}{'年末权益':>14}{'当年收益率':>11}{'年内MDD':>10}  {'MDD日期':<12}{'交易日':>6}{'成交笔数':>8}")
    for r in ys:
        print(f"{r['year']:<6}{r['end_eq']:>14,.0f}{r['ret']*100:>10.2f}%{r['mdd']*100:>9.2f}%  "
              f"{r['mdd_date']:<12}{r['days']:>6}{r['n_trips']:>8}")

    # ---- 2) 单月极值 ----
    print("\n【2】单月极值（按平仓月归并）")
    mp = monthly_pnl(trips)
    best_m, worst_m = pick_extreme_months(mp)
    pos_months = {k: v for k, v in mp.items() if v > 0}
    neg_months = {k: v for k, v in mp.items() if v < 0}
    prev_best = None
    keys = sorted(mp.keys())
    for label, m in [("最高盈利月", best_m), ("最大亏损月", worst_m)]:
        i = keys.index(m)
        prev = mp[keys[i - 1]] if i > 0 else None
        env = f"上月 {fmt_money(prev)}，环比 {fmt_money(mp[m] - prev)}" if prev is not None else "无上月"
        same_dir_sum = sum(pos_months.values()) if m in pos_months else sum(neg_months.values())
        share_pos = mp[m] / same_dir_sum if same_dir_sum else 0
        share_net = mp[m] / ws["total"] if ws["total"] else 0
        print(f"  {label}: {m}  {fmt_money(mp[m])}  ({env})")
        print(f"    占同向月份合计: {abs(share_pos)*100:.1f}%  |  占累计净盈利: {share_net*100:.1f}%")

    # ---- 3) 单笔极值 ----
    print("\n【3】单笔极值（round trip = position_id 归并，一仓多腿同属一笔）")
    best_t = max(trips, key=lambda t: t["pnl"])
    worst_t = min(trips, key=lambda t: t["pnl"])
    for label, t, denom in [("最高盈利", best_t, ws["gross_win"]), ("最大亏损", worst_t, ws["gross_loss"])]:
        share = t["pnl"] / denom if denom else 0
        print(f"  {label}: {t['code']}  {t['open']} → {t['close']}  "
              f"持有 {t['hold']} 个交易日  盈亏 {fmt_money(t['pnl'])}  "
              f"占{'毛盈利' if t['pnl']>0 else '毛亏损'} {abs(share)*100:.1f}%")

    # ---- 4/5) 剔前3 / 前10 ----
    for k in (3, 10):
        print(f"\n【{'4' if k==3 else '5'}】剔除前 {k} 后重算（期初 {args.initial:,.0f}）")
        ts = trim_stats(trips, k, args.initial)
        for label, r in ts.items():
            print(f"  {label}: 累计盈亏 {fmt_money(r['total'])}  收益率 {r['ret']*100:+.2f}%  "
                  f"笔数 {r['n']}  胜率 {r['win_rate']*100:.1f}%  "
                  f"(被剔 {r['excluded_sum']:+,.0f})")

    # ---- 6) 基线同窗对照 ----
    print("\n【6】与基线 v1 同时间窗对照（同窗 = 2005-01-01 ~ %s，按平仓日截断）" % until)
    beq = load_equity(base_path)
    btrips = load_round_trips(base_path, until=until)
    for t in btrips:
        t["hold"] = holding_days(t["open"], t["close"], day_index)
    bws = win_stats(btrips)
    bend = [r for r in beq if r[0] <= until][-1]
    bstart = args.initial

    def mdd_in(eqrows, until_d):
        rows = [r for r in eqrows if r[0] <= until_d]
        w = min(rows, key=lambda r: r[3])
        return w[3], w[0]

    arm_mdd, arm_mdd_d = mdd_in(eq, until)
    base_mdd, base_mdd_d = mdd_in(beq, until)
    btrim3 = trim_stats(btrips, 3, bstart)
    btrim10 = trim_stats(btrips, 10, bstart)
    atrim3 = trim_stats(trips, 3, args.initial)
    atrim10 = trim_stats(trips, 10, args.initial)

    print(f"{'指标':<16}{'D3+D4 臂':>16}{'基线 v1':>16}")
    print(f"{'累计盈亏':<16}{fmt_money(ws['total']):>16}{fmt_money(bws['total']):>16}")
    arm_ret = eq[-1][1] / args.initial - 1 if until == eq[-1][0] else ws['total']/args.initial
    base_ret = bws["total"] / bstart
    print(f"{'区间收益率':<16}{arm_ret*100:>15.2f}%{base_ret*100:>15.2f}%")
    print(f"{'年化(CAGR)':<16}{cagr(args.initial, eq[-1][1] if until==eq[-1][0] else args.initial*(1+arm_ret), '2005-01-01', until)*100:>15.2f}%"
          f"{cagr(bstart, bstart*(1+base_ret), '2005-01-01', until)*100:>15.2f}%")
    print(f"{'MDD':<16}{arm_mdd*100:>14.2f}% ({arm_mdd_d}){base_mdd*100:>13.2f}% ({base_mdd_d})")
    print(f"{'笔数':<16}{ws['n']:>16}{bws['n']:>16}")
    print(f"{'胜率':<16}{ws['win_rate']*100:>15.1f}%{bws['win_rate']*100:>15.1f}%")
    print(f"{'平均盈利':<16}{fmt_money(ws['avg_win']):>16}{fmt_money(bws['avg_win']):>16}")
    print(f"{'平均亏损':<16}{fmt_money(ws['avg_loss']):>16}{fmt_money(bws['avg_loss']):>16}")
    bw = max(btrips, key=lambda t: t['pnl'])['pnl'] if btrips else 0
    bw_share = bw / bws['gross_win'] if bws['gross_win'] else 0
    aw_share = best_t['pnl'] / ws['gross_win'] if ws['gross_win'] else 0
    print(f"{'单笔最高占比':<16}{aw_share*100:>15.1f}%{bw_share*100:>15.1f}%")
    for k, at, bt in [(3, atrim3, btrim3), (10, atrim10, btrim10)]:
        for lbl in [f"剔盈利前{k}", f"剔亏损前{k}"]:
            print(f"{lbl:<16}{fmt_money(at[lbl]['total']):>16}{fmt_money(bt[lbl]['total']):>16}")

    # 窗口覆盖度
    covered = len([d for d, *_ in eq])
    total_days_all = len(beq)
    print(f"\n窗口覆盖度：本窗已跑 {covered} 交易日（至 {until}）| "
          f"基线全窗 {total_days_all} | 本窗/全窗 {covered/total_days_all*100:.1f}%")

    # 参数与引擎差异（从库内读）
    print("\n差异标注：")
    for tag, p in [("D3+D4臂", db_path), ("基线v1", base_path)]:
        db = sqlite3.connect(p)
        c = db.cursor()
        try:
            c.execute("SELECT engine_sha256, param_fingerprint FROM baseline")
            row = c.fetchone()
            print(f"  {tag}: engine_sha256={row[0][:8]}  param_fp={row[1]}")
        except Exception as e:
            print(f"  {tag}: baseline 读不到 ({e})")
        try:
            c.execute("SELECT key, value FROM param WHERE key IN ('position.staledays','pool.blacklist_months','position.newhigh_ladder')")
            print(f"    关键参数: {dict(c.fetchall())}")
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    main()
