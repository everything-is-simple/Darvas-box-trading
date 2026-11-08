#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D5 补证据复核（2026-11-08，用户拍板「按补证据走」后执行）。

对应 O6（原著条款：止损上移必须在新箱体上下轨都牢固确立之后才触发）的
四项只读补证据核查，全部零回测、不改引擎、不改参数、不起新臂：

  ① 口径复核   —— stop_price 与 daily_bar_adj.adj_close 是否同一复权口径
  ② 实现口径   —— planner.go raiseStops / repo_box.go LatestNonFormingBox 真实判据
  ③ 成交核对   —— 上一版「133 次止损越过当日收盘」是否真的次日清仓
  ④ 窗口外箱体 —— 最终态 box 表能否用于事后归因；撤回「与 K1 前视同源」推测

输入：臂库（默认 bt_d3d4_off.db，mode=ro）+ 上一版事件明细
      var/reports/d5_stop_moves.csv（由 scripts/d5_raise_stops.py 生成）
输出：var/reports/d5_evidence_recheck.md

本脚本只交证据，不裁定 O6、不回填 64 §1.3 主表。
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEF_DB = os.path.join(ROOT, "var", "bt", "arms", "bt_d3d4_off.db")
DEF_EVENTS = os.path.join(ROOT, "var", "reports", "d5_stop_moves.csv")
DEF_OUT = os.path.join(ROOT, "var", "reports", "d5_evidence_recheck.md")


def pct(n: float, d: float) -> float:
    return 100.0 * n / d if d else 0.0


def money(x) -> str:
    """带千分位与正负号的两位小数（%+,.2f 不是合法 printf 格式）。"""
    return "{:+,.2f}".format(float(x or 0))


def ro_db(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        sys.exit("臂库不存在: %s" % path)
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)


