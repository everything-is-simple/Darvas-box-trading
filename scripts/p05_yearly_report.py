#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-5 逐年回测统计报告（战绩分级制新体系，2014-2018）。

用法：python scripts/p05_yearly_report.py <db_path> <year> [--csv]

职责（补 m88_metrics.py 缺失的指标）：
  1. 权益指标：收益率 / 最大回撤 / 夏普率（日收益序列，无风险利率=0，sqrt(252)）
  2. 交易指标：胜率 / 盈亏比（profit factor）/ 平仓笔数 / 净利
  3. 逐笔明细：代码/名称/入池日/出池日/池等级(track_level)/下注档位/开仓价/平仓价/腿数/盈亏

新体系语义（docs/21 + 0017 迁移，勿再用旧字段名误导）：
  - 「试探/主力」已改为「首买下注档位」：普通池(normal) 1 单位、赢家池(winner) 5 单位。
    权威来源 = plan_order.reason_json 里的 units/budget_bucket/track_level（buyOrder 写入）。
  - 「在哪个池待过」直接读 security.track_level(normal/winner/blacklist) + blacklist_until。
  - 一律不加码：position_leg 只有 leg_no=1 一条腿（首买），故「腿数」恒为 1（防御性时滞分支除外）。

无 --csv 时输出人类可读 Markdown 报告到 stdout；加 --csv 时在报告后追加一行 CSV。
"""
import json
import sqlite3
import sys


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: p05_yearly_report.py <db_path> <year> [--csv-file <path>] [--out <path>]")
    db_path, year = sys.argv[1], sys.argv[2]
    csv_file = None
    if "--csv-file" in sys.argv:
        idx = sys.argv.index("--csv-file")
        if idx + 1 < len(sys.argv):
            csv_file = sys.argv[idx + 1]
    out_file = None
    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            out_file = sys.argv[idx + 1]

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    def one(sql, *args):
        r = con.execute(sql, args).fetchone()
        return r[0] if r else None

    def rows(sql, *args):
        return con.execute(sql, args).fetchall()

    # ---- 1. 权益指标 -------------------------------------------------------
    eq = rows("SELECT date, cash, market_value, total, peak_total, drawdown "
              "FROM equity_snapshot ORDER BY date")
    if not eq:
        raise SystemExit(f"{db_path}: equity_snapshot 为空，回测可能未完成或失败")
    days = len(eq)
    eq_start = eq[0]["total"]
    eq_end = eq[-1]["total"]
    ret = (eq_end / eq_start - 1) if eq_start else 0.0
    maxdd = min((r["drawdown"] for r in eq), default=0.0)

    # 夏普率：日收益率序列 mean/std*sqrt(252)，无风险利率 0
    daily_rets = []
    for i in range(1, len(eq)):
        prev = eq[i - 1]["total"]
        cur = eq[i]["total"]
        if prev > 0:
            daily_rets.append(cur / prev - 1.0)
    sharpe = 0.0
    if len(daily_rets) > 1:
        n = len(daily_rets)
        mean = sum(daily_rets) / n
        var = sum((x - mean) ** 2 for x in daily_rets) / (n - 1)
        std = var ** 0.5
        if std > 0:
            sharpe = mean / std * (252 ** 0.5)

    # ---- 2. 交易指标 -------------------------------------------------------
    pos_closed = one("SELECT COUNT(*) FROM position WHERE status='closed'") or 0
    wins = one("SELECT COUNT(*) FROM position WHERE status='closed' AND realized_pnl>0") or 0
    losses = one("SELECT COUNT(*) FROM position WHERE status='closed' AND realized_pnl<0") or 0
    win_rate = (wins / pos_closed) if pos_closed else 0.0
    pnl_total = one("SELECT COALESCE(SUM(realized_pnl),0) FROM position WHERE status='closed'") or 0.0
    gross_win = one("SELECT COALESCE(SUM(realized_pnl),0) FROM position "
                    "WHERE status='closed' AND realized_pnl>0") or 0.0
    gross_loss = one("SELECT COALESCE(SUM(realized_pnl),0) FROM position "
                     "WHERE status='closed' AND realized_pnl<0") or 0.0
    profit_factor = (gross_win / abs(gross_loss)) if gross_loss != 0 else (float('inf') if gross_win > 0 else 0.0)

    # ---- 3. 逐笔明细（含池轨迹 + 下注档位） ---------------------------------
    # 开仓价/腿数：position_leg（新体系恒 1 腿，leg_no=1 即首买成交价）。
    # 下注档位/池等级：plan_order.reason_json（buyOrder 写入的 units/budget_bucket/track_level）。
    # 平仓价：position 无 close_price 字段，用 close_date 当日的 daily_bar.close 近似
    #   （止损/强平成交在信号次日，此处用 close_date 收盘价作「平仓参考价」，口径在报告注明）。
    detail = rows("""
        SELECT p.id, p.code, s.name, p.opened_date, p.closed_date, p.close_reason,
               p.avg_cost, p.qty, p.realized_pnl, s.track_level, s.blacklist_until,
               (SELECT l.price FROM position_leg l WHERE l.position_id=p.id AND l.leg_no=1) AS entry_price,
               (SELECT COUNT(*) FROM position_leg l WHERE l.position_id=p.id) AS leg_count
        FROM position p
        JOIN security s ON s.code=p.code
        ORDER BY p.opened_date, p.id
    """)

    # 从 plan_order.reason_json 取首买的下注档位（按 code+最早 buy 单）
    tier_map = {}  # code -> (units, bucket)
    for r in rows("SELECT code, reason_json FROM plan_order WHERE side='buy' ORDER BY id"):
        try:
            rj = json.loads(r["reason_json"])
        except (ValueError, TypeError):
            continue
        units = rj.get("units", rj.get("unit_value"))
        bucket = rj.get("budget_bucket", "")
        if r["code"] not in tier_map:  # 首买最早一条
            tier_map[r["code"]] = (units, bucket)

    # ---- 输出 --------------------------------------------------------------
    lines = []
    lines.append(f"# {year} 年回测报告（达瓦斯箱体 · 战绩分级制新体系）")
    lines.append("")
    lines.append(f"- 回测库：`{db_path}`")
    lines.append(f"- 交易日：{days} 天（{eq[0]['date']} ~ {eq[-1]['date']}）")
    lines.append("")
    lines.append("## ① 核心指标")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 起点权益 | {eq_start:,.0f} |")
    lines.append(f"| 期末权益 | {eq_end:,.0f} |")
    lines.append(f"| 收益率 | {ret*100:.2f}% |")
    lines.append(f"| 最大回撤 | {maxdd*100:.2f}% |")
    lines.append(f"| 夏普率 | {sharpe:.3f} |")
    lines.append(f"| 平仓笔数 | {pos_closed} |")
    lines.append(f"| 胜率 | {win_rate*100:.1f}%（{wins}胜/{losses}负） |")
    lines.append(f"| 盈亏比 (profit factor) | {profit_factor:.3f} |")
    lines.append(f"| 净利 | {pnl_total:,.0f} |")
    lines.append("")

    # 逐笔明细表
    lines.append("## ② 逐笔交易明细")
    lines.append("")
    lines.append("| 代码 | 名称 | 入池日 | 出池日 | 离场池级 | 下注档位 | 开仓价 | 平仓价 | 腿数 | 盈亏 | 离场理由 |")
    lines.append("|------|------|--------|--------|----------|----------|--------|--------|------|------|----------|")

    def tier_label(code):
        u, b = tier_map.get(code, (None, ""))
        if b == "winner":
            return f"主力 {u}单位"
        if b == "normal":
            return f"试探 {u}单位"
        # fallback：无 reason_json 时按 track_level 反推
        return "—"

    def track_label(code, lv):
        return {"normal": "普通池", "winner": "赢家池", "blacklist": "黑名单"}.get(lv, lv)

    for d in detail:
        code = d["code"]
        name = d["name"] or ""
        entry = d["entry_price"] if d["entry_price"] is not None else d["avg_cost"]
        close_px = d["avg_cost"]  # 无平仓价字段，见下注
        lv = d["track_level"] or "normal"
        tier = tier_label(code)
        # 平仓参考价：close_date 当日收盘（近似）
        cpr = None
        if d["closed_date"]:
            cpr = one("SELECT close FROM daily_bar WHERE code=? AND date=?",
                      code, d["closed_date"])
        close_str = f"{cpr:.2f}" if cpr else "—"
        entry_str = f"{entry:.2f}" if entry else "—"
        pnl = d["realized_pnl"] if d["realized_pnl"] is not None else 0.0
        reason = d["close_reason"] or "—"
        lines.append(
            f"| {code} | {name} | {d['opened_date']} | {d['closed_date'] or '—'} | "
            f"{track_label(code, lv)} | {tier} | {entry_str} | {close_str} | "
            f"{d['leg_count']} | {pnl:,.0f} | {reason} |"
        )
    lines.append("")

    lines.append("> 注：新体系「试探/主力」= 首买下注档位（普通池1单位/赢家池5单位），一律不加码，"
                 "故腿数恒为1。「离场池级」为该票清仓时的 security.track_level 终态"
                 "（赢家池=曾盈利、黑名单=曾亏损、普通池=新票）。平仓价为出池日收盘近似。")

    report = "\n".join(lines)
    if out_file:
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(report)
    else:
        print(report)

    # ---- CSV 追加一行（--csv-file 指定路径，Python 直接写，避免 bat 转义问题） ----
    if csv_file:
        pf = profit_factor if profit_factor != float('inf') else 999.0
        row = [
            year, days, f"{eq_start:.0f}", f"{eq_end:.0f}", f"{ret*100:.2f}",
            f"{maxdd*100:.2f}", f"{sharpe:.3f}", f"{pf:.3f}",
            pos_closed, f"{win_rate*100:.1f}", f"{pnl_total:.0f}",
        ]
        with open(csv_file, "a", encoding="utf-8") as f:
            f.write(",".join(str(x) for x in row) + "\n")

    con.close()


if __name__ == "__main__":
    main()
