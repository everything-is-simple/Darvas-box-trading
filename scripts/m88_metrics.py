#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M8.8 逐年回测指标提取（市场宽度 + 单位制仓位）。

用法：python scripts/m88_metrics.py <db_path> <year>
输出：一行 CSV（无表头，由 run_m88.bat 负责写入表头）。

口径：
  - probe_fills = position_leg.leg_no=1 笔数（试探腿成交）
  - main_fills  = position_leg.leg_no>=2 笔数（主力腿成交；应仅牛市出现）
  - unit_peak   = max(equity_snapshot.market_value / (equity_snapshot.total × pos.unit))
  - cash_min_pct= min(cash/total)，验证「永远留 30% 现金」
"""
import sqlite3
import sys


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: m88_metrics.py <db_path> <year>")
    db_path, year = sys.argv[1], sys.argv[2]
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def one(sql, *args):
        r = con.execute(sql, args).fetchone()
        return r[0] if r else None

    eq_start = one("SELECT total FROM equity_snapshot ORDER BY date LIMIT 1") or 0
    eq_end = one("SELECT total FROM equity_snapshot ORDER BY date DESC LIMIT 1") or 0
    maxdd = one("SELECT MIN(drawdown) FROM equity_snapshot") or 0
    days = one("SELECT COUNT(*) FROM equity_snapshot") or 0
    pos_unit = float(one("SELECT value FROM param WHERE key='pos.unit'") or 0.02)

    breakout = one("SELECT COUNT(*) FROM signal WHERE type='breakout'") or 0
    pyramid = one("SELECT COUNT(*) FROM signal WHERE type='pyramid'") or 0
    failed = one("SELECT COUNT(*) FROM signal WHERE type='failed_breakout'") or 0
    stops = one("SELECT COUNT(*) FROM signal WHERE type='stop'") or 0

    leg1 = one("SELECT COUNT(*) FROM position_leg WHERE leg_no=1") or 0
    leg2 = one("SELECT COUNT(*) FROM position_leg WHERE leg_no=2") or 0
    leg3 = one("SELECT COUNT(*) FROM position_leg WHERE leg_no=3") or 0
    probe_fills = leg1
    main_fills = leg2 + leg3
    charged = one("""SELECT COUNT(*) FROM (SELECT position_id FROM position_leg
                     GROUP BY position_id HAVING COUNT(*)>1)""") or 0

    pos_closed = one("SELECT COUNT(*) FROM position WHERE status='closed'") or 0
    pnl_total = one("SELECT COALESCE(SUM(realized_pnl),0) FROM position WHERE status='closed'") or 0
    wins = one("SELECT COUNT(*) FROM position WHERE status='closed' AND realized_pnl>0") or 0
    win_rate = (wins / pos_closed) if pos_closed else 0

    unit_peak = one("""SELECT COALESCE(MAX(market_value/(total*?)),0)
                       FROM equity_snapshot WHERE total>0""", pos_unit) or 0
    cash_min = one("""SELECT COALESCE(MIN(cash/total),1)
                      FROM equity_snapshot WHERE total>0""") or 1

    # 期末 open 敞口按 OpenExposureUnits 口径折算（leg1=试探；leg2+=主力；最新收盘估值）。
    unit_value = (eq_end * pos_unit) if eq_end > 0 else 0
    probe_units_end = main_units_end = 0.0
    if unit_value > 0:
        probe_units_end = one("""
            SELECT COALESCE(SUM(x.qty*COALESCE(x.close,x.avg_cost)),0)/? FROM (
                SELECT p.qty, p.avg_cost,
                       (SELECT d.close FROM daily_bar d WHERE d.code=p.code ORDER BY d.date DESC LIMIT 1) AS close,
                       COALESCE(MAX(l.leg_no),0) AS max_leg
                FROM position p LEFT JOIN position_leg l ON l.position_id=p.id
                WHERE p.status='open' AND p.qty>0
                GROUP BY p.id
            ) x WHERE x.max_leg<2
        """, unit_value) or 0
        main_units_end = one("""
            SELECT COALESCE(SUM(x.qty*COALESCE(x.close,x.avg_cost)),0)/? FROM (
                SELECT p.qty, p.avg_cost,
                       (SELECT d.close FROM daily_bar d WHERE d.code=p.code ORDER BY d.date DESC LIMIT 1) AS close,
                       COALESCE(MAX(l.leg_no),0) AS max_leg
                FROM position p LEFT JOIN position_leg l ON l.position_id=p.id
                WHERE p.status='open' AND p.qty>0
                GROUP BY p.id
            ) x WHERE x.max_leg>=2
        """, unit_value) or 0

    ret = (eq_end / eq_start - 1) if eq_start else 0
    cols = [
        year, days, f"{eq_start:.0f}", f"{eq_end:.0f}", f"{ret*100:.2f}",
        f"{maxdd*100:.2f}", breakout, pyramid, failed, stops,
        probe_fills, main_fills, leg1, leg2, leg3, charged, pos_closed,
        f"{win_rate*100:.1f}", f"{pnl_total:.0f}", f"{unit_peak:.2f}",
        f"{cash_min*100:.1f}", f"{probe_units_end:.2f}", f"{main_units_end:.2f}",
    ]
    print(",".join(str(x) for x in cols))
    con.close()


if __name__ == "__main__":
    main()
