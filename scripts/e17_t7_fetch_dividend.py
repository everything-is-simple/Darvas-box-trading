#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T7 数据抽取：从 TdxQuant 拉取真实除权除息记录，落库供引擎识别真实除权日。

用途（docs/experiments/e17_dividend_hold_framework.md §3.1）：
  - 引擎 `hold_adjusted` 档用「真实除权记录（Type=1）」替代 `isDividendGap` 价格反推；
  - 也用于校准 `isDividendGap` 的漏判/误判（docs/42 §3.1 三类风险）。

数据源：tqserver.get_divid_factors(code, "", "")  -> 返回该股全历史除权除息。
  - Type=1 除权除息 / 11 扩缩股 / 15 重新调整（本脚本只落 Type=1，其余单列登记）。
  - Bonus/ShareBonus/Allotment 为「每 10 股」口径（docs/42 §5.2 已钉死）。

落库：var/bt/dividend_events.db（独立库，不污染源库/回测库）。
表：dividend_event(code TEXT, date TEXT, type INT, bonus_10 REAL, share_bonus_10 REAL,
                  allot_10 REAL, allot_price REAL, PRIMARY KEY(code, date, type))

幂等：INSERT OR REPLACE；--codes 指定股票集；--limit 只拉前 N 只（标定用）。

用法：
  python scripts/e17_t7_fetch_dividend.py --limit 100          # 标定：box 池前 100 只
  python scripts/e17_t7_fetch_dividend.py                       # 全量：box 池 3,328 只
  python scripts/e17_t7_fetch_dividend.py --source daily_bar    # 全历史 11,940 只
"""
import sys, time, argparse, sqlite3
from pathlib import Path

sys.path.insert(0, r"D:\new_tdx64\PYPlugins\sys")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins")

ROOT = Path("I:/Darvas-box-trading")
OUT_DB = ROOT / "var" / "bt" / "dividend_events.db"

def parse_args():
    ap = argparse.ArgumentParser(description="T7 真实除权记录抽取")
    ap.add_argument("--limit", type=int, default=0, help="只拉前 N 只（0=全量）")
    ap.add_argument("--source", default="box", choices=["box", "daily_bar"],
                    help="股票集来源：box=代表年2017 box 池(3,328只)，daily_bar=全历史源库(11,940只)")
    ap.add_argument("--src-db", default="var/bt/full_hist_10pct.db",
                    help="daily_bar 源库路径")
    ap.add_argument("--box-db", default="var/bt/base/bt_baseA_2017.db",
                    help="box 池来源库（代表年 2017，box 表 3,328 只最全）")
    return ap.parse_args()


def collect_codes(args):
    """从指定源收集股票代码，返回 [('sh600000', ...)] 原始格式。"""
    if args.source == "daily_bar":
        db = ROOT / args.src_db
        sql = "SELECT DISTINCT code FROM daily_bar ORDER BY code"
    else:
        db = ROOT / args.box_db
        sql = "SELECT DISTINCT code FROM box ORDER BY code"
    c = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    rows = c.execute(sql).fetchall()
    c.close()
    codes = [r[0] for r in rows]
    return codes


def to_tq(code: str) -> str:
    """sh600000 -> 600000.SH；sz000001 -> 000001.SZ。"""
    code = code.lower()
    if code.startswith("sh"):
        return code[2:] + ".SH"
    if code.startswith("sz"):
        return code[2:] + ".SZ"
    return code


def init_db():
    Path(OUT_DB).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(OUT_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS dividend_event(
        code TEXT NOT NULL,
        date TEXT NOT NULL,
        type INTEGER NOT NULL,
        bonus_10 REAL,
        share_bonus_10 REAL,
        allot_10 REAL,
        allot_price REAL,
        PRIMARY KEY(code, date, type))""")
    c.execute("""CREATE TABLE IF NOT EXISTS fetch_log(
        code TEXT PRIMARY KEY, fetched_at TEXT, status TEXT, note TEXT)""")
    c.commit()
    return c


def main():
    args = parse_args()
    codes = collect_codes(args)
    if args.limit > 0:
        codes = codes[: args.limit]
    print(f"股票集：{len(codes)} 只（source={args.source}）")

    from tqserver import tqs  # 延迟 import，避免无依赖时崩溃

    c = init_db()
    t0 = time.time()
    ok = empty = err = 0
    total_rows = 0
    type_dist = {}
    skipped_non_type1 = 0

    for i, raw in enumerate(codes, 1):
        code = to_tq(raw)
        try:
            df = tqs.get_divid_factors(code, "", "")
            if df is None or (hasattr(df, "empty") and df.empty):
                empty += 1
                c.execute("INSERT OR REPLACE INTO fetch_log VALUES(?,?,?,?)",
                          (raw, time.strftime("%Y-%m-%d %H:%M:%S"), "empty", ""))
            else:
                ok += 1
                rows = 0
                for _, row in df.iterrows():
                    # 字段名兼容：官方返回 Date 索引 + Type/Bonus/AllotPrice/ShareBonus/Allotment
                    d = row.get("Date", None) if hasattr(row, "get") else row.name
                    if d is None:
                        d = row.name  # Date 是索引时
                    typ = int(row.get("Type", 0))
                    bonus = float(row.get("Bonus", 0) or 0)
                    share = float(row.get("ShareBonus", 0) or 0)
                    allot = float(row.get("Allotment", 0) or 0)
                    allot_px = float(row.get("AllotPrice", 0) or 0)
                    date_str = str(d)[:10]
                    type_dist[typ] = type_dist.get(typ, 0) + 1
                    if typ == 1:
                        c.execute(
                            "INSERT OR REPLACE INTO dividend_event VALUES(?,?,?,?,?,?,?)",
                            (raw, date_str, typ, bonus, share, allot, allot_px))
                        rows += 1
                    else:
                        skipped_non_type1 += 1
                total_rows += rows
                c.execute("INSERT OR REPLACE INTO fetch_log VALUES(?,?,?,?)",
                          (raw, time.strftime("%Y-%m-%d %H:%M:%S"), "ok", f"{rows} rows"))
        except Exception as e:
            err += 1
            c.execute("INSERT OR REPLACE INTO fetch_log VALUES(?,?,?,?)",
                      (raw, time.strftime("%Y-%m-%d %H:%M:%S"), "err", str(e)[:200]))

        if i % 200 == 0:
            c.commit()
            print(f"  ... {i}/{len(codes)} 完成，已耗时 {time.time()-t0:.0f}s")

    c.commit()
    dt = time.time() - t0

    # 汇总
    n_event = c.execute("SELECT COUNT(*) FROM dividend_event").fetchone()[0]
    n_code = c.execute("SELECT COUNT(DISTINCT code) FROM dividend_event").fetchone()[0]
    print(f"\n=== 抽取完成 ===")
    print(f"总耗时 {dt:.0f}s，单只平均 {dt/max(len(codes),1):.2f}s")
    print(f"成功 {ok} 只（有数据），空 {empty} 只，异常 {err} 只")
    print(f"Type=1 除权除息事件 {n_event} 条，覆盖 {n_code} 只股票")
    print(f"非 Type=1 事件（扩缩股/重调）跳过 {skipped_non_type1} 条；Type 分布 {type_dist}")
    print(f"落库：{OUT_DB}")
    c.close()


if __name__ == "__main__":
    main()
