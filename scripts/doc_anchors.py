#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_anchors.py —— 文档数字锚点台账（C8 唯一事实源链）

为什么要有这个
--------------
2026-11-06 之前，`55 §11` 的收官数字（1,142 笔 / +1,213,822 / ×2.214 …）只有人工核对：
账本算一遍、抄进文档、再被 `48`/`60`/`README` 转述三次。任何一次手抄、重跑、改口径，
都会让「文档说的」和「账本算的」悄悄分叉，而分叉不会报错——只会让后面所有结论建立在错数上。

本脚本把这条链钉成四段，每段都可机检（`check_consistency.py` 的 C8）：

    账本 DB  ──重算──▶  docs/anchors/ledger_anchors.json  （快照，入库可 diff）
                            │
                            ├──▶  docs/55 §11.10 生成区（唯一给人看的台账）
                            └──▶  C8c：48/60/README 里的「锚点句」逐个数字必须在册

用法
----
    python scripts/doc_anchors.py                  # 打印当前台账（从快照读，不碰库）
    python scripts/doc_anchors.py --recompute      # 从账本重算并打印（不写盘）
    python scripts/doc_anchors.py --write          # 重算 → 写快照 + 重写 55 生成区
    python scripts/doc_anchors.py --check          # 只校 55 生成区 == 快照（C8a）

维护约定
--------
* **禁止手改 `docs/55 §11.10` 的表**，也禁止手改快照 JSON；改账本后跑 `--write`。
* 新增/修改锚点：改本文件的 `ANCHORS`（键 → 中文名/说明）与 `FMT`，再 `--write`。
* 锚点只放「收官级、别处会引用」的数字。实验中间值不进锚点台账（它们在 `docs/experiments/`）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import re
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = "var/bt/base/bt_baseA_cont_adj.db"
SNAPSHOT = ROOT / "docs" / "anchors" / "ledger_anchors.json"
DOC55 = ROOT / "docs" / "55-ledger-and-lessons.md"

BEGIN = "<!-- BEGIN GENERATED:anchors -->"
END = "<!-- END GENERATED:anchors -->"

UNIT_PCT = 0.02          # 1 单位 = 2% 权益（A M1 定案）
EQUITY_BASE = 1_000_000.0


