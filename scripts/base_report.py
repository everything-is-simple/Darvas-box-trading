#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""base_report.py —— 连续基线长跑「HTML 进度报告」生成器

只读回测库，一次产出完整 HTML（可直接双击打开）。替代此前手工编辑的
docs/reports/new-baseline-progress-report.html —— 手改版会与库脱节。

产出内容：
  1. 进度卡片（交易日完成度 / 权益 / CAGR / 最大回撤）
  2. 按年账本（已实现口径，含 Wilson CI / 赔率 / PF / 剔前3 / 年收益）
  3. 全局概率统计（t-CI / bootstrap-CI / 保本胜率 / 集中度 / 年度符号检验）
  4. 阶梯四档（**最近档 ±0.35 + tx 桶**，与 base_loss_source.py 同口径，R23）
  5. 盈亏来源分解（分母端均亏 vs 分子端均盈的 CV / Spearman）
  6. 出口分解
  7. 仓位利用率与持有天数分带
  8. 回撤解剖（峰值后逐段已实现）
  9. 运行状态

口径纪律（.workbuddy/memory/MEMORY.md）：
  - 「权益变动」与「已实现」分列，不混用；
  - 未跑完整年的年度收益率是**临时口径**，必须标 ⏳；
  - 剔除前三赢家净利必报；
  - 阶梯分档禁用硬边界（R23）。

用法：
  python scripts/base_report.py                 # 写到默认路径
  python scripts/base_report.py --out docs/reports/xxx.html
  python scripts/base_report.py --stdout        # 只打印摘要
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from datetime import date, datetime
from pathlib import Path

import base_progress_stat as bps

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "var" / "bt" / "base" / "bt_baseA_cont_adj.db"
DEFAULT_OUT = ROOT / "docs" / "reports" / "new-baseline-progress-report.html"
TOTAL_DAYS = bps.TOTAL_DAYS

MINUS = "\u2212"


# ---------- 格式化 ----------
def n0(x: float | int | None, sign: bool = False) -> str:
    """整数千分位；负数用 U+2212。"""
    if x is None:
        return "—"
    v = round(float(x))
    s = f"{abs(v):,}"
    if v < 0:
        return MINUS + s
    return ("+" + s) if (sign and v > 0) else s


def cls(x: float | None) -> str:
    if x is None:
        return ""
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def f2(x: float | None, sign: bool = False) -> str:
    if x is None:
        return "—"
    s = f"{abs(x):.2f}"
    if x < 0:
        return MINUS + s
    return ("+" + s) if (sign and x > 0) else s


# ---------- 统计小工具 ----------
def quantile(sorted_vals: list[float], q: float) -> float:
    return bps.quantile(sorted_vals, q)


