#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-4 B 类量比扫描执行器（docs/29 §七 P1-4 B 类，2026-10-30 立项）。

只动 `box.volmult` 一个变量（1.0 / 1.5 / 2.0 基线），其余参数 100% 复刻新基线
（var/bt/base/bt_baseA_<year>.db 的 38 键黄金参照）。源库 = full_hist_10pct.db，
二进制 = darvas_base.exe，模式 = regime（regime 模式才真正应用 --set 参数覆盖）。

用法：
  python scripts/e17_volmult_run.py --dry-run
  python scripts/e17_volmult_run.py --years 2012 --volmult 1.0      # 冒烟：单档单年
  python scripts/e17_volmult_run.py --years 2012,2015,2017,2018 --volmult 1.0,1.5,2.0
"""
import argparse
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
OUT = BT / "volmult"          # 本次扫描库输出目录
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_base.exe"

# 基线共同参数（100% 复刻 base_run.py COMMON，只把 box.volmult 换成扫描值）
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

# 扫描档位（box.volmult）
VOLMULT_TIERS = ["1.0", "1.5", "2.0"]  # 2.0 = 基线


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, path=None):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    if path:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def setargs(volmult):
    p = dict(COMMON)
    p["box.volmult"] = volmult
    return ",".join(f"{k}={v}" for k, v in sorted(p.items()))


def db_complete(path):
    """库是否完整：存在 + equity_snapshot 非空（base_run.py 同款判据）。"""
    if not path.exists():
        return False
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return c.execute("SELECT COUNT(*) FROM equity_snapshot").fetchone()[0] > 0
        finally:
            c.close()
    except Exception:
        return False


def run_one(year, volmult, force=False):
    db = OUT / f"bt_volmult_{volmult}_{year}.db"
    logp = OUT / f"bt_volmult_{volmult}_{year}.log"
    if not force and db_complete(db):
        log(f"跳过（已完整）：{db.name}", logp)
        return 0
    if db.exists() and not force:
        log(f"发现不完整库，删除重跑：{db.name}", logp)
        for ext in ("", "-wal", "-shm"):
            p = Path(str(db) + ext)
            if p.exists():
                p.unlink()
    sa = setargs(volmult)
    cmd = [str(EXE), "backtest", "--src", str(SRC),
           "--from", f"{year}-01-01", "--to", f"{year}-12-31",
           "--mode", "regime", "--db", str(db), "--set", sa]
    log(f"START {year} volmult={volmult} → {db.name}", logp)
    log(f"  set: {sa}", logp)
    t0 = time.time()
    with logp.open("a", encoding="utf-8") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time() - t0
    if rc != 0:
        log(f"FAILED {year} volmult={volmult} rc={rc} 耗时 {dt/60:.1f}min", logp)
        return rc
    log(f"DONE {year} volmult={volmult} 耗时 {dt/60:.1f}min", logp)
    return 0


def parse_years(spec):
    out = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ".." in tok:
            a, b = tok.split("..", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(tok))
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2012",
                    help="逗号分隔或区间，如 2012,2015,2017,2018 或 2012..2018")
    ap.add_argument("--volmult", default="1.0",
                    help="逗号分隔档位，如 1.0,1.5,2.0（2.0=基线）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    years = parse_years(a.years)
    tiers = [t.strip() for t in a.volmult.split(",")]
    for t in tiers:
        if t not in VOLMULT_TIERS:
            raise SystemExit(f"未知档位 {t}，可选 {VOLMULT_TIERS}")
    OUT.mkdir(parents=True, exist_ok=True)
    if not EXE.exists():
        raise SystemExit(f"二进制不存在：{EXE}")
    if a.dry_run:
        print(f"二进制 {EXE.name}  源库 {SRC.name}")
        for y in years:
            for t in tiers:
                print(f"\n{y} volmult={t}")
                for kv in setargs(t).split(","):
                    print(f"   {kv}")
        return 0
    total = len(years) * len(tiers)
    i = 0
    for y in years:
        for t in tiers:
            i += 1
            print(f"\n=== [{i}/{total}] {y} volmult={t} ===", flush=True)
            if run_one(y, t, a.force) != 0:
                print(f"失败即停：{y} volmult={t}", flush=True)
                return 1
    print("\n全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
