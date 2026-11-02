#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T3 `sell.discount` 专项梯度扫描执行器（docs/29 §七 T3，2026-11-02 立项）。

只动 `sell.discount` 一个变量（0.95 / 0.97 / 0.99 三档），其余参数 100% 复刻
新基线（var/bt/base/bt_baseA_<year>.db 的共同参数）。源库 = full_hist_10pct.db，
二进制 = darvas_t3.exe（= 含 T2 追踪链改动的当前 HEAD 编译产物，写 reason.box_id +
plan_order.source_signal_id；不改变任何交易决策），模式 = regime。

为什么必须重跑 0.97 档而不直接复用 bt_baseA_*：0.97 是基线默认值，但基线库由
10-29 的旧二进制生成，缺 entry_box_id / source_signal_id；本实验要按「初始止损 vs
移动止损」分解出口，需要新库自带的强关联。三档同二进制同口径才有可比性。

用法：
  python scripts/e17_t3_bt_run.py --dry-run
  python scripts/e17_t3_bt_run.py --years 2018 --tiers 097          # 冒烟 + 等价性对账
  python scripts/e17_t3_bt_run.py --years 2018,2015,2017,2012 --tiers 095,097,099
"""
import argparse
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
OUT = BT / "t3_bt"                 # 本次扫描库输出目录
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_t3.exe"

# 基线共同参数（100% 复刻 base_run.py COMMON；唯一变量 sell.discount 在此不写死）
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

# 档位（文件名用 095/097/099 避免小数点）：参数值
TIERS = {"095": "0.95", "097": "0.97", "099": "0.99"}


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
    p["sell.discount"] = TIERS[tier]
    return ",".join(f"{k}={v}" for k, v in sorted(p.items()))


def db_complete(path, year):
    """库是否完整：存在 + equity_snapshot 覆盖到该年年末。

    判据：equity_snapshot 的最大 date ≥ year-12-20。
    仅「非空」会误判半成品库（沙箱回收坑）。

    2026-11-02 14:55 修正（跑中发现，非跑后调参）：原判据 ≥ year-12-01 过松——
    2017-12-04 即可通过，而当年最后交易日是 12-29，等于放行一个**缺整月**的库。
    实测 099_2017 跑到 12-04 时被判 OK 并计入汇总，直接把缺月数字写进对比表。
    年末最后一周（12-20~12-31）只有 7~8 个交易日，卡到 12-20 既能挡住缺月库，
    又不会把「跑到 12-28 因个别停牌少一天」的正常库误判成半成品。
    """
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
            return bool(maxd) and maxd >= f"{year}-12-20"
        finally:
            c.close()
    except Exception:
        return False


def run_one(year, tier, force=False):
    db = OUT / f"bt_t3_{tier}_{year}.db"
    logp = OUT / f"bt_t3_{tier}_{year}.log"
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
    log(f"START {year} sell.discount={TIERS[tier]} → {db.name}", logp)
    log(f"  set: {sa}", logp)
    t0 = time.time()
    with logp.open("a", encoding="utf-8") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time() - t0
    if rc != 0:
        log(f"FAILED {year} sell.discount={TIERS[tier]} rc={rc} 耗时 {dt/60:.1f}min", logp)
        return rc
    log(f"DONE {year} sell.discount={TIERS[tier]} 耗时 {dt/60:.1f}min", logp)
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
    ap.add_argument("--years", default="2018",
                    help="逗号分隔或区间，如 2018,2015,2017,2012")
    ap.add_argument("--tiers", default="097",
                    help="逗号分隔档位，如 095,097,099")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    years = parse_years(a.years)
    tiers = [t.strip() for t in a.tiers.split(",")]
    for t in tiers:
        if t not in TIERS:
            raise SystemExit(f"未知档位 {t}，可选 {list(TIERS)}")
    OUT.mkdir(parents=True, exist_ok=True)
    if not EXE.exists():
        raise SystemExit(f"二进制不存在：{EXE}")
    if a.dry_run:
        print(f"二进制 {EXE.name}  源库 {SRC.name}")
        for y in years:
            for t in tiers:
                print(f"\n{y} sell.discount={TIERS[t]}")
                for kv in setargs(t).split(","):
                    print(f"   {kv}")
        return 0
    total = len(years) * len(tiers)
    i = 0
    for y in years:
        for t in tiers:
            i += 1
            print(f"\n=== [{i}/{total}] {y} sell.discount={TIERS[t]} ===", flush=True)
            if run_one(y, t, a.force) != 0:
                print(f"失败即停：{y} sell.discount={TIERS[t]}", flush=True)
                return 1
    print("\n全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