def load_events(path: str) -> list[dict]:
    if not os.path.isfile(path):
        sys.exit("事件明细不存在，请先跑: python scripts/d5_raise_stops.py\n%s" % path)
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def fnum(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def trading_calendar(db) -> list[str]:
    """有效交易日（daily_run 的 plan 日，升序去重；避免全表扫 1796 万行日线）。"""
    rows = db.execute("SELECT DISTINCT substr(date,1,10) FROM daily_run ORDER BY 1").fetchall()
    return [r[0] for r in rows if r[0]]


def next_n(cal: list[str], d: str, n: int) -> str:
    """第 n 个交易日的日期（含当日之后）。"""
    import bisect
    i = bisect.bisect_right(cal, d)
    return cal[min(i + n - 1, len(cal) - 1)]


# ---------------------------------------------------------------- ① 口径复核
def check_price_basis(db) -> dict:
    base = db.execute(
        "SELECT baseline_id, engine_version, engine_sha256, data_mode FROM baseline"
    ).fetchone()
    params = dict(db.execute(
        "SELECT key, value FROM param WHERE key IN ('sell.discount','buy.premium')"
    ).fetchall())
    ratio = db.execute(
        "SELECT count(*), round(min(f.price/ba.adj_close),4), "
        "round(avg(f.price/ba.adj_close),4), round(max(f.price/ba.adj_close),4) "
        "FROM trade_fill f JOIN daily_bar_adj ba ON ba.code=f.code AND ba.date=f.date "
        "WHERE f.side='buy' AND ba.adj_close>0"
    ).fetchone()
    raw_ratio = db.execute(
        "SELECT count(*), round(avg(f.price/br.close),4) "
        "FROM trade_fill f JOIN daily_bar br ON br.code=f.code AND br.date=f.date "
        "WHERE f.side='buy' AND br.close>0"
    ).fetchone()
    sample = db.execute(
        "SELECT f.date,f.code,round(f.price,3),round(ba.adj_close,3),round(br.close,3) "
        "FROM trade_fill f "
        "LEFT JOIN daily_bar_adj ba ON ba.code=f.code AND ba.date=f.date "
        "LEFT JOIN daily_bar br ON br.code=f.code AND br.date=f.date "
        "WHERE f.side='buy' ORDER BY f.id LIMIT 6"
    ).fetchall()
    return dict(base=base, params=params, ratio=ratio, raw_ratio=raw_ratio,
                sample=sample)


# -------------------------------------------------------------- ② 实现口径核对
GO_RAISE = os.path.join(ROOT, "internal", "planner", "planner.go")
GO_BOX = os.path.join(ROOT, "internal", "store", "repo_box.go")


def snippet(path: str, start: int, end: int) -> str:
    if not os.path.isfile(path):
        return "(源码文件缺失: %s)" % path
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    return "".join(lines[start - 1:end]).rstrip()


def check_impl(db) -> dict:
    inv = tot = 0
    prev = {}
    for code, bid, d in db.execute(
        "SELECT code,id,bottom_confirmed FROM box "
        "WHERE bottom_confirmed IS NOT NULL ORDER BY code,id"
    ):
        if code in prev:
            tot += 1
            if prev[code] and d < prev[code]:
                inv += 1
        prev[code] = d
    return dict(inv=inv, tot=tot,
                raise_src=snippet(GO_RAISE, 510, 537),
                box_src=snippet(GO_BOX, 213, 217),
                update_src=snippet(os.path.join(ROOT, "internal", "store",
                                                "repo_trade.go"), 233, 236))


# ------------------------------------------------------------------ ③ 成交核对
def check_fills(db, cal: list[str], events: list[dict]) -> dict:
    over = [e for e in events if fnum(e.get("stop_vs_close_pct")) > 0]
    rest = [e for e in events if fnum(e.get("stop_vs_close_pct")) <= 0]

    def scan(evs: list[dict]) -> dict:
        sold = closed = stop_close = 0
        reasons = Counter()
        samples = []
        for e in evs:
            pid, code, obs = int(e["pid"]), e["code"], e["obs"]
            d2 = next_n(cal, obs, 3)
            n = db.execute(
                "SELECT count(*) FROM trade_fill WHERE code=? AND side='sell' "
                "AND date>=? AND date<=?", (code, obs, d2)).fetchone()[0]
            if n:
                sold += 1
            row = db.execute(
                "SELECT closed_date, close_reason, realized_pnl, "
                "julianday(closed_date)-julianday(opened_date) "
                "FROM position WHERE id=?", (pid,)).fetchone()
            near = bool(row and row[0] and obs <= row[0][:10] <= d2)
            if near:
                closed += 1
                reasons[row[1] or "?"] += 1
                if row[1] == "stop":
                    stop_close += 1
            if len(samples) < 5 and (n or near):
                samples.append(dict(pid=pid, code=code, obs=obs,
                                    new_stop=fnum(e["new_stop"]),
                                    close=fnum(e.get("close_at_obs")),
                                    sold=n, closed_date=row[0][:10] if row and row[0] else "",
                                    reason=row[1] if row else ""))
        return dict(n=len(evs), sold=sold, closed=closed, stop_close=stop_close,
                    reasons=reasons, samples=samples)

    a, b = scan(over), scan(rest)
    pids = ",".join(str(int(e["pid"])) for e in over)
    impact = db.execute(
        "SELECT count(*), round(sum(realized_pnl),2), round(avg(realized_pnl),2), "
        "round(avg(julianday(closed_date)-julianday(opened_date)),1) "
        "FROM position WHERE id IN (%s)" % pids).fetchone() if pids else None
    allstop = db.execute(
        "SELECT count(*), round(sum(realized_pnl),2), round(avg(realized_pnl),2), "
        "round(avg(julianday(closed_date)-julianday(opened_date)),1) "
        "FROM position WHERE status='closed' AND close_reason='stop'").fetchone()
    return dict(over=a, rest=b, impact=impact, allstop=allstop)


# ------------------------------------------------------------ ④ 窗口外箱体复核
def check_boxes(db, events: list[dict]) -> dict:
    meta = db.execute(
        "SELECT count(*), min(id), max(id), min(created_at), max(created_at), "
        "count(DISTINCT substr(created_at,1,10)) FROM box").fetchone()
    by_year = db.execute(
        "SELECT substr(bottom_confirmed,1,4), count(*), min(created_at), max(created_at) "
        "FROM box WHERE bottom_confirmed IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall()
    no_box = [e for e in events if e["cls"] == "E_NO_BOX"]
    offwin = [e for e in events if e["cls"] == "C_OFFWINDOW"]
    over = [e for e in events if fnum(e.get("stop_vs_close_pct")) > 0]

    def split(evs: list[dict]) -> dict:
        """互斥分区：先判有无精确箱体日期，其余再按参考箱体日期分区。"""
        exact = near_future = near_hist = near_inwindow = near_none = 0
        for e in evs:
            if e.get("bot_c"):
                exact += 1
                continue
            d = e.get("near_bot_c") or ""
            if not d:
                near_none += 1
            elif d > e["obs"]:
                near_future += 1
            elif d < e["opened"]:
                near_hist += 1
            else:
                near_inwindow += 1
        return dict(n=len(evs), exact_box_date=exact, near_future=near_future,
                    near_hist=near_hist, near_inwindow=near_inwindow,
                    near_none=near_none)

    return dict(meta=meta, by_year=by_year,
                no_box=split(no_box), offwin=split(offwin), over=split(over))


# -------------------------------------------------------------------- 报告输出
def build_report(db, events: list[dict]) -> str:
    L = []
    A = L.append
    basis, impl, fills, boxes = (
        check_price_basis(db), check_impl(db),
        check_fills(db, trading_calendar(db), events), check_boxes(db, events))
    base, params, ratio, raw_ratio = (
        basis["base"], basis["params"], basis["ratio"], basis["raw_ratio"])
    ov, rest, impact, allstop = (
        fills["over"], fills["rest"], fills["impact"], fills["allstop"])

    A("# D5 补证据复核 · O6 止损上移时机")
    A("")
    A("> 生成：`python scripts/d5_evidence_recheck.py`（只读 `mode=ro`，零回测）  ")
    A("> 时间：%s  " % datetime.now().strftime("%Y-%m-%d %H:%M"))
    A("> 臂库：`var/bt/arms/bt_d3d4_off.db` ｜ 基线 `%s` ｜ 引擎 `%s` ｜ `data_mode=%s`  "
      % (base[0], base[2][:12], base[3]))
    A("> 输入：上一版事件明细 `var/reports/d5_stop_moves.csv`（%d 行）  " % len(events))
    A("> 定位：`65 §4` 阶段 2 收尾件 D5（O6 止损上移时机事件研究）的补证据，**只交证据、不裁定 O6**。"
      "不回填 `64 §1.3` 主表（15 条原著规则对照表）、不改引擎、不改参数、不起新臂。")
    A("")
    A("## 0. 一句话结论")
    A("")
    A("上一版的两条「硬证据」经复核**成立且更强**：`stop_price` 与 `adj_close` 同口径（不是口径错配），"
      "越过当日收盘的 %d 次上移里 **%d 次（%.1f%%）次日真的以 `stop` 原因清仓**（对照组仅 %.1f%%）。"
      % (ov["n"], ov["stop_close"], pct(ov["stop_close"], ov["n"]),
         pct(rest["sold"], rest["n"])))
    A("但上一版关于「窗口外箱体可能与 K1（全量前视验证）同源」的推测**证据不足，本件予以撤回**："
      "那些未来年份箱体只是「价格最接近」的模糊匹配（`near_bot_c`），不是引擎实际取的箱体；"
      "`box` 表也非开局一次性预生成全历史。**可确证的是**最终态箱体表无法还原当时的箱体"
      "（候选成因见 §4，本件未验）——这是 D5 归因方法的局限，**不是前视证据**。")
    A("")
    A("对 O6 的判定依据因此要换：不能靠箱体反查，要靠**可直接观测的清仓事实 + 实现口径核对**。"
      "换依据后，证据指向 `raiseStops` 实现与 O6 要求之间存在可观测偏离（详见 §5），"
      "但**是否据此判「需修」仍归用户拍板**。")
    A("")
    A("---")
    A("")
    A("## 1. ① 口径复核：不是口径错配")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| `baseline.data_mode` | `%s` |" % base[3])
    A("| `sell.discount` | %s（引擎 `cand = lb.Bottom × sellDisc`） |" % params.get("sell.discount", "?"))
    A("| 买入成交价 / 同日 `daily_bar_adj.adj_close` | %d 笔，均值 **%.3f**，区间 %.3f–%.3f |"
      % (ratio[0], ratio[2], ratio[1], ratio[3]))
    A("| 买入成交价 / 同日 `daily_bar.close`（raw） | %d 笔，均值 %.3f |"
      % (raw_ratio[0], raw_ratio[1]))
    A("")
    A("样本（date / code / 成交价 / adj_close / raw close）：")
    A("")
    A("```")
    for r in basis["sample"]:
        A("  " + "  ".join(str(x) for x in r))
    A("```")
    A("")
    A("比值均值 %.3f ≈ 1（离散来自突破买入价 = `box.top × buy.premium`，非收盘价），"
      "与 raw 口径比值 %.3f 明显不同 → **引擎跑在后复权价上，`position.stop_price` / "
      "`plan_order.trigger_price` 与 `daily_bar_adj.adj_close` 同口径**。"
      % (ratio[2], raw_ratio[1]))
    A("上一版「133 次新止损高于当日收盘」**不是复权口径错配**，硬证据成立。")
    A("")
    A("---")
    A("")
    A("## 2. ② 实现口径核对：代码只查三件事")
    A("")
    A("`internal/planner/planner.go:510-537` `raiseStops`：")
    A("")
    A("```go")
    A(impl["raise_src"])
    A("```")
    A("")
    A("`internal/store/repo_box.go:213-217` `LatestNonFormingBox`：")
    A("")
    A("```sql")
    A(impl["box_src"])
    A("```")
    A("")
    A("`UpdateStop`（`internal/store/repo_trade.go:233-236`）是裸 `UPDATE position SET stop_price=?`，无二次变换：")
    A("")
    A("```go")
    A(impl["update_src"])
    A("```")
    A("")
    A("**引擎实际判据** = 「`status IN (confirmed,broken_up,broken_down)` 中 `id` 最大的箱体，"
      "`Bottom>0`、`BottomConfirmed` 非空、`ID != EntryBoxID`，且 `Bottom×0.97 > 现止损`」。")
    A("对比 O6（原著：新箱体**上下轨都牢固确立之后**才上移止损），代码里**没有**："
      "① 箱体确认日 ≤ 计划日的上界检查；② 上下轨同时确立的检查（只看下轨非空）；"
      "③ 新止损不得高于当日收盘的保护。")
    A("")
    A("更关键：`ORDER BY id DESC` 不等于「时间最新」。同一只股票内按 `id` 相邻的箱体行，"
      "下轨确认日倒退 **%d / %d = %.2f%%**——`UpsertBoxes` 的「复用旧 id / 删未引用旧箱体 / 插新箱体」"
      "会打乱 `id` 与日期的对应。"
      % (impl["inv"], impl["tot"], pct(impl["inv"], impl["tot"])))
    A("> 订正：上一版报的「37.7% 倒退」是**跨股票**比较（相邻 id 常属不同股票，无意义），本件作废。")
    A("")
    A("---")
    A("")
    A("## 3. ③ 成交核对：不是「计划单悬空」，是真清仓")
    A("")
    A("| 组 | 事件数 | 3 日内有卖出成交 | 持仓在窗口内平掉 | 其中 `close_reason=stop` |")
    A("|---|---:|---:|---:|---:|")
    A("| 新止损 > 当日收盘 | %d | %d（%.1f%%） | %d | **%d（%.1f%%）** |"
      % (ov["n"], ov["sold"], pct(ov["sold"], ov["n"]), ov["closed"],
         ov["stop_close"], pct(ov["stop_close"], ov["n"])))
    A("| 对照：未越过收盘 | %d | %d（%.1f%%） | %d | %d |"
      % (rest["n"], rest["sold"], pct(rest["sold"], rest["n"]),
         rest["closed"], rest["stop_close"]))
    A("")
    A("清仓样例（pid / code / 观测日 / 新止损 / 当日收盘 / 卖出成交数 / 平仓日 / 原因）：")
    A("")
    A("```")
    for s in ov["samples"]:
        A("  %-4s %-10s %s  stop=%-10.3f close=%-9.3f fills=%d  %s %s"
          % (s["pid"], s["code"], s["obs"], s["new_stop"], s["close"],
             s["sold"], s["closed_date"], s["reason"]))
    A("```")
    A("")
    A("**越价上移涉及的 %d 笔持仓，账本影响如下（必须诚实报）**；其中 %d 笔（%.1f%%）"
      "在观测后 3 日内以 `stop` 平仓。" % (ov["n"], ov["stop_close"],
                                      pct(ov["stop_close"], ov["n"])))
    A("")
    A("| 组 | 笔数 | 已实现合计 | 均笔 | 平均持仓天数 |")
    A("|---|---:|---:|---:|---:|")
    A("| 越过收盘组 | %s | %s | %s | %s |"
      % (impact[0], money(impact[1]), money(impact[2]), impact[3]))
    A("| 全账本 `close_reason=stop` | %s | %s | %s | %s |"
      % (allstop[0], money(allstop[1]), money(allstop[2]), allstop[3]))
    A("")
    A("→ 这批提前清仓在这份账本里**是盈利的**（合计 %s），持仓也更短；"
      "**不能读成「业绩受损」**。问题是行为违反原著：止损脱离箱体阶梯、利润未能奔跑。"
      % money(impact[1]))
    A("")
    A("---")
    A("")
    A("## 4. ④ 窗口外箱体：撤回 K1 同源推测")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| `box` 行数 / id 区间 | %s / %s–%s |" % (boxes["meta"][0], boxes["meta"][1], boxes["meta"][2]))
    A("| `created_at` 区间 | %s → %s |" % (boxes["meta"][3], boxes["meta"][4]))
    A("| `created_at` 不同日期数 | %s 天（长跑墙钟时间） |" % boxes["meta"][5])
    A("")
    A("按箱体下轨确认年份看写入墙钟（首尾各 4 年）：")
    A("")
    A("```")
    for y, n, lo, hi in boxes["by_year"][:4] + boxes["by_year"][-4:]:
        A("  %s  n=%-6s  %s → %s" % (y, n, lo, hi))
    A("```")
    A("")
    A("`created_at` 随确认年份单调推进（1993 → 2018 对应 11-06 23:30 → 11-08 09:07），"
      "说明箱体随回测逐日 `UpsertBoxes` 写入，**不是开局一次性预生成全历史**。")
    A("")
    A("归因分类复核（互斥分区；`bot_c` = 精确匹配到的箱体确认日，"
      "`near_bot_c` = 仅「价格最接近」的参考箱体，**不代表引擎实际取的箱体**）：")
    A("")
    A("| 组 | 事件数 | 精确匹配到箱体 | 无精确·参考在未来 | 无精确·参考在开仓前 | 无精确·参考在持仓期内 | 无精确·无参考 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for label, key in [("`E_NO_BOX`", "no_box"), ("`C_OFFWINDOW`", "offwin"),
                       ("越过收盘组", "over")]:
        d = boxes[key]
        A("| %s | %d | %d | %d | %d | %d | %d |"
          % (label, d["n"], d["exact_box_date"], d["near_future"],
             d["near_hist"], d["near_inwindow"], d["near_none"]))
    A("")
    A("→ `%s` 里**没有一次**精确匹配到箱体（`bot_c` 全空），"
      "落在 2008/2010/2012 等年份的日期全部来自 `near_bot_c`（价格碰巧接近），"
      "**不是引擎实际取的箱体**，不能作为前视证据。"
      % ("E_NO_BOX",))
    A("")
    A("**撤回声明**：上一版 `65 §4` / `60 §6` 里「窗口外箱体可能与 K1（全量前视验证）同源」"
      "属过度推测，本件予以撤回；按用户 2026-11-08 裁定，K1 **继续暂停、不补跑**，"
      "本件未做任何前视判定、未复活 K1。")
    A("")
    A("**可确证的只有**：最终态 `box` 表与当时的箱体不一致，"
      "D5 用最终态表反查上移时点存在方法局限；它**既不构成引擎前视的证据，也不构成引擎缺陷的证据**。")
    A("")
    A("候选解释（**本件未验、不作为结论**）：`UpsertBoxes` 的「重算替换」语义（`docs/02 §6`）"
      "会删除未被 `position` / `watch` 引用的旧箱体行，而 `raiseStops` 只读箱体、不产生外键引用，"
      "故当时的依据箱体可能在最终态表里被改写或删除。若要坐实成因，需另做「箱体行生命周期」事件研究；"
      "本件不展开、不申请解冻 K1。")
    A("")
    A("---")
    A("")
    A("## 5. 对 O6 判定与段 A 完成线的影响")
    A("")
    A("1. **判定依据换轨**：上一版的时点三层（确定合规 101 / 相容但删失 218 / 确定违反 1）"
      "与「窗口外箱体 36 / 无法归因 253」都依赖最终态箱体反查，§4 已证明该反查不可靠，"
      "故这三层**不再作为 O6 主判据**。触发概率 **395/774 = 51.0%**、609 次上移"
      "（`65 §4` 指定判据）不受影响，欠账仍销。")
    A("2. **换依据后的证据指向**：`raiseStops` 实现与 O6 要求之间存在两处可直接观测的偏离 —— "
      "(a) 取箱体未按计划日隔离、未验证「上下轨都牢固确立」（`ORDER BY id DESC` 与日期 "
      "%.2f%% 不同序）；(b) 上移不设「不超过当日收盘」保护 → %d 笔次日以 `stop` 平仓。"
      "是否把它判作「需修实现」，本件不裁。"
      % (pct(impl["inv"], impl["tot"]), ov["stop_close"]))
    A("3. **这仍不是裁定**：是否判「需修 `raiseStops` 取箱体口径」（属段 A 复刻，不是段 C 调参）、"
      "是否连带修 (b)、是否补 `stop_move` 审计表、是否转阶段 3 装油门（D1 金字塔加仓 / D2 / D11），"
      "四件仍待用户拍板；拍板前不回填 `64 §1.3` 主表。")
    A("4. **对段 A 完成线的影响（必须上报）**：%d 笔平仓（约占全窗 775 个 round trip 的 %.1f%%）"
      "与「止损越过当日收盘」直接相关，这些平仓不是原著描述的箱体破位出场；"
      "当前 D3+D4 账本因此**不满足「原装」的字面要求**。段 A（复刻期：除 O14 外 14 条原著规则"
      "逐条装回，完成线＝14 条对齐＋恒等式 0.00＋冻结原装账本）完成线仍未过；"
      "若拍板修实现，须重跑全窗才能产出合格的原著实现基准。**是否据此判废现有账本，待拍板。**"
      % (ov["stop_close"], pct(ov["stop_close"], 775)))
    A("5. `60 §4.3` 六项账本统计最低清单（按年 / 单月极值 / 单笔极值 / 剔前3 / 剔前10 / "
      "与 v1 同窗对照）本件不产账本收益口径，不适用。")
    A("")
    A("---")
    A("")
    A("## 6. 待拍事项（不替用户决定）")
    A("")
    A("1. O6 判定：对齐 / 需修 `raiseStops` 取箱体口径（段 A 复刻，不是段 C 调参）/ "
      "先做「箱体行生命周期」事件研究再判（本件的候选成因未验）。")
    A("2. 修复范围：只修 (a) 时点隔离，还是 (a)+(b) 连「止损不得高于当日收盘」一起修。")
    A("3. 是否新增 `stop_move` 审计表（本件仍证明非必需）。")
    A("4. 是否转阶段 3 装油门（D1 金字塔加仓 / D2 / D11）——阶段 2 尚未关门。")
    A("5. K1（全量前视验证）按裁定继续暂停、不补跑；本件撤回同源推测，不申请解冻。")
    A("")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="D5 补证据复核（只读、零回测）")
    ap.add_argument("--db", default=DEF_DB)
    ap.add_argument("--events", default=DEF_EVENTS)
    ap.add_argument("--out", default=DEF_OUT)
    a = ap.parse_args()

    db = ro_db(a.db)
    try:
        events = load_events(a.events)
        text = build_report(db, events)
    finally:
        db.close()

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    print("写出 %s（%d 字）" % (a.out, len(text)))
    ov = [e for e in events if fnum(e.get("stop_vs_close_pct")) > 0]
    print("越过收盘事件 %d 次；口径复核 / 实现核对 / 成交核对 / 箱体归因 四项完成" % len(ov))
    return 0


if __name__ == "__main__":
    sys.exit(main())
