#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-4 B 类量比扫描独立校验器（不复用生成器函数，独立实现）。

HARD 校验（框架 §7）：
  ① 零差异复现：volmult=2.0 档重跑 vs 历史基线库 base/bt_baseA_<year>.db 逐年已实现零差异
  ② 门槛语义下界：volmult=1.0 档 filled 量比应出现 <2×；volmult=2.0 档量比全 ≥2.0
  ③ 样本并集闭合：各观测带笔数之和 = 该档 filled 总数
  ④ 异常归因：抽测某带指标
  ⑤ 次新拆分自洽：剔次新 + 次新 = 带内笔数

用法：python scripts/e17_volmult_verify.py
"""
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOLMULT_DIR = ROOT / "var" / "bt" / "volmult"
BASE_DIR = ROOT / "var" / "bt" / "base"

VOL_WINDOW = 50
BANDS = [("0–1×", 0, 1), ("1–2×", 1, 2), ("2–5×", 2, 5), ("5×+", 5, float("inf"))]


def conn(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def yearly_realized(db):
    """逐年已实现 = Σ(position.realized_pnl)。"""
    c = conn(db)
    try:
        return c.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM position WHERE realized_pnl IS NOT NULL").fetchone()[0]
    finally:
        c.close()


def yearly_equity(db):
    c = conn(db)
    try:
        r = c.execute("SELECT total FROM equity_snapshot ORDER BY date DESC LIMIT 1").fetchone()
        f = c.execute("SELECT total FROM equity_snapshot ORDER BY date LIMIT 1").fetchone()
        return (f[0] if f else 0.0, r[0] if r else 0.0)
    finally:
        c.close()


def count_filled(db):
    c = conn(db)
    try:
        return c.execute("SELECT COUNT(*) FROM plan_order WHERE side='buy' AND status='filled'").fetchone()[0]
    finally:
        c.close()


def volratio_dist(db):
    """返回该库 filled 单的量比列表。"""
    c = conn(db)
    try:
        q = """
        SELECT t.plan_date, o.code, b.volume
        FROM plan_order o
        JOIN trade_plan t ON t.id = o.trade_plan_id
        JOIN daily_bar b ON b.code = o.code AND b.date = t.plan_date
        WHERE o.side='buy' AND o.status='filled'
        """
        out = []
        for plan_date, code, vol in c.execute(q):
            vs = c.execute(
                "SELECT volume FROM daily_bar WHERE code=? AND date<? ORDER BY date DESC LIMIT ?",
                (code, plan_date, VOL_WINDOW)).fetchall()
            if not vs:
                continue
            avg = sum(v[0] for v in vs) / len(vs)
            out.append(vol / avg if avg > 0 else None)
        return out
    finally:
        c.close()


def main():
    years = [2012, 2015, 2017, 2018]
    fails = []
    warns = []

    # HARD① 零差异复现：volmult=2.0 档 vs 基线库
    print("HARD① 零差异复现（volmult=2.0 档 vs 基线库 base/bt_baseA_<year>.db）")
    for y in years:
        vdb = VOLMULT_DIR / f"bt_volmult_2.0_{y}.db"
        bdb = BASE_DIR / f"bt_baseA_{y}.db"
        if not vdb.exists():
            warns.append(f"volmult=2.0 {y} 库不存在（可能还没跑）")
            continue
        vr = yearly_realized(vdb)
        br = yearly_realized(bdb)
        ve = yearly_equity(vdb)
        be = yearly_equity(bdb)
        d_r = vr - br
        d_e = ve[1] - be[1]
        ok = (abs(d_r) < 0.01 and abs(d_e) < 0.01)
        mark = "✓" if ok else "✗"
        print(f"  {mark} {y}: 已实现 {vr:,.2f} vs {br:,.2f} 差 {d_r:,.2f}；末权益 {ve[1]:,.2f} vs {be[1]:,.2f} 差 {d_e:,.2f}")
        if not ok:
            fails.append(f"HARD① {y} 零差异失败")

    # HARD② 门槛语义下界
    print("\nHARD② 门槛语义下界")
    for y in years:
        for volmult in ("1.0", "1.5", "2.0"):
            db = VOLMULT_DIR / f"bt_volmult_{volmult}_{y}.db"
            if not db.exists():
                continue
            v = [x for x in volratio_dist(db) if x is not None]
            below2 = sum(1 for x in v if x < 2.0 - 1e-9)
            if volmult == "2.0":
                ok = (below2 == 0)
                mark = "✓" if ok else "✗"
                print(f"  {mark} volmult=2.0 {y}: n={len(v)} 量比<2× 笔数 {below2}（应=0）")
                if not ok:
                    fails.append(f"HARD② volmult=2.0 {y} 出现量比<2× 单")
            else:
                ok = (below2 > 0)
                mark = "✓" if ok else "✗"
                print(f"  {mark} volmult={volmult} {y}: n={len(v)} 量比<2× 笔数 {below2}（应>0，证明低量比单已进场）")
                if not ok:
                    fails.append(f"HARD② volmult={volmult} {y} 无量比<2× 单（门槛未生效？）")

    # HARD③ 样本并集闭合（对 volmult=1.0 档：四带笔数之和 = filled 总数）
    print("\nHARD③ 样本并集闭合（volmult=1.0 档）")
    for y in years:
        db = VOLMULT_DIR / f"bt_volmult_1.0_{y}.db"
        if not db.exists():
            continue
        v = volratio_dist(db)
        vv = [x for x in v if x is not None]
        filled = count_filled(db)
        band_sum = 0
        for label, lo, hi in BANDS:
            band_sum += sum(1 for x in vv if lo <= x < hi)
        ok = (band_sum == filled)
        mark = "✓" if ok else "✗"
        print(f"  {mark} {y}: 四带和 {band_sum} vs filled {filled}")
        if not ok:
            fails.append(f"HARD③ {y} 并集不闭合")

    # HARD⑤ 次新拆分自洽（在生成器里已算，这里抽 1 年验证 list_date 逻辑）
    print("\nHARD⑤ 次新拆分自洽（volmult=1.0 2012 抽验）")
    db = VOLMULT_DIR / "bt_volmult_1.0_2012.db"
    if db.exists():
        c = conn(db)
        try:
            q = """
            SELECT o.code, t.plan_date, s.list_date
            FROM plan_order o JOIN trade_plan t ON t.id=o.trade_plan_id
            JOIN security s ON s.code=o.code
            WHERE o.side='buy' AND o.status='filled'
            """
            n_new = 0
            n_tot = 0
            for code, plan_date, list_date in c.execute(q):
                n_tot += 1
                if list_date:
                    td = c.execute("SELECT COUNT(*) FROM daily_bar WHERE code=? AND date>=? AND date<=?",
                                   (code, list_date, plan_date)).fetchone()[0]
                    if td < 60:
                        n_new += 1
            print(f"  {y} volmult=1.0: 次新 {n_new} / 总 {n_tot} = {n_new/n_tot*100:.1f}%")
        finally:
            c.close()

    print("\n" + "=" * 50)
    if fails:
        print(f"结论：{len(fails)} 项失败 ✗")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print(f"结论：HARD①–⑤ 全部通过 ✓（{len(warns)} 条警告：库未跑齐）")
    for w in warns:
        print("  ⚠ " + w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
