#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进化版逐年回测指标提取（M8.6 第②通道效果评估）。

用法：python scripts/evo_metrics.py <db_path> <year>
输出：一行 CSV（无表头由调用方负责）

指标口径：
  - pyramid_newbox = signal.type=pyramid 且 payload_json.trigger != 'addon'（第①通道）
  - pyramid_addon  = signal.type=pyramid 且 payload_json.trigger == 'addon'（第②通道）
  - pnl_addon      = 有 >1 条 position_leg 的持仓 realized_pnl 合计（加码贡献）
  - 其余见列名
"""
import json, sqlite3, sys


def main():
    db_path, year = sys.argv[1], sys.argv[2]
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def one(sql, *args):
        r = c.execute(sql, args).fetchone()
        return r[0] if r else None

    # 权益
    eq_start = one("SELECT total FROM equity_snapshot ORDER BY date LIMIT 1") or 0
    eq_end = one("SELECT total FROM equity_snapshot ORDER BY date DESC LIMIT 1") or 0
    maxdd = one("SELECT MIN(drawdown) FROM equity_snapshot") or 0
    days = one("SELECT COUNT(*) FROM equity_snapshot") or 0

    # 信号分类
    nx = 0
    addon = 0
    for typ, payload in c.execute(
            "SELECT type, payload_json FROM signal WHERE type='pyramid'").fetchall():
        trig = ""
        try:
            trig = (json.loads(payload or "{}") or {}).get("trigger", "") or ""
        except Exception:
            pass
        if trig == "addon":
            addon += 1
        else:
            nx += 1
    breakout = one("SELECT COUNT(*) FROM signal WHERE type='breakout'") or 0
    failed = one("SELECT COUNT(*) FROM signal WHERE type='failed_breakout'") or 0
    stops = one("SELECT COUNT(*) FROM signal WHERE type='stop'") or 0

    # 持仓
    pos_total = one("SELECT COUNT(*) FROM position") or 0
    pos_closed = one("SELECT COUNT(*) FROM position WHERE status='closed'") or 0
    pos_open = pos_total - pos_closed
    pnl_total = one("SELECT COALESCE(SUM(realized_pnl),0) FROM position WHERE status='closed'") or 0
    wins = one("SELECT COUNT(*) FROM position WHERE status='closed' AND realized_pnl>0") or 0
    losses = one("SELECT COUNT(*) FROM position WHERE status='closed' AND realized_pnl<0") or 0

    # 腿
    leg1 = one("SELECT COUNT(*) FROM position_leg WHERE leg_no=1") or 0
    leg2 = one("SELECT COUNT(*) FROM position_leg WHERE leg_no=2") or 0
    leg3 = one("SELECT COUNT(*) FROM position_leg WHERE leg_no=3") or 0
    charged = one("""SELECT COUNT(*) FROM (SELECT position_id FROM position_leg
                     GROUP BY position_id HAVING COUNT(*)>1)""") or 0

    # 加码贡献：有多腿的持仓 realized_pnl 合计
    pnl_addon = one("""SELECT COALESCE(SUM(p.realized_pnl),0) FROM position p
        WHERE p.status='closed' AND (SELECT COUNT(*) FROM position_leg l
        WHERE l.position_id=p.id) > 1""") or 0

    # 平均持仓天数（平仓单）
    avg_hold = one("""SELECT COALESCE(AVG(julianday(closed_date)-julianday(opened_date)),0)
        FROM position WHERE status='closed'""") or 0

    ret = (eq_end / eq_start - 1) if eq_start else 0
    win_rate = (wins / pos_closed) if pos_closed else 0
    addon_share = (pnl_addon / pnl_total) if pnl_total else 0

    cols = [
        year, days, f"{eq_start:.0f}", f"{eq_end:.0f}", f"{ret*100:.2f}",
        f"{maxdd*100:.2f}", breakout, nx, addon, failed, stops,
        pos_total, pos_closed, pos_open, leg1, leg2, leg3, charged,
        f"{pnl_total:.0f}", f"{pnl_addon:.0f}", f"{addon_share*100:.1f}",
        f"{win_rate*100:.1f}", f"{avg_hold:.1f}",
    ]
    print(",".join(str(x) for x in cols))
    c.close()


if __name__ == "__main__":
    main()
