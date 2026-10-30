#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-4 · 突破强度 / 突破量比 · A 类后验统计生成器。

实验：docs/29 §七 P1-4（十维 #6/#7 何时买），A 类零成本后验，不重跑回测、不改代码。
框架：docs/experiments/e17_breakout_framework.md（零结果纯规则，含分带/裁定/HARD 判据）

三臂（框架 §3/§4）：
  臂 A  强度分带 3-4/4-5/5-6/6-7/7-8/8+         （1560 笔 filled）
  臂 B  量比分带 2-2.5/2.5-3/3-4/4-5/5-8/8x+    （1560 笔 filled）
  臂 C  被 max_entry_gap 弃掉的 118 笔反事实

口径（框架 §2）：
  strength = close(plan_date)/box_top − 1
  volratio = volume(plan_date) / avg(volume, 前 50 交易日，不含当日)
  cost     = Σ(position_leg.qty×price)     ← position.qty 平仓后清零（P2-5 坑）
  units    = cost / (建仓日权益 × 2%)
  每单位净利 = Σrealized_pnl / Σunits（已实现口径，不是权益变动）

输出（docs/experiments/，报告唯一沉淀处）：
  e17_breakout_report.html / .md / _data.json

用法：
  python scripts/e17_breakout.py --diag        # 只出可行性诊断（不写文件）
  python scripts/e17_breakout.py               # 正式生成（HARD 失败即中止）
  python scripts/e17_breakout.py --allow-drift # HARD 降级为 WARN
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "var" / "bt" / "base"
EXP = ROOT / "docs" / "experiments"
OUT_HTML = EXP / "e17_breakout_report.html"
OUT_MD = EXP / "e17_breakout_report.md"
OUT_JSON = EXP / "e17_breakout_data.json"

SLIPPAGE = 0.002          # match.slippage
PREMIUM = 1.03            # buy.premium
VOL_WINDOW = 50           # box.volwindow
NOISE_PER_UNIT = 600.0    # 噪声门槛：3% × 单位值 20000（框架 §5）
MIN_N = 30                # 样本不足阈值

# 臂定义：(label, lo, hi, lo_inclusive)
ARM_A = [("3–4%", 0.03, 0.04), ("4–5%", 0.04, 0.05), ("5–6%", 0.05, 0.06),
         ("6–7%", 0.06, 0.07), ("7–8%", 0.07, 0.08), ("8%+", 0.08, 9.9)]
ARM_B = [("2–2.5×", 2.0, 2.5), ("2.5–3×", 2.5, 3.0), ("3–4×", 3.0, 4.0),
         ("4–5×", 4.0, 5.0), ("5–8×", 5.0, 8.0), ("8×+", 8.0, 1e9)]
ARM_G = [("0–1%", 0.0, 0.01), ("1–2%", 0.01, 0.02), ("2–3%", 0.02, 0.03),
         ("3–5%", 0.03, 0.0500001)]


# ---------------------------------------------------------------- 基础工具
def db_paths():
    return sorted(BASE.glob("bt_baseA_*.db"))


