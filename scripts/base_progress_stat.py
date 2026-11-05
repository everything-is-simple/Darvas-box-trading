#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""base_progress_stat.py —— 连续基线长跑「按年 + 概率统计」进度报表

只读回测库，不改任何东西。产出：
  1. 进度：交易日完成度 / 速率 / 预计收尾
  2. 按年：年末权益、年度收益率、已实现、笔数、胜率(Wilson 95%)、赔率、PF、剔前3/前10
  3. 全局概率：单笔盈亏分布分位、均值 t 检验、胜率 vs 保本胜率、年度符号二项检验
  4. 出口分解：close_reason 分组

口径纪律（.workbuddy/memory/MEMORY.md）：
  - 「权益变动」与「已实现」分列，不混用；
  - 年度收益率在长跑未结束时是**临时口径**，必须标 ⏳；
  - 剔除前三赢家净利必报。

用法：
  python scripts/base_progress_stat.py
  python scripts/base_progress_stat.py --db var/bt/base/bt_baseA_cont_adj.db --json out.json
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "var" / "bt" / "base" / "bt_baseA_cont_adj.db"
TOTAL_DAYS = 3402  # 2005-01-01 ~ 2018-12-31 交易日（引擎 resolveWindow 口径）


# ---------- 统计小工具 ----------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例 Wilson 置信区间（小样本比正态近似稳）。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def t_crit(df: int, alpha: float = 0.05) -> float:
    """双侧 t 临界值（df 较大时用正态近似，小 df 查表近似）。"""
    table = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 15: 2.131, 20: 2.086, 25: 2.060,
        30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980,
    }
    if df <= 0:
        return float("nan")
    if df in table:
        return table[df]
    keys = sorted(table)
    if df > keys[-1]:
        return 1.960
    lo = max(k for k in keys if k < df)
    hi = min(k for k in keys if k > df)
    w = (df - lo) / (hi - lo)
    return table[lo] + w * (table[hi] - table[lo])


def quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (pos - lo) * (sorted_vals[hi] - sorted_vals[lo])


def binom_two_sided_p(k: int, n: int, p0: float = 0.5) -> float:
    """精确二项检验（双侧）。对数空间计算，n>1000 不溢出。"""
    if n == 0:
        return float("nan")
    lgn = math.lgamma(n + 1)
    def logpmf(i):
        if p0 <= 0.0:
            return 0.0 if i == 0 else float("-inf")
        if p0 >= 1.0:
            return 0.0 if i == n else float("-inf")
        return (lgn - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                + i * math.log(p0) + (n - i) * math.log(1 - p0))
    obs = logpmf(k)
    total = 0.0
    for i in range(n + 1):
        lp = logpmf(i)
        if lp <= obs + 1e-9:
            total += math.exp(lp)
    return min(1.0, total)


# ---------- 主分析 ----------
def analyze(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c = con.cursor()
    out: dict = {"db": str(db)}

    # 进度
    n_days, dmin, dmax = c.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM daily_run WHERE task='plan' AND status='ok'"
    ).fetchone()
    out["progress"] = {
        "days_done": n_days,
        "days_total": TOTAL_DAYS,
        "pct": round(100.0 * n_days / TOTAL_DAYS, 2),
        "first_date": dmin,
        "last_date": dmax,
    }

    # 权益序列
    eq = c.execute("SELECT date, total, cash, market_value FROM equity_snapshot ORDER BY date").fetchall()
    out["progress"]["equity_days"] = len(eq)
    if eq:
        out["equity"] = {
            "start": eq[0][1],
            "now": eq[-1][1],
            "now_date": eq[-1][0],
            "peak": max(r[1] for r in eq),
            "maxdd_pct": round(100.0 * min(r[1] / max(1, max(x[1] for x in eq[:i + 1])) - 1
                                          for i, r in enumerate(eq)), 2),
        }
        out["equity"]["multiple"] = round(out["equity"]["now"] / out["equity"]["start"], 3)

    # 年末权益（每年该年最后一个交易日）
    yr_end: dict[int, float] = {}
    for d, t, _, _ in eq:
        yr_end[int(d[:4])] = t
    out["year_end_equity"] = {str(k): round(v, 2) for k, v in sorted(yr_end.items())}

    # 平仓交易
    rows = c.execute(
        "SELECT code, CAST(opened_date AS TEXT), CAST(closed_date AS TEXT), "
        "COALESCE(close_reason,''), COALESCE(realized_pnl,0) "
        "FROM position WHERE status='closed'"
    ).fetchall()
    trades = [{"code": r[0], "open": r[1], "close": r[2], "reason": r[3], "pnl": float(r[4])} for r in rows]
    out["trades_total"] = len(trades)
    out["open_positions"] = c.execute("SELECT COUNT(*) FROM position WHERE status<>'closed'").fetchone()[0]

    # 腿数与单位（首买额度口径）
    legs = c.execute("SELECT COUNT(*), SUM(qty*price) FROM position_leg").fetchone()
    out["legs"] = {"n": legs[0], "notional": round(float(legs[1] or 0), 2)}

    con.close()
    return out


