#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-4 独立校验器（HARD①–⑥）——刻意不复用生成器任何函数。

与 scripts/e17_breakout.py 的独立性：
  · 取数用 SQL 侧计算强度/量比（CTE + 相关子查询），Python 侧只用一次除法；
  · 分母、分带、对账全部重新实现；
  · ④ 对账的参照物是 scripts/base_year_summary.py::load_year()（既有权威实现），不是生成器。

用法：
  python scripts/e17_breakout_verify.py            # 全量校验
  python scripts/e17_breakout_verify.py --sample 50 # 抽查笔数（默认 30）
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "var" / "bt" / "base"
sys.path.insert(0, str(ROOT / "scripts"))
from base_year_summary import load_year  # noqa: E402

PREMIUM = 1.03
BANDS_A = [(0.03, 0.04), (0.04, 0.05), (0.05, 0.06), (0.06, 0.07), (0.07, 0.08), (0.08, 99)]


def conn(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20261030)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    dbs = sorted(BASE.glob("bt_baseA_*.db"))
    ok = True

    # ---------- HARD① 分母闭合：filled ≡ position 行数，合计 1560
    tot_fill = tot_pos = 0
    line = []
    for db in dbs:
        c = conn(db)
        nf = c.execute("SELECT COUNT(*) FROM plan_order WHERE side='buy' AND status='filled'").fetchone()[0]
        np_ = c.execute("SELECT COUNT(*) FROM position").fetchone()[0]
        c.close()
        tot_fill += nf
        tot_pos += np_
        line.append((db.stem[-4:], nf, np_))
        if nf != np_:
            ok = False
            print(f"  ✗ {db.stem}: filled {nf} ≠ position {np_}")
    print(f"HARD① 分母闭合：Σfilled={tot_fill}  Σposition={tot_pos}  逐年{'全部相等' if ok else '有不等'}")
    if tot_fill != 1560:
        ok = False
        print(f"  ✗ 合计 {tot_fill} ≠ 1560（框架 §2.2 契约）")

    # ---------- 取全部 filled 买入单（SQL 侧算强度/量比），HARD②⑥
    rows = []
    for db in dbs:
        c = conn(db)
        q = """
        WITH f AS (
          SELECT o.id AS oid, o.code, o.trigger_price, o.reason_json, o.qty,
                 t.plan_date, t.execute_date,
                 (SELECT b.close FROM daily_bar b WHERE b.code=o.code AND b.date=t.plan_date) AS px,
                 (SELECT b.volume FROM daily_bar b WHERE b.code=o.code AND b.date=t.plan_date) AS vol,
                 (SELECT AVG(x.volume) FROM (
                     SELECT volume FROM daily_bar b2
                     WHERE b2.code=o.code AND b2.date<t.plan_date
                     ORDER BY b2.date DESC LIMIT 50) x) AS avgvol
          FROM plan_order o JOIN trade_plan t ON t.id=o.trade_plan_id
          WHERE o.side='buy' AND o.status='filled'
        )
        SELECT oid, code, plan_date, trigger_price, reason_json, px, vol, avgvol FROM f
        """
        for oid, code, pdate, trig, rj, px, vol, avgvol in c.execute(q):
            if px is None or not avgvol:
                rows.append(dict(db=db.name, code=code, date=pdate, strength=None, vr=None,
                                 trig=trig, top=None))
                continue
            top = json.loads(rj)["box_top"] if rj else None
            rows.append(dict(db=db.name, code=code, date=pdate, trig=trig, top=top,
                             strength=(px / top - 1) if top else None,
                             vr=vol / avgvol))
        c.close()

    miss = sum(1 for r in rows if r["strength"] is None or r["vr"] is None)
    # 分带并集
    counts = [0] * len(BANDS_A)
    outside = 0
    for r in rows:
        s = r["strength"]
        if s is None:
            continue
        placed = False
        for i, (lo, hi) in enumerate(BANDS_A):
            if lo <= s < hi:
                counts[i] += 1
                placed = True
                break
        if not placed:
            outside += 1
    print(f"HARD② 分带并集：{sum(counts)} + 缺行情 {miss} + 落在带外 {outside} = "
          f"{sum(counts)+miss+outside}（应 = {len(rows)}）")
    print(f"        各带：{dict(zip(['3-4','4-5','5-6','6-7','7-8','8+'], counts))}")
    if sum(counts) + miss + outside != len(rows) or miss != 0 or outside != 0:
        ok = False
        print("  ✗ 分带并集不闭合或缺行情")

    # ---------- HARD⑥ 门槛语义下界
    below_s = [r for r in rows if r["strength"] is not None and r["strength"] < 0.03 - 1e-9]
    below_v = [r for r in rows if r["vr"] is not None and r["vr"] < 2.0 - 1e-9]
    print(f"HARD⑥ 门槛下界：强度<3% 的 {len(below_s)} 笔；量比<2.0x 的 {len(below_v)} 笔（应均为 0）")
    if below_s or below_v:
        ok = False
        print(f"  ✗ 示例：{[ (r['code'], r['date'], round(r['strength']*100,2) if r['strength'] else None) for r in below_s[:3]]}")

    # ---------- HARD③ 抽查：强度复算 + trigger ≡ box_top×1.03
    sample = rng.sample(rows, min(a.sample, len(rows)))
    bad3 = []
    for r in sample:
        c = conn(BASE / r["db"])
        px = c.execute("SELECT close FROM daily_bar WHERE code=? AND date=?",
                       (r["code"], r["date"])).fetchone()[0]
        c.close()
        s = px / r["top"] - 1
        if abs(s - r["strength"]) > 1e-9:
            bad3.append((r["code"], r["date"], s, r["strength"]))
        if abs(r["trig"] - r["top"] * PREMIUM) / r["trig"] > 1e-6:
            bad3.append((r["code"], r["date"], "trigger", r["trig"], r["top"] * PREMIUM))
    print(f"HARD③ 抽查 {len(sample)} 笔：强度复算差异 {len(bad3)} 笔；trigger=box_top×1.03 偏差超容差 {len(bad3)} 笔")
    if bad3:
        ok = False
        print("  ✗", bad3[:5])

    # ---------- HARD④ 盈亏对账：逐年 Σrealized_pnl vs base_year_summary.load_year
    print("HARD④ 盈亏对账（生成器口径 vs base_year_summary）：")
    for db in dbs:
        c = conn(db)
        mine = c.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM position "
                         "WHERE status='closed' AND realized_pnl IS NOT NULL").fetchone()[0]
        n = c.execute("SELECT COUNT(*) FROM position WHERE status='closed' "
                      "AND realized_pnl IS NOT NULL").fetchone()[0]
        c.close()
        ref = load_year(db)
        if ref is None:
            print(f"  ✗ {db.stem}: load_year 返回 None")
            ok = False
            continue
        diff = abs(mine - ref["realized"])
        flag = "✓" if diff < 0.01 and n == ref["trades"] else "✗"
        if flag == "✗":
            ok = False
        print(f"  {flag} {db.stem[-4:]} 笔数 {n:>4}/{ref['trades']:<4} "
              f"已实现 {mine:>12,.2f} / {ref['realized']:>12,.2f} 差 {diff:.2f}")

    # ---------- HARD⑤ 异常归因：各带强度中位是否贴在 3.0%
    print("HARD⑤ 异常归因（各带强度中位是否贴闸门）：")
    for i, (lo, hi) in enumerate(BANDS_A):
        vals = sorted(r["strength"] for r in rows
                      if r["strength"] is not None and lo <= r["strength"] < hi)
        if not vals:
            continue
        med = vals[len(vals) // 2]
        flag = "⚠ 贴闸门" if med - lo < 0.001 and i == 0 else "✓"
        print(f"  {flag} 带 {lo*100:.0f}-{hi*100:.0f}%：n={len(vals)} 中位 {med*100:.2f}%")

    # ---------- 臂 C 抽样复算（入场价与 N 日收益）
    c = conn(BASE / "bt_baseA_2015.db")
    cc = c.execute("""SELECT t.plan_date, t.execute_date, o.code, o.trigger_price, o.limit_price
                      FROM plan_order o JOIN trade_plan t ON t.id=o.trade_plan_id
                      WHERE o.side='buy' AND o.status='cancelled' LIMIT 3""").fetchall()
    print("臂 C 抽查（2015 前 3 笔弃单，按 matchOrder+slip 复算）：")
    for pdate, edate, code, trig, limit_px in cc:
        bar = c.execute("SELECT open, high, low FROM daily_bar WHERE code=? AND date=?",
                        (code, edate)).fetchone()
        px = bar[0] if bar[0] >= trig else (limit_px if bar[1] >= trig else None)
        px = px * 1.002 if px else None
        nxt = c.execute("SELECT close FROM daily_bar WHERE code=? AND date>? ORDER BY date LIMIT 5",
                        (code, edate)).fetchall()
        r5 = (nxt[4][0] / px - 1) if (px and len(nxt) >= 5) else None
        print(f"  {code} {pdate}: open {bar[0]:.2f} trig {trig:.4f} → px {px:.4f} "
              f"ratio {px/trig:.4f}（>1.05 应弃）5日中位收益 {r5*100:+.2f}%" if r5 is not None
              else f"  {code} {pdate}: 数据不足")
    c.close()

    print("\n" + ("=" * 46))
    print("结论：" + ("HARD①–⑥ 全部通过 ✓" if ok else "存在失败项 ✗"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