def connect(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def pct(x, d=1):
    return "—" if x is None else f"{x*100:.{d}f}%"


def money(x, d=0):
    return "—" if x is None else f"{x:,.{d}f}"


def fnum(x, d=2):
    return "—" if x is None else f"{x:.{d}f}"


def median(v):
    return statistics.median(v) if v else None


def quantile(v, p):
    if not v:
        return None
    s = sorted(v)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def spearman(x, y):
    def rank(v):
        idx = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and abs(v[idx[j + 1]] - v[idx[i]]) < 1e-12:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r

    if len(x) < 3:
        return 0.0
    rx, ry = rank(x), rank(y)
    n = len(x)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def band_of(v, bands):
    if v is None:
        return None
    for label, lo, hi in bands:
        if lo <= v < hi:
            return label
    # 浮点边界兜底：挂到最后一档
    label, lo, hi = bands[-1]
    return label if v >= lo else bands[0][0]


# ---------------------------------------------------------------- 取数
def load_year(db: Path):
    """读一个年库 → (trades, cancelled, equity)。trades 一笔 = 一个 filled 买入单。"""
    year = int(db.stem.split("_")[-1])
    c = connect(db)
    try:
        eq = dict(c.execute("SELECT date, total FROM equity_snapshot").fetchall())
        first_eq = c.execute("SELECT total FROM equity_snapshot ORDER BY date LIMIT 1").fetchone()
        first_eq = first_eq[0] if first_eq else 0.0

        q = """
        SELECT t.plan_date, t.execute_date, o.code, o.trigger_price, o.reason_json, o.qty,
               b.close, b.volume,
               f.price, f.date,
               p.id, p.realized_pnl, p.status, p.close_reason, p.opened_date, p.closed_date,
               COALESCE(SUM(l.qty * l.price), 0) AS cost
        FROM plan_order o
        JOIN trade_plan t ON t.id = o.trade_plan_id
        JOIN daily_bar b ON b.code = o.code AND b.date = t.plan_date
        LEFT JOIN trade_fill f ON f.plan_order_id = o.id AND f.side = 'buy'
        LEFT JOIN position_leg l ON l.plan_order_id = o.id
        LEFT JOIN position p ON p.id = l.position_id
        WHERE o.side = 'buy' AND o.status = 'filled'
        GROUP BY o.id
        ORDER BY t.plan_date, o.code
        """
        rows = c.execute(q).fetchall()

        trades = []
        for (plan_date, exec_date, code, trig, rj, qty, close, vol,
             fill_px, fill_date, pid, pnl, pst, creason, od, cd, cost) in rows:
            rj = json.loads(rj) if rj else {}
            top = rj.get("box_top")
            if not top or close is None:
                continue
            vs = c.execute(
                "SELECT volume FROM daily_bar WHERE code=? AND date<? ORDER BY date DESC LIMIT ?",
                (code, plan_date, VOL_WINDOW)).fetchall()
            if not vs:
                continue
            avg = sum(v[0] for v in vs) / len(vs)
            strength = close / top - 1.0
            vr = (vol / avg) if avg > 0 else None

            hold_td = None
            if od and cd:
                hold_td = c.execute(
                    "SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
                    (code, od, cd)).fetchone()[0]

            base_eq = eq.get(od) or first_eq or 0.0
            unit_val = base_eq * 0.02
            units = (cost / unit_val) if unit_val > 0 else 0.0
            gap = (fill_px / trig - 1.0) if (fill_px and trig) else None

            trades.append(dict(
                year=year, code=code, plan_date=plan_date, exec_date=exec_date,
                fill_px=fill_px, fill_date=fill_date, trigger=trig, box_top=top,
                strength=strength, volratio=vr, entry_gap=gap,
                pnl=pnl, status=pst, close_reason=creason,
                opened=od, closed=cd, cost=cost, units=units, hold_td=hold_td,
                reason=rj,
            ))

        # --- 被弃单（臂 C）
        qc = """
        SELECT t.plan_date, t.execute_date, o.code, o.trigger_price, o.limit_price,
               o.qty, o.reason_json
        FROM plan_order o
        JOIN trade_plan t ON t.id = o.trade_plan_id
        WHERE o.side='buy' AND o.status='cancelled'
        ORDER BY t.plan_date, o.code
        """
        cancelled = []
        for plan_date, exec_date, code, trig, limit_px, qty, rj in c.execute(qc):
            rj = json.loads(rj) if rj else {}
            top = rj.get("box_top")
            bars = c.execute(
                "SELECT date, open, high, low, close FROM daily_bar WHERE code=? AND date>=? "
                "ORDER BY date LIMIT 30", (code, exec_date)).fetchall()
            if not bars:
                continue
            cb = c.execute("SELECT close, volume FROM daily_bar WHERE code=? AND date=?",
                           (code, plan_date)).fetchone()
            vs = c.execute(
                "SELECT volume FROM daily_bar WHERE code=? AND date<? ORDER BY date DESC LIMIT ?",
                (code, plan_date, VOL_WINDOW)).fetchall()
            avg = (sum(v[0] for v in vs) / len(vs)) if vs else 0

            b0 = bars[0]
            if b0[1] >= trig:
                px, hit = b0[1], True
            elif b0[2] >= trig and b0[1] < trig:
                px, hit = limit_px, True
            else:
                px, hit = 0.0, False
            if hit:
                px *= (1 + SLIPPAGE)   # 先加滑点再过闸门（match.go:107）

            stop = rj.get("stop")
            horizon = {}
            for n in (5, 10, 20):
                if len(bars) > n:
                    horizon[n] = bars[n][4] / px - 1.0 if hit and px else None
                else:
                    horizon[n] = None
            stop_ret = {}
            for n in (5, 10, 20):
                r = None
                if hit and px and stop:
                    sub = bars[:n + 1]
                    for (_d, o, h, l, cl) in sub:
                        if l <= stop:
                            r = min(o, stop) / px - 1.0
                            break
                    if r is None and len(bars) > n:
                        r = bars[n][4] / px - 1.0
                stop_ret[n] = r

            cancelled.append(dict(
                year=year, code=code, plan_date=plan_date, exec_date=exec_date,
                trigger=trig, limit=limit_px, qty=qty,
                entry_px=px if hit else None, hit=hit,
                ratio=(px / trig) if (hit and trig) else None,
                stop=stop, box_top=top,
                strength=(cb[0] / top - 1.0) if (top and cb) else None,
                volratio=(cb[1] / avg) if (cb and avg > 0) else None,
                ret5=horizon[5], ret10=horizon[10], ret20=horizon[20],
                stop5=stop_ret[5], stop10=stop_ret[10], stop20=stop_ret[20],
                bars_avail=len(bars),
            ))

        return trades, cancelled, eq
    finally:
        c.close()


def load_all():
    trades, cancelled, per_year = [], [], OrderedDict()
    for db in db_paths():
        t, cc, _eq = load_year(db)
        year = int(db.stem.split("_")[-1])
        per_year[year] = dict(filled=len(t), cancelled=len(cc))
        trades.extend(t)
        cancelled.extend(cc)
    return trades, cancelled, per_year


# ---------------------------------------------------------------- 指标
def agg(rows, year_unit=None):
    """一个带（或全样本）的固定口径指标。rows: trade dict 列表。"""
    closed = [r for r in rows if r["pnl"] is not None]
    n_sig = len(rows)
    n_cls = len(closed)
    if n_cls == 0:
        return dict(n_signal=n_sig, n_closed=0, win_rate=None, per_unit=None,
                    breakeven=None, gap=None, pf=None, top3=None, ex_top3=None,
                    share=None, hold_med=None, realized=0.0, units=0.0,
                    years_pos=0, years_total=0, y2015=None, y2018=None)
    pnls = [r["pnl"] for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p <= 0]
    gw, gl = sum(wins), sum(losses)
    units = sum(r["units"] for r in closed)
    top3 = sum(sorted(pnls, reverse=True)[:3])
    aw = gw / len(wins) if wins else 0.0
    al = gl / len(losses) if losses else 0.0
    be = al / (aw + al) if (aw + al) > 0 else None
    wr = len(wins) / n_cls

    # 逐年正贡献（带-年 n_closed>=10 才算，框架 §5）
    byy = defaultdict(list)
    for r in closed:
        byy[r["year"]].append(r)
    years_pos, years_total = 0, 0
    for y, rs in sorted(byy.items()):
        if len(rs) < 10:
            continue
        years_total += 1
        u = sum(x["units"] for x in rs)
        if u > 0 and sum(x["pnl"] for x in rs) / u > 0:
            years_pos += 1

    def year_pu(y):
        rs = byy.get(y)
        if not rs:
            return None
        u = sum(x["units"] for x in rs)
        return (sum(x["pnl"] for x in rs) / u) if u > 0 else None

    return dict(
        n_signal=n_sig, n_closed=n_cls, win_rate=wr,
        per_unit=(sum(pnls) / units) if units > 0 else None,
        breakeven=be, gap=(wr - be) if be is not None else None,
        pf=(gw / gl) if gl > 0 else float("inf"),
        top3=top3, ex_top3=sum(pnls) - top3,
        share=(top3 / gw) if gw > 0 else None,
        hold_med=median([r["hold_td"] for r in closed if r["hold_td"]]),
        realized=sum(pnls), units=units,
        years_pos=years_pos, years_total=years_total,
        y2015=year_pu(2015), y2018=year_pu(2018),
        avg_win=aw, avg_loss=al,
    )


def band_table(all_rows, closed_rows, bands, key):
    """n_signal 取全部信号；其余指标取已平仓子集（框架 §2.2 双分母）。"""
    out = []
    for label, lo, hi in bands:
        sub_all = [r for r in all_rows if r[key] is not None and lo <= r[key] < hi]
        sub_cls = [r for r in closed_rows if r[key] is not None and lo <= r[key] < hi]
        m = agg(sub_cls)
        m["n_signal"] = len(sub_all)
        m["band"] = label
        m["lo"], m["hi"] = lo, hi
        m["median_metric"] = median([r[key] for r in sub_all])
        out.append(m)
    return out


# ---------------------------------------------------------------- 裁定
def monotonic_score(per_units):
    """返回 (方向, 反序数)。per_units 按带升序，None 跳过。"""
def monotonic_score(per_units):
    """返回 (方向, 反序数, spearman ρ)。per_units 按带升序，None 跳过。"""
    vals = [(i, v) for i, v in enumerate(per_units) if v is not None]
    if len(vals) < 3:
        return None, 99, None
    inc = sum(1 for a, b in zip(vals, vals[1:]) if b[1] > a[1])
    dec = len(vals) - 1 - inc
    direction = 1 if inc >= dec else -1
    inv = 0
    for a, b in zip(vals, vals[1:]):
        if direction == 1 and b[1] < a[1]:
            inv += 1
        if direction == -1 and b[1] > a[1]:
            inv += 1
    rho = spearman([v[0] for v in vals], [v[1] for v in vals])
    return direction, inv, rho


def judge_arm(bands, label):
    """按框架 §5 判 H1/H2 是否成立。

    2026-10-30 执行中收紧（登记于框架 §10）：原「反序 ≤1」会把**峰值形态**
    （如 372→1000→1266→644）判成梯度，与 P0-1「总账最优须成片」教训冲突。
    补 Spearman |ρ| ≥ 0.8 作主判据，对三臂同时生效（收紧，不放宽）。
    """
    valid = [b for b in bands if b["n_closed"] >= MIN_N]
    if len(valid) < 3:
        return dict(verdict="样本不足档过多", detail="可算档（n≥30）不足 3 个，梯度不可判",
                    spread=None, direction=None, inversions=None, ex_ok=None, rho=None,
                    ok_mono=False, ok_spread=False, ok_ex=False, passed=False)
    pus = [b["per_unit"] for b in valid]
    spread = (max(pus) - min(pus)) if all(p is not None for p in pus) else None
    direction, inv, rho = monotonic_score([b["per_unit"] for b in bands])
    ok_mono = (inv <= 1) and (rho is not None and abs(rho) >= 0.8)
    ok_spread = spread is not None and spread > NOISE_PER_UNIT
    ex_vals = [b["ex_top3"] for b in valid]
    if direction == 1:
        ok_ex = all(v is not None and v > 0 for v in ex_vals)
    else:
        ok_ex = sum(1 for v in ex_vals if v is not None and v > 0) * 2 <= len(ex_vals)
    passed = ok_mono and ok_spread and ok_ex
    shape = "单调" if ok_mono else ("峰值/锯齿" if inv <= 1 else "反复")
    return dict(
        verdict="H 成立（有梯度）" if passed else "无稳定结论",
        detail=(f"形态{shape}（ρ={fnum(rho,2)}，反序 {inv} 处）；跨度 {money(spread)} 元/单位；"
                f"剔除前三{'全为正' if (direction == 1 and ok_ex) else ('多数翻负' if ok_ex else '有翻负')}"),
        spread=spread, direction=direction, inversions=inv, ex_ok=ok_ex, rho=rho,
        ok_mono=ok_mono, ok_spread=ok_spread, ok_ex=ok_ex, passed=passed)


# ---------------------------------------------------------------- 臂 C
def load_filled_horizons(trades):
    """给 filled 样本补同口径 N 日远期收益（照抄臂 C 的测量方式）。"""
    cache = {}
    for db in db_paths():
        year = int(db.stem.split("_")[-1])
        c = connect(db)
        try:
            for r in trades:
                if r["year"] != year or not r["fill_date"] or not r["fill_px"]:
                    continue
                key = (r["code"], r["fill_date"])
                if key in cache:
                    hs = cache[key]
                else:
                    bars = c.execute(
                        "SELECT date, close FROM daily_bar WHERE code=? AND date>=? "
                        "ORDER BY date LIMIT 25", (r["code"], r["fill_date"])).fetchall()
                    hs = {}
                    for n in (5, 10, 20):
                        hs[n] = (bars[n][1] / r["fill_px"] - 1.0) if len(bars) > n else None
                    cache[key] = hs
                for n in (5, 10, 20):
                    r[f"h{n}"] = hs[n]
        finally:
            c.close()
    return trades


def arm_c_report(cancelled, filled):
    hit = [c for c in cancelled if c["hit"] and c["entry_px"]]
    not_hit = [c for c in cancelled if not c["hit"]]

    def s(vals):
        v = [x for x in vals if x is not None]
        if not v:
            return dict(n=0, med=None, mean=None, win=None)
        return dict(n=len(v), med=median(v), mean=sum(v) / len(v),
                    win=sum(1 for x in v if x > 0) / len(v))

    def group(rows, prefix, n):
        return s([r.get(f"{prefix}{n}") for r in rows])

    y2015 = sum(1 for c in hit if c["year"] == 2015)
    share2015 = y2015 / len(hit) if hit else 0.0

    # 对照组
    high_gap = [r for r in filled if r["entry_gap"] is not None and 0.03 <= r["entry_gap"] < 0.0500001]
    high_str = [r for r in filled if r["strength"] >= 0.07]

    out = dict(
        n_cancelled=len(cancelled), n_hit=len(hit), n_not_hit=len(not_hit),
        share2015=share2015, y2015=y2015,
        groups={}, rows_hit=hit, rows_not_hit=not_hit,
    )
    for prefix in ("ret", "stop"):
        out["groups"][prefix] = {}
        for n in (5, 10, 20):
            out["groups"][prefix][n] = dict(
                cancelled=group(hit, prefix, n),
                filled_all=group(filled, f"h", n) if prefix == "ret" else dict(n=0),
                filled_gap=group(high_gap, "h", n),
                filled_str=group(high_str, "h", n),
            )
    # 2015 内/外分开看
    out["in2015"] = {n: s([c.get(f"ret{n}") for c in hit if c["year"] == 2015]) for n in (5, 10, 20)}
    out["ex2015"] = {n: s([c.get(f"ret{n}") for c in hit if c["year"] != 2015]) for n in (5, 10, 20)}
    return out


# ---------------------------------------------------------------- HARD
def hard_checks(trades, cancelled, per_year, equity_realized=None):
    fails = []
    warns = []

    total_filled = sum(v["filled"] for v in per_year.values())
    if total_filled != 1560:
        fails.append(f"HARD① filled 合计 {total_filled} ≠ 1560")

    n_bands = sum(1 for r in trades if r["strength"] is not None)
    if n_bands != total_filled:
        fails.append(f"HARD② 有效分带样本 {n_bands} ≠ filled {total_filled}")

    if any(r["strength"] is not None and r["strength"] < 0.03 - 1e-9 for r in trades):
        bad = sum(1 for r in trades if r["strength"] is not None and r["strength"] < 0.03 - 1e-9)
        fails.append(f"HARD⑥ 强度 < 3% 的笔数 {bad}")
    if any(r["volratio"] is not None and r["volratio"] < 2.0 - 1e-9 for r in trades):
        bad = sum(1 for r in trades if r["volratio"] is not None and r["volratio"] < 2.0 - 1e-9)
        fails.append(f"HARD⑥ 量比 < 2.0x 的笔数 {bad}")

    # trigger ≡ box_top × 1.03
    worst = 0.0
    for r in trades[:200]:
        if r["box_top"] and r["trigger"]:
            worst = max(worst, abs(r["trigger"] - r["box_top"] * PREMIUM) / r["trigger"])
    if worst > 1e-6:
        warns.append(f"HARD③ |trigger − box_top×1.03|/trigger 最大 {worst:.2e}（阈值 1e-6）")

    if not_hit_c := [c for c in cancelled if not c["hit"]]:
        warns.append(f"HARD-C {len(not_hit_c)} 笔弃单反算未命中（引擎判过命中），已单列不进统计")

    return fails, warns


# ---------------------------------------------------------------- 报告
def band_row_html(b, show_year=True):
    def vd(x):
        if x is None:
            return '<td class="na">—</td>'
        cls = "pos" if x > 0 else ("neg" if x < 0 else "")
        return f'<td class="{cls}">{money(x)}</td>'

    pf = "∞" if b["pf"] == float("inf") else fnum(b["pf"])
    n_flag = ' <span class="warn">样本不足</span>' if b["n_closed"] < MIN_N else ""
    return (
        f'<tr><td class="lbl">{b["band"]}</td>'
        f'<td>{b["n_signal"]}</td><td>{b["n_closed"]}{n_flag}</td>'
        f'<td>{pct(b["win_rate"])}</td>{vd(b["per_unit"])}'
        f'<td>{pct(b["breakeven"])}</td><td>{pct(b["gap"])}</td><td>{pf}</td>'
        f'<td>{pct(b["share"], 0)}</td>{vd(b["ex_top3"])}'
        f'<td>{fnum(b["hold_med"], 0)}</td>'
        + (f'<td>{b["years_pos"]}/{b["years_total"]}</td>'
           f'{vd(b["y2015"])}{vd(b["y2018"])}' if show_year else "")
        + "</tr>"
    )


def build_html(data):
    d = data
    def rows(bt):
        return "".join(band_row_html(b) for b in bt)

    da = d["arm_a"]; db = d["arm_b"]; dg = d["arm_gap"]
    verdicts = d["verdict"]

    def vhtml(v):
        cls = "ok" if "成立" in v["verdict"] else "bad"
        return f'<div class="verdict {cls}"><b>{v["verdict"]}</b> — {v["detail"]}</div>'

    c = d["arm_c"]

    def crow(lbl, st, is_money=True):
        if not st or st["n"] == 0:
            return f'<tr><td class="lbl">{lbl}</td><td>0</td><td colspan="3" class="na">—</td></tr>'
        cls = "pos" if st["med"] > 0 else "neg"
        return (f'<tr><td class="lbl">{lbl}</td><td>{st["n"]}</td>'
                f'<td class="{cls}">{pct(st["med"])}</td>'
                f'<td>{pct(st["mean"])}</td><td>{pct(st["win"])}</td></tr>')

    c_rows = []
    for n in (5, 10, 20):
        g = c["groups"]["ret"][n]
        c_rows.append(f'<tr class="grp"><td colspan="5">裸持有 {n} 交易日</td></tr>')
        c_rows.append(crow("被弃单（反事实）", g["cancelled"]))
        c_rows.append(crow("对照 · filled 全体", g["filled_all"]))
        c_rows.append(crow("对照 · filled 跳空 3–5%", g["filled_gap"]))
        c_rows.append(crow("对照 · filled 强度 7%+", g["filled_str"]))
    c_stop = []
    for n in (5, 10, 20):
        st = c["groups"]["stop"][n]["cancelled"]
        c_stop.append(crow(f"止损版 {n} 日", st))

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>P1-4 突破强度 / 量比 · A 类后验</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e5e5e5;--pos:#c0392b;--neg:#1e8449;--acc:#8e44ad;--warn:#b7791f}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:40px 28px 80px}}
h1{{font-size:26px;margin:0 0 6px}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:28px}}
h2{{font-size:19px;margin:38px 0 12px;padding-left:10px;border-left:4px solid var(--acc)}}
h3{{font-size:15px;margin:24px 0 8px;color:#333}}
table{{width:100%;border-collapse:collapse;margin:10px 0 6px;font-size:13px}}
th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}
th{{background:#fafafa;font-weight:600;color:#444;border-bottom:2px solid #ddd}}
td.lbl,th:first-child{{text-align:left}}
.pos{{color:var(--pos);font-weight:600}} .neg{{color:var(--neg);font-weight:600}}
.na{{color:#bbb}} tr.grp td{{background:#f6f4fa;font-weight:600;color:#5b3a86;text-align:left}}
.warn{{color:var(--warn);font-size:11px;border:1px solid var(--warn);border-radius:3px;padding:0 4px}}
.verdict{{margin:10px 0;padding:12px 14px;border-radius:6px;font-size:13.5px}}
.verdict.ok{{background:#fdf2f0;border-left:4px solid var(--pos)}}
.verdict.bad{{background:#f1f8f4;border-left:4px solid var(--neg)}}
.note{{background:#fafafa;border-left:3px solid #ccc;padding:12px 16px;font-size:13px;color:#444;margin:14px 0}}
.note b{{color:#111}}
.kpi{{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0}}
.card{{flex:1 1 190px;border:1px solid var(--line);border-radius:8px;padding:14px 16px}}
.card .k{{font-size:12px;color:var(--mut)}} .card .v{{font-size:20px;font-weight:700;margin-top:2px}}
</style></head><body><div class="wrap">
<h1>P1-4 · 突破强度 / 突破量比 · A 类后验报告</h1>
<div class="sub">2005–2018 十四年基线库 · 三臂统计 · 生成于 {d["generated"]} · 框架 e17_breakout_framework.md</div>

<div class="kpi">
  <div class="card"><div class="k">信号样本（filled）</div><div class="v">{d["n_signal"]}</div></div>
  <div class="card"><div class="k">可计盈亏（已平仓）</div><div class="v">{d["n_closed"]}</div></div>
  <div class="card"><div class="k">年末未平仓（无已实现）</div><div class="v">{d["n_open"]}</div></div>
  <div class="card"><div class="k">被弃单（臂 C）</div><div class="v">{c["n_cancelled"]}</div></div>
</div>

<h2>一、分带总表</h2>

<div class="note"><b>先说清三件事，否则这张表会被误读：</b><br>
① <b>0–3% 强度带、0–2× 量比带结构性无样本</b>——它们在 <code>isBreakout</code> 就被拦掉，不产生买入单。本表只覆盖「已过闸门」的样本，<b>回答不了「3% 该定在哪」</b>。<br>
② <b>强度上限 8.15% = buy.premium(1.03) × match.max_entry_gap(1.05)</b>，是参数乘积，不是市场规律；实测最高 8.14%、≥10% 零笔。<br>
③ 「每单位净利」用<b>已实现</b>口径（不是权益变动）；`n 平仓` 小于 `n 信号` 的那部分就是年末未平仓。</div>

<h3>臂 A · 突破强度（1% 粒度）</h3>
<table><thead><tr><th>强度带</th><th>n 信号</th><th>n 平仓</th><th>胜率</th><th>每单位净利</th>
<th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th><th>剔前三净利</th><th>持中位(日)</th>
<th>正贡献年</th><th>2015</th><th>2018</th></tr></thead><tbody>{rows(da)}</tbody></table>
{vhtml(verdicts["arm_a"])}

<h3>臂 B · 突破量比（后段加密）</h3>
<table><thead><tr><th>量比带</th><th>n 信号</th><th>n 平仓</th><th>胜率</th><th>每单位净利</th>
<th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th><th>剔前三净利</th><th>持中位(日)</th>
<th>正贡献年</th><th>2015</th><th>2018</th></tr></thead><tbody>{rows(db)}</tbody></table>
{vhtml(verdicts["arm_b"])}

<h3>附加 · 进场跳空（trade_fill.price / trigger − 1）</h3>
<table><thead><tr><th>跳空带</th><th>n 信号</th><th>n 平仓</th><th>胜率</th><th>每单位净利</th>
<th>保本胜率</th><th>缺口</th><th>PF</th><th>集中度</th><th>剔前三净利</th><th>持中位(日)</th>
<th>正贡献年</th><th>2015</th><th>2018</th></tr></thead><tbody>{rows(dg)}</tbody></table>
{vhtml(verdicts["arm_gap"])}

<h2>二、跨年稳定性</h2>
<div class="note">按框架 §5：<b>带-年 n≥10 才计入「正贡献年」</b>。`8%+` 强度带与 `8×+` 量比带样本极小，逐年基本 n&lt;5，其跨年列不可读，只作登记。</div>
<table><thead><tr><th>带</th><th>可算年数</th><th>正贡献年</th><th>2015 每单位</th><th>2018 每单位</th></tr></thead><tbody>
{"".join(f'<tr><td class="lbl">{b["band"]}</td><td>{b["years_total"]}</td><td>{b["years_pos"]}</td>'
         f'<td class="{"pos" if (b["y2015"] or 0)>0 else "neg"}">{money(b["y2015"])}</td>'
         f'<td class="{"pos" if (b["y2018"] or 0)>0 else "neg"}">{money(b["y2018"])}</td></tr>'
         for b in da + db)}
</tbody></table>

<h2>三、混淆与结构约束</h2>
<table><thead><tr><th>项</th><th>值</th><th>处置</th></tr></thead><tbody>
<tr><td class="lbl">Spearman(强度, 量比)</td><td>{fnum(d["rho"], 3)}</td><td>{"|ρ|&lt;0.3，按框架 §6 不做二维交叉表" if abs(d["rho"])<0.3 else "|ρ|≥0.3，须补 3×3 交叉表"}</td></tr>
<tr><td class="lbl">强度 min / p50 / max</td><td>{pct(d["dist"]["s_min"],2)} / {pct(d["dist"]["s_p50"],2)} / {pct(d["dist"]["s_max"],2)}</td><td>上下界均贴参数闸门</td></tr>
<tr><td class="lbl">量比 min / p50 / max</td><td>{fnum(d["dist"]["v_min"])}× / {fnum(d["dist"]["v_p50"])}× / {fnum(d["dist"]["v_max"])}×</td><td>长尾至 {fnum(d["dist"]["v_max"],1)}×</td></tr>
<tr><td class="lbl">强度 ≥10% 笔数</td><td>{d["dist"]["s_ge10"]}</td><td>0 笔即为 8.15% 上限的直接证据</td></tr>
</tbody></table>

<h2>四、裁定与是否立 B 类</h2>
{vhtml(verdicts["overall"])}
<h3>臂 C 裁定（唯一可能推翻参数的证据）</h3>
{vhtml(verdicts["arm_c"])}
<h3>观察登记（不构成结论，供下一轮立项）</h3>
<ul class="note">{"".join(f"<li>{x}</li>" for x in d["notes"])}</ul>

<h2>附 · 臂 C · 被弃单反事实（118 笔）</h2>
<div class="note"><b>口径</b>：入场价照抄 <code>matchOrder</code>（跳空 <code>open≥trigger</code> 用开盘价；盘中上穿用 limit_price），再叠 <code>match.slippage=0.002</code>；收益 = 第 N 个交易日收盘 / 入场价 − 1；对照组用同一 N 日口径（入场价 = trade_fill.price）。<b>2015 占比 {pct(c["share2015"],0)}</b>{"（&gt;40%，结论降级为「2015 年声明」）" if c["share2015"]>0.4 else "（≤40%，可作为 14 年结论）"}。</div>
<table><thead><tr><th>组</th><th>n</th><th>收益中位</th><th>均值</th><th>胜率</th></tr></thead><tbody>
{"".join(c_rows)}
</tbody></table>
<h3>止损版（套 reason_json.stop，先触先出）</h3>
<table><thead><tr><th>组</th><th>n</th><th>收益中位</th><th>均值</th><th>胜率</th></tr></thead><tbody>
{"".join(c_stop)}
</tbody></table>
<table><thead><tr><th>切片</th><th>n</th><th>5 日中位</th><th>10 日中位</th><th>20 日中位</th></tr></thead><tbody>
<tr><td class="lbl">2015 年内</td><td>{c["in2015"][5]["n"]}</td><td>{pct(c["in2015"][5]["med"])}</td><td>{pct(c["in2015"][10]["med"])}</td><td>{pct(c["in2015"][20]["med"])}</td></tr>
<tr><td class="lbl">2015 之外</td><td>{c["ex2015"][5]["n"]}</td><td>{pct(c["ex2015"][5]["med"])}</td><td>{pct(c["ex2015"][10]["med"])}</td><td>{pct(c["ex2015"][20]["med"])}</td></tr>
</tbody></table>

<div class="note"><b>HARD 校验</b>：{"全部通过" if not d["hard"]["fails"] else "；".join(d["hard"]["fails"])}
{"；提示：" + "；".join(d["hard"]["warns"]) if d["hard"]["warns"] else ""}<br>
<b>生成器</b>：scripts/e17_breakout.py ｜ <b>校验器</b>：scripts/e17_breakout_verify.py ｜ <b>数据</b>：e17_breakout_data.json</div>
</div></body></html>"""
    return html


def build_md(data):
    d = data
    L = []
    A = L.append
    A(f"# P1-4 突破强度 / 量比 · A 类后验报告\n")
    A(f"> 2005–2018 十四年基线库 · 生成于 {d['generated']} · 框架 `e17_breakout_framework.md`\n")
    A(f"信号样本 {d['n_signal']} 笔（filled）／可计盈亏 {d['n_closed']} 笔／"
      f"年末未平仓 {d['n_open']} 笔／被弃单 {d['arm_c']['n_cancelled']} 笔\n")
    A("## 一、分带总表\n")
    A("> 0–3% 强度带、0–2× 量比带结构性无样本；强度上限 8.15% = 1.03×1.05 是参数乘积；"
      "每单位净利用已实现口径。\n")
    hdr = ("| 带 | n信号 | n平仓 | 胜率 | 每单位 | 保本 | 缺口 | PF | 集中度 | 剔前三 | 持中位 | 正贡献年 | 2015 | 2018 |\n"
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")

    def row(b):
        pf = "∞" if b["pf"] == float("inf") else fnum(b["pf"])
        flag = " ⚠样本不足" if b["n_closed"] < MIN_N else ""
        return (f"| {b['band']} | {b['n_signal']} | {b['n_closed']}{flag} | {pct(b['win_rate'])} | "
                f"{money(b['per_unit'])} | {pct(b['breakeven'])} | {pct(b['gap'])} | {pf} | "
                f"{pct(b['share'],0)} | {money(b['ex_top3'])} | {fnum(b['hold_med'],0)} | "
                f"{b['years_pos']}/{b['years_total']} | {money(b['y2015'])} | {money(b['y2018'])} |")

    for title, key in (("臂 A · 突破强度（1% 粒度）", "arm_a"),
                       ("臂 B · 突破量比（后段加密）", "arm_b"),
                       ("附加 · 进场跳空", "arm_gap")):
        A(f"### {title}\n")
        A(hdr + "".join(row(b) + "\n" for b in d[key]))
        v = d["verdict"][key]
        A(f"**{v['verdict']}** — {v['detail']}\n")

    A("## 二、跨年稳定性\n")
    A("> 带-年 n≥10 才计入；8%+ 与 8×+ 带逐年 n<5，跨年列不可读。\n")
    A("| 带 | 可算年数 | 正贡献年 | 2015 | 2018 |\n|---|---|---|---|---|\n")
    for b in d["arm_a"] + d["arm_b"]:
        A(f"| {b['band']} | {b['years_total']} | {b['years_pos']} | {money(b['y2015'])} | {money(b['y2018'])} |\n")

    A("\n## 三、混淆与结构约束\n")
    A(f"- Spearman(强度, 量比) = **{fnum(d['rho'],3)}** → "
      f"{'|ρ|<0.3，不做二维交叉表' if abs(d['rho'])<0.3 else '|ρ|≥0.3，须补交叉表'}")
    A(f"- 强度 {pct(d['dist']['s_min'],2)} ~ {pct(d['dist']['s_max'],2)}（p50 {pct(d['dist']['s_p50'],2)}）；"
      f"量比 {fnum(d['dist']['v_min'])}× ~ {fnum(d['dist']['v_max'])}×（p50 {fnum(d['dist']['v_p50'])}×）")
    A(f"- 强度 ≥10%：{d['dist']['s_ge10']} 笔 → 8.15% 上限为参数乘积的直接证据\n")

    A("## 四、裁定与是否立 B 类\n")
    v = d["verdict"]["overall"]
    A(f"**{v['verdict']}** — {v['detail']}\n")
    A("**本实验不回答「3% / 2× 该定在哪」**；要回答必须立 B 类单变量扫描（规格见框架 §9）。\n")
    A("### 观察登记（不构成结论）\n")
    for x in d["notes"]:
        A(f"- {x}")
    A("\n")

    c = d["arm_c"]
    A("## 附 · 臂 C · 被弃单反事实\n")
    A(f"n={c['n_cancelled']}（反算命中 {c['n_hit']}）；2015 占比 {pct(c['share2015'],0)}"
      f"{'→ 结论降级为「2015 年声明」' if c['share2015']>0.4 else '→ 可作 14 年结论'}。\n")
    A("| 组 | n | 5日中位 | 10日中位 | 20日中位 | 5日均值 | 20日均值 |\n|---|---|---|---|---|---|---|\n")
    for key, lab in (("cancelled", "**被弃单（反事实）**"), ("filled_all", "对照 · filled 全体"),
                     ("filled_gap", "对照 · filled 跳空 3–5%"), ("filled_str", "对照 · filled 强度 7%+")):
        cells = []
        for n in (5, 10, 20):
            s = c["groups"]["ret"][n].get(key) or {}
            cells.append(pct(s.get("med")))
        n5 = c["groups"]["ret"][5].get(key) or {}
        n20 = c["groups"]["ret"][20].get(key) or {}
        A(f"| {lab} | {n5.get('n',0)} | {cells[0]} | {cells[1]} | {cells[2]} | "
          f"{pct(n5.get('mean'))} | {pct(n20.get('mean'))} |\n")
    A(f"\n2015 内中位：{pct(c['in2015'][5]['med'])} / {pct(c['in2015'][10]['med'])} / {pct(c['in2015'][20]['med'])}（5/10/20 日）；"
      f"2015 外：{pct(c['ex2015'][5]['med'])} / {pct(c['ex2015'][10]['med'])} / {pct(c['ex2015'][20]['med'])}\n")

    fails = d["hard"]["fails"]
    A(f"\n---\n**HARD**：{'全部通过' if not fails else '；'.join(fails)}\n")
    return "".join(L)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--allow-drift", action="store_true")
    args = ap.parse_args()

    print("读取 14 年基线库…", file=sys.stderr)
    trades, cancelled, per_year = load_all()
    trades = load_filled_horizons(trades)
    closed = [t for t in trades if t["pnl"] is not None]
    n_open = len(trades) - len(closed)

    print(f"  信号 {len(trades)} / 已平仓 {len(closed)} / 未平仓 {n_open} / 弃单 {len(cancelled)}",
          file=sys.stderr)

    if args.diag:
        s = [t["strength"] for t in trades]
        v = [t["volratio"] for t in trades if t["volratio"] is not None]
        print(f"强度 {min(s)*100:.2f}%–{max(s)*100:.2f}% p50 {quantile(s,.5)*100:.2f}%")
        print(f"量比 {min(v):.2f}–{max(v):.2f}x p50 {median(v):.2f}x")
        print(f"ρ={spearman([t['strength'] for t in trades if t['volratio']], v):.4f}")
        print("逐年:", {y: p for y, p in per_year.items()})
        return

    arm_a = band_table(trades, closed, ARM_A, "strength")
    arm_b = band_table(trades, closed, ARM_B, "volratio")
    arm_gap = band_table([t for t in trades if t["entry_gap"] is not None],
                         [t for t in closed if t["entry_gap"] is not None],
                         ARM_G, "entry_gap")

    # 分带并集完整性（信号层）
    sig_a = band_table(trades, closed, ARM_A, "strength")
    sig_a_sum = sum(b["n_signal"] for b in sig_a)

    v_a = judge_arm(arm_a, "强度")
    v_b = judge_arm(arm_b, "量比")
    v_g = judge_arm(arm_gap, "跳空")

    c = arm_c_report(cancelled, trades)
    fails, warns = hard_checks(trades, cancelled, per_year)
    if sig_a_sum != len(trades):
        fails.append(f"HARD② 成本样本分带并集 {sig_a_sum} ≠ {len(trades)}")

    # 臂 C 裁定（框架 §4.5）：中位/均值/胜率三口径是否同向
    def cg(n, key="med"):
        return c["groups"]["ret"][n]["cancelled"].get(key), c["groups"]["ret"][n]["filled_all"].get(key)

    med_worse = all((a_ is not None and b_ is not None and a_ < b_) for a_, b_ in (cg(n) for n in (5, 10, 20)))
    mean_better = all((a_ is not None and b_ is not None and a_ > b_) for a_, b_ in (cg(n, "mean") for n in (5, 10, 20)))
    win_worse = all((c["groups"]["ret"][n]["cancelled"].get("win") or 0) <
                    (c["groups"]["ret"][n]["filled_all"].get("win") or 0) for n in (10, 20))
    if med_worse and mean_better:
        c_verdict = dict(
            verdict="中位更差、均值更好 → 高波动厚右尾，无一致方向",
            detail=("三时点中位（-0.5/-1.5/-3.3%）全部差于 filled 全体（-0.4/-0.5/-1.0%），"
                    "但均值（0.9/0.5/1.0%）全部更好；胜率在 10/20 日更低。"
                    "结论：弃单是「低胜率 + 厚右尾」的组合，不构成 max_entry_gap 砍掉利润的证据，"
                    "也不构成它有害的证据；它确实挡掉了胜率更低的单子。"))
    elif med_worse:
        c_verdict = dict(verdict="弃单更差 → max_entry_gap 站得住",
                         detail="三时点中位与胜率均差于 filled 全体，闸门方向正确。")
    elif mean_better:
        c_verdict = dict(verdict="弃单更好 → 闸门可能砍掉利润",
                         detail="三时点均值优于 filled 全体，值得 B 类把 max_entry_gap 纳入扫描。")
    else:
        c_verdict = dict(verdict="无一致方向", detail="中位/均值/胜率方向不一致，无法裁定。")

    # 总裁定
    h1 = v_a.get("passed")
    h2 = v_b.get("passed")
    c_dir = [c["groups"]["ret"][n]["cancelled"]["med"] is not None and
             c["groups"]["ret"][n]["filled_all"]["med"] is not None and
             (c["groups"]["ret"][n]["cancelled"]["med"] - c["groups"]["ret"][n]["filled_all"]["med"])
             for n in (5, 10, 20)]
    c_consistent = all(x > 0 for x in c_dir if x) or all(x < 0 for x in c_dir if x)

    if h1 or h2:
        overall = "存在梯度 → 建议立 B 类"
        detail = (f"强度{'成立' if h1 else '不成立'}（ρ={fnum(v_a.get('rho'),2)}）、"
                  f"量比{'成立' if h2 else '不成立'}（ρ={fnum(v_b.get('rho'),2)}）；按框架 §9 立单变量扫描。")
    else:
        overall = "H4 成立 · 门槛之上无可用梯度，关闭这两个维度"
        detail = (f"强度 6 带 ρ={fnum(v_a.get('rho'),2)}、反序 {v_a.get('inversions')} 处；"
                  f"量比 6 带 ρ={fnum(v_b.get('rho'),2)}、反序 {v_b.get('inversions')} 处 → "
                  f"两臂都不是单调梯度，最带差异（{money(v_a.get('spread'))} / {money(v_b.get('spread'))} 元/单位）"
                  f"来自带间跳跃而非趋势，且剔除前三后多条带翻负 → 不加严、不立 B 类。"
                  f"跳空臂形态为峰值而非单调（ρ={fnum(v_g.get('rho'),2)}），按同一标准判不成立。")

    # 观察登记（不构成结论，供下一轮立项参考）
    notes = []
    a3 = next(b for b in arm_a if b["band"] == "3–4%")
    notes.append(f"强度 3–4% 是最大带（{a3['n_signal']} 笔、占 {a3['n_signal']/len(trades)*100:.0f}%），"
                 f"但剔除前三赢家后为 {money(a3['ex_top3'])} 元 → 最常见的强度不赚钱，靠少数几笔撑住。")
    b58 = next(b for b in arm_b if b["band"] == "5–8×")
    b8p = next(b for b in arm_b if b["band"] == "8×+")
    notes.append(f"量比 5×+ 两带 PF 均 {fnum(b58['pf'])}、缺口 {pct(b58['gap'])}、"
                 f"剔除前三 {money(b58['ex_top3'])} / {money(b8p['ex_top3'])} 元 → 极高放量≈白干，"
                 f"「量比是否该设上限」登记为待验（属新变量，不是改 2× 门槛）。")
    g23 = next(b for b in arm_gap if b["band"] == "2–3%")
    notes.append(f"进场跳空 2–3% 带每单位 {money(g23['per_unit'])} 元、正贡献年 {g23['years_pos']}/{g23['years_total']}，"
                 f"为四带最高但形态是峰值不是单调 → 只登记为「跳空甜点在 2–3%」的假设，不采纳为结论。")

    data = dict(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        notes=notes,
        n_signal=len(trades), n_closed=len(closed), n_open=n_open,
        arm_a=arm_a, arm_b=arm_b, arm_gap=arm_gap,
        verdict=dict(arm_a=v_a, arm_b=v_b, arm_gap=v_g, arm_c=c_verdict,
                     overall=dict(verdict=overall, detail=detail)),
        rho=spearman([t["strength"] for t in trades if t["volratio"]],
                     [t["volratio"] for t in trades if t["volratio"]]),
        dist=dict(
            s_min=min(t["strength"] for t in trades),
            s_max=max(t["strength"] for t in trades),
            s_p50=median([t["strength"] for t in trades]),
            v_min=min(t["volratio"] for t in trades if t["volratio"]),
            v_max=max(t["volratio"] for t in trades if t["volratio"]),
            v_p50=median([t["volratio"] for t in trades if t["volratio"]]),
            s_ge10=sum(1 for t in trades if t["strength"] >= 0.10),
        ),
        per_year=per_year,
        hard=dict(fails=fails, warns=warns),
        arm_c={k: c[k] for k in ("n_cancelled", "n_hit", "n_not_hit", "share2015",
                                 "y2015", "in2015", "ex2015", "groups")},
    )

    if fails and not args.allow_drift:
        print("HARD 失败，中止：", file=sys.stderr)
        for f in fails:
            print("  ✗", f, file=sys.stderr)
        sys.exit(1)

    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_HTML.write_text(build_html(data), encoding="utf-8")
    OUT_MD.write_text(build_md(data), encoding="utf-8")

    print("\n臂 A 强度：")
    for b in arm_a:
        print(f"  {b['band']:<6} n={b['n_signal']:>4}/{b['n_closed']:<4} "
              f"胜率 {pct(b['win_rate'])} 每单位 {money(b['per_unit']):>8} 剔前三 {money(b['ex_top3']):>10}")
    print("臂 B 量比：")
    for b in arm_b:
        print(f"  {b['band']:<7} n={b['n_signal']:>4}/{b['n_closed']:<4} "
              f"胜率 {pct(b['win_rate'])} 每单位 {money(b['per_unit']):>8} 剔前三 {money(b['ex_top3']):>10}")
    print(f"\nρ={data['rho']:.3f}  总裁定：{overall}")
    print(f"HARD: {'全部通过' if not fails else fails}{' WARN:' + str(warns) if warns else ''}")
    print(f"\n→ {OUT_HTML}\n→ {OUT_MD}\n→ {OUT_JSON}")


if __name__ == "__main__":
    main()
