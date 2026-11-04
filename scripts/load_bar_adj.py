#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily_bar_adj 后复权装载器（docs/53-data-spec-new-baseline.md §8.6）。

数据源 = 通达信 tqcenter（进程内插件，依赖通达信客户端已启动；先例 scripts/tq_probe7.py）。
股票池 = 目标库 daily_bar 的 A5 前缀个股（sh60*/sh68*/sz00*/sz30*，实测 5,561 只），
**不用** get_stock_list（§4 明令：会漏 339 只退市股）。

口径（§8.0-③ / §8.6）：
  - 行生成轴 = daily_bar（判据⑤同轴）：逐股取本地 (date, ohlc, volume, amount)，
    TQ 返回按 (code,date) 对齐取 back_close；
  - factor = back_close / none_close（TQ 内部自洽）；factor_norm = factor / factor(T0)；
  - T0 = 2004-01-02 三档兜底（§8.0-②）：当日有 bar 取当日，否则之后首个有 bar 日，
    2004 后完全无数据 → 不归一化（factor_norm=1, adj_status='no_factor'）；
  - adj_* = none_* × factor_norm（none 用本地 daily_bar 原价）；
  - adj_volume = max(1, round(none_volume / factor_norm))；amount 原值拷贝（B6）；
  - TQ 整股无数据（≈339 只退市股）→ daily_bar 直通，factor_norm=1, adj_status='no_factor'；
  - TQ 个别日期缺失（停牌差异）→ 因子前向填充，再前向缺失用后向首个因子。

两阶段纪律（§8.2）：本脚本**绝不写 adj_ready**——灌完跑 verify_bar_adj.py，
8 条判据全过才由 verify --apply-ready 写 adj_ready=1。

用法：
  python scripts/load_bar_adj.py --db var/bt/full_hist_adj.db            # 全量（断点续灌）
  python scripts/load_bar_adj.py --db ... --limit 20                     # 小批量计时/冒烟
  python scripts/load_bar_adj.py --db ... --batch 50                     # 调分批大小
