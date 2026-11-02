#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把真实除权记录灌入回测源库 full_hist_10pct.db（T7 先修 bug 的数据落位）。

背景（2026-11-02）：撮合层除权识别从「价格反推 isDividendGap」改为「查真实除权
记录表 dividend_event」（migration 0022）。回测库经 Bootstrap 整库拷贝源库生成，
故 dividend_event 必须灌进源库，回测库才自动带上数据。

源库 schema_version=7（老库），没有 dividend_event 表——本脚本先在源库建表
（与 migration 0022 同 schema），再从 dividend_events.db 灌入 Type=1 记录。

幂等：INSERT OR REPLACE，重跑零重复。源库只追加新表，不动现有任何表。

用法：
  python scripts/e17_t7_load_dividend.py --apply        # 真正灌入（默认 dry-run）
"""
import argparse
import sqlite3
from pathlib import Path

ROOT = Path("I:/Darvas-box-trading")
SRC_DB = ROOT / "var" / "bt" / "full_hist_10pct.db"
EVT_DB = ROOT / "var" / "bt" / "dividend_events.db"

CREATE = """CREATE TABLE IF NOT EXISTS dividend_event (
    code           TEXT NOT NULL,
    date           TEXT NOT NULL,
    type           INTEGER NOT NULL DEFAULT 1,
    bonus_10       REAL NOT NULL DEFAULT 0,
    share_bonus_10 REAL NOT NULL DEFAULT 0,
    allot_10       REAL NOT NULL DEFAULT 0,
    allot_price    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (code, date)
);"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正灌入（默认 dry-run 只打印）")
    a = ap.parse_args()

    # 读事件
    ev = sqlite3.connect(f"file:{EVT_DB}?mode=ro&immutable=1", uri=True)
    rows = ev.execute(
        "SELECT code, date, type, bonus_10, share_bonus_10, allot_10, allot_price "
        "FROM dividend_event WHERE type = 1 ORDER BY code, date").fetchall()
    ev.close()
    print(f"事件源 {EVT_DB.name}: {len(rows)} 条 Type=1")

    # 检查源库现状
    src = sqlite3.connect(SRC_DB)
    has_tbl = src.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='dividend_event'").fetchone()[0]
    print(f"源库 {SRC_DB.name}: dividend_event 表{'已存在' if has_tbl else '不存在'}")

    if not a.apply:
        print(f"\n[dry-run] 将建表并灌入 {len(rows)} 条（--apply 才真正执行）")
        src.close()
        return

    # 建表 + 灌数据
    src.execute(CREATE)
    cur = src.cursor()
    cur.executemany(
        "INSERT OR REPLACE INTO dividend_event "
        "(code, date, type, bonus_10, share_bonus_10, allot_10, allot_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    src.commit()

    n = src.execute("SELECT COUNT(*), COUNT(DISTINCT code) FROM dividend_event").fetchone()
    print(f"\n灌入完成：{n[0]} 条事件，覆盖 {n[1]} 只股票")
    # 校验：与事件源一致
    assert n[0] == len(rows), f"灌入条数 {n[0]} != 源 {len(rows)}"
    print("校验通过：条数与事件源一致")
    src.close()


if __name__ == "__main__":
    main()
