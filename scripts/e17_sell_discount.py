#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T3 `sell.discount` 梯度报告生成器（框架：docs/experiments/e17_sell_discount_framework.md）。

只读回测库（file:?mode=ro），不重跑、不改任何库。产出：
  docs/experiments/e17_sell_discount_report.md / .html / _data.json

指标口径 100% 复用 LB-4（scripts/e17_lb4_bt.py）：
  每单位净利 = Σrealized_pnl / Σunits；units = cost / unit_val；unit_val = 开仓日权益 × 2%
  胜率 / 保本胜率 / 缺口 / 利润集中度 / PF / 剔除前三
新增（框架 §5 预注册）：
  标签 A 出口来源 = plan_order.reason_json.signal（经 trade_fill 关联成交）
  标签 B 止损位是否已上移 = |exit_px / (entry_box.top × disc) − 1| ≤ 0.5%
  标签 C 结构指标 = 平均/中位持仓天数、卖单未成交率

用法：
  python scripts/e17_sell_discount.py            # 生成全部产物
  python scripts/e17_sell_discount.py --check    # 只打印总表
"""
import argparse
import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
SRC_DIR = BT / "t3_bt"
OUT_DIR = ROOT / "docs" / "experiments"

UNIT_PCT = 0.02
TIERS = ["095", "097", "099"]
DISC = {"095": 0.95, "097": 0.97, "099": 0.99}
TIER_LABEL = {"095": "0.95（紧）", "097": "0.97（基线）", "099": "0.99（宽）"}
B_TOL = 0.005  # 0.5% 容差，框架 §5.2 预注册固定


def pct(x, d=1):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def money(x, d=0):
    return "—" if x is None else f"{x:,.{d}f}"


def fnum(x, d=2):
    return "—" if x is None else f"{x:.{d}f}"


def fsign(x, d=0):
    return "—" if x is None else f"{x:+,.{d}f}"


def connect(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def parse_dbname(name):
    # bt_t3_097_2018.db -> ("097", 2018)
    stem = Path(name).stem
    if not stem.startswith("bt_t3_"):
        return None, None
    parts = stem.split("_")
    return parts[2], int(parts[3])


TRADE_Q = """
SELECT p.id, p.code, p.opened_date, p.closed_date, p.realized_pnl, p.close_reason,
       p.entry_box_id, b.top AS entry_top,
       (SELECT SUM(l.qty * l.price) FROM position_leg l WHERE l.position_id = p.id) AS cost,
       (SELECT SUM(l.qty) FROM position_leg l WHERE l.position_id = p.id) AS qty,
       (SELECT f.price FROM trade_fill f
          WHERE f.code = p.code AND f.date = p.closed_date AND f.side = 'sell' LIMIT 1) AS exit_px,
       (SELECT json_extract(po.reason_json, '$.signal')
          FROM trade_fill f JOIN plan_order po ON po.id = f.plan_order_id
         WHERE f.code = p.code AND f.date = p.closed_date AND f.side = 'sell' LIMIT 1) AS sig,
       (SELECT COUNT(*) FROM trade_fill f
         WHERE f.code = p.code AND f.date = p.closed_date AND f.side = 'sell'
           AND f.raw_text = 'sim:dividend') AS n_div
