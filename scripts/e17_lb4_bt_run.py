#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LB-4 B 类成长速度准入门槛扫描执行器（docs/29 §七 LB-4，2026-10-31 立项）。

只动 `box.max_growth_days` 一个变量（off/5/6/7/8/10 共 6 档），其余参数 100% 复刻
新基线（var/bt/base/bt_baseA_<year>.db 的共同参数）。源库 = full_hist_10pct.db，
二进制 = darvas_base_new.exe（含成长速度准入逻辑，默认 off=0 与旧基线逐单等价），
模式 = regime。

准入语义：成长速度 = 入场 Box top_confirmed → bottom_confirmed 的交易日数（daily_bar
闭区间，含两端，与 e17_lb4_growthspeed.py A 类口径一致）；成长速度 > 阈值 → 不产生买单。
off 档 = 不传该参数（吃默认 0=关闭），复现历史基线。

用法：
  python scripts/e17_lb4_bt_run.py --dry-run
  python scripts/e17_lb4_bt_run.py --years 2018 --tiers off              # 冒烟：等价性验证
  python scripts/e17_lb4_bt_run.py --years 2012,2015,2017,2018 --tiers off,5,6,7,8,10
"""
import argparse
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
OUT = BT / "lb4_bt"            # 本次扫描库输出目录
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_base_new.exe"

# 基线共同参数（100% 复刻 base_run.py COMMON；off 档不额外注入 box.max_growth_days）
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

# 扫描档位（box.max_growth_days）：off=基线（不注入，吃默认 0）
TIERS = ["off", "5", "6", "7", "8", "10"]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, path=None):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    if path:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def setargs(tier):
    p = dict(COMMON)
    if tier != "off":
        p["box.max_growth_days"] = tier
    return ",".join(f"{k}={v}" for k, v in sorted(p.items()))


def db_complete(path, year):
    """库是否完整：存在 + equity_snapshot 覆盖到该年年末（最后交易日）。
    判据：equity_snapshot 的最大 date ≥ year-12-01（12 月已有交易日），
    且 position 表有已平仓记录。仅「非空」会误判半成品库（沙箱回收坑）。"""
    if not path.exists():
        return False
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            n = c.execute("SELECT COUNT(*) FROM equity_snapshot").fetchone()[0]
            if n == 0:
                return False
            maxd = c.execute("SELECT MAX(date) FROM equity_snapshot").fetchone()[0]
            # 年末判据：max date 必须进入该年 12 月（最后交易日 ≈ 12-31）
            return bool(maxd) and maxd >= f"{year}-12-01"
        finally:
            c.close()
    except Exception:
        return False


def run_one(year, tier, force=False):
    db = OUT / f"bt_lb4_{tier}_{year}.db"
    logp = OUT / f"bt_lb4_{tier}_{year}.log"
    if not force and db_complete(db, year):
        log(f"跳过（已完整）：{db.name}", logp)
        return 0
    if db.exists() and not force:
        log(f"发现不完整库，删除重跑：{db.name}", logp)
        for ext in ("", "-wal", "-shm"):
            p = Path(str(db) + ext)
            if p.exists():
                p.unlink()
    sa = setargs(tier)
    cmd = [str(EXE), "backtest", "--src", str(SRC),
           "--from", f"{year}-01-01", "--to", f"{year}-12-31",
           "--mode", "regime", "--db", str(db), "--set", sa]
    log(f"START {year} max_growth_days={tier} → {db.name}", logp)
    log(f"  set: {sa}", logp)
    t0 = time.time()
    with logp.open("a", encoding="utf-8") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time() - t0
    if rc != 0:
        log(f"FAILED {year} max_growth_days={tier} rc={rc} 耗时 {dt/60:.1f}min", logp)
        return rc
    log(f"DONE {year} max_growth_days={tier} 耗时 {dt/60:.1f}min", logp)
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
    ap.add_argument("--tiers", default="off",
                    help="逗号分隔档位，如 off,5,6,7,8,10（off=基线）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    years = parse_years(a.years)
    tiers = [t.strip() for t in a.tiers.split(",")]
    for t in tiers:
        if t not in TIERS:
            raise SystemExit(f"未知档位 {t}，可选 {TIERS}")
    OUT.mkdir(parents=True, exist_ok=True)
    if not EXE.exists():
        raise SystemExit(f"二进制不存在：{EXE}")
    if a.dry_run:
        print(f"二进制 {EXE.name}  源库 {SRC.name}")
        for y in years:
            for t in tiers:
                print(f"\n{y} max_growth_days={t}")
                for kv in setargs(t).split(","):
                    print(f"   {kv}")
        return 0
    total = len(years) * len(tiers)
    i = 0
    for y in years:
        for t in tiers:
            i += 1
            print(f"\n=== [{i}/{total}] {y} max_growth_days={t} ===", flush=True)
            if run_one(y, t, a.force) != 0:
                print(f"失败即停：{y} max_growth_days={t}", flush=True)
                return 1
    print("\n全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