幂等：INSERT OR REPLACE + progress 文件跳过已完成股，可反复重跑。
"""
import argparse
import os
import sqlite3
import sys
import time
import traceback

sys.path.insert(0, r"D:\new_tdx64\PYPlugins\sys")
sys.path.insert(0, r"D:\new_tdx64\PYPlugins")

from tqcenter import tq  # noqa: E402

T0 = "2004-01-02"
A5_LIKE = "(code LIKE 'sh60%' OR code LIKE 'sh68%' OR code LIKE 'sz00%' OR code LIKE 'sz30%')"
INSERT_SQL = (
    "INSERT OR REPLACE INTO daily_bar_adj"
    " (code, date, adj_open, adj_high, adj_low, adj_close, adj_volume,"
    "  amount, factor_norm, adj_status) VALUES (?,?,?,?,?,?,?,?,?,?)"
)
PROGRESS_NAME = "load_bar_adj.progress.tsv"
FAIL_NAME = "load_bar_adj.load_fail.tsv"


def to_tq(local: str) -> str:
    """sh600519 -> 600519.SH；sz000001 -> 000001.SZ。"""
    return f"{local[2:]}.{local[:2].upper()}"


def norm_date(d) -> str:
    """TQ 日期（'2016-05-20' / '20160520' / Timestamp）统一 YYYY-MM-DD。"""
    s = str(d)[:10]
    if "-" in s:
        return s
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def load_universe(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(f"SELECT DISTINCT code FROM daily_bar WHERE {A5_LIKE} ORDER BY code").fetchall()
    return [r[0] for r in rows]


def fetch_pass(codes_tq: list[str], dividend_type: str) -> dict:
    """一批股票的全历史单遍拉取。mode A 由 tqcenter 自动合页。

    返回 {tq_code: {date: (open, high, low, close, volume)}}。
    """
    fields = ["Date", "Open", "High", "Low", "Close", "Volume"]
    df = tq.get_market_data(
        field_list=fields, stock_list=codes_tq,
        period="1d", start_time="19900101", end_time="20991231",
        count=-1, dividend_type=dividend_type, fill_data=False,
    )
    out: dict = {}
    close_df = df.get("Close")
    if close_df is None:
        return out
    for tc in codes_tq:
        if tc not in close_df.columns:
            continue  # TQ 无此股（退市）→ 调用方按 no_factor 处理
        series = close_df[tc].dropna()
        if series.empty:
            continue
        dates = [norm_date(d) for d in series.index]
        cols = {}
        for f, key in (("Open", "Open"), ("High", "High"), ("Low", "Low"),
                       ("Close", "Close"), ("Volume", "Volume")):
            fdf = df.get(f)
            if fdf is None or tc not in fdf.columns:
                cols[key] = [float("nan")] * len(dates)
            else:
                s = fdf[tc].reindex(series.index)
                cols[key] = [float(v) for v in s.values]
        out[tc] = {
            dates[i]: (cols["Open"][i], cols["High"][i], cols["Low"][i],
                       cols["Close"][i], cols["Volume"][i])
            for i in range(len(dates))
        }
    return out


def process_stock(local_code: str, tq_code: str, local_rows: list,
                  none_map: dict, back_map: dict) -> tuple[list, str, int]:
    """单股：对齐 → 因子 → 后复权行。返回 (rows, status, n_filled)。

    n_filled = TQ 缺失日用填充因子补的行数（透明度指标，写 progress）。
    """
    if not back_map:
        # 整股无 TQ 数据（退市股）：daily_bar 直通
        rows = [(local_code, r[0], r[1], r[2], r[3], r[4], r[5], r[6], 1.0, "no_factor")
                for r in local_rows]
        return rows, "no_factor", 0

    # 因子序列（仅在 TQ 有 back+none 的日期上定义）
    # back_map 同样是 (o,h,l,c,v) 元组，取 [3] = back_close
    factors: dict = {}
    for d, (o, h, l, c, v) in none_map.items():
        bt = back_map.get(d)
        bc = bt[3] if bt else None
        if bc and c and bc > 0 and c > 0:
            factors[d] = bc / c
    if not factors:
        rows = [(local_code, r[0], r[1], r[2], r[3], r[4], r[5], r[6], 1.0, "no_factor")
                for r in local_rows]
        return rows, "no_factor", 0

    # T0 兜底：本地首个 >= T0 的 bar 日 → 该日（或其后首个 TQ 因子日）的因子
    t0_eff = next((r[0] for r in local_rows if r[0] >= T0), None)
    if t0_eff is None:
        t0_eff = local_rows[0][0] if local_rows else min(factors)
    f0 = None
    if t0_eff in factors:
        f0 = factors[t0_eff]
    else:
        later = sorted(d for d in factors if d >= t0_eff)
        f0 = factors[later[0]] if later else factors[sorted(factors)[0]]

    sorted_f = sorted(factors)
    rows = []
    n_filled = 0
    for (date, o, h, l, c, vol, amount) in local_rows:
        f = factors.get(date)
        if f is None:
            n_filled += 1
            earlier = [d for d in sorted_f if d < date]
            f = factors[earlier[-1]] if earlier else factors[sorted_f[0]]
        fn = f / f0
        rows.append((
            local_code, date,
            round(o * fn, 4), round(h * fn, 4), round(l * fn, 4), round(c * fn, 4),
            max(1, round(vol / fn)), amount, fn, "ok",
        ))
    return rows, "ok", n_filled


def main() -> int:
    ap = argparse.ArgumentParser(description="daily_bar_adj 后复权装载器（docs/53 §8.6）")
    ap.add_argument("--db", required=True, help="目标库路径（须已迁移 0032）")
    ap.add_argument("--batch", type=int, default=100, help="TQ 分批大小（上限 200，默认 100）")
    ap.add_argument("--limit", type=int, default=0, help="只灌前 N 只（计时/冒烟用）")
    ap.add_argument("--resume", action="store_true", default=True, help="跳过 progress 中已完成股（默认开）")
    ap.add_argument("--no-resume", dest="resume", action="store_false")
    args = ap.parse_args()

    progress_path = os.path.join(os.path.dirname(os.path.abspath(args.db)), PROGRESS_NAME)
    fail_path = os.path.join(os.path.dirname(os.path.abspath(args.db)), FAIL_NAME)

    db = sqlite3.connect(args.db, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")

    universe = load_universe(db)
    if args.limit:
        universe = universe[: args.limit]
    print(f"股票池 {len(universe)} 只（A5 前缀，库内 daily_bar 实测）")

    done: dict[str, str] = {}
    if args.resume and os.path.exists(progress_path):
        with open(progress_path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2 and parts[1] in ("ok", "no_factor"):
                    done[parts[0]] = parts[1]
        print(f"--resume：跳过已完成 {len(done)} 只")

    todo = [c for c in universe if c not in done]
    if not todo:
        print("无待灌股票（全部完成或空池）。")
        return 0

    # data_meta：开灌标记（**不写 adj_ready**，§8.2）
    db.execute(
        "INSERT OR REPLACE INTO data_meta(k,v) VALUES"
        " ('adj_t0', ?), ('adj_loader_ver', ?), ('adj_started_at', datetime('now','localtime'))",
        (T0, "load_bar_adj.py v1"),
    )

    tq.initialize(__file__)
    t_start = time.time()
    total_new = 0
    n_ok = n_nofactor = n_fail = 0

    for bi in range(0, len(todo), args.batch):
        batch = todo[bi : bi + args.batch]
        codes_tq = [to_tq(c) for c in batch]
        try:
            none_data = fetch_pass(codes_tq, "none")
            back_data = fetch_pass(codes_tq, "back")
        except Exception as e:  # noqa: BLE001
            # 整批失败：逐股降级为单股拉取，能救一只是一只
            print(f"批次 {bi}-{bi+len(batch)} 整批失败（{e}），降级单股重试")
            none_data, back_data = {}, {}
            for c in batch:
                tc = to_tq(c)
                try:
                    n1 = fetch_pass([tc], "none")
                    n2 = fetch_pass([tc], "back")
                    none_data.update(n1)
                    back_data.update(n2)
                except Exception as e2:  # noqa: BLE001
                    none_data.pop(tc, None)
                    back_data.pop(tc, None)
                    print(f"  单股重试仍失败 {tc}: {e2}")

        for local_code, tc in zip(batch, codes_tq):
            try:
                local_rows = db.execute(
                    "SELECT date, open, high, low, close, volume, amount"
                    " FROM daily_bar WHERE code = ? ORDER BY date",
                    (local_code,),
                ).fetchall()
                if not local_rows:
                    continue
                rows, status, n_filled = process_stock(
                    local_code, tc, local_rows,
                    none_data.get(tc, {}), back_data.get(tc, {}),
                )
                db.execute("BEGIN IMMEDIATE")
                db.executemany(INSERT_SQL, rows)
                db.execute("COMMIT")
                total_new += len(rows)
                n_ok += status == "ok"
                n_nofactor += status == "no_factor"
                with open(progress_path, "a", encoding="utf-8") as f:
                    f.write(f"{local_code}\t{status}\t{len(rows)}\t{n_filled}\n")
            except Exception as e:  # noqa: BLE001
                db.execute("ROLLBACK") if db.in_transaction else None
                n_fail += 1
                with open(fail_path, "a", encoding="utf-8") as f:
                    f.write(f"{local_code}\t{e}\n")
                with open(progress_path, "a", encoding="utf-8") as f:
                    f.write(f"{local_code}\tfail\t0\t0\n")
                print(f"  ✗ {local_code}: {e}")

        el = time.time() - t_start
        done_ct = bi + len(batch)
        rate = done_ct / el if el else 0
        eta = (len(todo) - done_ct) / rate if rate else 0
        print(
            f"[{done_ct}/{len(todo)}] ok={n_ok} no_factor={n_nofactor} fail={n_fail} "
            f"rows={total_new:,} | {rate:.2f} 只/s | ETA {eta/60:.1f} 分",
            flush=True,
        )

    db.close()
    print(f"完成：ok={n_ok} no_factor={n_nofactor} fail={n_fail}，共 {total_new:,} 行。"
          f"下一步：python scripts/verify_bar_adj.py --db {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
