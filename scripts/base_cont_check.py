#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""base_cont_check.py —— 连续单库对账（docs/50 阶段 C 验收 V5）

读连续库 `var/bt/base/bt_baseA_cont.db` 的 `equity_snapshot` + `position`，产出三样：
  1. 跨年持仓结转清单（V5-1：F2「79 笔被年末切断」在连续库里是否消失）
  2. 逐年复利分解（V5-3：连续库逐年收益率 vs 单年库，差异应极小=整手取整级）
  3. 总账对比（V5-2：连续库终值 vs base_snowball 链式终值，方向应可预期）

为什么真连续 ≠ 事后链式（docs/50 §二）：
  - 链式：每年 100 万起跑，收益率连乘（无真实资金放大）
  - 连续：现金池滚动，盈利滚进次年下单本金（真实资金约束）
  → 连续比链式更「陡」，方向可预期但值必然不同。

用法：
  python scripts/base_cont_check.py                     # 连续库跑完后
  python scripts/base_cont_check.py --cont var/bt/base/bt_cont_2005_2006.db  # 指定库（冒烟）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "var" / "bt" / "base"


def _open(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def daily_totals(db: Path):
    c = _open(db)
    try:
        return c.execute("SELECT date, total, cash, market_value FROM equity_snapshot ORDER BY date").fetchall()
    finally:
        c.close()


def yearly_splits(db: Path):
    """把连续库 equity_snapshot 按年切：{year: [(date, total), ...]}，及每年年末 total。"""
    rows = daily_totals(db)
    out = {}
    for date, total, cash, mv in rows:
        y = int(date[:4])
        out.setdefault(y, []).append((date, total))
    return out


def cross_year_positions(db: Path):
    """跨年持仓结转：opened_date 在某年、closed_date 在次年（或仍 open）的持仓。

    F2 缺口验证：单年库这些持仓在年末被切断；连续库应保留。"""
    c = _open(db)
    try:
        rows = c.execute(
            "SELECT code, CAST(opened_date AS TEXT), COALESCE(CAST(closed_date AS TEXT),''), "
            "status, qty, COALESCE(realized_pnl,0) FROM position "
            "WHERE status='open' OR (status='closed' AND substr(opened_date,1,4) != substr(closed_date,1,4)) "
            "ORDER BY opened_date"
        ).fetchall()
    finally:
        c.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cont", default=str(BASE / "bt_baseA_cont.db"),
                    help="连续库路径（默认 var/bt/base/bt_baseA_cont.db）")
    ap.add_argument("--years", default="2005..2018", help="单年库年份范围，用于逐年对比")
    a = ap.parse_args()

    cont = Path(a.cont)
    if not cont.exists():
        raise SystemExit(f"连续库不存在：{cont}（先跑 base_run.py --cont）")

    rows = daily_totals(cont)
    if not rows:
        raise SystemExit(f"连续库无 equity_snapshot：{cont}")
    print(f"连续库 {cont.name}: {len(rows)} 个交易日，首日 {rows[0][0]}，末日 {rows[-1][0]}，"
          f"终值 {rows[-1][1]:,.0f}")

    # V5-1 跨年持仓结转
    print("\n== V5-1 跨年持仓结转 ==")
    xpos = cross_year_positions(cont)
    open_cnt = sum(1 for r in xpos if r[3] == "open")
    print(f"跨年结转/未平仓持仓共 {len(xpos)} 笔，其中仍 open {open_cnt} 笔")
    for code, od, cd, st, qty, pnl in xpos[:20]:
        print(f"  {code}  {od} → {cd or '（open）'}  {st}  qty={qty}  pnl={pnl:,.0f}")
    if len(xpos) > 20:
        print(f"  ……（其余 {len(xpos)-20} 笔省略）")

    # V5-3 逐年复利分解
    print("\n== V5-3 逐年复利分解（连续库 vs 单年库）==")
    splits = yearly_splits(cont)
    print(f"{'年':<6}{'连续库年末':>14}{'连续库收益率':>12}{'单年库收益率':>12}{'差':>8}")
    import re
    m = re.fullmatch(r"(\d+)\.\.(\d+)", a.years)
    years = list(range(int(m.group(1)), int(m.group(2)) + 1)) if m else [int(x) for x in a.years.split(",")]
    for y in years:
        if y not in splits:
            print(f"{y:<6}{'（缺）':>14}")
            continue
        yrows = splits[y]
        y_end = yrows[-1][1]
        # 连续库该年收益率：年末/年初
        y_start = yrows[0][1] if y == years[0] else splits[y - 1][-1][1] if y - 1 in splits else yrows[0][1]
        cont_ret = y_end / y_start - 1.0
        # 单年库收益率：该库自身首末比
        single_db = BASE / f"bt_baseA_{y}.db"
        single_ret = None
        if single_db.exists():
            srows = daily_totals(single_db)
            if srows:
                single_ret = srows[-1][1] / srows[0][1] - 1.0
        sr = f"{single_ret*100:+.2f}%" if single_ret is not None else "（无单年库）"
        diff = f"{(cont_ret-single_ret)*100:+.2f}pp" if single_ret is not None else ""
        print(f"{y:<6}{y_end:>14,.0f}{cont_ret*100:>+11.2f}%{sr:>12}{diff:>8}")

    # V5-2 总账对比
    print("\n== V5-2 总账对比（连续 vs 链式）==")
    final = rows[-1][1]
    print(f"连续库终值: {final:,.0f}")
    print(f"链式口径终值: 见 base_snowball.py（事后链式，值不同是预期的——真实复利更陡）")
    print("\n→ 完整结论见 docs/50 §三 验收判据 V5。")


if __name__ == "__main__":
    raise SystemExit(main())
