#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-3 B 类 MA60 闸门实验（B-1 half / B-2 stop）逐年回测队列。

实验设计（docs/experiments/e17_regime_attribution_framework.md §9 B 类清单）：
- B-1（half）：上证 sh000001 收盘 < MA60 时，首买降为 0.5 单位（planner gate.ma60.mode=half）
- B-2（stop）：上证收盘 < MA60 时，不生成买单（gate.ma60.mode=stop）
- 基线 = E17（gate=off），已有 bt_e17_<year>.db，不重跑
- 两变体与 E17 唯一差别就是 gate.ma60.mode——同轮同参数对照纪律

分阶段：
- --phase1  等价验证：off 模式跑 2014，产物 bt_b12_off_2014.db 应与 bt_e17_2014.db 逐单一致
- --phase2  正式实验：half/stop × 代表年（2007 牛 / 2008 深熊 / 2010 震荡 / 2011 阴跌）

规则：
- 严格串行，任一年失败即停止队列；源库/生产库只读不动。
- 每年完成后生成年报 + 追加 b12_metrics_<variant>.csv。
- 产物命名沿用既有纪律：bt_b12_<variant>_<year>.db（脚本层 --db 指定，Go 侧无感知）。

用法：
  python scripts/b12_run_queue.py --check    # 只检查启动条件
  python scripts/b12_run_queue.py --phase1   # off 2014 等价验证（单跑）
  python scripts/b12_run_queue.py --phase2   # half/stop × 4 代表年（8 跑）
  python scripts/b12_run_queue.py --jobs half:2007,stop:2011   # 指定子集
"""
import argparse
import csv
import json
import msvcrt
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_b12.exe"
STATUS = BT / "b12_years_status.json"
LOCK = BT / "b12_years.lock"
RUN_LOG = BT / "b12_run.log"
PY = sys.executable
# 与 run_e17.bat / e17_run_years.py 完全相同的 E17 阶梯参数——B 类对照纪律：
# B-1/B-2 与基线的唯一差别只允许是 gate.ma60.mode。
SETARGS_BASE = (
    "pos.newhigh_ladder=on,pos.newhigh.lookback=252,pos.newhigh.cutoff=40,"
    "pos.newhigh.units.t1=3,pos.newhigh.units.t2=2,pos.newhigh.units.t3=1,"
    "pos.newhigh.units.t4=0.5"
)
# 代表年（用户 2026-10-26 拍板）：2007 牛市对照 + 2008 深熊 + 2010 震荡 + 2011 阴跌
REP_YEARS = (2007, 2008, 2010, 2011)


def now():
    return datetime.now().isoformat(timespec="seconds")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def set_status(state, detail, job=None):
    payload = {"state": state, "detail": detail, "job": job, "updated_at": now()}
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS)
    log(f"{state}: {detail}")


def acquire_lock():
    f = LOCK.open("a+b")
    try:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        raise RuntimeError("b12 队列已有进程持有锁，本次不重复启动")
    return f


def validate_source():
    if not SRC.exists():
        raise RuntimeError(f"源库不存在：{SRC}")
    if not EXE.exists():
        raise RuntimeError(f"回测程序不存在：{EXE}")
    con = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    try:
        for year in REP_YEARS + (2014,):
            row = con.execute(
                "SELECT COUNT(DISTINCT date) FROM daily_bar "
                "WHERE CAST(date AS TEXT) LIKE ?", (f"{year}%",)).fetchone()
            if not row or not row[0]:
                raise RuntimeError(f"源库缺少 {year} 年日线数据")
    finally:
        con.close()


def parse_jobs(spec):
    jobs = []
    for part in spec.split(","):
        variant, year = part.strip().split(":")
        if variant not in ("off", "half", "stop"):
            raise RuntimeError(f"未知变体 {variant}（只允许 off/half/stop）")
        jobs.append((variant, int(year)))
    return jobs


def run_job(variant, year):
    db = BT / f"bt_b12_{variant}_{year}.db"
    bt_log = BT / f"b12_{variant}_{year}.log"
    report = BT / f"bt_b12_{variant}_{year}_report.md"
    metrics = BT / f"b12_metrics_{variant}.csv"
    if db.exists():
        raise RuntimeError(f"发现已存在的 {db}，为避免混入旧数据，停止（如需重跑请先删除）")
    set_status("running", f"{variant} {year} 回测启动", f"{variant}:{year}")
    t0 = time.time()
    setargs = f"{SETARGS_BASE},gate.ma60.mode={variant}"
    with bt_log.open("a", encoding="utf-8") as out:
        cmd = [str(EXE), "backtest", "--src", str(SRC), "--from", f"{year}-01-01",
               "--to", f"{year}-12-31", "--mode", "regime", "--db", str(db),
               "--set", setargs]
        log(f"RUN {' '.join(cmd)}")
        rc = subprocess.run(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        raise RuntimeError(f"{variant} {year} 回测失败 rc={rc}；详见 {bt_log}")
    subprocess.run(
        [PY, str(ROOT / "scripts" / "p05_yearly_report.py"), str(db), str(year),
         "--out", str(report)],
        cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    if not report.exists() or report.stat().st_size == 0:
        raise RuntimeError(f"{variant} {year} 年报生成失败：{report}")
    subprocess.run([PY, str(ROOT / "scripts" / "p05_yearly_report.py"), str(db), str(year),
                    "--csv-file", str(metrics)], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)
    set_status("running",
               f"{variant} {year} 完成，用时 {(time.time() - t0) / 60:.1f} 分钟", f"{variant}:{year}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查启动条件")
    parser.add_argument("--phase1", action="store_true", help="等价验证：off 2014")
    parser.add_argument("--phase2", action="store_true", help="正式实验：half/stop × 代表年")
    parser.add_argument("--jobs", type=str, default="", help="自定义子集，如 half:2007,stop:2011")
    args = parser.parse_args()

    validate_source()
    if args.jobs:
        jobs = parse_jobs(args.jobs)
    elif args.phase1:
        jobs = [("off", 2014)]
    elif args.phase2:
        jobs = [(v, y) for v in ("half", "stop") for y in REP_YEARS]
    else:
        jobs = [("off", 2014)] + [(v, y) for v in ("half", "stop") for y in REP_YEARS]
    print(json.dumps({"jobs": jobs, "exe": str(EXE)}, ensure_ascii=False, indent=2))
    if args.check:
        return 0

    lock = acquire_lock()
    try:
        for variant, year in jobs:
            dbp = BT / f"bt_b12_{variant}_{year}.db"
            if dbp.exists() and dbp.stat().st_size > 0:
                log(f"跳过 {variant}:{year}（库已存在且非空，视为已完成）")
                continue
            if dbp.exists():
                log(f"{variant}:{year} 存在 0 字节空库（疑似残留），删除后重跑")
                dbp.unlink()
            run_job(variant, year)
        set_status("done", f"全部任务完成：{jobs}")
        return 0
    except Exception as exc:
        tb = traceback.format_exc()
        set_status("failed", f"{exc}（详见 {RUN_LOG.name}）", None)
        log(f"ERROR: {exc}\n{tb}")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