FROM position p
LEFT JOIN box b ON b.id = p.entry_box_id
WHERE p.status = 'closed'
GROUP BY p.id
ORDER BY p.closed_date, p.code
"""


def load_db(db: Path):
    tier, year = parse_dbname(db.name)
    c = connect(db)
    try:
        eq_rows = c.execute("SELECT date, total, COALESCE(drawdown,0) FROM equity_snapshot ORDER BY date").fetchall()
        if not eq_rows:
            return None
        eq = {d: t for d, t, _ in eq_rows}
        first_eq = eq_rows[0][1]
        last_eq = eq_rows[-1][1]
        mdd = -min((dd for _, _, dd in eq_rows), default=0.0)  # drawdown 以负值存储

        # 水下天数：drawdown < 0 的交易日数；最长连续水下
        under = 0
        longest = 0
        cur = 0
        for _, _, dd in eq_rows:
            if dd < -1e-9:
                under += 1
                cur += 1
                longest = max(longest, cur)
            else:
                cur = 0

        # 卖单未成交率
        tot_sell = c.execute("SELECT COUNT(*) FROM plan_order WHERE side='sell'").fetchone()[0]
        filled_sell = c.execute("SELECT COUNT(*) FROM plan_order WHERE side='sell' AND status='filled'").fetchone()[0]
        unfilled = (tot_sell - filled_sell) / tot_sell if tot_sell else None

        trades = []
        for (pid, code, od, cd, pnl, creason, boxid, etop, cost, qty, exit_px, sig, n_div) in c.execute(TRADE_Q):
            base_eq = eq.get(od) or first_eq or 0.0
            unit_val = base_eq * UNIT_PCT
            units = (cost / unit_val) if unit_val and unit_val > 0 else 0.0
            hold = None
            if od and cd:
                try:
                    hold = (datetime.fromisoformat(str(cd)) - datetime.fromisoformat(str(od))).days
                except Exception:
                    hold = None
            # 出口来源判定（优先级：复权清仓 > 计划单信号 > 兜底）。
            # 复权跳空日强制清仓由 match.go §3.8 触发（raw_text='sim:dividend'），与 sell.discount 无关；
            # 若当日恰有在途卖单，该笔清仓会被 ApplyFill 误配给该计划单（close_reason 记成 stop），
            # 因此必须按成交来源而非计划单信号判定，否则规则强制清仓会被混入策略出口、稀释档位对比。
            if n_div and n_div > 0:
                exit_src = "dividend_clear"
            elif sig:
                exit_src = sig
            elif creason == "manual":
                exit_src = "manual"
            else:
                exit_src = "(未知)"
            # 标签 B
            tagB = "n/a"
            if exit_src == "initial_stop":
                if etop and exit_px:
                    ref = etop * DISC[tier]
                    tagB = "初始位未上移" if abs(exit_px / ref - 1) <= B_TOL else "已上移"
                else:
                    tagB = "缺 entry_top"
            trades.append(dict(
                code=code, opened=str(od), closed=str(cd), pnl=pnl, close_reason=creason,
                cost=cost, qty=qty, units=units, exit_px=exit_px,
                sig=exit_src,
                tagB=tagB, hold_days=hold,
            ))
        return dict(tier=tier, year=year, trades=trades, first_eq=first_eq, last_eq=last_eq,
                    mdd=mdd, under_days=under, longest_under=longest, unfilled=unfilled,
                    n_sell_orders=tot_sell, max_date=eq_rows[-1][0])
    finally:
        c.close()


def agg(trades):
    closed = [r for r in trades if r["pnl"] is not None]
    n = len(closed)
    if n == 0:
        return dict(n=0)
    wins = [r["pnl"] for r in closed if r["pnl"] > 0]
    losses = [r["pnl"] for r in closed if r["pnl"] <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    tot = sum(r["pnl"] for r in closed)
    units = sum(r["units"] for r in closed)
    per_unit = tot / units if units > 0 else None
    win_rate = len(wins) / n
    avg_win = statistics.mean(wins) if wins else 0.0
    # 保本胜率 = |avg_loss| / (avg_win + |avg_loss|)。
    # 注：e17_lb4_bt.py 原式用 avg_loss 的负数直接除，得 −L/(W−L)，符号与分母都错
    #     （LB-4 报告 off 档 −14.5% 即此错；正确应为 11.3%）。本脚本已修正，见报告脚注。
    avg_loss_abs = -statistics.mean(losses) if losses else 0.0
    breakeven = avg_loss_abs / (avg_win + avg_loss_abs) if (avg_win + avg_loss_abs) > 0 else None
    gap = (win_rate - breakeven) if breakeven is not None else None
    top3 = sum(sorted([r["pnl"] for r in closed], reverse=True)[:3])
    share = top3 / gross_win if gross_win > 0 else None
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else None)
    holds = [r["hold_days"] for r in closed if r["hold_days"] is not None]
    return dict(n=n, win_rate=win_rate, per_unit=per_unit, breakeven=breakeven, gap=gap,
                pf=pf, share=share, ex_top3=tot - top3, tot=tot, units=units,
                avg_hold=statistics.mean(holds) if holds else None,
                med_hold=statistics.median(holds) if holds else None)


def tier_summary(by_db, tier):
    keys = sorted([k for k in by_db if by_db[k]["tier"] == tier], key=lambda k: by_db[k]["year"])
    trades = []
    per_year = []
    for k in keys:
        d = by_db[k]
        trades += d["trades"]
        a = agg(d["trades"])
        per_year.append(dict(year=d["year"], **a,
                             equity_delta=d["last_eq"] - d["first_eq"],
                             mdd=d["mdd"], under_days=d["under_days"],
                             unfilled=d["unfilled"]))
    s = agg(trades)
    # 可比口径：剔除复权跳空日强制清仓（撮合层规则 §3.8，与 sell.discount 无关）。
    # 三档之间唯一可能变化的只有受 sell.discount 影响的出场，混入规则强制清仓会稀释档位差异。
    ex_div_trades = [t for t in trades if t["sig"] != "dividend_clear"]
    s["ex_div"] = agg(ex_div_trades)
    s["div_n"] = len(trades) - len(ex_div_trades)
    s["per_year"] = per_year
    s["years"] = [by_db[k]["year"] for k in keys]
    s["mdd_worst"] = max((d["mdd"] for k in keys for d in [by_db[k]]), default=None)
    s["under_total"] = sum(by_db[k]["under_days"] for k in keys)
    s["equity_delta"] = sum(by_db[k]["last_eq"] - by_db[k]["first_eq"] for k in keys)
    s["unfilled_avg"] = (sum(by_db[k]["unfilled"] or 0 for k in keys) / len(keys)) if keys else None
    return s


def exit_decomp(by_db, tier, tag):
    """按标签 A（sig）或标签 B（tagB）分组统计。"""
    out = {}
    for k in [k for k in by_db if by_db[k]["tier"] == tier]:
        for t in by_db[k]["trades"]:
            key = t["sig"] if tag == "A" else t["tagB"]
            out.setdefault(key, []).append(t)
    return {k: agg(v) for k, v in out.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dir", default=None, help="库目录（默认 var/bt/t3_bt）")
    a = ap.parse_args()
    src_dir = Path(a.dir) if a.dir else SRC_DIR

    by_db = {}
    for db in sorted(src_dir.glob("bt_t3_*.db")):
        tier, year = parse_dbname(db.name)
        if tier not in TIERS:
            continue
        d = load_db(db)
        if not d or not d["trades"]:
            continue
        # 完整性守卫：equity_snapshot 必须覆盖到该年年末，否则是半成品库（回测被中断），
        # 计入会给出「半年的笔数与已实现」并在报告里与其他完整年并列，属可比口径污染。
        if not d.get("max_date") or d["max_date"] < f"{year}-12-01":
            print(f"[skip] {db.name} 未完整（equity 末日期 {d.get('max_date')} < {year}-12-01）")
            continue
        by_db[f"{tier}_{year}"] = d

    if not by_db:
        raise SystemExit("没有可用库：var/bt/t3_bt/bt_t3_*.db")

    sums = {t: tier_summary(by_db, t) for t in TIERS if any(by_db[k]["tier"] == t for k in by_db)}
    base = sums.get("097")

    # 控制台总表
    print(f"{'档位':<12}{'笔数':>5}{'每单位':>9}{'胜率':>8}{'PF':>7}{'剔前三':>13}{'已实现':>13}{'权益变动':>13}{'MDD':>8}")
    for t, s in sums.items():
        pf = "inf" if s["pf"] == float("inf") else fnum(s["pf"])
        print(f"{TIER_LABEL[t]:<12}{s['n']:>5}{fnum(s['per_unit'],0):>9}{pct(s['win_rate']):>8}"
              f"{pf:>7}{money(s['ex_top3']):>13}{money(s['tot']):>13}{money(s['equity_delta']):>13}{pct(s['mdd_worst']):>8}")

    if a.check:
        return 0

    data = dict(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        tiers={t: {k: v for k, v in s.items() if k != "per_year"} for t, s in sums.items()},
        per_year={t: s["per_year"] for t, s in sums.items()},
        exit_a={t: exit_decomp(by_db, t, "A") for t in sums},
        exit_b={t: exit_decomp(by_db, t, "B") for t in sums},
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "e17_sell_discount_data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---- Markdown ----
    L = []
    L.append("# T3 · `sell.discount` 梯度实验报告\n")
    L.append(f"> 生成：{data['generated_at']}｜框架：`e17_sell_discount_framework.md`｜库：`var/bt/t3_bt/`\n")
    L.append("> **本报告不是「0.97 已验证最优」的证据**，只是 0.95 / 0.97 / 0.99 三点比较。\n")
    L.append("\n## 1 档位总表（四年同集合并，已实现口径）\n")
    L.append("| 档位 | 笔数 | 每单位净利 | 胜率 | 保本胜率 | 缺口 | PF | 集中度 | 剔除前三 | 已实现净利 | 权益变动 | 最差MDD | 平均持仓 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for t, s in sums.items():
        pf = "∞" if s["pf"] == float("inf") else fnum(s["pf"])
        L.append(f"| {TIER_LABEL[t]} | {s['n']} | {fnum(s['per_unit'],0)} | {pct(s['win_rate'])} | "
                 f"{pct(s['breakeven'])} | {pct(s['gap'])} | {pf} | {pct(s['share'])} | "
                 f"{money(s['ex_top3'])} | {money(s['tot'])} | {money(s['equity_delta'])} | "
                 f"{pct(s['mdd_worst'])} | {fnum(s['avg_hold'],1)} 天 |")
    L.append("\n## 2 逐年明细\n")
    L.append("\n### 2.0 可比口径（剔除复权跳空日强制清仓）\n")
    L.append("> 复权清仓由撮合层规则（§3.8：除权跳空日按昨收×(1−滑点) 强制清仓）产生，**与 `sell.discount` 无关**，"
             "且按构造偏正（避开跳空损失）。档位差异只能来自其余出场，故并列此表。\n")
    L.append("| 档位 | 复权清仓笔数 | 剩余笔数 | 每单位净利 | 胜率 | PF | 剔除前三 | 已实现净利 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for t, s in sums.items():
        e = s["ex_div"]
        pf = "∞" if e["pf"] == float("inf") else fnum(e["pf"])
        L.append(f"| {TIER_LABEL[t]} | {s['div_n']} | {e.get('n',0)} | {fnum(e.get('per_unit'),0)} | "
                 f"{pct(e.get('win_rate'))} | {pf} | {money(e.get('ex_top3'))} | {money(e.get('tot'))} |")
    for t, s in sums.items():
        L.append(f"\n### {TIER_LABEL[t]}\n")
        L.append("| 年 | 笔数 | 每单位净利 | 胜率 | PF | 剔除前三 | 已实现 | 权益变动 | MDD | 水下天数 | 卖单未成交率 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for y in s["per_year"]:
            pf = "∞" if y["pf"] == float("inf") else fnum(y["pf"])
            L.append(f"| {y['year']} | {y['n']} | {fnum(y['per_unit'],0)} | {pct(y['win_rate'])} | {pf} | "
                     f"{money(y['ex_top3'])} | {money(y['tot'])} | {money(y['equity_delta'])} | "
                     f"{pct(y['mdd'])} | {y['under_days']} | {pct(y['unfilled'])} |")
    return _finish_md(L, sums, data)


def _finish_md(L, sums, data):
    L.append("\n## 3 出口分解 · 标签 A（出口来源）\n")
    L.append("> `dividend_clear` = 复权跳空日强制清仓（成交 `raw_text='sim:dividend'`），撮合层规则产物、"
             "非策略出口，不参与档位归因；其余按计划单 `reason.signal` 归类。\n")
    for t in sums:
        L.append(f"\n### {TIER_LABEL[t]}\n")
        L.append("| 出口来源 | 笔数 | 已实现净利 | 每单位净利 | 胜率 | 平均持仓 |")
        L.append("|---|---|---|---|---|---|")
        for k, s in sorted(data["exit_a"][t].items(), key=lambda kv: -kv[1]["n"]):
            L.append(f"| `{k}` | {s['n']} | {money(s['tot'])} | {fnum(s['per_unit'],0)} | "
                     f"{pct(s['win_rate'])} | {fnum(s['avg_hold'],1)} 天 |")
    L.append("\n## 4 出口分解 · 标签 B（止损位是否已上移，仅 initial_stop）\n")
    for t in sums:
        L.append(f"\n### {TIER_LABEL[t]}\n")
        L.append("| 分类 | 笔数 | 已实现净利 | 每单位净利 | 胜率 | 平均持仓 |")
        L.append("|---|---|---|---|---|---|")
        for k, s in sorted(data["exit_b"][t].items(), key=lambda kv: -kv[1]["n"]):
            L.append(f"| {k} | {s['n']} | {money(s['tot'])} | {fnum(s['per_unit'],0)} | "
                     f"{pct(s['win_rate'])} | {fnum(s['avg_hold'],1)} 天 |")
    L.append("\n---\n")
    L.append("## 5 口径声明\n")
    L.append("1. **保本胜率** = |平均亏损| ÷ (平均盈利 + |平均亏损|)。历史脚本 `e17_lb4_bt.py` 误用负数直接除，"
             "得 `−L/(W−L)`（LB-4 off 档显示 −14.5%，正确应为 11.3%），故 LB-4／P1-4 等历史报告的"
             "「保本胜率」「缺口」两列不可引用；本报告已修正。各实验的判定未使用这两列，主结论不受影响。\n")
    L.append("2. **每单位净利** = 已实现净利 ÷ Σ单位数，单位数按开仓日权益 × 2% 折出。\n")
    L.append("3. **权益变动** = 年末权益 − 年初权益（含浮盈）；**已实现净利**只含已平仓。"
             "讨论出场质量用已实现，账户层用权益变动。\n")
    L.append("4. **复权清仓**单列，见 §2.0 与标签 A 的 `dividend_clear`。\n")
    md = "\n".join(L)
    (OUT_DIR / "e17_sell_discount_report.md").write_text(md, encoding="utf-8")
    print(f"\n已写 {OUT_DIR / 'e17_sell_discount_report.md'}")
    print(f"已写 {OUT_DIR / 'e17_sell_discount_data.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
