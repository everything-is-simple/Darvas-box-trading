#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 4.4GB 回测 scratch 库压缩成分析用小库（去掉 daily_bar 主体）。

用法：python scripts/slim_bt_db.py <scratch.db> <slim.db>

保留表：security / position / position_leg / trade_fill / plan_order / trade_plan /
signal / equity_snapshot / watch / box / daily_run / candidate；
daily_bar 仅保留 sh000001（交易日历 + 指数权益口径所需）。
"""
import os
import sqlite3
import sys

TABLES = [
    "security", "position", "position_leg", "trade_fill", "plan_order",
    "trade_plan", "signal", "equity_snapshot", "watch", "box", "daily_run",
    "candidate",
]


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: slim_bt_db.py <scratch.db> <slim.db>")
    src, dst = sys.argv[1], sys.argv[2]
    if os.path.exists(dst):
        os.remove(dst)
    con = sqlite3.connect(dst)
    con.execute("ATTACH DATABASE ? AS s", (f"file:{src}?mode=ro",))
    for t in TABLES:
        exists = con.execute(
            "SELECT 1 FROM s.sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
        if not exists:
            continue
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM s.{t}")
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n}")
    con.execute(
        "CREATE TABLE daily_bar AS SELECT * FROM s.daily_bar WHERE code='sh000001'"
    )
    print("daily_bar(sh000001):", con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0])
    con.commit()
    con.execute("DETACH DATABASE s")
    con.close()
    mb = os.path.getsize(dst) / 1e6
    print(f"ok: {dst} ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
