#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计报告最低清单生成器（用户 2026-11-07 定的长期标准，docs/60 §4.3）。

六项硬门槛，1→6 顺序逐项输出，缺项即不合格报告：
  1) 按年：年末权益、当年收益率、年内 MDD（含日期）、当年交易日数（已跑/全年日历）、当年成交笔数（买/卖）
  2) 单月极值：最高盈利月 / 最大亏损月（月份、金额、环比），占比双口径，首尾不满月标注
  3) 单笔极值：最高盈利 / 最大亏损（标的、建仓→清仓、持有交易日、盈亏、占毛盈利/毛亏损），标明归并方式
  4) 剔除盈利前 3 / 亏损前 3 后重算：累计盈亏、收益率、笔数、胜率
  5) 剔除盈利前 10 / 亏损前 10 后：同上
  6) 与基线 v1 同时间窗对照（R34：已实现按平仓日 ≤ 统计边界截断）+ 参数/引擎/窗口覆盖度差异

口径（60 §4.3 固定）：
  权益口径 = equity_snapshot.total；已实现口径 = position.realized_pnl；两者不混用。
  金额两位小数、百分比一位小数；环比 = 与上一个自然月比较；统计一律 mode=ro 只读。

round trip 归并：现库按 position 行聚合，实测 MAX(leg_no)=1，即「一 position 一腿」。
  D1 金字塔加仓上线后若出现一仓多腿，须重新声明归并方式并重跑本脚本。
  盈亏取 position.realized_pnl；持有交易日 = 建仓日→清仓日之间的交易日数。

用法：
  python scripts/stats_report.py --db var/bt/arms/bt_d3d4_off.db \
      --base var/bt/base/bt_baseA_cont_adj.db [--until 2018-10-10] [--initial 1000000]