def spearman(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 3:
        return float("nan")

    def rank(x):
        order = sorted(range(n), key=lambda i: x[i])
        r = [0] * n
        for k, i in enumerate(order):
            r[i] = k + 1
        return r

    ra, rb = rank(a), rank(b)
    d = sum((ra[i] - rb[i]) ** 2 for i in range(n))
    return 1 - 6 * d / (n * (n * n - 1))


def cv(vals: list[float]) -> float:
    m = sum(vals) / len(vals)
    if m == 0 or len(vals) < 2:
        return float("nan")
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
    return sd / abs(m)


# ---------- 取数 ----------
def load(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c = con.cursor()
    d: dict = {}

    nd, dmin, dmax = c.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM daily_run WHERE task='plan' AND status='ok'"
    ).fetchone()
    d["progress"] = {"done": nd, "total": TOTAL_DAYS,
                     "pct": 100.0 * nd / TOTAL_DAYS,
                     "first": dmin, "last": dmax,
                     "remain": TOTAL_DAYS - nd}

    d["eq"] = c.execute(
        "SELECT date, total, cash, market_value FROM equity_snapshot ORDER BY date"
    ).fetchall()

    d["trades"] = [
        {"code": r[0], "open": r[1], "close": r[2], "reason": r[3], "pnl": float(r[4])}
        for r in c.execute(
            "SELECT code, CAST(opened_date AS TEXT), CAST(closed_date AS TEXT), "
            "COALESCE(close_reason,''), COALESCE(realized_pnl,0) "
            "FROM position WHERE status='closed'"
        )
    ]
    d["open_pos"] = c.execute("SELECT COUNT(*) FROM position WHERE status<>'closed'").fetchone()[0]
    d["legs"] = c.execute("SELECT COUNT(*) FROM position_leg").fetchone()[0]

    d["leg1"] = {r[0]: float(r[1] or 0) for r in c.execute(
        "SELECT position_id, SUM(qty*price) FROM position_leg WHERE leg_no=1 GROUP BY position_id")}

    bl = c.execute(
        "SELECT baseline_id, engine_sha256, engine_version, data_mode, created_at "
        "FROM baseline ORDER BY id LIMIT 1").fetchone()
    d["baseline"] = {"id": bl[0], "sha": bl[1], "ver": bl[2],
                     "mode": bl[3], "created": bl[4]} if bl else {}
    d["param_n"] = c.execute("SELECT COUNT(*) FROM param").fetchone()[0]

    # 出口 × 档位交叉所需的 close_reason 已在 trades 里
    d["pos_rows"] = c.execute(
        "SELECT id, opened_date, closed_date, COALESCE(realized_pnl,0) "
        "FROM position WHERE status='closed'").fetchall()
    con.close()
    return d


# ---------- 分析 ----------
def analyze(d: dict) -> dict:
    eq = d["eq"]
    trades = d["trades"]
    out: dict = {}

    # 权益 / 回撤
    peak = -1e18
    peak_date = ""
    mdd = 0.0
    mdd_peak_d = mdd_trough_d = ""
    cur_peak = None
    for dt, tot, _, _ in eq:
        if tot > peak:
            peak, peak_date = tot, dt
            cur_peak = (tot, dt)
        dd = tot / peak - 1 if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
            mdd_peak_d, mdd_trough_d = cur_peak[1], dt
    out["equity"] = {
        "start": eq[0][1], "now": eq[-1][1], "now_date": eq[-1][0],
        "peak": peak, "peak_date": peak_date,
        "mdd": mdd * 100, "mdd_peak": mdd_peak_d, "mdd_trough": mdd_trough_d,
        "mult": eq[-1][1] / eq[0][1],
    }
    d0 = date.fromisoformat(d["progress"]["first"])
    d1 = date.fromisoformat(d["progress"]["last"])
    yrs = (d1 - d0).days / 365.25
    out["cagr"] = 100 * ((eq[-1][1] / eq[0][1]) ** (1 / yrs) - 1)
    out["years_span"] = yrs

    # 年末权益
    yr_end: dict[int, float] = {}
    for dt, tot, _, _ in eq:
        yr_end[int(dt[:4])] = tot
    out["yr_end"] = yr_end

    # 按年
    out["by_year"] = bps.by_year(trades, yr_end)
    out["global"] = bps.global_stats(trades)
    out["by_reason"] = bps.by_reason(trades)

    # 年度符号检验
    pos = len([r for r in out["by_year"] if r["realized"] > 0])
    tot = len(out["by_year"])
    out["sign_test"] = {"pos": pos, "tot": tot, "p": bps.binom_two_sided_p(pos, tot, 0.5)}

    # 阶梯四档（最近档 ±0.35 + tx）—— R23
    eqmap = {dt: tot for dt, tot, _, _ in eq}
    bands = {"t1(3)": [], "t2(2)": [], "t3(1)": [], "t4(0.5)": [], "tx": []}
    for pid, od, cd, pnl in d["pos_rows"]:
        e = eqmap.get(od, 0)
        notional = d["leg1"].get(pid, 0)
        u = notional / (e * 0.02) if e > 0 else 0.0
        lab = "tx"
        for nm, v in (("t1(3)", 3.0), ("t2(2)", 2.0), ("t3(1)", 1.0), ("t4(0.5)", 0.5)):
            if abs(u - v) <= 0.35:
                lab = nm
                break
        bands[lab].append({"pnl": float(pnl), "units": u, "open": od, "close": cd})
    out["bands"] = {}
    for k, rows in bands.items():
        if not rows:
            continue
        ps = [r["pnl"] for r in rows]
        w = [p for p in ps if p > 0]
        lo = [p for p in ps if p <= 0]
        gw, gl = sum(w), -sum(lo)
        aw = gw / len(w) if w else 0.0
        al = gl / len(lo) if lo else 0.0
        out["bands"][k] = {
            "n": len(ps), "total": sum(ps),
            "wr": 100 * len(w) / len(ps),
            "avg_win": aw, "avg_loss": -al,
            "payoff": aw / al if al else None,
            "be": 100 * al / (aw + al) if (aw + al) else None,
            "pf": gw / gl if gl else None,
            "excl3": sum(sorted(ps)[:-3]) if len(ps) > 3 else 0.0,
            "per_unit": sum(ps) / sum(r["units"] for r in rows) if rows else None,
        }

    # 分母端 / 分子端
    mean_loss = []
    mean_win = []
    win_pairs = []   # (年均盈, 年已实现) 仅在当年有赢家时成对
    loss_pairs = []
    for r in out["by_year"]:
        sub = [t["pnl"] for t in trades if t["close"] and int(t["close"][:4]) == r["year"]]
        w = [p for p in sub if p > 0]
        l = [p for p in sub if p <= 0]
        # 无赢家 / 无输家的年份（如 2018 全年 0 胜）不参与均值，否则把 0 当成观测拉偏 CV
        if w:
            mw = sum(w) / len(w)
            mean_win.append(mw)
            win_pairs.append((mw, r["realized"]))
        if l:
            ml = sum(l) / len(l)
            mean_loss.append(ml)
            loss_pairs.append((ml, r["realized"]))
    out["den_num"] = {
        "avg_loss_cv": cv(mean_loss), "avg_win_cv": cv(mean_win),
        "avg_loss_min": min(mean_loss), "avg_loss_max": max(mean_loss),
        "avg_win_min": min(mean_win), "avg_win_max": max(mean_win),
        "avg_loss_mean": sum(mean_loss) / len(mean_loss),
        "avg_win_mean": sum(mean_win) / len(mean_win),
        "rho_n": spearman([r["n"] for r in out["by_year"]],
                          [r["realized"] for r in out["by_year"]]),
        "rho_wr": spearman([r["win_rate"] for r in out["by_year"]],
                           [r["realized"] for r in out["by_year"]]),
        "rho_avgloss": spearman([x for x, _ in loss_pairs], [y for _, y in loss_pairs]),
        "rho_avgwin": spearman([x for x, _ in win_pairs], [y for _, y in win_pairs]),
    }

    # 仓位利用率
    ocd = [(r[1], r[2]) for r in d["pos_rows"]]
    busy = []
    mv_ratio = []
    for dt, tot, cash, mval in eq:
        k = 0
        for o, cl in ocd:
            if o and o <= dt and (not cl or cl >= dt):
                k += 1
        busy.append(k)
        if tot > 0:
            mv_ratio.append(mval / tot)
    busy_sorted = sorted(busy)
    mv_sorted = sorted(mv_ratio)
    out["util"] = {
        "avg_pos": sum(busy) / len(busy),
        "med_pos": quantile(sorted(float(x) for x in busy), 0.5),
        "max_pos": max(busy),
        "avg_mv": 100 * sum(mv_ratio) / len(mv_ratio),
        "med_mv": 100 * quantile(mv_sorted, 0.5),
        "p95_mv": 100 * quantile(mv_sorted, 0.95),
        "max_mv": 100 * max(mv_ratio),
        "flat_days": sum(1 for x in busy if x == 0),
        "days": len(busy),
        "slots": 14,
    }

    # 持有天数分带
    idx = {dt: i for i, (dt, _, _, _) in enumerate(eq)}
    hold = []
    for t in trades:
        if not t["close"]:
            continue
        a, b = idx.get(t["open"]), idx.get(t["close"])
        hold.append((b - a) if (a is not None and b is not None) else 0)
    buckets = [("≤2 日", 0, 2), ("3–5 日", 3, 5), ("6–10 日", 6, 10),
               ("11–20 日", 11, 20), (">20 日", 21, 10 ** 6)]
    hb = []
    for nm, lo, hi in buckets:
        ps = [t["pnl"] for t, h in zip(trades, hold) if lo <= h <= hi]
        if not ps:
            continue
        w = [p for p in ps if p > 0]
        hb.append({"name": nm, "n": len(ps), "total": sum(ps),
                   "avg": sum(ps) / len(ps), "wr": 100 * len(w) / len(ps)})
    out["hold"] = hb

    # 回撤解剖：峰值次月起已实现（不含峰值当月，避免把峰值月的大额兑现算成"失血"）
    peak_d = out["equity"]["peak_date"]
    cut = peak_d[:7] + "-31"                      # ISO 日期可直接字符串比较
    after = [t for t in trades if t["close"] and t["close"] > cut]
    seg: dict[int, list[float]] = {}
    for t in after:
        seg.setdefault(int(t["close"][:4]), []).append(t["pnl"])
    # 峰值后权益最低点
    post_eq = [(dt, tot) for dt, tot, _, _ in eq if dt > peak_d]
    trough_d, trough_v = min(post_eq, key=lambda x: x[1]) if post_eq else ("", out["equity"]["peak"])
    out["post_peak"] = {
        "peak_date": peak_d,
        "peak_eq": out["equity"]["peak"],
        "peak_mv": next((mv for dt, _, _, mv in eq if dt == peak_d), 0.0),
        "trough_date": trough_d,
        "trough_eq": trough_v,
        "drop": trough_v - out["equity"]["peak"],
        "cut": cut,
        "n": len(after),
        "total": sum(t["pnl"] for t in after),
        "seg": [{"year": y, "n": len(v), "total": sum(v)} for y, v in sorted(seg.items())],
    }

    # 单笔亏损集中度
    ps_all = sorted(t["pnl"] for t in trades)
    losses = [p for p in ps_all if p <= 0]
    worst20 = sum(sorted(losses)[:20]) if len(losses) >= 20 else sum(losses)
    out["loss_conc"] = {
        "n_loss": len(losses), "loss_total": sum(losses),
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "worst": min(losses) if losses else 0,
        "worst20_share": 100 * worst20 / sum(losses) if losses else None,
        "worst20": worst20,
    }
    return out


# ---------- HTML ----------
CSS = """
  :root{
    --bg:#f7f8fa; --card:#ffffff; --ink:#1a1d23; --muted:#6b7280;
    --line:#e5e7eb; --up:#d63031; --down:#16a34a; --accent:#185fa5; --warn:#ba7517;
  }
  *{box-sizing:border-box}
  body{margin:0;padding:32px 20px 64px;background:var(--bg);color:var(--ink);
       font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:1120px;margin:0 auto}
  h1{font-size:24px;margin:0 0 6px;letter-spacing:-.3px;font-weight:550}
  .sub{color:var(--muted);font-size:13px;margin-bottom:24px;line-height:1.8}
  code{background:#eef0f3;padding:1px 5px;border-radius:4px;font-size:12px}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:28px}
  .card{background:var(--card);border:.5px solid var(--line);border-radius:10px;padding:16px 18px}
  .card .k{font-size:12px;color:var(--muted);margin-bottom:6px}
  .card .v{font-size:22px;font-weight:600;letter-spacing:-.5px}
  .card .n{font-size:12px;color:var(--muted);margin-top:4px}
  h2{font-size:16px;margin:34px 0 12px;padding-bottom:8px;border-bottom:.5px solid var(--line);font-weight:550}
  table{width:100%;border-collapse:collapse;background:var(--card);
        border:.5px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
  th,td{padding:8px 9px;text-align:right;border-bottom:.5px solid var(--line);white-space:nowrap}
  th{background:#f1f3f6;font-weight:600;font-size:12px;color:#374151}
  td:first-child,th:first-child{text-align:left;font-weight:600}
  tr:last-child td{border-bottom:none}
  tr.sum td{background:#f8f9fb;font-weight:600}
  .pos{color:var(--up)} .neg{color:var(--down)}
  .bar{height:4px;background:#e5e7eb;border-radius:2px;position:relative;margin-top:8px}
  .bar i{position:absolute;left:0;top:0;height:4px;background:var(--accent);border-radius:2px}
  .note{background:#faeeda;border:.5px solid #f0d9a8;border-radius:8px;padding:12px 16px;
        font-size:13px;color:#7a4d0e;margin:16px 0}
  .info{background:#e6f1fb;border-color:#bcd9f0;color:#12456f}
  .kv{display:grid;grid-template-columns:repeat(2,1fr);gap:10px 28px;font-size:13.5px;
      background:var(--card);border:.5px solid var(--line);border-radius:10px;padding:16px 20px}
  .kv div{display:flex;justify-content:space-between;gap:12px}
  .kv span{color:var(--muted)} .kv b{font-weight:600}
  ul{padding-left:20px;margin:10px 0} li{margin:9px 0;font-size:14px}
  .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:20px;
       background:#eef2ff;color:#3730a3;margin-left:6px;vertical-align:middle;font-weight:400}
  .tagw{background:#fbeecf;color:#8a5b0a}
  .foot{color:var(--muted);font-size:12px;margin-top:40px;border-top:.5px solid var(--line);padding-top:14px}
"""


def render(d: dict, a: dict, now: str) -> str:
    p, e, g = d["progress"], a["equity"], a["global"]
    dn = a["den_num"]
    bl = d["baseline"]
    sha8 = (bl.get("sha") or "")[:8]

    L: list[str] = []
    w = L.append

    w(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>新基线长跑进度报告 · {now}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<h1>新基线长跑进度报告</h1>
<div class="sub">
  库 <code>var/bt/base/bt_baseA_cont_adj.db</code> · 引擎 <code>{bl.get('ver','')}</code> sha <code>{sha8}…</code><br>
  基线身份 <code>{bl.get('id','')}</code> · 数据口径 <b>{bl.get('mode','')}</b> · 参数 <b>{d['param_n']} 键入库 / 49 键实读</b> · 快照时间 <b>{now}</b>
</div>

<div class="cards">
  <div class="card">
    <div class="k">交易日进度</div>
    <div class="v">{p['done']:,} / {p['total']:,}</div>
    <div class="n">{p['pct']:.2f}% · 至 {p['last']}</div>
    <div class="bar"><i style="width:{p['pct']:.2f}%"></i></div>
  </div>
  <div class="card">
    <div class="k">权益</div>
    <div class="v {'pos' if e['now'] > e['start'] else 'neg'}">{e['now']/10000:.1f} 万</div>
    <div class="n">×{e['mult']:.3f}（起始 100 万）</div>
  </div>
  <div class="card">
    <div class="k">年化 CAGR</div>
    <div class="v">{a['cagr']:.2f}%</div>
    <div class="n">{a['years_span']:.2f} 年（含未完成年）</div>
  </div>
  <div class="card">
    <div class="k">最大回撤</div>
    <div class="v neg">{MINUS}{abs(e['mdd']):.2f}%</div>
    <div class="n">峰值 {e['peak']/10000:.1f} 万 @ {e['peak_date']}</div>
  </div>
</div>
""")

    # 一、按年账本
    w("<h2>一、按年账本（已实现口径）</h2>\n<table>\n<thead><tr>"
      "<th>年</th><th>笔数</th><th>已实现</th><th>胜率%</th><th>95% CI</th>"
      "<th>赔率</th><th>PF</th><th>剔前3</th><th>均笔</th><th>年末权益</th><th>年收益%</th>"
      "</tr></thead>\n<tbody>\n")
    last_year = int(p["last"][:4])
    for r in a["by_year"]:
        partial = (r["year"] == last_year and p["pct"] < 100)
        tag = '<span class="tag tagw">进行中</span>' if partial else ""
        w(f"<tr><td>{r['year']}{tag}</td><td>{r['n']}</td>"
          f"<td class=\"{cls(r['realized'])}\">{n0(r['realized'], True)}</td>"
          f"<td>{r['win_rate']:.1f}</td>"
          f"<td>{r['win_ci'][0]:.1f}–{r['win_ci'][1]:.1f}</td>"
          f"<td>{f2(r['payoff'])}</td>"
          f"<td>{f2(r['pf'])}</td>"
          f"<td class=\"{cls(r['excl_top3'])}\">{n0(r['excl_top3'], True)}</td>"
          f"<td class=\"{cls(r['avg_pnl'])}\">{n0(r['avg_pnl'], True)}</td>"
          f"<td>{n0(r['year_end_equity'])}</td>"
          f"<td class=\"{cls(r['year_ret_pct'])}\">{(MINUS if (r['year_ret_pct'] or 0) < 0 else ('+' if (r['year_ret_pct'] or 0) > 0 else ''))}{abs(r['year_ret_pct'] or 0):.1f}{'⏳' if partial else ''}</td></tr>\n")
    w(f"<tr class=\"sum\"><td>合计</td><td>{g['n']}</td>"
      f"<td class=\"{cls(g['total'])}\">{n0(g['total'], True)}</td>"
      f"<td>{g['win_rate']:.1f}</td>"
      f"<td>{g['win_ci'][0]:.1f}–{g['win_ci'][1]:.1f}</td>"
      f"<td>{f2(g['payoff'])}</td><td>{f2(g['pf'])}</td>"
      f"<td class=\"{cls(g['excl_top3'])}\">{n0(g['excl_top3'], True)}</td>"
      f"<td class=\"{cls(g['mean'])}\">{n0(g['mean'], True)}</td>"
      f"<td>{n0(e['now'])}</td>"
      f"<td>×{e['mult']:.3f}</td></tr>\n</tbody>\n</table>\n")

    if p["pct"] < 100:
        w(f"""<div class="note">
<b>{last_year} 是进行时口径（{p['pct']:.2f}%，剩 {p['remain']} 个交易日），不能当年度结论。</b>
年内权益变动与已实现之间的差额含未平仓浮盈；同理任何未跑完整年的「转正」都只是临时数（R15）。
</div>
""")

    # 二、概率统计
    w(f"""<h2>二、概率统计（n = {g['n']:,} 笔已平仓）</h2>
<div class="kv">
  <div><span>胜率</span><b>{g['win_rate']}%　CI [{g['win_ci'][0]}%, {g['win_ci'][1]}%]</b></div>
  <div><span>赔率（均盈 / 均亏）</span><b>{f2(g['payoff'])} : 1</b></div>
  <div><span>保本胜率 1/(1+赔率)</span><b>{g['breakeven_win_rate']}%</b></div>
  <div><span>胜率余量</span><b class="{'pos' if g['wr_margin_pp']>0 else 'neg'}">{'+' if g['wr_margin_pp']>0 else MINUS}{abs(g['wr_margin_pp'])}pp</b></div>
  <div><span>PF（总盈 / 总亏）</span><b>{f2(g['pf'])}</b></div>
  <div><span>均笔 / 中位笔</span><b>{n0(g['mean'], True)} / {n0(g['median'])}</b></div>
  <div><span>均笔 95% CI（t 分布）</span><b>[{n0(g['mean_ci'][0], True)}, {n0(g['mean_ci'][1], True)}]　t={g['t_stat']}</b></div>
  <div><span>均笔 95% CI（bootstrap）</span><b>[{n0(g['boot_ci'][0], True)}, {n0(g['boot_ci'][1], True)}]</b></div>
  <div><span>偏度</span><b>{g['skew']}（极度右偏）</b></div>
  <div><span>分位 p5 / p25 / p75 / p95</span><b>{n0(g['p05'])} / {n0(g['p25'])} / {n0(g['p75'])} / {n0(g['p95'])}</b></div>
  <div><span>剔前 3 笔</span><b class="{'pos' if g['excl_top3']>0 else 'neg'}">{n0(g['excl_top3'], True)}</b></div>
  <div><span>剔前 10 笔 <span class="tagw tag">仅列不打分</span></span><b class="{'pos' if g['excl_top10']>0 else 'neg'}">{n0(g['excl_top10'], True)}</b></div>
  <div><span>前 1% 笔贡献总盈利</span><b>{g['top1pct_share']}%</b></div>
  <div><span>前 5% 笔贡献</span><b>{g['top5pct_share']}%</b></div>
  <div><span>年度正负</span><b>{a['sign_test']['pos']} / {a['sign_test']['tot']}，二项检验 p = {a['sign_test']['p']:.3f}</b></div>
  <div><span>剔除最好年总账 <span class="tag">主判据</span></span><b class="{'pos' if g['total']-max(r['realized'] for r in a['by_year'])>0 else 'neg'}">{n0(g['total'] - max(r['realized'] for r in a['by_year']), True)}</b></div>
</div>
""")

    # 三、阶梯四档
    w("""<h2>三、阶梯四档：赔率 × 保本胜率</h2>
<table>
<thead><tr><th>档</th><th>n</th><th>占比</th><th>实胜率%</th><th>均盈</th><th>均亏</th>
<th>赔率</th><th>保本%</th><th>余量pp</th><th>PF</th><th>每单位</th><th>总账</th></tr></thead>
<tbody>
""")
    tot_n = sum(b["n"] for b in a["bands"].values())
    for k in ("t1(3)", "t2(2)", "t3(1)", "t4(0.5)", "tx"):
        b = a["bands"].get(k)
        if not b:
            continue
        margin = b["wr"] - (b["be"] or 0)
        lab = {"t1(3)": "t1（3 单位）", "t2(2)": "t2（2 单位）", "t3(1)": "t3（1 单位）",
               "t4(0.5)": "t4（0.5 单位）", "tx": "tx（畸形仓）"}[k]
        bold = ' style="background:#fdf6f6"' if k == "t4(0.5)" else ""
        w(f"<tr{bold}><td>{lab}</td><td>{b['n']}</td><td>{100*b['n']/tot_n:.1f}%</td>"
          f"<td>{b['wr']:.1f}</td><td>{n0(b['avg_win'])}</td><td>{n0(b['avg_loss'])}</td>"
          f"<td>{f2(b['payoff'])}</td><td>{b['be']:.1f}</td>"
          f"<td class=\"{'pos' if margin>0 else 'neg'}\">{'+' if margin>0 else MINUS}{abs(margin):.1f}</td>"
          f"<td>{f2(b['pf'])}</td><td class=\"{cls(b['per_unit'])}\">{n0(b['per_unit'], True)}</td>"
          f"<td class=\"{cls(b['total'])}\">{n0(b['total'], True)}</td></tr>\n")
    w("</tbody>\n</table>\n")
    w("""<div class="note">
分档口径 = <b>「最近档 ±0.35 + tx 未归类桶」</b>：<code>units = Σ(leg_no=1 qty×price) / (当日权益 × 2%)</code>，
<b>实测为连续量</b>（p25≈0.49、p50≈0.98）。<b>禁用硬边界</b>——硬边界 <code>u≥0.5→t3</code> 会把 101 笔 0.5 档错划进 t3（R23）。
</div>
""")

    # 四、盈亏来源分解
    w(f"""<h2>四、盈亏来源：分母是常数，分子是变量</h2>
<table>
<thead><tr><th>量</th><th>年际 min</th><th>年际 max</th><th>均值</th><th>CV</th></tr></thead>
<tbody>
<tr><td>均亏（分母端）</td><td>{n0(dn['avg_loss_min'])}</td><td>{n0(dn['avg_loss_max'])}</td><td>{n0(dn['avg_loss_mean'])}</td><td><b>{dn['avg_loss_cv']:.2f}</b></td></tr>
<tr><td>均盈（分子端）</td><td>{n0(dn['avg_win_min'])}</td><td>{n0(dn['avg_win_max'])}</td><td>{n0(dn['avg_win_mean'])}</td><td><b>{dn['avg_win_cv']:.2f}</b></td></tr>
</tbody>
</table>

<div class="kv" style="margin-top:12px">
  <div><span>Spearman（年笔数, 年已实现）</span><b>{dn['rho_n']:+.3f}</b></div>
  <div><span>Spearman（年胜率, 年已实现）</span><b>{dn['rho_wr']:+.3f}</b></div>
  <div><span>Spearman（年均亏, 年已实现）</span><b>{dn['rho_avgloss']:+.3f}</b></div>
  <div><span>Spearman（年均盈, 年已实现）</span><b>{dn['rho_avgwin']:+.3f}</b></div>
</div>

<div class="note">
<b>均亏 CV {dn['avg_loss_cv']:.2f}（{n0(dn['avg_loss_min'])} ~ {n0(dn['avg_loss_max'])}）＝ 常数；均盈 CV {dn['avg_win_cv']:.2f}（{n0(dn['avg_win_min'])} ~ {n0(dn['avg_win_max'])}）＝ 变量。</b>
单笔盈利跨了 {abs(dn['avg_win_max']/max(1.0, dn['avg_win_min'])):.0f} 倍，单笔亏损从未失控。
→ 这套系统赚不赚钱，<b>100% 是「能不能抓到一次大涨幅」的问题，不是「会不会亏得多」的问题</b>。
所有出场端 / 止损端参数都在动分母，而分母已到底 —— 这解释了 <code>sell.discount</code>、<code>staledays</code> 放宽为何全是钝参数（R24）。
</div>
""")

    # 五、出口分解
    w("""<h2>五、出口分解</h2>
<table>
<thead><tr><th>出口</th><th>笔数</th><th>占比</th><th>合计</th><th>均笔</th><th>胜率</th></tr></thead>
<tbody>
""")
    for r in a["by_reason"]:
        w(f"<tr><td>{r['reason'] or '(空)'}</td><td>{r['n']}</td>"
          f"<td>{100*r['n']/g['n']:.1f}%</td>"
          f"<td class=\"{cls(r['total'])}\">{n0(r['total'], True)}</td>"
          f"<td class=\"{cls(r['avg'])}\">{n0(r['avg'], True)}</td>"
          f"<td>{r['win_rate']:.1f}%</td></tr>\n")
    w("</tbody>\n</table>\n")

    lc = a["loss_conc"]
    w(f"""<div class="note">
<b>没有灾难单，只有千刀万剐。</b>{lc['n_loss']:,} 笔亏损单合计 {n0(lc['loss_total'])}，均笔 {n0(lc['avg_loss'])}，
单笔最大 {n0(lc['worst'])}。最差 20 笔合计 {n0(lc['worst20'])}，仅占总亏损 <b>{lc['worst20_share']:.1f}%</b>。
</div>
""")

    # 六、仓位与持有
    u = a["util"]
    w(f"""<h2>六、仓位利用率与持有天数</h2>
<div class="kv">
  <div><span>平均在仓笔数</span><b>{u['avg_pos']:.2f} / {u['slots']} 槽位（{100*u['avg_pos']/u['slots']:.1f}%）</b></div>
  <div><span>中位 / 最大在仓</span><b>{u['med_pos']:.0f} / {u['max_pos']}</b></div>
  <div><span>平均仓位</span><b>{u['avg_mv']:.1f}%</b></div>
  <div><span>中位 / p95 仓位</span><b>{u['med_mv']:.1f}% / {u['p95_mv']:.1f}%</b></div>
  <div><span>空仓交易日</span><b>{u['flat_days']:,} / {u['days']:,}（{100*u['flat_days']/u['days']:.1f}%）</b></div>
  <div><span>当前在仓</span><b>{d['open_pos']} 笔</b></div>
</div>

<table style="margin-top:12px">
<thead><tr><th>持有天数</th><th>笔数</th><th>占比</th><th>合计</th><th>均笔</th><th>胜率</th></tr></thead>
<tbody>
""")
    for h in a["hold"]:
        w(f"<tr><td>{h['name']}</td><td>{h['n']}</td><td>{100*h['n']/g['n']:.1f}%</td>"
          f"<td class=\"{cls(h['total'])}\">{n0(h['total'], True)}</td>"
          f"<td class=\"{cls(h['avg'])}\">{n0(h['avg'], True)}</td>"
          f"<td>{h['wr']:.1f}%</td></tr>\n")
    w("</tbody>\n</table>\n")

    # 七、回撤解剖
    pp = a["post_peak"]
    neg_seg = [s for s in pp["seg"] if s["total"] < 0]
    unreal = pp["drop"] - pp["total"]        # 权益变动 − 已实现 ≈ 浮盈回吐
    w(f"""<h2>七、回撤解剖：峰值之后发生了什么</h2>
<div class="kv">
  <div><span>权益峰值</span><b>{n0(pp['peak_eq'])} @ {pp['peak_date']}（市值 {pp['peak_mv']/10000:.1f} 万，仓位 {100*pp['peak_mv']/pp['peak_eq']:.1f}%）</b></div>
  <div><span>峰值后最低权益</span><b>{n0(pp['trough_eq'])} @ {pp['trough_date']}</b></div>
  <div><span>峰值→最低 权益减少</span><b class="neg">{n0(pp['drop'])}</b></div>
  <div><span>其中 已实现（峰值次月起）</span><b class="{'pos' if pp['total']>0 else 'neg'}">{n0(pp['total'], True)}（{pp['n']} 笔）</b></div>
  <div><span>其中 浮盈回吐（残差）</span><b class="{'pos' if unreal>0 else 'neg'}">{n0(unreal, True)}</b></div>
  <div><span>回撤幅度</span><b class="neg">{MINUS}{abs(e['mdd']):.2f}%</b></div>
</div>
<table style="margin-top:12px">
<thead><tr><th>段（峰值次月起）</th><th>笔数</th><th>已实现</th></tr></thead>
<tbody>
""")
    for s in pp["seg"]:
        w(f"<tr><td>{s['year']}</td><td>{s['n']}</td>"
          f"<td class=\"{cls(s['total'])}\">{n0(s['total'], True)}</td></tr>\n")
    w("</tbody>\n</table>\n")
    w(f"""<div class="note">
峰值后 {pp['n']} 笔合计 <b>{n0(pp['total'], True)}</b>，其中 <b>{len(neg_seg)} 个年度段为负</b>
（{'、'.join(str(s['year']) for s in neg_seg)}）。
权益减少 {n0(pp['drop'])} 里，已实现只占 <b>{100*pp['total']/pp['drop'] if pp['drop'] else 0:.0f}%</b>，
其余 <b>{n0(unreal)}</b> 是<b>高位持仓的浮盈回吐</b>——回撤不是"卖出来亏的"，是"没卖"。
</div>
""")

    # 八、结构性结论
    yr = a["by_year"]
    best = max(yr, key=lambda r: r["realized"])
    best2 = sorted(yr, key=lambda r: -r["realized"])[1]
    worst = min(yr, key=lambda r: r["realized"])
    w(f"""<h2>八、结构性结论</h2>
<ul>
  <li><b>赚钱靠赔率不靠胜率。</b>胜率 {g['win_rate']}%，保本胜率仅 {g['breakeven_win_rate']}%，余量 {g['wr_margin_pp']}pp；
      赔率 {f2(g['payoff'])}:1 撑起整本账。</li>
  <li><b>利润极度集中。</b>{best['year']} 一年 {n0(best['realized'], True)} = 全账本的 {100*best['realized']/g['total']:.1f}%；
      剔除最好年（{best['year']}）后总账 {n0(g['total']-best['realized'], True)}，剔除最好两年后
      {n0(g['total']-best['realized']-best2['realized'], True)}。前 1% 笔贡献 {g['top1pct_share']}% 总盈利。</li>
  <li><b>最差的一年是 {worst['year']}：{n0(worst['realized'], True)}（{worst['n']} 笔、胜率 {worst['win_rate']}%）。</b>
      而它笔数是 14 年里第 {sorted([r['n'] for r in yr], reverse=True).index(worst['n'])+1} 高 ——
      「坏年份反而下注更多」ρ(笔数, 年已实现) = {dn['rho_n']:+.3f}。</li>
  <li><b>t4 档是唯一负值档。</b>{a['bands']['t4(0.5)']['n']} 笔（{100*a['bands']['t4(0.5)']['n']/tot_n:.1f}% 笔数）、
      赔率 {f2(a['bands']['t4(0.5)']['payoff'])}、保本胜率 {a['bands']['t4(0.5)']['be']:.1f}%、实胜率 {a['bands']['t4(0.5)']['wr']:.1f}%
      —— <b>赔率结构性覆盖不了胜率</b>，合计 {n0(a['bands']['t4(0.5)']['total'], True)}。</li>
</ul>
""")

    # 九、运行状态
    w(f"""<h2>九、运行状态</h2>
<div class="kv">
  <div><span>窗口</span><b>2005-01-01 ~ 2018-12-31（{p['total']:,} 交易日）</b></div>
  <div><span>已完成</span><b>{p['done']:,}（至 {p['last']}）</b></div>
  <div><span>剩余</span><b>{p['remain']} 交易日</b></div>
  <div><span>平仓 / 在仓 / 腿</span><b>{g['n']:,} / {d['open_pos']} / {d['legs']:,}</b></div>
  <div><span>基线参数</span><b>{d['param_n']} 键入库（docs/44 唯一事实源）</b></div>
  <div><span>引擎 sha256</span><b><code>{sha8}…</code></b></div>
</div>

<div class="note info">
续跑机制：<code>--resume-run</code> 校验<b>参数指纹 + 数据口径 + 引擎 sha256</b> 三项一致后，
从「六任务全 ok 的最大日期」接着跑，已完成的日子不重跑、不重复记账。进程被杀可随时重新拉起，账本幂等。
</div>

<div class="foot">
  本报告由 <code>python scripts/base_report.py</code> 自动生成，只读 <code>{bl.get('id','')}</code> 回测库；
  所有数字均从库内 <code>position</code> / <code>position_leg</code> / <code>equity_snapshot</code> 现算，<b>禁止手改</b>。<br>
  生成时间 {now} · 口径纪律见 <code>.workbuddy/memory/MEMORY.md</code> 与 <code>docs/55</code> 研究纪律 R13–R24。
</div>

</div>
</body>
</html>
""")
    return "".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--stdout", action="store_true", help="只打印摘要，不写文件")
    args = ap.parse_args()

    db = Path(args.db)
    raw = load(db)
    an = analyze(raw)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = render(raw, an, now)

    p, g = raw["progress"], an["global"]
    print(f"进度 {p['done']}/{p['total']} = {p['pct']:.2f}%  至 {p['last']}")
    print(f"权益 {an['equity']['now']:,.0f}（×{an['equity']['mult']:.3f}）  CAGR {an['cagr']:.2f}%  "
          f"MDD {an['equity']['mdd']:.2f}%")
    print(f"n={g['n']} 总账 {g['total']:,.0f}  胜率 {g['win_rate']}%  赔率 {g['payoff']}")
    if args.stdout:
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"HTML → {out}  ({len(html):,} 字符)")


if __name__ == "__main__":
    main()