# ---------------------------------------------------------------- 锚点定义
# (键, 中文名, 说明)。顺序即渲染顺序：先总账，再概率，再分档。
ANCHORS: list[tuple[str, str, str]] = [
    ("days",        "交易日",            "连续库 equity_snapshot 行数（= 六任务全 ok 的日数）"),
    ("trades_n",    "已平仓笔数",        "status='closed' 且 closed_date ≤ 有效边界（R34 按平仓日截断）"),
    ("net_pnl",     "Σ已实现净利（加法账）", "Σrealized_pnl，与复利账分开报"),
    ("equity_mult", "权益倍数（复利账）",  "期末 total / 期初 100 万"),
    ("cagr",        "CAGR",             "(期末/期初)^(365.25/自然日数) − 1"),
    ("mdd",         "最大回撤",          "equity_snapshot.drawdown 最小值"),
    ("win_rate",    "胜率",             "pnl>0 的笔数 / 总笔数"),
    ("odds",        "赔率",             "均盈 / 均亏（绝对值）"),
    ("pf",          "PF",               "Σ盈利 / Σ亏损（绝对值）"),
    ("avg_pnl",     "均笔",             "Σpnl / 笔数"),
    ("t_stat",      "t 值",             "均笔 / (笔标准差/√n)，检验「均笔≠0」"),
    ("ex3_pnl",     "剔前 3 笔",         "笔级：去掉盈利最大的 3 笔后 Σpnl"),
    ("ex_best_year", "剔最好年",         "年账口径（按平仓日归年）：Σpnl − 最好一年"),
    ("ex2_year",    "剔前 2 年",         "年账口径：去掉 2007+2015 两年后 Σpnl（现行主判据）"),
    ("pos_years",   "盈利年数",          "按平仓日归年，pnl>0 的年数 / 总年数"),
    ("identity_gap", "恒等式差",         "max|total − (cash + market_value)|，单位元"),
    ("band1_n",     "档1 笔数",          "首腿 3 单位（距 52 周高点 ≤5%），±0.35 容差归带"),
    ("band1_pnl",   "档1 净利",          "同上"),
    ("band1_unit",  "档1 每单位",        "档1 Σpnl / Σ首腿单位"),
    ("band2_n",     "档2 笔数",          "首腿 2 单位（5–10%）"),
    ("band2_pnl",   "档2 净利",          "同上"),
    ("band2_unit",  "档2 每单位",        "档2 Σpnl / Σ首腿单位"),
    ("band3_n",     "档3 笔数",          "首腿 1 单位（10–20%）"),
    ("band3_pnl",   "档3 净利",          "同上"),
    ("band3_ex3",   "档3 剔前 3 笔",      "档3 内部去掉最好 3 笔（检验是否靠个别赢家）"),
    ("band3_unit",  "档3 每单位",        "档3 Σpnl / Σ首腿单位"),
    ("band4_n",     "档4 笔数",          "首腿 0.5 单位（20–40%）"),
    ("band4_pnl",   "档4 净利",          "同上（A1 消融对象）"),
    ("band4_unit",  "档4 每单位",        "档4 Σpnl / Σ首腿单位"),
    ("tx_n",        "未归类笔数",        "首腿单位不在四档 ±0.35 内（R23：必须显式报，不许藏）"),
    ("tx_pnl",      "未归类净利",        "同上"),

    # ── 二级锚点（2026-11-06 增，`64` 总典的过程数字必须同样受管）──────────
    # 持有天数分档：交易日数 = 平仓日与开仓日之间的交易日数 − 1（即「持有 N 个交易日」）
    ("hold_le2_share", "持有≤2日 占比",   "该档笔数 / 总笔数（表中第三列，必须与 n 同步）"),
    ("hold_le2_n",   "持有≤2日 笔数",    "按交易日计（含开仓日、不含平仓日），下同"),
    ("hold_le2_pnl", "持有≤2日 净利",    "同上"),
    ("hold_le2_wr",  "持有≤2日 胜率",    "同上"),
    ("hold_3_5_share", "持有3–5日 占比", "同上"),
    ("hold_3_5_n",   "持有3–5日 笔数",   "同上"),
    ("hold_3_5_pnl", "持有3–5日 净利",   "同上"),
    ("hold_3_5_wr",  "持有3–5日 胜率",   "同上"),
    ("hold_6_10_share", "持有6–10日 占比", "同上"),
    ("hold_6_10_n",  "持有6–10日 笔数",  "同上"),
    ("hold_6_10_pnl", "持有6–10日 净利", "同上"),
    ("hold_6_10_wr", "持有6–10日 胜率",  "同上"),
    ("hold_11_20_share", "持有11–20日 占比", "同上"),
    ("hold_11_20_n", "持有11–20日 笔数", "同上"),
    ("hold_11_20_pnl", "持有11–20日 净利", "同上"),
    ("hold_11_20_wr", "持有11–20日 胜率", "同上"),
    ("hold_gt20_share", "持有>20日 占比", "同上"),
    ("hold_gt20_n",  "持有>20日 笔数",   "同上（利润唯一来源，全系统最重的一格）"),
    ("hold_gt20_pnl", "持有>20日 净利",  "同上"),
    ("hold_gt20_wr", "持有>20日 胜率",   "同上"),
    # 出口分解：position.close_reason
    ("exit_stop_n",      "初始止损 笔数",   "close_reason='stop'"),
    ("exit_stop_pnl",    "初始止损 净利",   "同上"),
    ("exit_stop_box_n",  "破下轨 笔数",     "close_reason='stop_box'"),
    ("exit_stop_box_pnl", "破下轨 净利",    "同上"),
    ("exit_time_stop_n", "两日强平 笔数",   "close_reason='time_stop'（O2 违反点）"),
    ("exit_time_stop_pnl", "两日强平 净利", "同上（唯一大额净损出口）"),
    ("exit_failed_n",    "失败突破 笔数",   "close_reason='failed_breakout'"),
    ("exit_failed_pnl",  "失败突破 净利",   "同上"),
    # 集中度与尾部（口径同 scripts/base_progress_stat.py）
    ("skew",         "偏度",             "Σ((p−均值)/样本sd)³ / n"),
    ("top1pct_share", "前 1% 笔贡献",     "最大 round(n×1%) 笔 Σpnl / 总 Σpnl"),
    ("top5pct_share", "前 5% 笔贡献",     "同上，5%"),
    ("ex10_pnl",     "剔前 10 笔",        "笔级：去掉盈利最大的 10 笔后 Σpnl"),
    ("loss_streak_n", "最长连亏 笔数",     "按平仓日排序，连续 pnl≤0 的最长一段"),
    ("loss_streak_pnl", "最长连亏 金额",   "同一段的 Σpnl"),
]

