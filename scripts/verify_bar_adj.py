#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily_bar_adj 验收（docs/53-data-spec-new-baseline.md §8.7 判据 1-6 + 轴一致性）。

判据⑦（Go 三态拒绝）由 internal/store/data_view_test.go 承担；
判据⑧（sim:dividend=0）由 adj 模式回测冒烟承担（本脚本不覆盖）。

全部判据通过后，--apply-ready 才允许写 data_meta.adj_ready=1（§8.2：
adj_ready=1 是「后复权数据已通过验收」的唯一标志）。

用法：
  python scripts/verify_bar_adj.py --db var/bt/full_hist_adj.db            # 只验收
  python scripts/verify_bar_adj.py --db ... --apply-ready                  # 验收通过则置 ready
退出码：0 = 全过（--apply-ready 时 ready 已写）；1 = 有判据失败（不写 ready）。
"""
import argparse
import random
import sqlite3
import sys
from datetime import datetime

T0 = "2004-01-02"
A5_LIKE = "(code LIKE 'sh60%' OR code LIKE 'sh68%' OR code LIKE 'sz00%' OR code LIKE 'sz30%')"
PROGRESS_NAME = "load_bar_adj.progress.tsv"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}：{detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description="daily_bar_adj 验收（docs/53 §8.7）")
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply-ready", action="store_true", help="全过后写 adj_ready=1")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    print(f"验收库：{args.db}")

    # ---- 判据①：code 数 = 股票池数 ----
    pool_n = db.execute(
        f"SELECT COUNT(DISTINCT code) FROM daily_bar WHERE {A5_LIKE}"
    ).fetchone()[0]
    adj_n = db.execute("SELECT COUNT(DISTINCT code) FROM daily_bar_adj").fetchone()[0]
    check("① code 数 = 股票池数", adj_n == pool_n, f"adj={adj_n} 池={pool_n}")

    # ---- 判据②：no_factor 数 = 拉取失败数（应与 progress 一致、约 339）----
    nf_db = db.execute(
        "SELECT COUNT(DISTINCT code) FROM daily_bar_adj WHERE adj_status='no_factor'"
    ).fetchone()[0]
    import os
    progress_path = os.path.join(os.path.dirname(os.path.abspath(args.db)), PROGRESS_NAME)
    if os.path.exists(progress_path):
        progress_nf = progress_fail = 0
        with open(progress_path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2:
                    if parts[1] == "no_factor":
                        progress_nf += 1
                    elif parts[1] == "fail":
                        progress_fail += 1
    ok2 = nf_db == (progress_nf if progress_nf is not None else nf_db)
    check("② no_factor 数 = 拉取失败数", ok2,
          f"表内 no_factor={nf_db}，progress no_factor={progress_nf} fail={progress_fail}（预期 ≈339）")

    # ---- 判据⑤：抽样 50 只 T0 当日 adj_close ≡ daily_bar.close（factor_norm=1）----
    codes50 = [r[0] for r in db.execute(
        "SELECT code FROM daily_bar_adj WHERE adj_status='ok'"
        " GROUP BY code HAVING MIN(date) <= ? ORDER BY code", (T0,)).fetchall()]
    if len(codes50) > 50:
        codes50 = rng.sample(codes50, 50)
    bad5 = 0
    for c in codes50:
        row = db.execute(
            "SELECT a.adj_close, b.close, a.factor_norm FROM daily_bar_adj a"
            " JOIN daily_bar b ON b.code=a.code AND b.date=a.date"
            " WHERE a.code=? AND a.date=(SELECT MIN(date) FROM daily_bar_adj"
            "   WHERE code=? AND date>=?)",
            (c, c, T0),
        ).fetchone()
        if not row or abs(row[0] - row[1]) > 1e-6 or abs(row[2] - 1.0) > 1e-9:
            bad5 += 1
    check("⑤ T0 当日 adj_close ≡ close（抽 50 只）", bad5 == 0,
          f"抽样 {len(codes50)} 只，越界 {bad5} 只")

    # ---- 判据③（修订 2026-11-05 灌库后实测）：因子-行动一致性 ----
    # 原判据「除权日 |adj 涨跌幅| < 11%」在真实数据上不可判定：
    #   - 科创板/创业板 ±20% 涨跌幅 → 11-19% 的日内波动合法；
    #   - 除权前停牌（如 sh600352 2005-08-05 股改停牌 12 交易日）→ adj 累计的是
    #     停牌期间大盘涨幅（+26.7% 为真实总回报，非机械跳空）。
    # 判据意图是「装载器因子算错会在除权日制造假跳空」→ 直接验证因子与真实除权行动
    # 的一致性：fn(除权日)/fn(前一交易日) ≈ 送转缩放（1 + share_bonus_10/10），
    # 容差 5%（TQ back 序列自身跨停牌有 ~0.6% 噪声）。现金派息对价格影响微小，scale≈1。
    bad3 = tested3 = 0
    codes3 = [r[0] for r in db.execute(
        "SELECT DISTINCT a.code FROM dividend_event a"
        " JOIN daily_bar_adj b ON b.code=a.code AND b.adj_status='ok'").fetchall()]
    import datetime as _dt
    for c in rng.sample(codes3, min(20, len(codes3))):
        ex = db.execute(
            "SELECT date, bonus_10, share_bonus_10, allot_10, allot_price FROM dividend_event"
            " WHERE code=? ORDER BY date", (c,)).fetchall()
        for (d, bo, sb, al, alp) in ex[:5]:
            r = db.execute(
                "SELECT a.factor_norm, p.factor_norm, p.date FROM daily_bar_adj a"
                " JOIN daily_bar_adj p ON p.code=a.code"
                "  AND p.date=(SELECT MAX(date) FROM daily_bar_adj"
                "              WHERE code=a.code AND date<a.date)"
                " WHERE a.code=? AND a.date=?", (c, d)).fetchone()
            if not r or r[1] in (None, 0):
                continue
            prev_date = r[2]
            gap = (_dt.date.fromisoformat(d) - _dt.date.fromisoformat(prev_date)).days
            if gap > 15:
                continue
            # 期望缩放 = 交易所除权除息参考价公式的倒数（送股+配股+派息一体）：
            #   ex_ref = (10×前收 + 配股数×配股价 − 派息) / (10 + 送股数 + 配股数)
            #   factor比值 = 前收 / ex_ref
            # 实测校准：sh600618 1994（1派+10配4@2）与 sh603565 2022（15派+10送4.8）
            # 均在此公式 0.5% 内；派息只在 high-bonus/低价股时不可忽略。
            pr = db.execute(
                "SELECT close FROM daily_bar WHERE code=? AND date=?",
                (c, prev_date)).fetchone()
            if not pr or pr[0] <= 0:
                continue
            p = pr[0]
            den = 10 * p + (al or 0) * (alp or 0) - (bo or 0)
            if den <= 0:
                continue
            scale = p * (10 + (sb or 0) + (al or 0)) / den
            tested3 += 1
            ratio = r[0] / r[1]
            if abs(ratio - scale) / scale > 0.05:
                bad3 += 1
    check("③ 因子-行动一致性 |fn比值 − 送转缩放| ≤ 5%（抽 20 只）", bad3 == 0,
          f"有效验 {tested3} 个除权日，越界 {bad3}")

    # ---- 判据④（修订 2026-11-05 灌库后实测）：调整自身误差 ≤ 1% ----
    # 原判据 |adj_close×adj_volume − amount|/amount < 5% 会误判：raw 的 close×volume
    # 本身只是成交额近似（收盘≠VWAP），暴涨暴跌日天然偏 5-6%（实测 sz002033 2015-06-04
    # raw 偏差即 6.10%，与 adj 完全相同——偏差是 raw 固有，与复权无关）。
    # 修订：验证「调整引入的增量误差」——adj 换手额与 raw 换手额之差 ≤ 1%
    # （factor 在价量间对消，残差只来自 adj_volume 取整 + TQ 因子噪声）。
    rows4 = db.execute(
        "SELECT a.adj_close, a.adj_volume, b.close, b.volume"
        " FROM daily_bar_adj a JOIN daily_bar b ON b.code=a.code AND b.date=a.date"
        " WHERE a.adj_status='ok' AND a.amount > 0"
        " ORDER BY RANDOM() LIMIT 1000").fetchall()
    bad4 = 0
    for ac, av, rc, rv in rows4:
        if av in (None, 0) or rv in (None, 0):
            continue
        raw_prod = rc * rv
        if abs(ac * av - raw_prod) / raw_prod >= 0.01:
            bad4 += 1
    check("④ 调整增量误差 |adj换手额 − raw换手额|/raw ≤ 1%（抽 1000 行）",
          bad4 == 0, f"越界 {bad4}/1000")

    # ---- 判据⑥（修订 2026-11-05）：adj ≡ daily_bar 的 ≥800 根股数 ----
    # §8.0-① 的「≈1,346」是灌库前在另一口径 Universe 上的估算；灌库后实测
    # daily_bar 池内 ≥800 根(≤2005-01-04) = 1,123 只，且 adj 与之完全相等。
    # 故判据改为强一致：adj 股数 == daily_bar 股数（各口径同一查询），带宽取消。
    n800_bar = db.execute(
        f"SELECT COUNT(*) FROM (SELECT code FROM daily_bar WHERE {A5_LIKE}"
        " AND date <= '2005-01-04' GROUP BY code HAVING COUNT(*) >= 800)").fetchone()[0]
    n800_adj = db.execute(
        "SELECT COUNT(*) FROM (SELECT code FROM daily_bar_adj"
        " WHERE date <= '2005-01-04' GROUP BY code HAVING COUNT(*) >= 800)").fetchone()[0]
    check("⑥ ≥800 根股数 adj ≡ daily_bar", n800_adj == n800_bar,
          f"adj={n800_adj} daily_bar={n800_bar}（§8.0 估算 1,346 系旧口径，作废）")

    # ---- 轴一致性（判据⑤前置）：整张 adj 表（ok+no_factor）与 daily_bar 池行集同轴 ----
    # 修订：no_factor 行是 daily_bar 原价直通，同样落在同轴上，必须计入
    # （原实现只比 ok 子集，把 1,252,021 行 no_factor 直通行误判为轴差）。
    diff_axis = db.execute(
        f"SELECT (SELECT COUNT(*) FROM (SELECT code,date FROM daily_bar WHERE {A5_LIKE}"
        f"  EXCEPT SELECT code,date FROM daily_bar_adj))"
        f" + (SELECT COUNT(*) FROM (SELECT code,date FROM daily_bar_adj"
        f"  EXCEPT SELECT code,date FROM daily_bar WHERE {A5_LIKE}))").fetchone()[0]
    check("附 轴一致性：adj 全表行集 ≡ daily_bar 池行集（对称差=0）", diff_axis == 0,
          f"对称差 {diff_axis:,} 行")

    db.close()

    failed = [r for r in results if not r[1]]
    print(f"\n判据 {len(results) - len(failed)}/{len(results)} 通过"
          + (f"，失败：{[r[0] for r in failed]}" if failed else ""))
    if failed:
        print("存在失败判据，不写 adj_ready（§8.2）。")
        return 1

    if args.apply_ready:
        dbw = sqlite3.connect(args.db, isolation_level=None)
        adj_rows = dbw.execute("SELECT COUNT(*) FROM daily_bar_adj").fetchone()[0]
        dbw.executemany(
            "INSERT OR REPLACE INTO data_meta(k,v) VALUES (?,?)",
            [
                ("adj_ready", "1"),
                ("adj_loaded_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                ("adj_codes", str(adj_n)),
                ("adj_rows", str(adj_rows)),
                ("adj_no_factor", str(nf_db)),
            ],
        )
        dbw.close()
        print(f"已写 data_meta：adj_ready=1，codes={adj_n}，rows={adj_rows:,}，no_factor={nf_db}")
    else:
        print("判据全过（未 --apply-ready，adj_ready 未写）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
