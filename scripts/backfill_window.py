# -*- coding: utf-8 -*-
"""20交易日历史回放（M7.1 观察期模拟验收）。

用法（在仓库根目录）:
  python scripts/backfill_window.py                          # 2026-10-16 往前20个交易日
  python scripts/backfill_window.py --start 2024-09-21 --end 2024-10-31 \
      --out var/backtest2024.db                              # 指定区间（924行情窗口）

设计要点:
  1. 交易日清单取自源库 sh000001 K线（ADR-2 日历权威），法定假日自动跳过；
  2. sqlite backup API 拷贝源库→回测副本（兼容 WAL，源库全程只读零写入）；
  3. 副本上清空事务态（信号/计划/成交/持仓/权益/池/候选/运行留痕），
     模拟"新系统从零无人值守 N 天"；box 表保留——清空会让早期 screen 缺箱体上下文，
     且与调度器 CatchUp 补跑语义一致（catch-up 从不清箱体）；
  4. 逐日按调度器同款顺序跑单任务链 ingest→boxscan→screen→poolsync→signal→
     plan→report（单任务 CLI 不弹浏览器，upsert 幂等可安全重跑）；
  5. 末日汇总区间内 breakout/pyramid 可交易信号。

说明: 突破日必伴随创60日新高或量≥均量×倍数（达拉斯箱体突破的量价定义），
     必命中 boxscan 预过滤 → 逐日重放会以"截至该日"的K线窗口正确重放状态，
     区间内信号不因历史回放而漏报。
"""
import argparse
import os
import sqlite3
import subprocess
import sys

TASKS = ["ingest", "boxscan", "screen", "poolsync", "signal", "plan", "report"]

# 事务态表：清空后从零演化；box/daily_bar/security/param 保留。
RESET_TABLES = [
    "signal", "plan_order", "trade_plan", "fill", "position",
    "equity_snapshot", "watch", "candidate", "daily_run",
]


def trade_days(src, start, end, days):
    """取区间交易日清单（sh000001 为日历权威）。"""
    if start:
        rows = src.execute(
            "SELECT DISTINCT substr(date,1,10) FROM daily_bar WHERE code='sh000001'"
            " AND substr(date,1,10) BETWEEN ? AND ? ORDER BY 1", (start, end)).fetchall()
    else:
        rows = src.execute(
            "SELECT DISTINCT substr(date,1,10) FROM daily_bar WHERE code='sh000001'"
            " AND substr(date,1,10) <= ? ORDER BY 1 DESC LIMIT ?", (end, days)).fetchall()
        rows = rows[::-1]
    return [r[0] for r in rows]


def main():
    ap = argparse.ArgumentParser(
        description="20交易日历史回放（M7.1 观察期模拟验收）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例: python scripts/backfill_window.py --end 2026-10-16 --days 20")
    ap.add_argument("--db", default="var/perf_test.db", help="源库（只读，绝不写入）")
    ap.add_argument("--out", default="var/backtest20.db", help="回测副本库")
    ap.add_argument("--end", default="2026-10-16", help="区间终点交易日")
    ap.add_argument("--days", type=int, default=20, help="从终点往前取N个交易日")
    ap.add_argument("--start", default="", help="显式起点（如 2024-09-21），给定则忽略 --days")
    ap.add_argument("--exe", default="darvas_bt.exe", help="darvas 可执行文件路径")
    a = ap.parse_args()

    a.db = os.path.abspath(a.db)
    a.out = os.path.abspath(a.out)
    if not os.path.exists(a.db):
        sys.exit(f"源库不存在: {a.db}")

    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    days = trade_days(src, a.start, a.end, a.days)
    src.close()
    if not days:
        sys.exit("区间内无交易日：请检查 --end/--days/--start 与源库数据")
    print(f"[window] {days[0]} ~ {days[-1]}，共 {len(days)} 个交易日", flush=True)

    # 拷贝源库→副本（backup API 兼容 WAL；源库全程只读）
    for p in (a.out, a.out + "-wal", a.out + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    dst = sqlite3.connect(a.out)
    src.backup(dst)
    dst.close()
    src.close()

    # 副本上清空事务态：模拟新系统从零无人值守 N 天
    db = sqlite3.connect(a.out)
    for t in RESET_TABLES:
        db.execute(f"DELETE FROM {t}")
    db.commit()
    db.close()
    print(f"[reset] 已清空事务态: {', '.join(RESET_TABLES)}", flush=True)

    exe = os.path.abspath(a.exe)
    if not os.path.exists(exe):
        sys.exit(f"可执行文件不存在: {exe}（先 go build -o darvas_bt.exe ./cmd/darvas）")

    ok = 0
    for d in days:
        print(f"=== {d} ===", flush=True)
        day_ok = True
        for task in TASKS:
            r = subprocess.run([exe, task, "--date", d, "--db", a.out])
            if r.returncode != 0:
                print(f"!! {d} {task} 失败 exit={r.returncode}", flush=True)
                day_ok = False
        if day_ok:
            ok += 1
    print(f"[runs] {ok}/{len(days)} 日全链成功", flush=True)

    # 汇总区间可交易信号
    db = sqlite3.connect(f"file:{a.out}?mode=ro", uri=True)
    sig = db.execute(
        "SELECT substr(date,1,10), code, type FROM signal"
        " WHERE substr(date,1,10) BETWEEN ? AND ? AND type IN ('breakout','pyramid')"
        " ORDER BY 1, 2", (days[0], days[-1])).fetchall()
    db.close()
    print(f"\n=== 区间 {days[0]}~{days[-1]} 可交易信号（breakout/pyramid）: {len(sig)} 条 ===")
    for s in sig:
        print("  %s  %s  %s" % s)
    print(f"回测库: {a.out}（源库 {a.db} 未受影响）")


if __name__ == "__main__":
    main()
