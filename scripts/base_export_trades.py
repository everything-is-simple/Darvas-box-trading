#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把新基线回测年库的逐笔明细导出成 CSV（只读，零写库）。

为什么要有这个：年库 bt_baseA_*.db 每个 4.4 GB，14 年 = 62 GB，搬机器时没法带。
但逐笔明细只有几百 KB，带走它 = 带走了全部可复核证据，不必为了看结果重跑整年。

口径（与 scripts/base_year_summary.py 严格一致，勿改）：
  - 成本 = Σ(position_leg.qty × price)：position.qty 平仓后清零（P2-5 踩坑）
  - 1 单位 = 建仓日账户权益的 2%（migration 0009）
  - 只导 status='closed' 且 realized_pnl 非空
  - 持有时长 = equity_snapshot 交易日序号差（不是自然日）

用法：
  python scripts/base_export_trades.py                 # 导出全部可用年库
  python scripts/base_export_trades.py --years 2005,2007
  python scripts/base_export_trades.py --check         # 只校验与 summary 对账，不写文件
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "var" / "bt" / "base"
OUTDIR = BASE / "trades_csv"

COLS = [
    "year", "code", "name", "opened_date", "closed_date", "hold_days",
    "qty_total", "cost", "avg_entry", "exit_px_implied",
    "units", "pnl", "pnl_pct", "close_reason",
]


def year_ok(c: sqlite3.Connection) -> bool:
    """完成判据：equity_snapshot 最后一天 ≥ 当年 12-25（同 base_watch.py；12-15 太松，会误判）。"""
    r = c.execute("SELECT MAX(date) FROM equity_snapshot").fetchone()
    return bool(r and r[0] and r[0][5:] >= "12-25")


def export(db: Path) -> dict | None:
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception as e:
        print(f"  !! 打不开 {db.name}: {e}")
        return None
    try:
        if not year_ok(c):
            print(f"  .. 跳过 {db.name}：未跑完（数据未到 12-15）")
            return None

        eq = c.execute("SELECT date, total FROM equity_snapshot ORDER BY date").fetchall()
        idx = {d: i for i, (d, _) in enumerate(eq)}
        eq_by_date = {d: t for d, t in eq}

        names = {}
        try:
            for code, name in c.execute("SELECT code, name FROM security"):
                names[code] = name
        except Exception:
            pass

        rows = c.execute(
            """SELECT p.id, p.code, p.opened_date, p.closed_date, p.close_reason,
                      p.realized_pnl,
                      COALESCE(SUM(l.qty), 0)      AS qty_total,
                      COALESCE(SUM(l.qty*l.price),0) AS cost
               FROM position p
               LEFT JOIN position_leg l ON l.position_id = p.id
               WHERE p.realized_pnl IS NOT NULL AND p.status = 'closed'
               GROUP BY p.id
               ORDER BY p.opened_date, p.id"""
        ).fetchall()

        year = db.stem.split("_")[-1]
        out = []
        for pid, code, od, cd, reason, pnl, qty, cost in rows:
            avg_entry = (cost / qty) if qty else 0.0
            base_eq = eq_by_date.get(od) or (eq[0][1] if eq else 0.0)
            unit_amt = base_eq * 0.02
            units = (cost / unit_amt) if unit_amt else 0.0
            hold = (idx[cd] - idx[od] + 1) if (od in idx and cd in idx) else ""
            exit_px = avg_entry + (pnl / qty) if qty else 0.0
            pnl_pct = (pnl / cost * 100.0) if cost else 0.0
            out.append({
                "year": year, "code": code, "name": names.get(code, ""),
                "opened_date": od, "closed_date": cd or "",
                "hold_days": hold,
                "qty_total": round(qty, 2), "cost": round(cost, 2),
                "avg_entry": round(avg_entry, 4),
                "exit_px_implied": round(exit_px, 4),
                "units": round(units, 3),
                "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                "close_reason": reason or "",
            })

        if not out:
            print(f"  .. 跳过 {db.name}：零平仓笔")
            return None

        OUTDIR.mkdir(parents=True, exist_ok=True)
        fp = OUTDIR / f"trades_{year}.csv"
        with fp.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            w.writerows(out)

        net = sum(r["pnl"] for r in out)
        print(f"  OK {year}: {len(out)} 笔 → {fp.name}  合计 {net:,.0f}")
        return {"year": year, "trades": len(out), "net": net, "file": fp.name}
    finally:
        c.close()


