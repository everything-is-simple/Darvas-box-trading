#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3-8 报表刷新监视器：轮询回测库，每有新库完成就重生成 MD + HTML 报表。

与回测执行器解耦（独立进程），不干扰正在跑的回测；只读库、只写报表文件。
全部 20 库完成后自动退出（--keep 可常驻）。

用法：
  python scripts/e17_p38_watch.py              # 跑到 20 库全完成即退出
  python scripts/e17_p38_watch.py --keep       # 常驻
  python scripts/e17_p38_watch.py --interval 30
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt" / "p38_bt"
OUT_MD = ROOT / "docs" / "experiments" / "e17_p38_progress.md"
OUT_HTML = ROOT / "docs" / "experiments" / "e17_p38_progress.html"
LOGP = BT / "watch.log"
STATS = ROOT / "scripts" / "e17_p38_stats.py"

# 2026-11-03 13:20 用户裁决：`all`（历史最高箱）与 `up_all` 已否决，不进队列（28 → 20 库）
TIERS = ["off", "up", "t60", "t120", "t250"]
YEARS = [2007, 2010, 2013, 2018]
TOTAL = len(TIERS) * len(YEARS)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    try:
        with LOGP.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def done_set():
    """返回已完成（equity_snapshot 覆盖到当年 12 月）的库名集合。"""
    import sqlite3
    out = set()
    for y in YEARS:
        for t in TIERS:
            p = BT / f"bt_p38_{t}_{y}.db"
            if not p.exists():
                continue
            try:
                c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                try:
                    maxd = c.execute("SELECT MAX(date) FROM equity_snapshot").fetchone()[0]
                    if maxd and maxd >= f"{y}-12-01":
                        out.add(f"{t}_{y}")
                finally:
                    c.close()
            except Exception:
                continue
    return out


def refresh():
    rc = subprocess.call([sys.executable, str(STATS), "--md", str(OUT_MD), "--html", str(OUT_HTML)])
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    last = None
    log(f"监视器启动：interval={a.interval}s，目标 {TOTAL} 库，报表 → {OUT_MD.name} / {OUT_HTML.name}")
    while True:
        cur = done_set()
        if cur != last:
            newly = cur - (last or set())
            if newly:
                log(f"新完成 {len(newly)} 库：{', '.join(sorted(newly))}（累计 {len(cur)}/{TOTAL}）")
            rc = refresh()
            log(f"报表已刷新 rc={rc}（{len(cur)}/{TOTAL}）")
            last = cur
        if len(cur) >= TOTAL and not a.keep:
            log(f"全部 {TOTAL} 库完成，监视器退出")
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
