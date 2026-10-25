#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E17 回测监控：每年完成时生成/更新报告，重点突出 20-40% 分档风险收益。

用法：
  python scripts/e17_watch.py

输入：
  var/bt/e17_metrics.csv       已完成年份（由 run_e17*.bat 在每年结束后写入）
  var/bt/bt_p05_<year>.db      基线库（可选，用于对照）
  var/bt/bt_e17_<year>.db      E17 库
输出：
  var/bt/e17_watch_report.md   汇总报告（覆盖写入，含所有已完成年份）
状态：
  无（每次全量重建报告，幂等）
"""
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
METRICS = BT / "e17_metrics.csv"
REPORT = BT / "e17_watch_report.md"
STATUS_2008 = BT / "e17_2008_status.json"


def metrics(con):
    eq = con.execute(
        "SELECT date, total, market_value, drawdown FROM equity_snapshot ORDER BY date"
    ).fetchall()
    if not eq:
        return None
    days = len(eq)
    eq_start, eq_end = eq[0]["total"], eq[-1]["total"]
    ret = (eq_end / eq_start - 1) if eq_start else 0.0
    maxdd = min((r["drawdown"] for r in eq), default=0.0)
    max_expo = max(
        (r["market_value"] / r["total"] for r in eq if r["total"] > 0), default=0.0
    )
    rets = [
        eq[i]["total"] / eq[i - 1]["total"] - 1
        for i in range(1, len(eq))
        if eq[i - 1]["total"] > 0
    ]
    sharpe = 0.0
    if len(rets) > 1:
        n = len(rets)
        mean = sum(rets) / n
        var = sum((x - mean) ** 2 for x in rets) / (n - 1)
        std = var ** 0.5
        if std > 0:
            sharpe = mean / std * (252 ** 0.5)
    row = con.execute(
        """SELECT COUNT(*) n,
                  SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                  SUM(CASE WHEN realized_pnl>0 THEN realized_pnl ELSE 0 END) gw,
                  SUM(CASE WHEN realized_pnl<0 THEN realized_pnl ELSE 0 END) gl,
                  COALESCE(SUM(realized_pnl),0) pnl
           FROM position WHERE status='closed'"""
    ).fetchone()
    n = row["n"] or 0
    gw, gl = row["gw"] or 0.0, row["gl"] or 0.0
    pf = (gw / abs(gl)) if gl != 0 else (float("inf") if gw > 0 else 0.0)
    open_n = con.execute("SELECT COUNT(*) FROM position WHERE status!='closed'").fetchone()[0]
    return {
        "days": days, "eq_start": eq_start, "eq_end": eq_end, "ret": ret,
        "maxdd": maxdd, "max_expo": max_expo, "sharpe": sharpe,
        "n": n, "wins": row["wins"] or 0, "wr": (row["wins"] or 0) / n if n else 0.0,
        "pf": pf, "pnl": row["pnl"], "open_n": open_n,
    }


def tiers(con):
    rows = con.execute(
        """SELECT p.realized_pnl, p.status, o.reason_json, o.qty
           FROM position_leg l
           JOIN position p ON p.id = l.position_id
           LEFT JOIN plan_order o ON o.id = l.plan_order_id
           WHERE l.leg_no = 1"""
    ).fetchall()
    import json
    out = {}
    for r in rows:
        try:
            rj = json.loads(r["reason_json"] or "{}")
        except ValueError:
            rj = {}
        basis = rj.get("sizing_basis") or "unknown"
        d = out.setdefault(basis, {"n": 0, "closed": 0, "wins": 0, "pnl": 0.0, "units": 0.0, "dist": []})
        d["n"] += 1
        units = rj.get("units")
        if units is None:
            units = rj.get("unit_value")
        try:
            units = float(units)
        except (TypeError, ValueError):
            units = 1.0
        d["units"] += units
        dist = rj.get("newhigh_distance_pct")
        if dist is not None:
            d["dist"].append(float(dist))
        if r["status"] == "closed" and r["realized_pnl"] is not None:
            d["closed"] += 1
            d["pnl"] += r["realized_pnl"]
            if r["realized_pnl"] > 0:
                d["wins"] += 1
    return out


def fmt_pf(x):
    return "∞" if x == float("inf") else f"{x:.2f}"


def build_report():
    lines = [
        "# E17 新高距离仓位阶梯 · 回测监控报告",
        "",
        "> 重点：**20-40% 分档（newhigh_t4，深度回调区）风险收益**。",
        "> 档位定义：t1=0-5%(3单位)、t2=5-10%(2单位)、t3=10-20%(1单位)、t4=20-40%(0.5单位)、>40% 拒买。",
        "> 2008 年为样本外验证，排在 2015-2018 主队列正常完成后串行执行。",
        "",
        "## 队列状态",
        "",
    ]
    if STATUS_2008.exists():
        try:
            status = json.loads(STATUS_2008.read_text(encoding="utf-8"))
            lines.append(f"- 2008 样本外：**{status.get('state', 'unknown')}**；{status.get('detail', '')}（更新：{status.get('updated_at', '—')}）")
        except (ValueError, OSError):
            lines.append("- 2008 样本外：状态文件暂不可读，下次刷新重试。")
    else:
        lines.append("- 2008 样本外：排队脚本尚未启动。")
    lines.append("")
    if not METRICS.exists():
        lines.append("尚无已完成年份。")
        return "\n".join(lines)
    years = [r[0] for r in csv.reader(METRICS.open()) if r and r[0].strip().isdigit()]
    for year in years:
        db = BT / f"bt_e17_{year}.db"
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        em = metrics(con)
        et = tiers(con)
        con.close()
        base_db = BT / f"bt_p05_{year}.db"
        bm = None
        if base_db.exists():
            bc = sqlite3.connect(f"file:{base_db}?mode=ro", uri=True)
            bc.row_factory = sqlite3.Row
            bm = metrics(bc)
            bc.close()
        lines.append(f"## {year} 年")
        lines.append("")
        if bm:
            lines.append("| 指标 | 基线 A M1 | E17 阶梯 | Δ |")
            lines.append("|---|---|---|---|")
            lines.append(f"| 净利 | {bm['pnl']:,.0f} | {em['pnl']:,.0f} | {em['pnl']-bm['pnl']:+,.0f} |")
            lines.append(f"| 收益率 | {bm['ret']*100:.2f}% | {em['ret']*100:.2f}% | {(em['ret']-bm['ret'])*100:+.2f}pp |")
            lines.append(f"| 最大回撤 | {bm['maxdd']*100:.2f}% | {em['maxdd']*100:.2f}% | {(em['maxdd']-bm['maxdd'])*100:+.2f}pp |")
            lines.append(f"| 胜率 | {bm['wr']*100:.1f}% | {em['wr']*100:.1f}% | {(em['wr']-bm['wr'])*100:+.1f}pp |")
            lines.append(f"| 盈亏比 | {fmt_pf(bm['pf'])} | {fmt_pf(em['pf'])} | — |")
        else:
            lines.append("| 指标 | E17 数值 |")
            lines.append("|---|---|")
            lines.append(f"| 净利 | {em['pnl']:,.0f} |")
            lines.append(f"| 收益率 | {em['ret']*100:.2f}% |")
            lines.append(f"| 最大回撤 | {em['maxdd']*100:.2f}% |")
            lines.append(f"| 胜率 | {em['wr']*100:.1f}% |")
            lines.append(f"| 盈亏比 | {fmt_pf(em['pf'])} |")
        lines.append("")
        lines.append("### 20-40% 分档（深度回调区）风险收益")
        lines.append("")
        t4 = et.get("newhigh_t4")
        if t4 and t4["n"]:
            wr = t4["wins"] / t4["closed"] * 100 if t4["closed"] else 0
            per_u = t4["pnl"] / t4["units"] if t4["units"] else 0
            avg_d = sum(t4["dist"]) / len(t4["dist"]) if t4["dist"] else 0
            lines.append(f"- 建仓 **{t4['n']}** 笔，平仓 **{t4['closed']}** 笔，胜率 **{wr:.1f}%**")
            lines.append(f"- 净利 **{t4['pnl']:,.0f}**，每单位净利 **{per_u:,.0f}**，平均距新高 **{avg_d:.1f}%**")
        else:
            lines.append("- 本年无 20-40% 分档成交（或全部未平仓）。")
        lines.append("")
        lines.append("### 全部档位对照")
        lines.append("")
        lines.append("| 档位 | 建仓 | 平仓 | 胜率 | 净利 | 每单位净利 | 平均距新高 |")
        lines.append("|---|---|---|---|---|---|---|")
        order = ["newhigh_t1", "newhigh_t2", "newhigh_t3", "newhigh_t4", "A_regime_up", "default", "newhigh_unavailable"]
        keys = [k for k in order if k in et] + [k for k in sorted(et) if k not in order]
        for k in keys:
            d = et[k]
            wr = d["wins"] / d["closed"] * 100 if d["closed"] else 0
            per_u = d["pnl"] / d["units"] if d["units"] else 0
            avg_d = f"{sum(d['dist'])/len(d['dist']):.1f}%" if d["dist"] else "—"
            lines.append(f"| {k} | {d['n']} | {d['closed']} | {wr:.1f}% | {d['pnl']:,.0f} | {per_u:,.0f} | {avg_d} |")
        lines.append("")
    return "\n".join(lines)


def main():
    REPORT.write_text(build_report(), encoding="utf-8")
    print(f"written: {REPORT}")
    try:
        from e17_annual_tier_report import OUT as ANNUAL_REPORT, build_report as build_annual
        ANNUAL_REPORT.write_text(build_annual(), encoding="utf-8")
        print(f"written: {ANNUAL_REPORT}")
    except Exception as exc:  # 详细报告失败不影响年度摘要监控
        print(f"annual tier report skipped: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
