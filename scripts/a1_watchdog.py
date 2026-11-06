#!/usr/bin/env python3
"""
A1 续跑看门狗 —— 单次检查版，由 Windows 任务计划程序每 2 分钟触发。

设计要点：
- 不做长循环：每次触发只检查一次就退出，避免守护自身被回收后失联。
- 由 Task Scheduler 托管，进程树不属于任何 Agent 会话，不会被会话清理连带杀掉。
- 只在 darvas-dev 不存在、且库未跑到终点时拉起；带 90 秒启动锁防并发写同一库。
- 跑完（>= 2018-12-20）自动停手，需手动删除计划任务。

手动单次调用：python scripts/a1_watchdog.py
"""
import os
import sys
import time
import sqlite3
import subprocess
from datetime import datetime

LOOP = "--loop" in sys.argv
LOOP_SEC = 30
LOOP_MAX = 2000

ROOT = r"I:\Darvas-box-trading"
DB = r"var\bt\arms\bt_a1_t4off.db"
LOG = os.path.join(ROOT, r"var\bt\arms\a1_watchdog.log")
LOCK = os.path.join(ROOT, r"var\bt\arms\a1_watchdog.lock")
END_DATE = "2018-12-20"
START_LOCK_SEC = 90


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")


def engine_alive():
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq darvas-dev.exe", "/FO", "CSV"],
        capture_output=True, text=True, errors="replace")
    return "darvas-dev.exe" in (r.stdout or "")


def progress():
    p = os.path.join(ROOT, DB)
    if not os.path.exists(p):
        return None, 0
    c = sqlite3.connect(f"file:{p.replace(chr(92), '/')}?mode=ro", uri=True)
    mx, n = c.execute("select max(date),count(distinct date) from daily_run").fetchone()
    c.close()
    return mx, n


def main():
    os.chdir(ROOT)
    mx, n = progress()
    if mx and mx >= END_DATE:
        log(f"DONE {mx} {n}/3402 -- watchdog stands down")
        return
    if engine_alive():
        log(f"ALIVE {mx} {n}/3402")
        return
    # 启动锁：90 秒内已有拉起动作则不重复（防止并发写库）
    now = time.time()
    if os.path.exists(LOCK):
        try:
            if now - float(open(LOCK).read().strip()) < START_LOCK_SEC:
                log(f"LOCKED {mx} {n}/3402 (launched <{START_LOCK_SEC}s ago)")
                return
        except Exception:
            pass
    with open(LOCK, "w") as f:
        f.write(str(now))

    py = sys.executable
    args = [
        py, "scripts/base_run.py", "--cont",
        "--src", "var/bt/full_hist_adj.db",
        "--exe", "bin/darvas-dev.exe",
        "--db", "var/bt/arms/bt_a1_t4off.db",
        "--resume",
    ]
    out = open(os.path.join(ROOT, r"var\bt\arms\a1_watchdog_run.out"), "a", encoding="utf-8")
    err = open(os.path.join(ROOT, r"var\bt\arms\a1_watchdog_run.err"), "a", encoding="utf-8")
    # DETACHED_PROCESS：脱离父进程；Task Scheduler 触发时进程树本就独立
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, cwd=ROOT, stdout=out, stderr=err, creationflags=flags)
    log(f"RELAUNCH from {mx} {n}/3402 (--resume)")


def main_loop():
    log(f"LOOP start pid={os.getpid()} every {LOOP_SEC}s max {LOOP_MAX} ticks")
    for i in range(LOOP_MAX):
        try:
            mx, n = progress()
            if mx and mx >= END_DATE:
                log(f"LOOP DONE {mx} {n}/3402 -- exit")
                return
            main()
        except Exception as e:
            log(f"LOOP ERROR tick={i}: {e}")
        time.sleep(LOOP_SEC)
    log("LOOP max ticks reached -- exit")


if __name__ == "__main__":
    main_loop() if LOOP else main()