def by_year(trades: list[dict], yr_end: dict[int, float], start_equity: float = 1_000_000) -> list[dict]:
    years = sorted({int(t["close"][:4]) for t in trades if t["close"]})
    out = []
    prev_eq = start_equity
    for y in years:
        sub = [t for t in trades if t["close"] and int(t["close"][:4]) == y]
        pnls = sorted(t["pnl"] for t in sub)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        n = len(pnls)
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        eq_y = yr_end.get(y)
        ret = (eq_y / prev_eq - 1) if (eq_y and prev_eq) else None
        excl3 = sum(pnls[:-3]) if n > 3 else 0.0
        excl10 = sum(pnls[:-10]) if n > 10 else 0.0
        lo, hi = wilson(len(wins), n)
        payoff = ((gross_win / len(wins)) / (gross_loss / len(losses))
                  if (wins and losses) else None)
        out.append({
            "year": y,
            "n": n,
            "realized": round(sum(pnls), 0),
            "win_rate": round(100.0 * len(wins) / n, 1) if n else None,
            "win_ci": [round(100 * lo, 1), round(100 * hi, 1)],
            "avg_pnl": round(sum(pnls) / n, 0) if n else None,
            "median_pnl": round(quantile(pnls, 0.5), 0) if n else None,
            "payoff": round(payoff, 2) if payoff else None,
            "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "excl_top3": round(excl3, 0),
            "excl_top10": round(excl10, 0),
            "best": round(pnls[-1], 0) if n else None,
            "worst": round(pnls[0], 0) if n else None,
            "year_end_equity": round(eq_y, 0) if eq_y else None,
            "year_ret_pct": round(100 * ret, 1) if ret is not None else None,
        })
        prev_eq = eq_y
    return out


def global_stats(trades: list[dict]) -> dict:
    pnls = sorted(t["pnl"] for t in trades)
    n = len(pnls)
    if n == 0:
        return {}
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    se = sd / math.sqrt(n)
    tc = t_crit(n - 1)
    med = quantile(pnls, 0.5)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    # 赔率 = 平均盈利 / 平均亏损（均值比）；PF = 总盈利 / 总亏损（总额比）。两者不同，勿混。
    payoff = ((gross_win / len(wins)) / (gross_loss / len(losses))
              if (wins and losses) else float("inf"))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    # 保本胜率由赔率决定：p* = 1/(1+赔率)
    breakeven_wr = 100.0 / (1 + payoff) if payoff != float("inf") else 0.0
    lo, hi = wilson(len(wins), n)
    total = sum(pnls)

    # Bootstrap 均值 CI（10000 次，非参数；偏度大时比 t 区间可信）
    import random
    random.seed(20261105)
    boot = []
    for _ in range(10000):
        s = sum(pnls[random.randrange(n)] for _ in range(n))
        boot.append(s / n)
    boot.sort()
    boot_ci = [round(boot[250], 0), round(boot[9750], 0)]

    # 集中度：前 1%/5%/10% 笔数贡献占总盈利比
    def top_share(frac):
        k = max(1, int(round(n * frac)))
        return round(100.0 * sum(pnls[-k:]) / total, 1) if total > 0 else None

    # 偏度
    if sd > 0:
        skew = sum(((p - mean) / sd) ** 3 for p in pnls) / n
    else:
        skew = 0.0

    return {
        "n": n,
        "total": round(total, 0),
        "mean": round(mean, 0),
        "mean_ci": [round(mean - tc * se, 0), round(mean + tc * se, 0)],
        "t_stat": round(mean / se, 2) if se > 0 else None,
        "median": round(med, 0),
        "sd": round(sd, 0),
        "skew": round(skew, 2),
        "p05": round(quantile(pnls, 0.05), 0),
        "p25": round(quantile(pnls, 0.25), 0),
        "p75": round(quantile(pnls, 0.75), 0),
        "p95": round(quantile(pnls, 0.95), 0),
        "win_rate": round(100.0 * len(wins) / n, 1),
        "win_ci": [round(100 * lo, 1), round(100 * hi, 1)],
        "payoff": round(payoff, 2) if payoff != float("inf") else None,
        "pf": round(pf, 2) if pf != float("inf") else None,
        "boot_ci": boot_ci,
        "binom_p_vs_breakeven": round(binom_two_sided_p(len(wins), n, breakeven_wr / 100.0), 4),
        "breakeven_win_rate": round(breakeven_wr, 1),
        "wr_margin_pp": round(100.0 * len(wins) / n - breakeven_wr, 1),
        "excl_top3": round(sum(pnls[:-3]), 0) if n > 3 else 0,
        "excl_top10": round(sum(pnls[:-10]), 0) if n > 10 else 0,
        "top1pct_share": top_share(0.01),
        "top5pct_share": top_share(0.05),
        "top10pct_share": top_share(0.10),
        "avg_win": round(gross_win / len(wins), 0) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 0) if losses else None,
    }


