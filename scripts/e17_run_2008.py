#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E17 2008 样本外回测：等待 2015-2018 主队列正常完成后串行执行。

用法：
  python scripts/e17_run_2008.py          # 等待主队列，随后运行 2008
  python scripts/e17_run_2008.py --check  # 只检查数据与启动条件，不运行

只写 var/bt/bt_e17_2008.db；源库与生产库不修改。
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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
YEAR = "2008"
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_e17.exe"
DB = BT / f"bt_e17_{YEAR}.db"
METRICS = BT / "e17_metrics.csv"
REST_LOG = BT / "e17_run_rest.log"
RUN_LOG = BT / f"e17_run_{YEAR}.log"
BT_LOG = BT / f"e17_{YEAR}.log"
STATUS = BT / f"e17_{YEAR}_status.json"
LOCK = BT / f"e17_{YEAR}.lock"
REPORT = BT / f"bt_e17_{YEAR}_report.md"
PY = sys.executable
SETARGS = (
    "pos.newhigh_ladder=on,pos.newhigh.lookback=252,pos.newhigh.cutoff=40,"
    "pos.newhigh.units.t1=3,pos.newhigh.units.t2=2,pos.newhigh.units.t3=1,"
    "pos.newhigh.units.t4=0.5"
)


def now():
    return datetime.now().isoformat(timespec="seconds")


def set_status(state, detail):
    payload = {"year": int(YEAR), "state": state, "detail": detail, "updated_at": now()}
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS)
    print(f"[{payload['updated_at']}] {state}: {detail}", flush=True)


def metrics_years():
    if not METRICS.exists():
        return set()
    with METRICS.open(encoding="utf-8", errors="replace", newline="") as f:
        return {r[0].strip() for r in csv.reader(f) if r and r[0].strip().isdigit()}


def validate_source():
    if not SRC.exists():
        raise RuntimeError(f"源库不存在：{SRC}")
    con = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    try:
        result = {}
        for year in ("2007", YEAR):
            row = con.execute(
                "SELECT COUNT(*), COUNT(DISTINCT date), MIN(date), MAX(date) "
                "FROM daily_bar WHERE CAST(date AS TEXT) LIKE ?",
                (f"{year}%",),
            ).fetchone()
            if not row or row[0] == 0:
                raise RuntimeError(f"源库缺少 {year} 年日线数据")
            result[year] = {"rows": row[0], "days": row[1], "min": row[2], "max": row[3]}
        return result
    finally:
        con.close()


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
        raise RuntimeError("2008 回测队列已有进程持有锁，本次不重复启动")
    return f


def rest_state():
    if not REST_LOG.exists():
        return "waiting", "主队列日志尚未创建"
    text = REST_LOG.read_text(encoding="utf-8", errors="replace")
    if "CHAIN STOPPED ON FAILURE" in text or " FAILED rc=" in text:
        return "failed", "2015-2018 主队列失败，2008 不启动"
    if "=== ALL DONE" in text and "2018" in metrics_years():
        return "ready", "2015-2018 主队列已正常完成"
    return "waiting", "等待 2015-2018 主队列正常完成"


def run_year():
    if YEAR in metrics_years():
        set_status("done", "2008 已存在于 e17_metrics.csv，无需重跑")
        return 0
    if DB.exists():
        raise RuntimeError(f"发现未登记的 2008 库 {DB}，为避免混入旧数据，停止自动执行")
    if not EXE.exists():
        raise RuntimeError(f"回测程序不存在：{EXE}")

    coverage = validate_source()
    set_status("waiting", f"数据核验通过（2007/2008={coverage['2007']['days']}/{coverage[YEAR]['days']} 个交易日）；等待主队列")
    while True:
        state, detail = rest_state()
        if state == "failed":
            set_status("blocked", detail)
            return 2
        if state == "ready":
            break
        time.sleep(60)

    set_status("running", "2018 已完成，2008 样本外回测启动")
    with RUN_LOG.open("a", encoding="utf-8") as ctl, BT_LOG.open("a", encoding="utf-8") as out:
        ctl.write(f"=== {YEAR} START {now()} ===\n")
        ctl.flush()
        cmd = [
            str(EXE), "backtest", "--src", str(SRC), "--from", f"{YEAR}-01-01",
            "--to", f"{YEAR}-12-31", "--mode", "regime", "--db", str(DB),
            "--set", SETARGS,
        ]
        rc = subprocess.run(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT).returncode
        ctl.write(f"=== {YEAR} {'DONE' if rc == 0 else 'FAILED'} rc={rc} {now()} ===\n")
        ctl.flush()
    if rc != 0:
        set_status("failed", f"2008 回测失败，rc={rc}；详见 {BT_LOG}")
        return rc

    report_text = subprocess.run(
        [PY, str(ROOT / "scripts" / "p05_yearly_report.py"), str(DB), YEAR],
        cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    REPORT.write_text(report_text, encoding="utf-8")
    subprocess.run(
        [PY, str(ROOT / "scripts" / "p05_yearly_report.py"), str(DB), YEAR,
         "--csv-file", str(METRICS)],
        cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
    )
    set_status("done", "2008 回测与分档统计已完成，已并入 e17_metrics.csv")
    subprocess.run([PY, str(ROOT / "scripts" / "e17_watch.py")], cwd=ROOT, check=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查，不等待、不回测")
    args = parser.parse_args()
    coverage = validate_source()
    state, detail = rest_state()
    print(json.dumps({"coverage": coverage, "queue": state, "detail": detail,
                      "metrics_has_2008": YEAR in metrics_years()}, ensure_ascii=False, indent=2))
    if args.check:
        return 0 if state != "failed" else 2
    lock = acquire_lock()
    try:
        return run_year()
    except Exception as exc:
        set_status("failed", str(exc))
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
