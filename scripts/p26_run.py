#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-6a `position.staledays` 梯度回测执行器。

设计要点：
- 参数不手抄：全部从历史基线库 bt_e17_<year>.db 的 param 表读取，
  只覆盖 position.staledays —— 保证与历史基线**同源、单变量**。
- 每次跑独立 scratch 库 var/bt/p26/p26_sd<N>_<year>.db，绝不覆盖历史基线库。
- 串行执行（回测并发会互相拖慢且污染 CPU 计时），失败即停。
- 源库只读；生产库/实时库不碰。

用法：
  python scripts/p26_run.py --years 2012 --staledays 2
  python scripts/p26_run.py --years 2012,2005 --staledays 0,2,3,5,10
  python scripts/p26_run.py --years 2012 --staledays 2 --dry-run
"""
import argparse
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
OUT = BT / "p26"
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_e17.exe"

# 只覆盖历史命令实际传过的阶梯参数（run_e17.bat 的 SETARGS），其余交给 migration 种子。
# 理由：历史库 param 表里混有引擎运行时回写的状态值（如 regime.bear_thresh），
# 回灌会把「运行后状态」当初始值，引入未登记变量。单变量对照必须复刻原命令。
PARAM_PREFIXES = ("pos.newhigh",)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, path=None):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    if path:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def baseline_params(year):
    """从历史基线库读参数（它是唯一事实源）。"""
    db = BT / f"bt_e17_{year}.db"
    if not db.exists():
        raise SystemExit(f"历史基线库不存在：{db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT key, value FROM param").fetchall()
    finally:
        con.close()
    return {k: v for k, v in rows if k.startswith(PARAM_PREFIXES)}


def run_one(year, stale, force=False):
    db = OUT / f"p26_sd{stale}_{year}.db"
    logp = OUT / f"p26_sd{stale}_{year}.log"
    if db.exists() and not force:
        log(f"跳过（已存在）：{db.name}", logp)
        return 0
    params = baseline_params(year)
    base_stale = params.get("position.staledays", "?")
    params["position.staledays"] = str(stale)
    setargs = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
    cmd = [str(EXE), "backtest", "--src", str(SRC),
           "--from", f"{year}-01-01", "--to", f"{year}-12-31",
           "--mode", "regime", "--db", str(db), "--set", setargs]
    log(f"START {year} staledays={stale}（基线 {base_stale}）→ {db.name}", logp)
    log(f"  set: {setargs}", logp)
    t0 = time.time()
    with logp.open("a", encoding="utf-8") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time() - t0
    if rc != 0:
        log(f"FAILED {year} staledays={stale} rc={rc} 耗时 {dt/60:.1f}min", logp)
        return rc
    log(f"DONE {year} staledays={stale} 耗时 {dt/60:.1f}min", logp)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", required=True)
    ap.add_argument("--staledays", required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    years = [int(x) for x in a.years.split(",")]
    stales = [int(x) for x in a.staledays.split(",")]
    OUT.mkdir(parents=True, exist_ok=True)
    if not EXE.exists():
        raise SystemExit(f"二进制不存在：{EXE}")
    if a.dry_run:
        for y in years:
            p = baseline_params(y)
            print(f"{y} 基线 staledays={p.get('position.staledays')} 共 {len(p)} 个参数")
            for k in sorted(p):
                print(f"   {k} = {p[k]}")
        return 0
    fails = []
    for y in years:
        for s in stales:
            if run_one(y, s, a.force) != 0:
                fails.append((y, s))
    if fails:
        print(f"\n失败组合：{fails}")
        return 1
    print("\n全部完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