def by_reason(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for t in trades:
        groups.setdefault(t["reason"] or "(空)", []).append(t["pnl"])
    out = []
    for r, ps in sorted(groups.items(), key=lambda kv: -sum(kv[1])):
        n = len(ps)
        w = len([p for p in ps if p > 0])
        out.append({
            "reason": r, "n": n, "total": round(sum(ps), 0),
            "avg": round(sum(ps) / n, 0), "win_rate": round(100 * w / n, 1),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    db = Path(a.db)
    res = analyze(db)

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT code, CAST(opened_date AS TEXT), CAST(closed_date AS TEXT), "
        "COALESCE(close_reason,''), COALESCE(realized_pnl,0) "
        "FROM position WHERE status='closed'"
    ).fetchall()
    con.close()
    trades = [{"code": r[0], "open": r[1], "close": r[2], "reason": r[3], "pnl": float(r[4])} for r in rows]

    yr_end = {int(k): v for k, v in res["year_end_equity"].items()}
    res["by_year"] = by_year(trades, yr_end)
    res["global"] = global_stats(trades)
    res["by_reason"] = by_reason(trades)

    p = res["progress"]
    print(f"进度 {p['days_done']}/{p['days_total']} = {p['pct']}%  "
          f"({p['first_date']} ~ {p['last_date']})")
    e = res.get("equity", {})
    print(f"权益 {e.get('start',0):,.0f} → {e.get('now',0):,.0f} "
          f"(×{e.get('multiple')})  峰值 {e.get('peak',0):,.0f}  最大回撤 {e.get('maxdd_pct')}%")
    # CAGR：按实际覆盖的交易年数（首末日期）
    from datetime import date
    d0 = date.fromisoformat(p["first_date"]); d1 = date.fromisoformat(p["last_date"])
    yrs = (d1 - d0).days / 365.25
    if e.get("start"):
        cagr = (e["now"] / e["start"]) ** (1 / yrs) - 1
        print(f"年化（CAGR，{yrs:.2f} 年）= {100*cagr:.2f}%")
        res["global_cagr_pct"] = round(100 * cagr, 2)
    print(f"平仓 {res['trades_total']} 笔，在仓 {res['open_positions']} 笔，腿 {res['legs']['n']}")
    print()
    print(f"{'年':<6}{'笔数':>5}{'已实现':>12}{'胜率%':>7}{'赔率':>6}{'PF':>6}"
          f"{'剔前3':>12}{'均笔':>9}{'年末权益':>13}{'年收益%':>9}")
    for r in res["by_year"]:
        print(f"{r['year']:<6}{r['n']:>5}{r['realized']:>12,.0f}{r['win_rate']:>7.1f}"
              f"{(r['payoff'] or 0):>6.2f}{(r['pf'] or 0):>6.2f}{r['excl_top3']:>12,.0f}"
              f"{(r['avg_pnl'] or 0):>9,.0f}{(r['year_end_equity'] or 0):>13,.0f}"
              f"{(r['year_ret_pct'] if r['year_ret_pct'] is not None else float('nan')):>9.1f}")
    print()
    g = res["global"]
    print(f"全局：n={g['n']} 合计 {g['total']:,.0f} 均笔 {g['mean']:,.0f} "
          f"t-CI[{g['mean_ci'][0]:,.0f}, {g['mean_ci'][1]:,.0f}] t={g['t_stat']}  "
          f"boot-CI[{g['boot_ci'][0]:,.0f}, {g['boot_ci'][1]:,.0f}]")
    print(f"      胜率 {g['win_rate']}% CI{g['win_ci']} 保本胜率 {g['breakeven_win_rate']}% "
          f"余量 {g['wr_margin_pp']}pp  赔率 {g['payoff']}  PF {g['pf']}  "
          f"二项p(vs保本)={g['binom_p_vs_breakeven']}")
    print(f"      中位 {g['median']:,.0f} 偏度 {g['skew']} p5 {g['p05']:,.0f} p95 {g['p95']:,.0f}")
    print(f"      剔前3 {g['excl_top3']:,.0f}  剔前10 {g['excl_top10']:,.0f}  "
          f"前1%笔贡献 {g['top1pct_share']}% 前5% {g['top5pct_share']}%")
    # 年度符号检验
    pos = len([r for r in res["by_year"] if r["realized"] > 0])
    tot = len(res["by_year"])
    if tot:
        pv = binom_two_sided_p(pos, tot, 0.5)
        print(f"      已实现为正的年数 {pos}/{tot}，二项检验 p={pv:.3f}")
        res["year_sign_test"] = {"positive": pos, "total": tot, "p": round(pv, 3)}
    print()
    print("出口分解：")
    for r in res["by_reason"]:
        print(f"  {r['reason']:<18}{r['n']:>5} 笔  合计 {r['total']:>12,.0f}  均笔 {r['avg']:>8,.0f}  胜率 {r['win_rate']:>5.1f}%")

    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON → {a.json}")


if __name__ == "__main__":
    main()
