#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E17 2005-2018 基本历史段逐年补跑队列。

规则：
- 目标年份 2005..2018；已在 e17_metrics.csv 中的年份直接跳过（2014-2018、2008 不重跑）。
- 严格串行：先等 2008 队列（e17_run_2008.py）状态变为 done，再按年份升序逐年执行。
- 每年使用同一 darvas_e17.exe 与同一阶梯参数，只写独立 scratch 库 bt_e17_<year>.db。
- 每年完成后：生成年度报告 -> 追加 e17_metrics.csv -> 刷新监控与逐年分层报告。
- 任一年失败即停止队列，不继续后续年份；源库与生产库不修改。

用法：
  python scripts/e17_run_years.py          # 等待并串行补跑
  python scripts/e17_run_years.py --check  # 只检查数据与启动条件
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
EXE = BT / "darvas_e17.exe"
METRICS = BT / "e17_metrics.csv"
YEARS = list(range(2005, 2019))
STATUS = BT / "e17_years_status.json"
LOCK = BT / "e17_years.lock"
RUN_LOG = BT / "e17_run_years.log"
STATUS_2008 = BT / "e17_2008_status.json"
PY = sys.executable
SETARGS = (
    "pos.newhigh_ladder=on,pos.newhigh.lookback=252,pos.newhigh.cutoff=40,"
    "pos.newhigh.units.t1=3,pos.newhigh.units.t2=2,pos.newhigh.units.t3=1,"
    "pos.newhigh.units.t4=0.5"
)


def now():
    return datetime.now().isoformat(timespec="seconds")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def set_status(state, detail, year=None):
    payload = {"state": state, "detail": detail, "year": year, "updated_at": now()}
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS)
    log(f"{state}: {detail}")


def metrics_years():
    if not METRICS.exists():
        return set()
    with METRICS.open(encoding="utf-8", errors="replace", newline="") as f:
        return {int(r[0]) for r in csv.reader(f) if r and r[0].strip().isdigit()}


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
        raise RuntimeError("补跑队列已有进程持有锁，本次不重复启动")
    return f


def validate_source():
    if not SRC.exists():
        raise RuntimeError(f"源库不存在：{SRC}")
    con = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    try:
        result = {}
        for year in range(2004, 2019):
            row = con.execute(
                "SELECT COUNT(DISTINCT date), MIN(date), MAX(date) FROM daily_bar "
                "WHERE CAST(date AS TEXT) LIKE ?", (f"{year}%",)).fetchone()
            if not row or not row[0]:
                raise RuntimeError(f"源库缺少 {year} 年日线数据")
            result[year] = row[0]
        return result
    finally:
        con.close()


def wait_for_2008():
    """2008 由既有队列负责；若仍在跑则等待，失败则阻止补跑。"""
    while True:
        done = metrics_years()
        if 2008 in done:
            return
        state, detail = "unknown", ""
        if STATUS_2008.exists():
            try:
                payload = json.loads(STATUS_2008.read_text(encoding="utf-8"))
                state, detail = payload.get("state", ""), payload.get("detail", "")
            except ValueError:
                pass
        if state in ("failed", "blocked"):
            raise RuntimeError(f"2008 队列状态为 {state}（{detail}），补跑不启动")
        set_status("waiting", f"等待 2008 完成（当前 {state}）；已完成年份 {sorted(done)}")
        time.sleep(60)


def run_year(year):
    db = BT / f"bt_e17_{year}.db"
    bt_log = BT / f"e17_{year}.log"
    report = BT / f"bt_e17_{year}_report.md"
    if db.exists():
        raise RuntimeError(f"发现未登记的 {year} 库 {db}，为避免混入旧数据，停止自动执行")
    set_status("running", f"{year} 年回测启动", year)
    t0 = time.time()
    with bt_log.open("a", encoding="utf-8") as out:
        cmd = [str(EXE), "backtest", "--src", str(SRC), "--from", f"{year}-01-01",
               "--to", f"{year}-12-31", "--mode", "regime", "--db", str(db),
               "--set", SETARGS]
        rc = subprocess.run(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        raise RuntimeError(f"{year} 回测失败 rc={rc}；详见 {bt_log}")
    # 用 --out 直接写文件，避免计划任务环境下 stdout 捕获返回 None
    subprocess.run(
        [PY, str(ROOT / "scripts" / "p05_yearly_report.py"), str(db), str(year),
         "--out", str(report)],
        cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    if not report.exists() or report.stat().st_size == 0:
        raise RuntimeError(f"{year} 年报生成失败：{report} 不存在或为空")
    subprocess.run([PY, str(ROOT / "scripts" / "p05_yearly_report.py"), str(db), str(year),
                    "--csv-file", str(METRICS)], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run([PY, str(ROOT / "scripts" / "e17_watch.py")], cwd=ROOT, check=True)
    set_status("running", f"{year} 完成，用时 {(time.time()-t0)/60:.1f} 分钟，已并入 metrics 与分层报告", year)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查，不等待、不回测")
    args = parser.parse_args()
    coverage = validate_source()
    done = metrics_years()
    todo = [y for y in YEARS if y not in done]
    print(json.dumps({"trading_days": coverage, "done": sorted(done), "todo": todo},
                     ensure_ascii=False, indent=2))
    if args.check:
        return 0
    lock = acquire_lock()
    try:
        if not todo:
            set_status("done", "2005-2018 全部年份已在 metrics 中，无需补跑")
            return 0
        if not EXE.exists():
            raise RuntimeError(f"回测程序不存在：{EXE}")
        if 2008 not in done:
            wait_for_2008()
        for year in [y for y in YEARS if y not in metrics_years()]:
            run_year(year)
        set_status("done", "2005-2018 基本历史段全部完成")
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