BANDS = [("band1", 3.0), ("band2", 2.0), ("band3", 1.0), ("band4", 0.5)]


# ---------------------------------------------------------------- 账本重算
def _open_ro(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def last_complete(con: sqlite3.Connection) -> str | None:
    r = con.execute("""
        SELECT MAX(date) FROM (
          SELECT date FROM daily_run
          GROUP BY date
          HAVING COUNT(*)=6 AND SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)=6
        )""").fetchone()
    return r[0] if r else None


def compute(db: str = DEFAULT_DB) -> dict:
    """从账本重算全部锚点。只读。"""
    path = Path(db) if Path(db).is_absolute() else ROOT / db
    if not path.exists():
        raise SystemExit(f"账本不存在：{path}")
    con = _open_ro(str(path))
    try:
        cut = last_complete(con)
        eq_rows = con.execute(
            "SELECT date, total, cash, market_value, drawdown FROM equity_snapshot "
            "WHERE date <= ? ORDER BY date", (cut,)).fetchall()
        if not eq_rows:
            raise SystemExit(f"{path} 没有 equity_snapshot")
        eq = {d: t for d, t, _c, _m, _dd in eq_rows}
        leg = {p: q for p, q in con.execute(
            "SELECT position_id, qty*price FROM position_leg WHERE leg_no=1")}

        rows = []
        for pid, od, cd, pnl, reason in con.execute("""
                SELECT id, cast(opened_date as text), cast(closed_date as text),
                       coalesce(realized_pnl, 0), coalesce(close_reason, '')
                FROM position WHERE status='closed'"""):
            od, cd = od[:10], (cd or "")[:10]
            # R34：按平仓日截断（按建仓日截断会把窗口外才平仓的赢家提前计入）
            if not cd or cd > cut:
                continue
            notional = leg.get(pid, 0.0)
            e = eq.get(od, 0.0)
            rows.append(dict(cd=cd, od=od, pnl=float(pnl), reason=reason,
                             units=(notional / (e * UNIT_PCT) if e > 0 else 0.0)))

        # 交易日历（持有天数分档用）：全市场 distinct date 足够，不必按个股
        cal = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM daily_bar WHERE date <= ? ORDER BY date", (cut,))]

        bl = con.execute("SELECT baseline_id, engine_sha256, data_mode "
                         "FROM baseline ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        con.close()

    pnls = [r["pnl"] for r in rows]
    n = len(pnls)
    if n == 0:
        raise SystemExit(f"{path} 没有已平仓记录")
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_w, gross_l = sum(wins), -sum(losses)
    net = sum(pnls)
    mean_w = gross_w / len(wins) if wins else 0.0
    mean_l = gross_l / len(losses) if losses else 0.0

    first_d, last_d = eq_rows[0][0], eq_rows[-1][0]
    d0 = _dt.date(*map(int, first_d.split("-")))
    d1 = _dt.date(*map(int, last_d.split("-")))
    cal_days = max((d1 - d0).days, 1)
    mult = eq_rows[-1][1] / EQUITY_BASE
    cagr = mult ** (365.25 / cal_days) - 1 if mult > 0 else 0.0
    mdd = min(r[4] or 0.0 for r in eq_rows)
    ident = max(abs(t - (c + m)) for _d, t, c, m, _dd in eq_rows)

    by_year: dict[str, float] = {}
    for r in rows:
        by_year[r["cd"][:4]] = by_year.get(r["cd"][:4], 0.0) + r["pnl"]
    ranked = sorted(by_year.items(), key=lambda kv: -kv[1])
    best_year_pnl = ranked[0][1] if ranked else 0.0
    second_year_pnl = ranked[1][1] if len(ranked) > 1 else 0.0

    bands: dict[str, list[dict]] = {k: [] for k, _v in BANDS}
    bands["tx"] = []
    for r in rows:
        hit = "tx"
        for name, v in BANDS:
            if abs(r["units"] - v) <= 0.35:
                hit = name
                break
        bands[hit].append(r)

    def band_stats(name: str):
        rs = bands[name]
        ps = [x["pnl"] for x in rs]
        units = sum(x["units"] for x in rs)
        return (len(rs), sum(ps),
                sum(sorted(ps)[:-3]) if len(ps) > 3 else 0.0,
                (sum(ps) / units) if units else 0.0)

    b1n, b1p, _b1e, b1u = band_stats("band1")
    b2n, b2p, _b2e, b2u = band_stats("band2")
    b3n, b3p, b3e, b3u = band_stats("band3")
    b4n, b4p, _b4e, b4u = band_stats("band4")
    txn, txp, _txe, _txu = band_stats("tx")

    # ── 二级锚点：持有天数分档 / 出口分解 / 集中度尾部 ──────────────────
    # 持有天数口径：平仓日与开仓日之间的交易日数 − 1（=「持有 N 个交易日」）。
    # 实测该分布只落在 2/5/8/11/14… 上，故 5 档等价于 ≤2 / 3–5 / 6–10 / 11–20 / >20。
    import bisect
    hold: dict[str, list[float]] = {"le2": [], "3_5": [], "6_10": [], "11_20": [], "gt20": []}
    for r in rows:
        h = bisect.bisect_right(cal, r["cd"]) - bisect.bisect_left(cal, r["od"]) - 1
        k = ("le2" if h <= 2 else "3_5" if h <= 5 else "6_10" if h <= 10
             else "11_20" if h <= 20 else "gt20")
        hold[k].append(r["pnl"])

    hold_vals: dict[str, float] = {}
    for k, ps in hold.items():
        hold_vals[f"hold_{k}_n"] = len(ps)
        hold_vals[f"hold_{k}_pnl"] = sum(ps)
        hold_vals[f"hold_{k}_share"] = (len(ps) / n) if n else 0.0
        hold_vals[f"hold_{k}_wr"] = (sum(1 for x in ps if x > 0) / len(ps)) if ps else 0.0

    exits: dict[str, list[float]] = {}
    for r in rows:
        exits.setdefault(r["reason"] or "unknown", []).append(r["pnl"])
    for tag, key in (("stop", "stop"), ("stop_box", "stop_box"),
                     ("time_stop", "time_stop"), ("failed_breakout", "failed")):
        ps = exits.get(tag, [])
        hold_vals[f"exit_{key}_n"] = len(ps)
        hold_vals[f"exit_{key}_pnl"] = sum(ps)

    # 集中度：口径与 scripts/base_progress_stat.py 完全一致（样本 sd、总体三阶矩）
    mean_all = net / n
    sd_all = statistics.stdev(pnls) if n > 1 else 0.0
    skew = (sum(((p - mean_all) / sd_all) ** 3 for p in pnls) / n) if sd_all else 0.0
    srt = sorted(pnls)

    def top_share(frac: float) -> float:
        k = max(1, int(round(n * frac)))
        return (sum(srt[-k:]) / net) if net else 0.0

    # 最长连亏（按平仓日排序；同日顺序不确定，只作量级参考）
    streak_n = cur_n = 0
    streak_pnl = cur_pnl = 0.0
    for r in sorted(rows, key=lambda x: x["cd"]):
        if r["pnl"] <= 0:
            cur_n += 1
            cur_pnl += r["pnl"]
            if cur_n > streak_n:
                streak_n, streak_pnl = cur_n, cur_pnl
        else:
            cur_n, cur_pnl = 0, 0.0

    hold_vals.update({
        "skew": skew,
        "top1pct_share": top_share(0.01),
        "top5pct_share": top_share(0.05),
        "ex10_pnl": sum(srt[:-10]) if n > 10 else 0.0,
        "loss_streak_n": streak_n,
        "loss_streak_pnl": streak_pnl,
    })

    return {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db": str(Path(db).as_posix()),
        "cut": cut,
        "first_day": first_d,
        "last_day": last_d,
        "baseline_id": bl[0] if bl else None,
        "engine_sha256": bl[1] if bl else None,
        "data_mode": bl[2] if bl else None,
        "values": {
            "days": len(eq_rows),
            "trades_n": n,
            "net_pnl": net,
            "equity_mult": mult,
            "cagr": cagr,
            "mdd": mdd,
            "win_rate": len(wins) / n,
            "odds": (mean_w / mean_l) if mean_l else float("inf"),
            "pf": (gross_w / gross_l) if gross_l else float("inf"),
            "avg_pnl": net / n,
            "t_stat": (net / n) / (statistics.stdev(pnls) / math.sqrt(n)) if n > 1 else 0.0,
            "ex3_pnl": sum(sorted(pnls)[:-3]),
            "ex_best_year": net - best_year_pnl,
            "ex2_year": net - best_year_pnl - second_year_pnl,
            "pos_years": f"{sum(1 for v in by_year.values() if v > 0)}/{len(by_year)}",
            "identity_gap": ident,
            "band1_n": b1n, "band1_pnl": b1p, "band1_unit": b1u,
            "band2_n": b2n, "band2_pnl": b2p, "band2_unit": b2u,
            "band3_n": b3n, "band3_pnl": b3p, "band3_ex3": b3e, "band3_unit": b3u,
            "band4_n": b4n, "band4_pnl": b4p, "band4_unit": b4u,
            "tx_n": txn, "tx_pnl": txp,
            **hold_vals,
        },
    }


# ---------------------------------------------------------------- 格式化
def _minus(s: str) -> str:
    return s.replace("-", "−")


def fmt_value(key: str, v) -> str:
    if key == "pos_years":
        return str(v)          # "7/14" 形式，不是单值
    if key in ("cagr", "mdd"):
        return _minus(f"{v * 100:.2f}%")
    if key == "equity_mult":
        return f"×{v:.3f}"
    if key == "identity_gap":
        return f"{v:.2f}"
    if key == "skew":
        return f"{v:.2f}"
    if key == "win_rate" or key.endswith("_wr"):
        return f"{v * 100:.1f}%"
    if key.endswith("_share"):
        return f"{v * 100:.1f}%"
    if key == "days" or key.endswith("_n"):
        return f"{int(v):,}"
    if key.endswith("_pnl") or key.endswith("_unit") or key.endswith("_ex3") \
            or key in ("ex_best_year", "ex2_year"):
        return _minus(f"{v:+,.0f}")
    return f"{v:.2f}"


def tokens_of(key: str, fmt: str) -> list[str]:
    """可被跨文档引用的数字串（去单位、去 ×）。"""
    s = fmt.replace("×", "")
    m = re.findall(r"[+−]?[\d,]+(?:\.\d+)?%?", s)
    return m


# ---------------------------------------------------------------- 渲染/解析
def table_rows(snap: dict) -> list[tuple[str, str, str, str]]:
    out = []
    values = snap["values"]
    for key, label, note in ANCHORS:
        v = values.get(key)
        if v is None:
            continue
        out.append((key, label, fmt_value(key, v), note))
    return out


def render_block(snap: dict) -> str:
    out = [
        "> 本表由 `scripts/doc_anchors.py` 从账本重算生成，**不要手改**（改账本后跑 "
        "`python scripts/doc_anchors.py --write`）。",
        f"> 源库 `{snap['db']}`｜有效边界 `{snap['cut']}`｜"
        f"baseline `{snap.get('baseline_id')}`｜引擎 `{str(snap.get('engine_sha256'))[:12]}`｜"
        f"生成于 {snap['generated_at']}。",
        "> `C8` 校验三段：①本表 == 快照 JSON ②快照 == 账本重算 ③`48`/`60`/`README` 引用的数字都在册。",
        "",
        "| 锚点键 | 指标 | 值 | 口径 |",
        "|---|---|---|---|",
    ]
    for key, label, fmt, note in table_rows(snap):
        out.append(f"| `{key}` | {label} | `{fmt}` | {note} |")
    return "\n".join(out)


def extract_block(text: str) -> dict[str, str] | None:
    m = re.search(re.escape(BEGIN) + r"(.*?)" + re.escape(END), text, re.S)
    if not m:
        return None
    got: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip().startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        key = cells[0].strip("`")
        got[key] = cells[2].strip("`")
    return got


# ---------------------------------------------------------------- 跨文档对账
# 只检查「明确在引基线收官数字」的句子，避免把历史实验数字误判成漂移。
CROSS_DOCS = [
    "docs/64-darvas-master.md",   # 第一入口，人体读过它就不再翻 55 —— 数字必须同样受管
    "docs/48-market-regime.md",
    "docs/60-plan-and-board.md",
    "docs/README.md",
    "docs/29-rulings-registry.md",
    "docs/53-data-and-rebuild.md",
]
# 只认「承载收官锚点」的节：§11 整章 / §11.2 账本 / §11.3 概率 / §11.9 判据 / §11.10 台账。
# §11.5 亏损分解、§11.7 加仓等引的是过程数字（如 time_stop 累计亏损），不是收官锚点，误判进来会报假警。
ANCHOR_LINE = re.compile(r"基线(账本|14\s*年|全窗|收官)|`55 §11(?:\.(?:2|3|9|10))?(?![.\d])")
SEC_REF = re.compile(r"§\s*\d+(?:\.\d+)*|§\s*[一二三四五六七八九十]+")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# 只认「像金额/百分比/倍数」的数字，避免把章号（55 §11.9）、档号（剔前3）、年份误判成漂移。
MONEY = re.compile(r"[+−-]\d{1,3}(?:,\d{3})+(?:\.\d+)?")
MONEY_UNSIGNED = re.compile(r"(?<![+−\-\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?")
PCT = re.compile(r"[+−-]?\d+\.\d+%")
MULT = re.compile(r"×\d+\.\d+")
# 判据容忍线等「非账本数字」：白名单化，加一个必须给理由
EXTRA_OK = {
    "−33.50%",  # 判据 5 的 MDD 容忍线 = 基线 −32.50% + 1.0pp
    "33.50%",
}


def check_quotes(snap: dict) -> list[str]:
    """跨文档锚点对账：引基线收官数字的句子里，每个金额/百分比/倍数都必须「在册」。"""
    allowed: set[str] = set()
    for key, _label, _note in ANCHORS:
        v = snap["values"].get(key)
        if v is None:
            continue
        allowed.update(tokens_of(key, fmt_value(key, v)))
    allowed |= EXTRA_OK
    allowed_unsigned = {t.lstrip("+") for t in allowed}

    problems: list[str] = []
    for rel in CROSS_DOCS:
        p = ROOT / rel
        if not p.exists():
            problems.append(f"{rel}: 文件不存在")
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if not ANCHOR_LINE.search(line) or line.lstrip().startswith("#"):
                continue
            scan = DATE.sub(" ", SEC_REF.sub(" ", line))
            for pattern, pool in ((MONEY, allowed), (MULT, allowed),
                                  (PCT, allowed), (MONEY_UNSIGNED, allowed_unsigned)):
                for m in pattern.finditer(scan):
                    norm = _minus(m.group(0))
                    if pattern is MULT:
                        norm = norm.replace("×", "")
                    if norm in pool:
                        continue
                    problems.append(f"{rel}:{i} 未在册数字 {m.group(0)} → {line.strip()[:70]}")
    return problems


# ------------------------------------------------ 近似值检测（抓手抄错）
# 为什么需要它：C8③ 只扫「明确在引基线数字」的句子，而 docs/55 正文自己就抄了好几份副本
# （§0 状态行、§11.2 摘要、§11.3 表、§11.9 判据、§13）。这些副本手改一位数，C8③ 看不见，
# 肉眼也极难发现。所以补一层「近似值」：不在册 + 与某锚点**同形状** + 相对差 ≤0.1% → 判抄错。
#
# 两个约束缺一不可，否则噪声爆炸（实测 12 条全是误报）：
#   ① 同形状：千分位/整数位数/小数位数都相同。`14.0%` 与 14.0 年、`18.56%` 与 18.60% 因此不误伤。
#   ② 0.1%：金额 7 位时约 1000 元，正好是「末三位抄错」的量级；`1,059` vs `1,063`（0.38%）不触发。
NEAR_REL = 0.001
NEAR_PATS = ((MONEY, "金额"), (MONEY_UNSIGNED, "金额"), (PCT, "百分比"), (MULT, "倍数"))


def _num(tok: str) -> float | None:
    s = _minus(tok).replace(",", "").replace("×", "").replace("%", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def _shape(tok: str) -> tuple[bool, int, int] | None:
    """(带千分位, 整数位数, 小数位数)。只描述「长什么样」，不含数值。"""
    t = _minus(tok).replace("×", "").replace("%", "").replace("+", "")
    m = re.fullmatch(r"-?([\d,]+)(?:\.(\d+))?", t)
    if not m:
        return None
    return ("," in m.group(1), len(m.group(1).replace(",", "")), len(m.group(2) or ""))


def check_near_miss(snap: dict) -> list[str]:
    """全现役 docs/*.md 扫近似值。生成区里的在册数字自然跳过，不必特判。"""
    allowed: set[str] = set()
    pool: list[tuple[str, float, tuple]] = []
    for key, _label, _note in ANCHORS:
        v = snap["values"].get(key)
        if v is None:
            continue
        for tok in tokens_of(key, fmt_value(key, v)):
            allowed.add(tok)
            n, sh = _num(tok), _shape(tok)
            if n is not None and sh is not None:
                pool.append((key, n, sh))
    allowed |= EXTRA_OK
    unsigned = {t.lstrip("+") for t in allowed}

    problems: list[str] = []
    for p in sorted((ROOT / "docs").glob("*.md")):
        rel = p.relative_to(ROOT).as_posix()
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            scan = DATE.sub(" ", SEC_REF.sub(" ", line))
            for pat, kind in NEAR_PATS:
                for m in pat.finditer(scan):
                    tok = m.group(0)
                    norm = _minus(tok).replace("×", "")
                    if norm in allowed or norm.lstrip("+") in unsigned:
                        continue
                    val, sh = _num(tok), _shape(tok)
                    if val is None or sh is None:
                        continue
                    # 只对「像金额的大数」做近似检测。小额无量纲值必然撞：`skew=8.06`
                    # 与旧口径 `CAGR 8.06%` 形状数值全同，无法区分（实测假警 2 处）。
                    if "," not in norm and abs(val) < 10000:
                        continue
                    for key, av, ash in pool:
                        if ash != sh or av == 0:
                            continue
                        if abs(val - av) / abs(av) <= NEAR_REL:
                            problems.append(
                                f"{rel}:{i} 疑似抄错 {tok}（≈锚点 {key} = {fmt_value(key, av)}）"
                                f" → {line.strip()[:60]}")
                            break
    return problems


# ---------------------------------------------------------------- 落盘
def load_snapshot() -> dict | None:
    if not SNAPSHOT.exists():
        return None
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def stabilize_ts(snap: dict) -> dict:
    """内容没变就复用旧 generated_at —— 让 `--write` 幂等，不制造纯时间戳 diff。

    否则每次 `--write` 都会弄脏工作区两份文件，git diff 里躺着一条什么都没改的提交，
    真正的内容变化反而被淹没。
    """
    old = load_snapshot()
    if not old:
        return snap
    same = ({k: v for k, v in old.items() if k != "generated_at"}
            == {k: v for k, v in snap.items() if k != "generated_at"})
    if same and old.get("generated_at"):
        snap = dict(snap, generated_at=old["generated_at"])
    return snap


def write_snapshot(snap: dict) -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rewrite_doc55(snap: dict) -> None:
    text = DOC55.read_text(encoding="utf-8")
    block = f"{BEGIN}\n{render_block(snap)}\n{END}"
    if BEGIN not in text or END not in text:
        raise SystemExit("docs/55 里没有锚点生成区标记，请先插入 "
                         f"{BEGIN} … {END}")
    new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _m: block, text, flags=re.S)
    DOC55.write_text(new, encoding="utf-8")


def check_doc_vs_snapshot(snap: dict) -> list[str]:
    got = extract_block(DOC55.read_text(encoding="utf-8"))
    if got is None:
        return ["docs/55 找不到锚点生成区标记"]
    want = {k: fmt_value(k, snap["values"][k]) for k, _l, _n in ANCHORS if k in snap["values"]}
    bad = []
    for k in sorted(set(want) - set(got)):
        bad.append(f"docs/55 缺锚点 {k}")
    for k in sorted(set(got) - set(want)):
        bad.append(f"docs/55 多锚点 {k}（快照无此键）")
    for k in sorted(set(want) & set(got)):
        if want[k] != got[k]:
            bad.append(f"docs/55 {k}: 快照={want[k]} 文档={got[k]}")
    return bad


def check_vs_db(snap: dict) -> tuple[list[str], bool]:
    """快照 vs 账本重算。库不在 → ( [], False ) 表示跳过。"""
    db = snap.get("db") or DEFAULT_DB
    path = Path(db) if Path(db).is_absolute() else ROOT / db
    if not path.exists():
        return [], False
    fresh = compute(db)
    bad = []
    for key, _l, _n in ANCHORS:
        a, b = snap["values"].get(key), fresh["values"].get(key)
        if a is None or b is None:
            continue
        if fmt_value(key, a) != fmt_value(key, b):
            bad.append(f"{key}: 快照={fmt_value(key, a)} 账本={fmt_value(key, b)}")
    if snap.get("engine_sha256") != fresh.get("engine_sha256"):
        bad.append(f"引擎换了：快照={str(snap.get('engine_sha256'))[:12]} "
                   f"账本={str(fresh.get('engine_sha256'))[:12]}")
    return bad, True


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help=f"账本路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--recompute", action="store_true", help="从账本重算并打印")
    ap.add_argument("--write", action="store_true", help="重算并写快照 + docs/55 生成区")
    ap.add_argument("--check", action="store_true", help="只校 docs/55 == 快照")
    a = ap.parse_args()

    if a.write or a.recompute:
        snap = compute(a.db or DEFAULT_DB)
    else:
        snap = load_snapshot()
        if snap is None:
            print(f"✗ 快照不存在：{SNAPSHOT} → 跑 --write")
            return 1

    if a.write:
        snap = stabilize_ts(snap)
        write_snapshot(snap)
        rewrite_doc55(snap)
        print(f"✓ 已写快照 {SNAPSHOT.relative_to(ROOT)} 并重写 docs/55 生成区"
              f"（{len(snap['values'])} 锚点，边界 {snap['cut']}）")
        return 0

    if a.check:
        bad = check_doc_vs_snapshot(snap)
        if bad:
            for b in bad:
                print("✗", b)
            print("  → 跑 `python scripts/doc_anchors.py --write`")
            return 1
        print(f"✓ docs/55 锚点区与快照一致（{len(snap['values'])} 键）")
        return 0

    print(render_block(snap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
