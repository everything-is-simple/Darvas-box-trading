#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B-4 仓位语义成对重跑 · 执行器。

变体（框架 §2.1，跑前写死）：
  1 = 旧语义      pos.equity_gate=off  pool.max_hold=7    （对照锚，对标历史库）
  2 = 过渡        pos.equity_gate=off  pool.max_hold=14   （只数上限翻倍，仍无预算）
  3 = 新默认      pos.equity_gate=on   pool.max_hold=0    （50% 预算 + 单票 8% + 挂单预占）

设计要点：
- 参数不手抄：阶梯 + staledays 全部从历史基线库 bt_e17_<year>.db 读取，
  只覆盖变体那两个参数 —— 保证与历史基线同源、单变量。
- 每次跑独立 scratch 库 var/bt/p4/p4_v<k>_<year>.db，绝不覆盖历史基线库。
- 串行执行，失败即停；源库只读；生产库/实时库不碰。
- 二进制必须是 var/bt/darvas_b4.exe（当前 HEAD 编译），不能用 darvas_e17.exe。

用法：
  python scripts/p4_run.py --dry-run
  python scripts/p4_run.py --years 2007 --variants 1
  python scripts/p4_run.py --years 2007,2010,2013,2018 --variants 2,3
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
OUT = BT / "p4"
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_b4.exe"

# 变体表：id -> (equity_gate, max_hold, 中文名)
VARIANTS = {
    1: ("off", "7", "旧语义"),
    2: ("off", "14", "过渡"),
    3: ("on", "0", "新默认"),
}

# 从基线库继承的参数前缀（历史命令实际传过的阶梯 + 固定 staledays）
PARAM_PREFIXES = ("pos.newhigh", "position.staledays")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, path=None):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    if path:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def baseline_params(year):
    """从历史基线库读参数（唯一事实源）。"""
    db = BT / f"bt_e17_{year}.db"
    if not db.exists():
        raise SystemExit(f"历史基线库不存在：{db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT key, value FROM param").fetchall()
    finally:
        con.close()
    return {k: v for k, v in rows if k.startswith(PARAM_PREFIXES)}


def build_setargs(year, vid):
    gate, maxhold, _ = VARIANTS[vid]
    p = baseline_params(year)
    if "position.staledays" not in p:
        raise SystemExit(f"{year} 基线库缺 position.staledays，拒绝猜默认值")
    p["pos.equity_gate"] = gate
    p["pool.max_hold"] = maxhold
    return p


def run_one(year, vid, force=False):
    gate, maxhold, name = VARIANTS[vid]
    db = OUT / f"p4_v{vid}_{year}.db"
    logp = OUT / f"p4_v{vid}_{year}.log"
    if db.exists() and not force:
        log(f"跳过（已存在）：{db.name}", logp)
        return 0
    params = build_setargs(year, vid)
    setargs = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
    cmd = [str(EXE), "backtest", "--src", str(SRC),
           "--from", f"{year}-01-01", "--to", f"{year}-12-31",
           "--mode", "regime", "--db", str(db), "--set", setargs]
    log(f"START {year} v{vid}({name}) gate={gate} max_hold={maxhold} → {db.name}", logp)
    log(f"  set: {setargs}", logp)
    t0 = time.time()
    with logp.open("a", encoding="utf-8") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time() - t0
    if rc != 0:
        log(f"FAILED {year} v{vid} rc={rc} 耗时 {dt / 60:.1f}min", logp)
        return rc
    log(f"DONE {year} v{vid} 耗时 {dt / 60:.1f}min", logp)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2007,2010,2013,2018")
    ap.add_argument("--variants", default="1,2,3")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    years = [int(x) for x in a.years.split(",")]
    vids = [int(x) for x in a.variants.split(",")]
    for v in vids:
        if v not in VARIANTS:
            raise SystemExit(f"未知变体 {v}，可选 {sorted(VARIANTS)}")
    OUT.mkdir(parents=True, exist_ok=True)
    if not EXE.exists():
        raise SystemExit(f"二进制不存在：{EXE}（须先 go build -o var/bt/darvas_b4.exe ./cmd/darvas）")
    if a.dry_run:
        print(f"二进制 {EXE.name}  源库 {SRC.name}")
        for y in years:
            for v in vids:
                p = build_setargs(y, v)
                gate, mh, name = VARIANTS[v]
                print(f"\n{y} v{v}({name})  共 {len(p)} 参数  gate={gate} max_hold={mh}")
                for k in sorted(p):
                    print(f"   {k} = {p[k]}")
        return 0
    fails = []
    total = len(years) * len(vids)
    i = 0
    for y in years:
        for v in vids:
            i += 1
            print(f"\n=== [{i}/{total}] {y} v{v} ===", flush=True)
            if run_one(y, v, a.force) != 0:
                fails.append((y, v))
                print(f"失败即停：{y} v{v}", flush=True)
                return 1
    if fails:
        print(f"\n失败组合：{fails}")
        return 1
    print("\n全部完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
