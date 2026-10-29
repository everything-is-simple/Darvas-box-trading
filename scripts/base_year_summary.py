#!/usr/bin/env python3
"""新基线（单镜上证）逐年后验汇总 —— 只读回测库，零写库。

口径（照 docs/29 §2.5.1 与 docs/experiments/README 四项汇报标准）：
  胜率 / 每单位净利 / 保本胜率 / 利润集中度（前三赢家）
  外加：净利、复利终值、最大回撤、PF、笔数、剔除前三赢家净利

单位定义：1 单位 = 建仓日账户权益的 2%（migration 0009 单位制）。
  每单位净利 = realized_pnl / (建仓金额 / 该单位金额)

两套净利口径并列（docs/29 §2.5.2，2026-10-30 修正）：
  **权益变动**（equity_snapshot 首末差，含年末浮盈）→ 账户层：收益/终值/MDD
  **已实现**（Σ realized_pnl）→ 规则层：每单位净利、集中度、剔除前三
  旧版本用「权益变动」算每单位与集中度，牛市年会被未平仓浮盈污染（2007 差 18 万）；
  集中度旧算法 top3/net 在亏损年为 NaN，已按 docs/29 §2.5.1 第 4 条改为 top3/gross_win。

用法：
  python scripts/base_year_summary.py            # 汇总 var/bt/base/bt_baseA_*.db 全部
  python scripts/base_year_summary.py --json     # 额外输出 JSON
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "var" / "bt" / "base"


def load_year(db: Path):
    """读一个年库，返回该年的统计 dict；库不可用返回 None。"""
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception:
        return None
    try:
        eq = c.execute(
            "SELECT date, total FROM equity_snapshot ORDER BY date"
        ).fetchall()
        if not eq:
            return None
        first_eq = eq[0][1]
        last_eq = eq[-1][1]
        eq_by_date = dict(eq)

        # 最大回撤：按日终权益跑峰谷
        peak = first_eq
        mdd = 0.0
        mdd_date = eq[0][0]
        for d, tot in eq:
            peak = max(peak, tot)
            dd = tot / peak - 1.0
            if dd < mdd:
                mdd, mdd_date = dd, d

        # 成本必须取 Σ(position_leg.qty×price)：position.qty 平仓后清零（P2-5 踩坑）
        rows = c.execute(
            """SELECT p.opened_date, p.closed_date, p.realized_pnl, p.close_reason,
                      COALESCE(SUM(l.qty * l.price), 0) AS cost
               FROM position p
               LEFT JOIN position_leg l ON l.position_id = p.id
               WHERE p.realized_pnl IS NOT NULL AND p.status = 'closed'
               GROUP BY p.id
               ORDER BY p.realized_pnl DESC"""
        ).fetchall()

        n = len(rows)
        if n == 0:
            return dict(year=db.stem.split("_")[-1], trades=0, first_equity=first_eq,
                        last_equity=last_eq, last_date=eq[-1][0], net=last_eq - first_eq,
                        mdd=mdd, mdd_date=mdd_date, days=len(eq),
                        complete=eq[-1][0] >= f"{db.stem.split('_')[-1]}-12-25",
                        win_rate=0.0, breakeven=0.0, gap=0.0, pf=0.0,
                        units=0.0, per_unit=0.0, realized=0.0, per_unit_realized=0.0,
                        ret_pct=(last_eq - first_eq) / first_eq if first_eq else 0.0,
                        top3=0.0, top3_share=float("nan"), top3_share_gw=float("nan"),
                        net_ex_top3=last_eq - first_eq, realized_ex_top3=0.0,
                        hold_median=None, gross_win=0.0, gross_loss=0.0)

        pnls = [r[2] for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        net = sum(pnls)

        # 每单位净利：建仓成本 / (建仓日权益 × 2%)
        units_total = 0.0
        for od, _cd, _pnl, _reason, cost in rows:
            base_eq = eq_by_date.get(od) or first_eq
            unit_val = base_eq * 0.02
            units_total += cost / unit_val if unit_val > 0 else 0.0

        top3 = sum(pnls[:3])
        avg_win = gross_win / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        breakeven = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else 0.0

        # 持有时长（自然日）：opened_date → closed_date
        holds = []
        for od, cd, _p, _r, _c in rows:
            if od and cd:
                try:
                    holds.append(
                        (datetime.date.fromisoformat(cd[:10])
                         - datetime.date.fromisoformat(od[:10])).days
                    )
                except ValueError:
                    pass
        hold_median = statistics.median(holds) if holds else None

        return dict(
            year=db.stem.split("_")[-1],
            first_equity=first_eq,
            last_equity=last_eq,
            last_date=eq[-1][0],
            # 完成判据 12-25（2026-10-29 修正：原 12-15 太松，2005-12-16 就被误判为跑完，
            # 而 A 股年度最后交易日恒在 12-28~12-31，故以 12-25 为安全下界）
            complete=eq[-1][0] >= f"{db.stem.split('_')[-1]}-12-25",
            net=last_eq - first_eq,
            net_pnl_sum=net,
            days=len(eq),
            trades=n,
            wins=len(wins),
            losses=len(losses),
            win_rate=len(wins) / n,
            gross_win=gross_win,
            gross_loss=gross_loss,
            pf=(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            avg_win=avg_win,
            avg_loss=avg_loss,
            breakeven=breakeven,
            gap=(len(wins) / n) - breakeven,
            mdd=mdd,
            mdd_date=mdd_date,
            units=units_total,
            # 账户层（权益变动）与规则层（已实现）并列，禁止混用
            per_unit=(net / units_total) if units_total > 0 else 0.0,
            realized=sum(pnls),
            per_unit_realized=(sum(pnls) / units_total) if units_total > 0 else 0.0,
            ret_pct=(last_eq - first_eq) / first_eq if first_eq else 0.0,
            top3=top3,
            top3_share=(top3 / net) if net > 0 else float("nan"),  # 旧口径，仅供对照
            top3_share_gw=(top3 / gross_win) if gross_win > 0 else float("nan"),
            net_ex_top3=net - top3,
            realized_ex_top3=sum(pnls) - top3,
            hold_median=hold_median,
        )
    except Exception as e:
        print(f"  [!] {db.name}: {type(e).__name__} {e}", file=sys.stderr)
        return None
    finally:
        c.close()


def fmt_money(v):
    return f"{v:,.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mirror", default="A")
    a = ap.parse_args()

    dbs = sorted(BASE.glob(f"bt_base{a.mirror}_*.db"))
    # 排除正在写入的（有 -wal 且无 -shm 不判断；仅按 equity_snapshot 是否闭合判断）
    results = []
    for db in dbs:
        r = load_year(db)
        if r:
            r["file"] = db.name
            results.append(r)
    results.sort(key=lambda x: x["year"])

    if not results:
        print("无可读库")
        return

    print(f"\n{'年':<6}{'收益':>9}{'权益变动':>12}{'已实现':>12}{'笔数':>6}"
          f"{'胜率':>7}{'保本率':>7}{'缺口':>7}{'PF':>6}{'MDD':>8}"
          f"{'每单位':>8}{'集中度':>8}{'剔前三':>11}{'持中位':>7}")
    print("-" * 122)
    tot_net = 0
    tot_real = 0
    for r in results:
        pf = "  ∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        share = "—" if r["top3_share_gw"] != r["top3_share_gw"] else f"{r['top3_share_gw']*100:.0f}%"
        hold = "—" if r["hold_median"] is None else f"{r['hold_median']:.0f}"
        yr = r["year"] + ("" if r["complete"] else "*")
        print(f"{yr:<6}{r['ret_pct']*100:>8.1f}%{fmt_money(r['net']):>12}{fmt_money(r['realized']):>12}"
              f"{r['trades']:>6}{r['win_rate']*100:>6.1f}%{r['breakeven']*100:>6.1f}%"
              f"{r['gap']*100:>+6.1f}{pf:>6}{r['mdd']*100:>7.2f}%"
              f"{r['per_unit_realized']:>8,.0f}{share:>8}{fmt_money(r['realized_ex_top3']):>11}{hold:>7}")
        if r["complete"]:
            tot_net += r["net"]
            tot_real += r["realized"]
    print("-" * 122)
    print(f"{'合计':<6}{'':>9}{fmt_money(tot_net):>12}{fmt_money(tot_real):>12}"
          f"  （仅计完整年；算术和≠复利；每单位/集中度/剔前三用已实现）")
    partial = [r for r in results if not r["complete"]]
    for r in partial:
        print(f"  * {r['year']} 未完成：数据截至 {r['last_date']}，仅 {r['days']} 个交易日 / {r['trades']} 笔，不计入合计")

    if a.json:
        out = BASE / "base_year_summary.json"
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON → {out}")


if __name__ == "__main__":
    main()
