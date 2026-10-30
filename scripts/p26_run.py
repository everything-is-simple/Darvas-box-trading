#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-6a `position.staledays` 梯度回测执行器（docs/experiments/e17_staledays_framework.md）。

设计要点：
- 参数不手抄：从**新基线库** `var/bt/base/bt_baseA_<year>.db` 的 param 表按白名单读，
  只覆盖 `position.staledays` —— 保证与新基线同源、单变量。
- 每次跑独立 scratch 库 `var/bt/p26/p26_sd<N>_<year>.db`，绝不覆盖基线库。
- 串行执行，失败即停。源库只读，生产库/实时库不碰。

迁移适配（2026-10-30，登记于框架 §10）：
  老机版读 `bt_e17_<year>.db` + 用 `darvas_e17.exe`；那批库与二进制未随包带到本机。
  本机改为读新基线 `bt_baseA_<year>.db` + 用 `darvas_base.exe`（**与 14 年新基线同一份二进制**），
  HARD① 基线复现的对照对象相应改为 `bt_baseA_<year>.db`。

白名单为什么是 11 项（框架 §3.2 原写「只传 7 项」）：
  新基线语义由 12 项显式 `--set` 共同定义 —— `pool.max_hold=14`、`pos.equity_gate=off`、
  `gate.ma60.mode=stop@sh000001` 都**不是 migration 种子**，不传就退回种子语义（≠ 新基线）。
  只传 7 项会得到另一套仓位/闸门语义，HARD① 必失败。
  故 = 11 项继承 + 1 项（staledays）作变量，与 `scripts/base_run.py` 的 COMMON 等价。
  仍不回灌全部 38 项：其中混有运行期回写的状态键（如 regime.bear_thresh）。

档位（框架 §2.2，跑前写死）：
  0    收紧极限（`n < 0` 恒假 → 所有持仓每天都进入判断；**不是「关闭」**）
  2    基线（14 年新基线所用值）
  5 / 10   放宽
  off  100000（数值上界 → `n < 100000` 恒真 → 永不触发）

用法：
  python scripts/p26_run.py --years 2012 --staledays 0,2,5,10,off --dry-run
  python scripts/p26_run.py --years 2012 --staledays 0,2,5,10,off      # 阶段 1
  python scripts/p26_run.py --years 2011,2015,2017 --staledays 5,2     # 阶段 2
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
BASE = BT / "base"            # 新基线库目录
OUT = BT / "p26"              # 本实验独立 scratch
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_base.exe"  # 与 14 年新基线同一份二进制 → 单变量

# 从基线库继承的 11 项（第 12 项 position.staledays 由档位决定）
INHERIT = [
    "pos.newhigh_ladder",
    "pos.newhigh.lookback",
    "pos.newhigh.cutoff",
    "pos.newhigh.units.t1",
    "pos.newhigh.units.t2",
    "pos.newhigh.units.t3",
    "pos.newhigh.units.t4",
    "pool.max_hold",
    "pos.equity_gate",
    "gate.ma60.mode",
    "gate.ma60.index",
]

OFF = 100000


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, path=None):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    if path:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def parse_grade(tok):
    """'off' → 100000；其余整数字面量。"""
    t = tok.strip().lower()
    if t == "off":
        return OFF
    return int(t)


def baseline_params(year):
    """从新基线库读 11 项继承参数；缺项直接报错，不静默用代码默认值兜底。"""
    db = BASE / f"bt_baseA_{year}.db"
    if not db.exists():
        raise SystemExit(f"新基线库不存在：{db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        d = dict(con.execute("SELECT key, value FROM param").fetchall())
    finally:
        con.close()
    miss = [k for k in INHERIT if k not in d]
    if miss:
        raise SystemExit(f"基线库 {db.name} 缺参数 {miss} —— 拒绝用代码默认值兜底")
    return {k: d[k] for k in INHERIT}


def run_one(year, stale, force=False):
    tag = "off" if stale == OFF else str(stale)
    db = OUT / f"p26_sd{tag}_{year}.db"
    logp = OUT / f"p26_sd{tag}_{year}.log"
    if db.exists() and not force and db_complete(db):
        log(f"跳过（已完整）：{db.name}", logp)
        return 0
    if db.exists() and not force:
        log(f"发现不完整库，删除重跑：{db.name}", logp)
        for ext in ("", "-wal", "-shm"):
            q = Path(str(db) + ext)
            if q.exists():
                q.unlink()
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


def db_complete(path):
    """完成判据：库里有 equity_snapshot，且最后交易日 ≥ 当年 12-25。

    2026-10-30 修正：原判据只查 `COUNT(*) > 0`，任何跑了一两天的残缺库都会被判为
    「已完整」→ 续跑时静默跳过，造成档位数据缺失却显示完成。
    A 股年度最后交易日恒在 12-28~12-31，故以 12-25 为安全下界（同 base_year_summary）。
    """
    if not path.exists():
        return False
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            n, mx = c.execute(
                "SELECT COUNT(*), MAX(date) FROM equity_snapshot"
            ).fetchone()
            if not n or not mx:
                return False
            year = path.stem.rsplit("_", 1)[-1]
            return mx >= f"{year}-12-25"
        finally:
            c.close()
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", required=True)
    ap.add_argument("--staledays", required=True, help="逗号分隔，如 0,2,5,10,off")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    years = [int(x) for x in a.years.split(",")]
    stales = [parse_grade(x) for x in a.staledays.split(",")]
    OUT.mkdir(parents=True, exist_ok=True)
    if not EXE.exists():
        raise SystemExit(f"二进制不存在：{EXE}")
    if a.dry_run:
        print(f"二进制 {EXE.name}（{EXE.stat().st_size:,} B）")
        print(f"源库   {SRC.name}")
        print(f"输出   {OUT}")
        for y in years:
            p = baseline_params(y)
            print(f"\n{y} 基线 staledays=2，继承 {len(p)} 项参数")
            for k in sorted(p):
                print(f"   {k} = {p[k]}")
            for s in stales:
                tag = "off" if s == OFF else str(s)
                print(f"   → 档 p26_sd{tag}_{y}.db  position.staledays={s}")
        return 0
    t_all = time.time()
    fails = []
    total = len(years) * len(stales)
    i = 0
    for y in years:
        for s in stales:
            i += 1
            print(f"\n=== [{i}/{total}] {y} staledays={s} ===", flush=True)
            if run_one(y, s, a.force) != 0:
                fails.append((y, s))
                print(f"失败即停：{y} staledays={s}", flush=True)
                return 1
    print(f"\n全部完成，总耗时 {(time.time()-t_all)/60:.1f} min")
    if fails:
        print(f"失败组合：{fails}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