def identity(db: Path) -> dict | None:
    """核对恒等式：权益变动 = 已实现 + 年末未平仓浮盈 + 残差（docs/29 §2.5.2）。

    残差 = 未计入 realized_pnl 的佣金/印花税，恒为负、量级约 0.007%。
    """
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception:
        return None
    try:
        if not year_ok(c):
            return None
        eq = c.execute("SELECT date, total FROM equity_snapshot ORDER BY date").fetchall()
        if not eq:
            return None
        last = eq[-1][0]
        d = eq[-1][1] - eq[0][1]
        real = c.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM position "
            "WHERE realized_pnl IS NOT NULL AND status='closed'"
        ).fetchone()[0]
        rows = c.execute(
            """SELECT p.code, COALESCE(SUM(l.qty),0), COALESCE(SUM(l.qty*l.price),0)
               FROM position p LEFT JOIN position_leg l ON l.position_id = p.id
               WHERE p.status != 'closed' GROUP BY p.id"""
        ).fetchall()
        unreal = 0.0
        for code, qty, cost in rows:
            px = c.execute(
                "SELECT close FROM daily_bar WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
                (code, last),
            ).fetchone()
            if px and px[0] is not None:
                unreal += qty * px[0] - cost
        return {"year": db.stem.split("_")[-1], "delta": d, "realized": real,
                "unrealized": unreal, "residual": d - real - unreal, "open_n": len(rows)}
    finally:
        c.close()


def print_identity(dbs: list[Path]) -> None:
    print(f"{'年':<6}{'权益变动':>13}{'已实现':>13}{'年末浮盈':>12}{'残差':>9}{'未平仓':>7}")
    print("-" * 62)
    for db in dbs:
        r = identity(db)
        if not r:
            continue
        print(f"{r['year']:<6}{r['delta']:>13,.0f}{r['realized']:>13,.0f}"
              f"{r['unrealized']:>12,.0f}{r['residual']:>9,.1f}{r['open_n']:>7}")
    print("\n口径：权益变动 = 已实现 + 年末浮盈 + 残差（残差=未计入 realized_pnl 的费用）")
    print("详见 docs/29 §2.5.2 —— 引用「年净利」必须注明是哪一个")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="")
    ap.add_argument("--check", action="store_true", help="只对账，不写文件")
    ap.add_argument("--identity", action="store_true",
                    help="只打印「权益变动 = 已实现 + 年末浮盈 + 残差」恒等式核对表")
    a = ap.parse_args()

    want = [y.strip() for y in a.years.split(",") if y.strip()]
    dbs = sorted(BASE.glob("bt_baseA_*.db"))
    if want:
        dbs = [d for d in dbs if d.stem.split("_")[-1] in want]
    if not dbs:
        print("没有找到年库（var/bt/base/bt_baseA_*.db）")
        return 1

    if a.identity:
        print_identity(dbs)
        return 0

    print(f"扫描 {len(dbs)} 个年库，输出到 {OUTDIR}")
    done = []
    for db in dbs:
        r = export(db)
        if r:
            done.append(r)

    if not done:
        print("\n没有任何跑完的年份可用。")
        return 1

    tot = sum(d["net"] for d in done)
    print(f"\n合计 {len(done)} 年 / {sum(d['trades'] for d in done)} 笔 / 算术和 {tot:,.0f}")
    print("（逐年净利相加 ≠ 复利，复利要连乘 last/first，见 base_progress.md §六）")

    if a.check:
        js = BASE / "year_json"
        print("\n与 year_json 对账：")
        bad = 0
        for d in done:
            jf = js / f"bt_baseA_{d['year']}.json"
            if not jf.exists():
                print(f"  ?? {d['year']} 无 json")
                continue
            j = json.loads(jf.read_text(encoding="utf-8"))
            n_ok = j.get("trades") == d["trades"]
            p_ok = abs(j.get("net", 0) - d["net"]) < 1.0
            flag = "OK" if (n_ok and p_ok) else "MISMATCH"
            if not (n_ok and p_ok):
                bad += 1
            print(f"  {flag} {d['year']}: 笔数 {d['trades']} vs {j.get('trades')} | "
                  f"净利 {d['net']:,.0f} vs {j.get('net', 0):,.0f}")
        print("对账通过" if bad == 0 else f"有 {bad} 年不一致，需查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
