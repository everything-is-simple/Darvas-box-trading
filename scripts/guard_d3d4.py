#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D3+D4 臂守护循环：夜间长进程被沙箱回收后自动断点续跑，直到跑完。

机制（依赖 base_run.py 的 db_complete 判据）：
  - 每 POLL 秒查一次：
      1. darvas-dev.exe 进程是否存活（tasklist）；
      2. 库是否「完整」（equity_snapshot.max(date) >= MIN_END 且逐日六任务全 ok）。
  - 进程活着 → 继续等。
  - 进程死了但库未完整 → 调 base_run.py --resume 续跑（不带 --set）。
  - 库完整 → 退出 0。
  - 续跑后立刻失败（rc!=0 或进程几秒内消失且进度未动）→ 记错，退避重试，
    连续 MAX_FAIL 次仍失败则退出非 0（避免空转）。

用法：
  python scripts/guard_d3d4.py
  python scripts/guard_d3d4.py --poll 30 --max-fail 6

只读查库 + 只续跑，不删库、不重头跑。手动停：Ctrl+C 或 taskkill 本进程。
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "bt" / "arms" / "bt_d3d4_off.db"
EXE = ROOT / "bin" / "darvas-dev.exe"
SRC = ROOT / "var" / "bt" / "full_hist_adj.db"
FROM = "2005-01-01"
TO = "2018-12-31"
# 完整判据终点：与 base_run.run_cont 的 min_end 一致（to_date 12 月 → 当年 12-01）
MIN_END = "2018-11-26"

# 完整判据所需的最小日期（保守取 12-01 前推 35 天，与 db_complete 的 min_end 对齐）
# 实际 2018-12-31 → min_end = 2018-12-01（base_run 里 12 月特判为 replace(day=1)）
MIN_END = "2018-12-01"


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


def proc_alive():
    """darvas-dev.exe 是否存活。tasklist 中文输出为 GBK，须按 GBK 解码，
    不要用 text=True（默认 UTF-8 会炸）。查不到时返回 True 避免误杀。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq darvas-dev.exe"],
            capture_output=True, timeout=15,
        ).stdout.decode("gbk", errors="replace")
        return "darvas-dev.exe" in out
    except Exception:
        return True  # 查不到就当作还活着，避免误杀


def db_complete():
    """库是否完整可用（复用 base_run.db_complete 的三条判据）。"""
    if not DB.exists():
        return False
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            n = c.execute("SELECT COUNT(*) FROM equity_snapshot").fetchone()[0]
            if n == 0:
                return False
            maxd = c.execute("SELECT MAX(date) FROM equity_snapshot").fetchone()[0]
            if maxd is None or str(maxd) < MIN_END:
                return False
            bad = c.execute(
                "SELECT COUNT(*) FROM equity_snapshot es WHERE es.date NOT IN ("
                " SELECT date FROM daily_run WHERE task IN "
                "('boxscan','screen','poolsync','signal','plan','report')"
                " GROUP BY date HAVING SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)=6)"
            ).fetchone()[0]
            return bad == 0
        finally:
            c.close()
    except Exception:
        return False


def last_progress():
    """当前进度 (days, max_date)，查询失败返回 (None, None)。"""
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            row = c.execute(
                "SELECT COUNT(DISTINCT date), MAX(date) FROM daily_run"
            ).fetchone()
            return row[0], row[1]
        finally:
            c.close()
    except Exception:
        return None, None


def resume():
    """调 base_run.py --resume 续跑，返回 returncode。"""
    cmd = [
        sys.executable, str(ROOT / "scripts" / "base_run.py"),
        "--cont", "--from", FROM, "--to", TO, "--mirrors", "A",
        "--src", str(SRC), "--exe", str(EXE), "--db", str(DB), "--resume",
    ]
    log("续跑启动: " + " ".join(cmd))
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=int, default=30, help="轮询间隔秒")
    ap.add_argument("--max-fail", type=int, default=6, help="连续续跑失败上限")
    a = ap.parse_args()

    log(f"守护启动：DB={DB.name} 目标 MIN_END={MIN_END} 轮询={a.poll}s")
    fails = 0
    while True:
        if db_complete():
            d, m = last_progress()
            log(f"完成。进度 {d} 日 / {m}。守护退出。")
            return 0

        alive = proc_alive()
        d, m = last_progress()
        log(f"存活={alive} 进度={d} 日 / {m}")

        if alive:
            fails = 0
            time.sleep(a.poll)
            continue

        # 进程死了且未完成 → 续跑
        log("进程已停，尝试断点续跑…")
        rc = resume()
        if rc != 0:
            fails += 1
            log(f"续跑返回 rc={rc}，失败累计 {fails}/{a.max_fail}")
            if fails >= a.max_fail:
                log("连续失败达上限，守护退出（需人工介入）")
                return 2
        else:
            # 续跑命令正常返回：检查是否真的推进了（避免「一启动就结束」假成功）
            time.sleep(10)
            d2, m2 = last_progress()
            if d2 == d and not proc_alive():
                fails += 1
                log(f"续跑后进度未动且进程已停，失败累计 {fails}/{a.max_fail}")
                if fails >= a.max_fail:
                    log("连续失败达上限，守护退出")
                    return 2
            else:
                fails = 0
        time.sleep(a.poll)


if __name__ == "__main__":
    raise SystemExit(main())