"""
import argparse
import hashlib
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL_WINDOW_DAYS = 3402
WINDOW_START = "2005-01-01"
WINDOW_END = "2018-12-31"   # 名义窗口终点（起跑命令 --to），非实际最后交易日
MIN_BARS_PER_DAY = 100      # 有效交易日门槛：全市场当日 bar 数下限（滤残数据日）


def ro_connect(p):
    """只读连接（60 §4.3：统计一律 mode=ro，不写库）。"""
    return sqlite3.connect("file:%s?mode=ro" % Path(p).as_posix(), uri=True)


def parse_d(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def fmt_money(x):
    return f"{x:+,.2f}"


def fmt_plain(x):
    return f"{x:,.2f}"


def fmt_pct(x, signed=False):
    return ("%+.1f%%" if signed else "%.1f%%") % (x * 100)


def prev_month(m):
    y, mm = int(m[:4]), int(m[5:7])
    return "%d-12" % (y - 1) if mm == 1 else "%d-%02d" % (y, mm - 1)


def load_round_trips(p, until=None):
    """round trip = position 行。until: 只统计平仓日 <= until（R34）。"""
    db = ro_connect(p)
    rows = db.execute(
        "SELECT id, code, opened_date, closed_date, realized_pnl FROM position"
    ).fetchall()
    db.close()
    trips = []
    for pid, code, od, cd, pnl in rows:
        if cd is None or pnl is None:
            continue
        if until and parse_d(cd) > parse_d(until):
            continue
        trips.append({"pid": pid, "code": code, "pnl": pnl, "open": od, "close": cd})
    return trips


def load_equity(p):
    db = ro_connect(p)
    rows = db.execute(
        "SELECT date, total, peak_total, drawdown FROM equity_snapshot ORDER BY date"
    ).fetchall()
    db.close()
    return [(d, t, pk, dd) for d, t, pk, dd in rows]


def load_trade_days(p):
    db = ro_connect(p)
    days = [r[0] for r in db.execute("SELECT DISTINCT date FROM daily_bar ORDER BY date")]
    db.close()
    return days


def load_calendar_days(base_path):
    """全年日历交易日（用基线库行情日作日历，不受臂库进度影响）。

    2026-11-08 订正：基线库含 2011-12-31 / 2018-12-31 两个假交易日（各仅 2 行 bar），
    旧口径 distinct date 得 3404 日，导致年度日历多算、窗口末日误判为 12-31、
    覆盖度报 3402/3403。按 MIN_BARS_PER_DAY 过滤后 = 3402 日，与引擎窗口一致。
    """
    db = ro_connect(base_path)
    days = [r[0] for r in db.execute(
        "SELECT date FROM daily_bar WHERE date >= ? AND date <= ? "
        "GROUP BY date HAVING COUNT(*) >= ? ORDER BY date",
        (WINDOW_START, WINDOW_END, MIN_BARS_PER_DAY))]
    db.close()
    return days


def load_failed_runs(p):
    """账本完整性：非 ok 的 daily_run 任务（60 §4.3 报告首行须注明库状态）。"""
    db = ro_connect(p)
    rows = db.execute(
        "SELECT date, task, status, error FROM daily_run WHERE status <> 'ok' ORDER BY date"
    ).fetchall()
    db.close()
    return rows


def load_fills_by_year(p, until):
    db = ro_connect(p)
    rows = db.execute(
        "SELECT date, side FROM trade_fill WHERE date <= ?", (until,)
    ).fetchall()
    db.close()
    out = defaultdict(int)
    for d, side in rows:
        out[(d[:4], side)] += 1
    return out


def load_open_positions(p, until):
    db = ro_connect(p)
    n = db.execute(
        "SELECT COUNT(*) FROM position WHERE closed_date IS NULL OR closed_date > ?",
        (until,)
    ).fetchone()[0]
    db.close()
    return n


def load_identity(p):
    db = ro_connect(p)
    try:
        row = db.execute("SELECT engine_sha256, param_fingerprint FROM baseline").fetchone()
    except Exception:
        row = None
    try:
        params = dict(db.execute("SELECT key, value FROM param").fetchall())
    except Exception:
        params = {}
    db.close()
    return (row[0][:16], row[1][:16]) if row else ("?", "?"), params


def current_bin_sha():
    exe = ROOT / "bin" / "darvas-dev.exe"
    if not exe.exists():
        return "（缺失）"
    return hashlib.sha256(exe.read_bytes()).hexdigest()[:16]


def holding_days(open_d, close_d, day_index):
    lo = day_index.get(parse_d(open_d))
    hi = day_index.get(parse_d(close_d))
    return None if lo is None or hi is None else hi - lo


def yearly_stats(eq, trips, fills, calendar, initial):
    out = []
    by_year = defaultdict(int)
    for d in calendar:
        by_year[d[:4]] += 1
    start_equity = initial
    for y in sorted({d[:4] for d, *_ in eq}):
        rows = [(d, t, pk, dd) for d, t, pk, dd in eq if d[:4] == y]
        worst = min(rows, key=lambda r: r[3])
        out.append({
            "year": y, "end_eq": rows[-1][1], "ret": rows[-1][1] / start_equity - 1,
            "mdd": worst[3], "mdd_date": worst[0],
            "days": len(rows), "calendar": by_year.get(y, 0),
            "buy": fills.get((y, "buy"), 0), "sell": fills.get((y, "sell"), 0),
            "closed": sum(1 for t in trips if t["close"][:4] == y),
        })
        start_equity = rows[-1][1]
    return out


def monthly_pnl(trips):
    m = defaultdict(float)
    for t in trips:
        m[t["close"][:7]] += t["pnl"]
    return dict(sorted(m.items()))


def win_stats(trips):
    n = len(trips)
    wins = [t for t in trips if t["pnl"] > 0]
    losses = [t for t in trips if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trips)
    gw = sum(t["pnl"] for t in wins)
    gl = sum(t["pnl"] for t in losses)
    return {
        "n": n, "win_rate": len(wins) / n if n else 0.0, "total": total,
        "gross_win": gw, "gross_loss": gl,
        "avg_win": gw / len(wins) if wins else 0.0,
        "avg_loss": gl / len(losses) if losses else 0.0,
    }


def trim_stats(trips, k, initial):
    by_pnl = sorted(trips, key=lambda t: t["pnl"], reverse=True)
    res = {}
    for label, excluded in [("剔盈利前%d" % k, by_pnl[:k]),
                            ("剔亏损前%d" % k, sorted(trips, key=lambda t: t["pnl"])[:k])]:
        ex_ids = {t["pid"] for t in excluded}
        kept = [t for t in trips if t["pid"] not in ex_ids]
        total = sum(t["pnl"] for t in kept)
        wins = [t for t in kept if t["pnl"] > 0]
        res[label] = {
            "total": total, "ret": total / initial, "n": len(kept),
            "win_rate": len(wins) / len(kept) if kept else 0.0,
            "excluded_sum": sum(t["pnl"] for t in excluded),
        }
    return res


def cagr_from_eq(eq, until, initial):
    rows = [r for r in eq if r[0] <= until]
    if len(rows) < 2:
        return 0.0
    days = (parse_d(rows[-1][0]) - parse_d(rows[0][0])).days
    if days <= 0:
        return 0.0
    return (rows[-1][1] / initial) ** (365.0 / days) - 1


def mdd_in(eq, until):
    w = min((r for r in eq if r[0] <= until), key=lambda r: r[3])
    return w[3], w[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--until", default=None, help="统计边界日，默认取臂库最新快照日")
    ap.add_argument("--initial", type=float, default=1_000_000.0)
    args = ap.parse_args()

    db_path = Path(args.db) if Path(args.db).is_absolute() else ROOT / args.db
    base_path = Path(args.base) if Path(args.base).is_absolute() else ROOT / args.base

    eq = load_equity(db_path)
    until = args.until or eq[-1][0]
    eq_u = [r for r in eq if r[0] <= until]
    end_equity = eq_u[-1][1] if eq_u else args.initial
    trips = load_round_trips(db_path, until=until)
    all_days = load_trade_days(db_path)
    day_index = {parse_d(d): i for i, d in enumerate(all_days)}
    for t in trips:
        t["hold"] = holding_days(t["open"], t["close"], day_index)
    calendar = load_calendar_days(base_path)
    fills = load_fills_by_year(db_path, until)
    n_open = load_open_positions(db_path, until)
    (arm_sha, arm_fp), arm_params = load_identity(db_path)
    (base_sha, base_fp), base_params = load_identity(base_path)
    bin_sha = current_bin_sha()

    print("=" * 78)
    print("统计报告（docs/60 §4.3 最低清单口径，1→6 全项）")
    print("快照时间: %s" % datetime.now().isoformat(timespec="seconds"))
    print("统计边界: %s | 库: %s | 引擎 sha: %s" % (until, args.db, arm_sha))
    print("基线库: %s | 基线引擎 sha: %s | 当前二进制 bin/darvas-dev.exe sha: %s"
          % (args.base, base_sha, bin_sha))
    effective_end = calendar[-1] if calendar else WINDOW_END
    if until < effective_end:
        print("⚠ 进行中快照（未到引擎实际末日 %s），非终稿；全部数字随长跑推进会变。" % effective_end)
    else:
        print("✅ 全窗完成 · 终稿（引擎实际末日 %s；名义终点 %s 无有效行情，不计交易日）"
              % (effective_end, WINDOW_END))
    bad_runs = load_failed_runs(db_path)
    if bad_runs:
        bd = sorted({r[0] for r in bad_runs})
        print("⚠ 账本完整性：%d 个任务非 ok，涉及 %d 天（%s）；相关数字须标 ⚠，续跑不能补历史"
              % (len(bad_runs), len(bd), ", ".join(bd)))
    print("期初权益 %s | 期末权益 %s | 已实现按平仓日 ≤ 边界截断（R34）"
          % (fmt_plain(args.initial), fmt_plain(end_equity)))
    print("边界日未平仓持仓 %d 笔（不计入已实现 round trip；权益口径已含其浮动盈亏）" % n_open)
    print("=" * 78)

    ws = win_stats(trips)
    print("\n总览：round trip %d 笔 | 胜率 %s | 累计盈亏 %s | 毛盈利 %s | 毛亏损 %s"
          % (ws["n"], fmt_pct(ws["win_rate"]), fmt_money(ws["total"]),
             fmt_money(ws["gross_win"]), fmt_money(ws["gross_loss"])))

    # ---- 1 ----
    print("\n【1】按年统计（收益率/年内MDD = 权益口径；笔数 = 成交口径）")
    print("%-6s%14s%11s%10s  %-12s%12s%16s%8s" %
          ("年份", "年末权益", "当年收益率", "年内MDD", "MDD日期", "交易日已跑/日历", "成交买/卖", "平仓笔数"))
    for r in yearly_stats(eq_u, trips, fills, calendar, args.initial):
        print("%-6s%14s%10s%9s  %-12s%12s%16s%8d" % (
            r["year"], fmt_plain(r["end_eq"]), fmt_pct(r["ret"], True), fmt_pct(r["mdd"], True),
            r["mdd_date"], "%d/%d" % (r["days"], r["calendar"]),
            "%d/%d" % (r["buy"], r["sell"]), r["closed"]))

    # ---- 2 ----
    print("\n【2】单月极值（按平仓月归并；环比 = 与上一个自然月比）")
    mp = monthly_pnl(trips)
    first_m, last_m = min(mp), max(mp)
    best_m = max(mp, key=lambda k: mp[k])
    worst_m = min(mp, key=lambda k: mp[k])
    pos_sum = sum(v for v in mp.values() if v > 0)
    neg_sum = sum(v for v in mp.values() if v < 0)
    for label, m in [("最高盈利月", best_m), ("最大亏损月", worst_m)]:
        pm = prev_month(m)
        prev = mp.get(pm, 0.0)
        flag = ("首月不满月" if m == first_m else
                "末月不满月，截至边界日" if m == last_m else "满月")
        same = pos_sum if mp[m] > 0 else neg_sum
        print("  %s: %s  %s  [%s]" % (label, m, fmt_money(mp[m]), flag))
        print("    上月(%s) %s，环比 %s" % (pm, fmt_money(prev), fmt_money(mp[m] - prev)))
        print("    占同向月份合计 %s | 占累计净盈利 %s" %
              (fmt_pct(abs(mp[m] / same)) if same else "n/a",
               fmt_pct(mp[m] / ws["total"], True) if ws["total"] else "n/a"))

    # ---- 3 ----
    print("\n【3】单笔极值（round trip = position 行；实测 MAX(leg_no)=1，即一 position 一腿）")
    print("     归并方式：现库无加仓（0017 废除），一仓一腿；D1 上线后须重新声明并跑本脚本")
    best_t = max(trips, key=lambda t: t["pnl"])
    worst_t = min(trips, key=lambda t: t["pnl"])
    for label, t, denom in [("最高盈利", best_t, ws["gross_win"]),
                            ("最大亏损", worst_t, ws["gross_loss"])]:
        share = t["pnl"] / denom if denom else 0
        print("  %s: %s  %s → %s  持有 %s 个交易日  盈亏 %s  占%s %s" % (
            label, t["code"], t["open"], t["close"],
            t["hold"] if t["hold"] is not None else "n/a",
            fmt_money(t["pnl"]), "毛盈利" if t["pnl"] > 0 else "毛亏损",
            fmt_pct(abs(share))))

    # ---- 4 / 5 ----
    for k in (3, 10):
        print("\n【%s】剔除前 %d 后重算（期初 %s）" %
              ("4" if k == 3 else "5", k, fmt_plain(args.initial)))
        for label, r in trim_stats(trips, k, args.initial).items():
            print("  %s: 累计盈亏 %s  收益率 %s  笔数 %d  胜率 %s  (被剔 %s)" %
                  (label, fmt_money(r["total"]), fmt_pct(r["ret"], True), r["n"],
                   fmt_pct(r["win_rate"]), fmt_money(r["excluded_sum"])))

    # ---- 6 ----
    print("\n【6】与基线 v1 同时间窗对照（%s ~ %s，已实现按平仓日 ≤ 边界截断 R34）"
          % (WINDOW_START, until))
    beq = load_equity(base_path)
    btrips = load_round_trips(base_path, until=until)
    bws = win_stats(btrips)
    arm_mdd, arm_mdd_d = mdd_in(eq, until)
    base_mdd, base_mdd_d = mdd_in(beq, until)
    at3, at10 = trim_stats(trips, 3, args.initial), trim_stats(trips, 10, args.initial)
    bt3, bt10 = trim_stats(btrips, 3, args.initial), trim_stats(btrips, 10, args.initial)
    bw = max(btrips, key=lambda t: t["pnl"])["pnl"] if btrips else 0

    rows = [
        ("累计盈亏(已实现)", fmt_money(ws["total"]), fmt_money(bws["total"])),
        ("区间收益率(权益)", fmt_pct(end_equity / args.initial - 1, True),
         fmt_pct([r for r in beq if r[0] <= until][-1][1] / args.initial - 1, True)),
        ("年化 CAGR(权益曲线)", fmt_pct(cagr_from_eq(eq, until, args.initial)),
         fmt_pct(cagr_from_eq(beq, until, args.initial))),
        ("MDD", "%s (%s)" % (fmt_pct(arm_mdd, True), arm_mdd_d),
         "%s (%s)" % (fmt_pct(base_mdd, True), base_mdd_d)),
        ("笔数", str(ws["n"]), str(bws["n"])),
        ("胜率", fmt_pct(ws["win_rate"]), fmt_pct(bws["win_rate"])),
        ("平均盈利", fmt_money(ws["avg_win"]), fmt_money(bws["avg_win"])),
        ("平均亏损", fmt_money(ws["avg_loss"]), fmt_money(bws["avg_loss"])),
        ("单笔最高占毛盈利", fmt_pct(best_t["pnl"] / ws["gross_win"]),
         fmt_pct(bw / bws["gross_win"])),
        ("剔盈利前3 累计盈亏", fmt_money(at3["剔盈利前3"]["total"]), fmt_money(bt3["剔盈利前3"]["total"])),
        ("剔亏损前3 累计盈亏", fmt_money(at3["剔亏损前3"]["total"]), fmt_money(bt3["剔亏损前3"]["total"])),
        ("剔盈利前10 累计盈亏", fmt_money(at10["剔盈利前10"]["total"]), fmt_money(bt10["剔盈利前10"]["total"])),
        ("剔亏损前10 累计盈亏", fmt_money(at10["剔亏损前10"]["total"]), fmt_money(bt10["剔亏损前10"]["total"])),
    ]
    print("%-24s%26s%26s" % ("指标", "D3+D4 臂", "基线 v1"))
    for label, a, b in rows:
        print("%-24s%26s%26s" % (label, a, b))

    covered = len([d for d, *_ in eq if d <= until])
    expected = len([d for d in calendar if d[:10] <= until])
    full = len(calendar)
    print("\n窗口覆盖度：本窗已跑 %d / 本窗应有 %d = %s | 本窗/全窗 %d 日 = %s（有效交易日末日 %s）" %
          (covered, expected, fmt_pct(covered / expected) if expected else "n/a",
           full, fmt_pct(covered / full) if full else "n/a", effective_end))
    if full != FULL_WINDOW_DAYS:
        print("  ⚠ 日历 %d 日与引擎窗口常量 %d 日不一致，须核对源库" % (full, FULL_WINDOW_DAYS))

    print("\n差异标注：")
    keys = sorted(set(arm_params) | set(base_params))
    diffs = [(k, base_params.get(k, "—"), arm_params.get(k, "—"))
             for k in keys if base_params.get(k) != arm_params.get(k)]
    print("  参数差异（基线 v1 → D3+D4 臂）共 %d 项：" % len(diffs))
    for k, bv, av in diffs:
        print("    %s: %s → %s" % (k, bv, av))
    print("  引擎：臂 %s / param_fp %s | 基线 %s / param_fp %s" %
          (arm_sha, arm_fp, base_sha, base_fp))
    print("  二进制：当前 bin/darvas-dev.exe = %s；臂库 %s | 基线 %s" %
          (bin_sha, "与当前二进制一致" if arm_sha == bin_sha else "与当前二进制不一致",
           "与当前二进制一致" if base_sha == bin_sha else "旧引擎，已非当前二进制"))


if __name__ == "__main__":
    main()
