#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""0032 切表冒烟对账（docs/53-data-spec-new-baseline.md §8.7 判据⑧ + 切表中性）。

两个冒烟，一个脚本：
  1. **raw 模式（切表中性）**：新二进制 + raw 源库跑 2005 单年，对 verify_0029_2005.db
     做表行数 + 成交/权益/腿 md5 对账——必须逐分一致，证明 0032 切表没有扰动 raw 行为。
  2. **adj 模式（判据⑧）**：同一参数打 adj 源库（须已 adj_ready=1），断言
     sim:dividend 成交数 = 0（除权清仓已随 adj 模式下线，match.go 0032 豁免）；
     账本与 raw 不同为预期（后复权口径），只报告不判失败。

用法：
  python scripts/smoke_0032.py --exe bin/darvas-dev.exe            # 两个冒烟都跑
  python scripts/smoke_0032.py --exe bin/darvas-dev.exe --skip-raw
退出码：0 = 跑的冒烟全过；1 = 有失败。
"""
import argparse
import hashlib
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
RAW_SRC = BT / "full_hist_10pct.db"
ADJ_SRC = BT / "full_hist_adj.db"
REF = BT / "verify_0029_2005.db"

# 与 scripts/base_run.py COMMON 严格一致（注入项写死、与 migration 0025 一致）。
COMMON = {
    "pos.newhigh_ladder": "on",
    "pos.newhigh.lookback": "252",
    "pos.newhigh.cutoff": "40",
    "pos.newhigh.units.t1": "3",
    "pos.newhigh.units.t2": "2",
    "pos.newhigh.units.t3": "1",
    "pos.newhigh.units.t4": "0.5",
    "pool.max_hold": "14",
    "pos.equity_gate": "off",
    "gate.ma60.mode": "stop",
    "gate.ma60.index": "sh000001",
    "position.staledays": "2",
}
SETARGS = ",".join(f"{k}={v}" for k, v in sorted(COMMON.items()))

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}：{detail}")


def run_year(exe: Path, src: Path, scratch: Path, year: int) -> int:
    for ext in ("", "-wal", "-shm"):
        p = Path(str(scratch) + ext)
        if p.exists():
            p.unlink()
    cmd = [str(exe), "backtest", "--src", str(src),
           "--from", f"{year}-01-01", "--to", f"{year}-12-31",
           "--mode", "regime", "--db", str(scratch), "--set", SETARGS]
    print(f"  回放：{' '.join(cmd)}")
    t0 = time.time()
    with open(str(scratch) + ".log", "w", encoding="utf-8") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    print(f"  rc={rc} 耗时 {(time.time()-t0)/60:.1f} min（日志 {scratch.name}.log）")
    return rc


def md5_rows(db: sqlite3.Connection, sql: str) -> str:
    h = hashlib.md5()
    for row in db.execute(sql):
        h.update(repr(tuple(row)).encode())
        h.update(b"\n")
    return h.hexdigest()


def table_count(db: sqlite3.Connection, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def smoke_raw(exe: Path, year: int, reuse: bool) -> None:
    print(f"\n== RAW 冒烟（切表中性）：{year} vs {REF.name} ==")
    scratch = BT / f"smoke_0032_raw_{year}.db"
    if not (reuse and scratch.exists()):
        if run_year(exe, RAW_SRC, scratch, year) != 0:
            check("raw 回放 rc=0", False, f"见 {scratch.name}.log")
            return
    a = sqlite3.connect(f"file:{scratch}?mode=ro", uri=True)
    b = sqlite3.connect(f"file:{REF}?mode=ro", uri=True)
    # 只比对两库共有的表（0032 新增 daily_bar_adj/data_meta 不在参照库属预期）
    common = set(r[0] for r in a.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")).intersection(
        r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    for t in ("trade_fill", "plan_order", "trade_plan", "position_leg", "position",
              "equity_snapshot", "signal", "box", "watch"):
        if t not in common:
            check(f"表行数 {t}", False, "两库共有表中不存在")
            continue
        ca, cb = table_count(a, t), table_count(b, t)
        check(f"表行数 {t}", ca == cb, f"新={ca:,} 参照={cb:,}")
    fa = md5_rows(a, "SELECT date,code,side,price,qty,fee,raw_text FROM trade_fill ORDER BY date,code,side,price,qty")
    fb = md5_rows(b, "SELECT date,code,side,price,qty,fee,raw_text FROM trade_fill ORDER BY date,code,side,price,qty")
    check("trade_fill 逐分 md5", fa == fb, f"新={fa[:16]}… 参照={fb[:16]}…")
    ea = md5_rows(a, "SELECT date, cash, market_value, total, peak_total, drawdown FROM equity_snapshot ORDER BY date")
    eb = md5_rows(b, "SELECT date, cash, market_value, total, peak_total, drawdown FROM equity_snapshot ORDER BY date")
    check("equity_snapshot 逐日 md5", ea == eb, f"新={ea[:16]}… 参照={eb[:16]}…")
    la = md5_rows(a, "SELECT position_id, leg_no, date, qty, price, stop_after FROM position_leg ORDER BY position_id, leg_no")
    lb = md5_rows(b, "SELECT position_id, leg_no, date, qty, price, stop_after FROM position_leg ORDER BY position_id, leg_no")
    check("position_leg md5", la == lb, f"新={la[:16]}… 参照={lb[:16]}…")
    a.close()
    b.close()


def smoke_adj(exe: Path, year: int, reuse: bool) -> None:
    print(f"\n== ADJ 冒烟（判据⑧）：{year} @ {ADJ_SRC.name} ==")
    if not ADJ_SRC.exists():
        check("adj 源库存在", False, str(ADJ_SRC))
        return
    probe = sqlite3.connect(f"file:{ADJ_SRC}?mode=ro", uri=True)
    ready = probe.execute(
        "SELECT v FROM data_meta WHERE k='adj_ready'").fetchone()
    probe.close()
    if not ready or ready[0] != "1":
        check("adj_ready=1", False, f"当前={ready[0] if ready else '无行'}——先跑 verify_bar_adj.py --apply-ready")
        return
    check("adj_ready=1", True, "源库已验收")
    scratch = BT / f"smoke_0032_adj_{year}.db"
    if not (reuse and scratch.exists()):
        if run_year(exe, ADJ_SRC, scratch, year) != 0:
            check("adj 回放 rc=0", False, f"见 {scratch.name}.log")
            return
    a = sqlite3.connect(f"file:{scratch}?mode=ro", uri=True)
    mode = a.execute(
        "SELECT data_mode FROM baseline ORDER BY id DESC LIMIT 1").fetchone()
    check("baseline.data_mode='adj'", bool(mode and mode[0] == "adj"),
          f"当前={mode[0] if mode else '无基线行'}")
    n_div = a.execute(
        "SELECT COUNT(*) FROM trade_fill WHERE raw_text='sim:dividend'").fetchone()[0]
    check("⑧ sim:dividend 成交数 = 0", n_div == 0, f"实际 {n_div}")
    n_fill = a.execute("SELECT COUNT(*) FROM trade_fill").fetchone()[0]
    eq = a.execute(
        "SELECT total FROM equity_snapshot ORDER BY date DESC LIMIT 1").fetchone()
    print(f"  参考：成交 {n_fill:,} 笔，年末权益 {eq[0]:,.0f}"
          f"（与 raw 不同为预期——后复权口径，不判失败）")
    a.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="0032 切表冒烟对账")
    ap.add_argument("--exe", default=str(ROOT / "bin" / "darvas-dev.exe"))
    ap.add_argument("--year", type=int, default=2005)
    ap.add_argument("--skip-raw", action="store_true")
    ap.add_argument("--skip-adj", action="store_true")
    ap.add_argument("--reuse", action="store_true",
                    help="scratch 库已存在则跳过重放，只做对账（省 35 分钟）")
    args = ap.parse_args()
    exe = Path(args.exe)
    if not exe.exists():
        print(f"二进制不存在：{exe}")
        return 1

    if not args.skip_raw:
        smoke_raw(exe, args.year, args.reuse)
    if not args.skip_adj:
        smoke_adj(exe, args.year, args.reuse)

    failed = [r for r in results if not r[1]]
    print(f"\n冒烟判据 {len(results)-len(failed)}/{len(results)} 通过"
          + (f"，失败：{[r[0] for r in failed]}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
