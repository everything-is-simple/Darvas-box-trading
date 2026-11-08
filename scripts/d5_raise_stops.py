#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D5 · raiseStops 事件研究（零回测、只读），docs/65 §4 阶段 2 / 原著 O6。

判据（65 §4）：**触发概率分布**——不是三判据，不是收益类统计。
本件不产生账本的权益/收益率/MDD/单笔盈亏口径，故 `60 §4.3` 的六项账本
统计最低清单不适用；本件全部数字来自引擎自己写出的账本证据。

方法（2026-11-08 修订 11-07 的判断）：
  臂库确实没有 stop_move 事件表，但 planner 每计划日给每笔持仓挂一张常驻
  止损卖单（internal/planner/planner.go:270-293），plan_order.reason_json 带
  position_id 与当日 pos.StopPrice。因此「逐日止损轨迹」无需拿 daily_bar+box
  离线近似重放，可直接用账本原始快照还原；相邻快照上升即一次止损上移事件。
  引擎真实写出的证据强于离线重放，且可与 position.stop_price 终值 / 
  position_leg.stop_after 初值做恒等式校验。

O6 原著：「总是等到下一个新箱体的上下轨都牢固确立之后才上移止损位，
          设在新箱体的下轨之下一点点」（64 §1.3）。
实现（现行）：raiseStops 取 LatestConfirmedBoxAsOf（只依据两轨确认日均 ≤ 计划日的箱体，
      按「完全确立日」取最新），cand = bottom × sell.discount > 现止损则 UpdateStop。
      （旧版按 LatestNonFormingBox / ORDER BY id DESC 的叙述已作废，见挂11 / 65 §4 终验。）

叙述文字已于 2026-11-09（挂11）做纯文字修复：不再写死旧账本数字与旧待拍结论，
一律以本次运行实测与 65 §4 终验为准；算法、字段、输出文件名与判据均未改动。
本脚本下方的事件重放 SQL 仍按历史 id 口径（算法未改、仅叙述修复），勿与引擎现行实现混读。

用法：
  python scripts/d5_raise_stops.py
  python scripts/d5_raise_stops.py --db var/bt/arms/bt_d3d4_off.db \
      --out var/reports/d5_raise_stops.md --csv var/reports/d5_stop_moves.csv
"""
import argparse
import bisect
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPS = 1e-6


def ro_connect(p):
    """只读连接，绝不写库（60 §4.3 只读纪律）。"""
    return sqlite3.connect("file:%s?mode=ro" % Path(p).as_posix(), uri=True)


def pct(n, d):
    return 0.0 if not d else 100.0 * n / d


def f2(x):
    return "0.00" if x is None else "%.2f" % x


def load_all(db, codes):
    """一次性取回所需数据；box/daily_bar_adj 按持仓涉及的代码切片。"""
    out = {}
    out["positions"] = {}
    for r in db.execute(
        "SELECT id, code, opened_date, closed_date, entry_box_id, qty, avg_cost, "
        "stop_price, status, close_reason, realized_pnl FROM position ORDER BY id"
    ):
        out["positions"][r[0]] = dict(
            pid=r[0], code=r[1], opened=r[2][:10], closed=(r[3] or "")[:10],
            entry_box=r[4], qty=r[5], avg_cost=r[6], final_stop=r[7],
            status=r[8], reason=r[9], pnl=r[10],
        )
    out["legs"] = defaultdict(list)
    for r in db.execute(
        "SELECT position_id, leg_no, date, qty, price, stop_after "
        "FROM position_leg ORDER BY id"
    ):
        out["legs"][r[0]].append(dict(
            leg=r[1], date=r[2][:10], qty=r[3], price=r[4], stop=r[5]))
    out["trail"] = defaultdict(list)
    for r in db.execute(
        "SELECT tp.plan_date, po.trigger_price, po.status, "
        "json_extract(po.reason_json,'$.position_id') "
        "FROM plan_order po JOIN trade_plan tp ON tp.id = po.trade_plan_id "
        "WHERE json_extract(po.reason_json,'$.signal') = 'initial_stop' "
        "AND json_extract(po.reason_json,'$.position_id') IS NOT NULL "
        "ORDER BY tp.plan_date, po.id"
    ):
        out["trail"][int(r[3])].append((r[0][:10], float(r[1]), r[2]))
    out["calendar"] = [r[0][:10] for r in db.execute(
        "SELECT plan_date FROM trade_plan ORDER BY plan_date")]
    out["discount"] = float(db.execute(
        "SELECT value FROM param WHERE key='sell.discount'").fetchone()[0])
    row = db.execute(
        "SELECT baseline_id, engine_version, engine_sha256, data_mode FROM baseline "
        "ORDER BY id DESC LIMIT 1").fetchone()
    out["baseline"] = row
    out["failed_runs"] = db.execute(
        "SELECT date, task, status FROM daily_run WHERE status <> 'ok' "
        "ORDER BY date").fetchall()

    out["boxes"] = defaultdict(list)
    chunks = [codes[i:i + 400] for i in range(0, len(codes), 400)]
    for ch in chunks:
        q = ("SELECT id, code, top, bottom, "
             "CAST(top_confirmed AS TEXT), CAST(bottom_confirmed AS TEXT), status, "
             "prev_box_id, CAST(broken_date AS TEXT), CAST(dead_date AS TEXT) "
             "FROM box WHERE code IN (%s)" % ",".join("?" * len(ch)))
        for r in db.execute(q, ch):
            out["boxes"][r[1]].append(dict(
                id=r[0], top=r[2], bottom=r[3],
                top_c=(r[4] or "")[:10], bot_c=(r[5] or "")[:10],
                status=r[6], prev=r[7], broken=(r[8] or "")[:10],
                dead=(r[9] or "")[:10],
            ))
    return out


def td_index(calendar):
    return {d: i for i, d in enumerate(calendar)}


def td_gap(calendar, a, b):
    """a→b 的交易日数（按引擎计划日历）。"""
    if not a or not b:
        return None
    ia, ib = bisect.bisect_left(calendar, a), bisect.bisect_left(calendar, b)
    return ib - ia


def box_match(boxes, new_stop, disc):
    """反查上移所依据的箱体；返回 (box, match_type, nearest, nearest_err_pct)。

    按引擎 raiseStops 口径（planner.go:526 cand = bottom × sell.discount）优先，
    同一价格多箱体时取 id 最大（LatestNonFormingBox 的 ORDER BY id DESC）。
    未精确命中时给出 bottom×disc 最接近的箱体及其相对误差，供诊断「依据了
    窗口外箱体 / box 行后续被 upsert 覆盖」两种可能，不拿近似值当命中。
    """
    attempts = [
        ("bottom*disc", new_stop / disc, "bottom"),
        ("bottom", new_stop, "bottom"),
        ("top*disc", new_stop / disc, "top"),
        ("top", new_stop, "top"),
    ]
    for kind, target, field in attempts:
        hits = []
        for b in boxes:
            v = b[field]
            if v is None:
                continue
            tol = max(1e-4, abs(target) * 1e-5)
            if abs(float(v) - target) <= tol:
                hits.append(b)
        if hits:
            hits.sort(key=lambda x: x["id"], reverse=True)
            return hits[0], kind, None, None
    cand = [b for b in boxes if b["bottom"]]
    if not cand:
        return None, "none", None, None
    near = min(cand, key=lambda b: abs(float(b["bottom"]) * disc - new_stop))
    err = abs(float(near["bottom"]) * disc - new_stop) / new_stop * 100.0
    return None, "none", near, err


def classify(pos, box, prev_obs, obs, entry_box):
    """O6 时点判定。区间删失：真实上移日 ∈ (prev_obs, obs]。

    三层结论：
      确定合规  C_OK        conf ≤ prev_obs（上移必在确立之后）
      相容但删失 C_BOUNDARY prev_obs < conf ≤ obs（先后不可判，但与 O6 相容）
      确定违反  C_EARLY    conf > obs（真实上移日 ≤ obs < conf）
    异常类（同样违反「下一个新箱体」语义）：窗口外箱体 / 入场箱体 / 未确立。
    """
    if box is None:
        return "E_NO_BOX", None
    if not box["top_c"] or not box["bot_c"]:
        return "C_UNCONFIRMED", None
    conf = max(box["top_c"], box["bot_c"])
    if conf > obs:
        return "C_EARLY", conf          # 确定早移（含 box 后写导致终态日期偏移的可能）
    if entry_box and box["id"] == entry_box:
        return "C_HIST", conf          # 入场箱体本身，不是「下一个新箱体」
    if box["bot_c"] < pos["opened"] or (pos["closed"] and box["bot_c"] > pos["closed"]):
        return "C_OFFWINDOW", conf     # 依据箱体确立于持仓窗口之外
    if conf <= prev_obs:
        return "C_OK", conf
    return "C_BOUNDARY", conf


def enrich_bars(db, events_by_code):
    """日线增强（只读 daily_bar_adj，走 (code,date) 主键）：

    1) close_at_obs：观测日（或之前最近一根）后复权收盘；
       stop_vs_close_pct = 新止损 / 收盘 − 1。原著止损必在市价之下，
       若 > 0 表示止损被抬到市价之上，卖单会立即无理由触发清仓。
    2) giveback：观测区间内最高收盘 − 观测日收盘，× 持仓股数（浮盈回吐线索）。
       旧稿 D5 提过此项，现役 65 §4 判据只认触发概率分布，故此处仅作线索、不是判据。
    """
    bars = {}
    for code, evs in events_by_code.items():
        lo = min(e["prev_obs"] for e in evs)
        hi = max(e["obs"] for e in evs)
        rows = db.execute(
            "SELECT date, adj_close FROM daily_bar_adj "
            "WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
            (code, lo, hi)).fetchall()
        bars[code] = [(r[0][:10], float(r[1])) for r in rows]

    for code, evs in events_by_code.items():
        arr = bars.get(code, [])
        dates = [d for d, _ in arr]
        for e in evs:
            j = bisect.bisect_right(dates, e["obs"]) - 1
            if j >= 0:
                e["close_at_obs"] = arr[j][1]
                e["stop_vs_close_pct"] = (
                    100.0 * (e["new_stop"] / arr[j][1] - 1.0) if arr[j][1] else None)
            else:
                e["close_at_obs"] = None
                e["stop_vs_close_pct"] = None
            i0 = bisect.bisect_right(dates, e["prev_obs"])
            i1 = bisect.bisect_left(dates, e["obs"])
            window = arr[i0:i1 + 1]
            if not window:
                e["giveback"] = None
                continue
            peak = max(c for _, c in window)
            last = window[-1][1]
            e["giveback"] = (peak - last) * e["qty"]


def build(db, d, with_giveback=True):
    P, L, T = d["positions"], d["legs"], d["trail"]
    cal, disc, B = d["calendar"], d["discount"], d["boxes"]
    cal_idx = td_index(cal)

    # ---- 轨迹恒等式 ----
    ident = Counter()
    coverage = []
    for pid, p in P.items():
        t = T.get(pid, [])
        if not t:
            ident["no_trail"] += 1
            continue
        leg1 = min(L.get(pid, []), key=lambda x: x["leg"], default=None)
        if leg1 is None:
            ident["no_leg"] += 1
        elif abs(t[0][1] - leg1["stop"]) <= max(EPS, abs(leg1["stop"]) * 1e-9):
            ident["first_ok"] += 1
        else:
            ident["first_bad"] += 1
        if abs(t[-1][1] - p["final_stop"]) <= max(EPS, abs(p["final_stop"]) * 1e-9):
            ident["last_ok"] += 1
        else:
            ident["last_bad"] += 1
        if any(t[i][1] < t[i - 1][1] - EPS for i in range(1, len(t))):
            ident["decrease"] += 1
        else:
            ident["monotonic"] += 1
        life = td_gap(cal, p["opened"], p["closed"]) or 0
        coverage.append((len(t), life))

    # ---- 事件抽取 ----
    events = []
    ev_per_pid = Counter()
    for pid, t in T.items():
        p = P[pid]
        for i in range(1, len(t)):
            old, new = t[i - 1][1], t[i][1]
            if new <= old + EPS:
                continue
            prev_obs, obs = t[i - 1][0], t[i][0]
            box, kind, near, near_err = box_match(B.get(p["code"], []), new, disc)
            cls, conf = classify(p, box, prev_obs, obs, p["entry_box"])
            e = dict(
                pid=pid, code=p["code"], opened=p["opened"], closed=p["closed"],
                prev_obs=prev_obs, obs=obs, gap_td=td_gap(cal, prev_obs, obs),
                old_stop=old, new_stop=new, delta=new - old,
                delta_pct=100.0 * (new - old) / old if old else None,
                match=kind, box_id=box["id"] if box else None,
                box_top=box["top"] if box else None,
                box_bottom=box["bottom"] if box else None,
                top_c=box["top_c"] if box else "", bot_c=box["bot_c"] if box else "",
                box_status=box["status"] if box else "", cls=cls, conf=conf,
                lag_td=td_gap(cal, conf, obs) if conf else None,
                first_td=td_gap(cal, p["opened"], obs),
                qty=sum(l["qty"] for l in L.get(pid, [])),  # position.qty 平仓后归零，用腿数
                near_box_id=near["id"] if near else None,
                near_bot_c=near["bot_c"] if near else "",
                near_err_pct=near_err,
                close_at_obs=None, stop_vs_close_pct=None, giveback=None,
            )
            events.append(e)
            ev_per_pid[pid] += 1
    events.sort(key=lambda x: (x["obs"], x["pid"]))

    if with_giveback:
        by_code = defaultdict(list)
        for e in events:
            by_code[e["code"]].append(e)
        enrich_bars(db, by_code)

    # ---- C_OK 里再分「是否真的是阶梯新箱体」 ----
    box_by_id = {}
    for lst in B.values():
        for b in lst:
            box_by_id[b["id"]] = b
    def ladder_successor(box, entry_id):
        """沿 prev_box_id 回溯，能否到达入场箱体（= 真正的「下一个新箱体」）。"""
        if not box or not entry_id:
            return False
        cur, seen_ids = box, set()
        for _ in range(8):
            prev = cur["prev"]
            if not prev or prev in seen_ids:
                return False
            if prev == entry_id:
                return True
            seen_ids.add(prev)
            cur = box_by_id.get(prev)
            if cur is None:
                return False
        return False

    for e in events:
        p = P[e["pid"]]
        b = box_by_id.get(e["box_id"])
        e["is_new_ladder"] = bool(
            b and e["cls"] in ("C_OK", "C_BOUNDARY")
            and ladder_successor(b, p["entry_box"])
        )

    return dict(ident=ident, coverage=coverage, events=events,
                ev_per_pid=ev_per_pid)


def render(d, r, args):
    P, T, cal, disc = d["positions"], d["trail"], d["calendar"], d["discount"]
    ev, per = r["events"], r["ev_per_pid"]
    ident = r["ident"]
    n_pos, n_trail = len(P), len([pid for pid in P if T.get(pid)])
    with_ev = len(per)
    buckets = Counter()
    cls = Counter(e["cls"] for e in ev)
    matches = Counter(e["match"] for e in ev)
    gaps = Counter(e["gap_td"] for e in ev)
    yr = defaultdict(lambda: [0, 0])
    for pid, p in P.items():
        yr[p["opened"][:4]][0 if per.get(pid, 0) else 1] += 1
    ok_new = sum(1 for e in ev if e["is_new_ladder"])
    c_ok = cls.get("C_OK", 0)
    gb = [e["giveback"] for e in ev if e["giveback"] is not None]
    gb_pos = [x for x in gb if x > 0]
    no_box = [e for e in ev if e["cls"] == "E_NO_BOX"]
    boxed = [e for e in ev if e["cls"] != "E_NO_BOX"]
    over = [e for e in ev if e.get("stop_vs_close_pct") is not None
            and e["stop_vs_close_pct"] > 0]
    near_out = sum(1 for e in no_box if e["near_bot_c"] and
                   (e["near_bot_c"] < e["opened"] or
                    (e["closed"] and e["near_bot_c"] > e["closed"])))

    def mean_jump(xs):
        v = [x["delta_pct"] for x in xs if x["delta_pct"] is not None]
        return sum(v) / len(v) if v else 0.0

    bid, ver, sha, mode = d["baseline"]

    L = []
    A = L.append
    A("# D5 · raiseStops 事件研究报告（原著 O6 止损上移时机）")
    A("")
    A("> **段 A 复刻期收尾件 · 零回测 · 只读**。判据按 `65 §4 阶段 2 表` = **触发概率分布**，"
      "不是三判据、不是收益类统计。本件不产生权益/收益率/MDD/单笔盈亏口径，"
      "故 `60 §4.3` 六项账本统计最低清单**不适用**（该清单约束的是回测账本报告）。")
    A("> 本件不修改引擎、不改参数、不起新臂、不接 lifespan，符合 `65 §1.1` 三段纪律。")
    A("")
    A("## 0. 审计首行")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| 账本 | `%s` |" % args.db)
    A("| 引擎 sha256 | `%s` |" % sha)
    A("| baseline / version / data_mode | `%s` / `%s` / `%s` |" % (bid, ver, mode))
    A("| sell.discount | %.4f（读自 param，未硬编码） |" % disc)
    A("| 计划日历 | %d 日（%s ~ %s，取 `trade_plan.plan_date`） |"
      % (len(cal), cal[0], cal[-1]))
    A("| 非 ok 的 daily_run | %d 条（历史版本曾因 signal SQLITE_BUSY 写入失败产生缺口，用户 2026-11-08 裁定旧库「就这样了，以后注意点」接受存档；本轮次数以本次运行实测为准） |"
      % len(d["failed_runs"]))
    A("| 持仓 | %d 笔（已平 %d） |" % (n_pos, sum(1 for p in P.values() if p["status"] == "closed")))
    A("| 常驻止损单快照 | %d 张，覆盖 %d 笔持仓 |" % (sum(len(t) for t in T.values()), n_trail))
    A("| 生成命令 | `python scripts/d5_raise_stops.py --db %s` |" % args.db)
    A("")
    A("## 1. 方法：对 11-07 判断的修订")
    A("")
    A("`60 §6` 原记「臂库没有 `stop_move` 事件表，须拿 `daily_bar + box` 离线重算约 504 笔持仓的止损轨迹；"
      "根治要引擎新增 `stop_move` 表」。核查结论：")
    A("")
    A("- **表缺失属实**：表清单中确无 `stop_move`，`position.stop_price` 每笔一行终值、"
      "`position_leg.stop_after` 每腿一格初值。")
    A("- **但不必离线重算**：`internal/planner/planner.go:270-293` 每计划日给每笔持仓挂一张"
      "常驻止损卖单，`trigger_price = pos.StopPrice`，`reason_json` 带 `position_id`。"
      "这是引擎自己写出的**逐日止损快照**，比拿日线近似重放更硬、无前视争议。")
    A("- **样本口径**：不用旧稿「约 504 笔」，取段 A 当前最新账本全量 %d 笔持仓。" % n_pos)
    A("- **区间删失**：真实上移日 ∈ `(上次快照日, 本次快照日]`，因为当日若已有别的卖出单，"
      "引擎不再挂常驻止损单。判定 O6 时按此区间处理，不假装知道精确日。")
    A("- **根治建议降级**：新增 `stop_move` 表从「必需」降为**可选审计增强**（记录 "
      "position_id / date / old / new / box_id / reason），不再是 D5 的前置条件。"
      "引擎改动须用户拍板，本件不实施。")
    A("")
    A("```")
    A("   plan_order 逐日快照        相邻上升 = 一次上移事件")
    A("   ─────────────────────────────────────────────────────────► 时间")
    A("   D0        D1        D2        D3        D4")
    A("   stop=s0   stop=s0   (缺)      stop=s1   stop=s1      s1 > s0")
    A("                       └─ 真实上移日 ∈ (D1, D3] ─┘")
    A("                          O6 判定：新箱体 conf 日 ≤ D1 → 必然在确立之后")
    A("                                      D1 < conf ≤ D3 → 区间删失不可判定")
    A("                                      conf > D3      → 确定违反")
    A("```")
    A("")
    A("## 2. 轨迹恒等式（完整性校验）")
    A("")
    A("| 校验 | 笔数 | 说明 |")
    A("|---|---:|---|")
    A("| 有轨迹 | %d / %d | 首快照应等于 `position_leg.stop_after` |" % (ident["first_ok"] + ident["first_bad"], n_pos))
    A("| 首值 == 腿1 初始止损 | %d ✅ | — |" % ident["first_ok"])
    A("| 首值不等 | %d | — |" % ident["first_bad"])
    A("| 末值 == `position.stop_price` 终值 | %d ✅ | — |" % ident["last_ok"])
    A("| 末值不等 | %d | 末次快照之后仍有上移（含平仓当日已有其他卖单、不再挂常驻单的情形） |" % ident["last_bad"])
    A("| 单调不减 | %d ✅ | DB 触发器只升不降 |" % ident["monotonic"])
    A("| 出现下降 | %d | — |" % ident["decrease"])
    A("| 无轨迹 | %d | 开仓当日即平，从未挂常驻单 |" % ident["no_trail"])
    A("| 无腿记录 | %d | — |" % ident["no_leg"])
    cov = r["coverage"]
    if cov:
        A("| 快照覆盖率（持仓期内） | %.1f%% | Σ快照日 %d / Σ持仓交易日 %d |"
          % (pct(sum(a for a, _ in cov), sum(b for _, b in cov)),
             sum(a for a, _ in cov), sum(b for _, b in cov)))
    A("")
    A("**快照间隔机制**（决定删失窗口宽度，不是数据缺失）：相邻快照间隔 **%d/%d 次为 3 交易日"
      "（%.1f%%）**，其余为 %s。原因是常驻止损单在 `pending/placed` 期间不重复挂，"
      "`PendingSellCodes()`（`internal/store/repo_trade.go:398`）挡住重挂，旧单 `expired` 后才挂新单；"
      "新单 `trigger_price` 取当日 `pos.StopPrice`，故上移会在下一次重挂时立即体现。"
      "**因此 3 日是观测粒度，不是止损生效延迟**；`C_BOUNDARY` 占比高主要是这一 3 日窗口的算术结果，"
      "不应读作「同比例违规」。"
      % (gaps.get(3, 0), len(ev), pct(gaps.get(3, 0), len(ev)),
         "、".join("%d 日×%d" % (k, v) for k, v in sorted(gaps.items())
                  if k is not None and k != 3) or "无"))
    A("")
    A("## 3. 触发概率分布（65 §4 主判据）")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    A("| 至少上移 1 次的持仓 | %d / %d = **%.1f%%** |" % (with_ev, n_trail, pct(with_ev, n_trail)))
    A("| 全程未上移 | %d / %d = %.1f%% |" % (n_trail - with_ev, n_trail, pct(n_trail - with_ev, n_trail)))
    A("| 上移事件总数 | %d 次 |" % len(ev))
    A("| 每笔平均上移次数（有轨迹口径） | %.2f |" % (len(ev) / n_trail if n_trail else 0))
    A("")
    A("**上移次数分布（每笔持仓）**")
    A("")
    A("| 次数 | 笔数 | 占比 |")
    A("|---:|---:|---:|")
    dist = Counter(min(v, 5) for v in per.values())
    zero = n_trail - with_ev
    A("| 0 | %d | %.1f%% |" % (zero, pct(zero, n_trail)))
    for k in range(1, 6):
        label = "%d" % k if k < 5 else "≥5"
        A("| %s | %d | %.1f%% |" % (label, dist.get(k, 0), pct(dist.get(k, 0), n_trail)))
    A("")
    seen, firsts = set(), []
    for e in ev:
        if e["pid"] not in seen:
            seen.add(e["pid"])
            firsts.append(e)
    if firsts:
        for e in firsts:
            n = e["first_td"] or 0
            buckets["0–2" if n <= 2 else "3–5" if n <= 5 else "6–10" if n <= 10
                    else "11–20" if n <= 20 else ">20"] += 1
        A("**首次上移距开仓的交易日数分布**（首事件 %d 笔）" % len(firsts))
        A("")
        A("| 档 | 笔数 | 占比 |")
        A("|---|---:|---:|")
        for k in ["0–2", "3–5", "6–10", "11–20", ">20"]:
            A("| %s | %d | %.1f%% |" % (k, buckets.get(k, 0), pct(buckets.get(k, 0), len(firsts))))
        A("")
    A("**按开仓年分布**（有上移 / 无上移）")
    A("")
    A("| 年 | 有上移 | 无上移 | 触发率 |")
    A("|---|---:|---:|---:|")
    for y in sorted(yr):
        a, b = yr[y]
        A("| %s | %d | %d | %.1f%% |" % (y, a, b, pct(a, a + b)))
    A("")
    A("## 4. O6 时点判定：上移是否真在「新箱体上下轨牢固确立之后」")
    A("")
    A("反查方法：`new_stop / sell.discount` 反推 `box.bottom`（同 code 容差 1e-4，"
      "多命中取 id 最大，模拟 `LatestNonFormingBox` 的 `ORDER BY id DESC`）；"
      "`conf = max(top_confirmed, bottom_confirmed)` 即上下轨都确立之日。")
    A("")
    A("| 分类 | 次数 | 占事件 | 含义 |")
    A("|---|---:|---:|---|")
    labels = [
        ("C_OK", "✅ **确定合规**：conf ≤ 上次快照日，上移必然发生在新箱体确立之后"),
        ("C_BOUNDARY", "◐ **相容但删失**：上次快照 < conf ≤ 本次快照，先后不可判，但不与 O6 矛盾"),
        ("C_EARLY", "⛔ **确定违反**：conf > 本次快照日，真实上移日 ≤ obs < 确立日"),
        ("C_OFFWINDOW", "⛔ 窗口外箱体：下轨确认日不在持仓 [开仓, 平仓] 内，不是「下一个新箱体」"),
        ("C_HIST", "⛔ 入场箱体：依据的就是入场箱体本身"),
        ("C_UNCONFIRMED", "⛔ 依据箱体上轨或下轨确认日为空，谈不上「牢固确立」"),
        ("E_NO_BOX", "⚠ **无法归因**：账本找不到 bottom 与新止损对应的箱体（见缺口诊断）"),
    ]
    for k, desc in labels:
        A("| `%s` | %d | %.1f%% | %s |" % (k, cls.get(k, 0), pct(cls.get(k, 0), len(ev)), desc))
    A("")
    A("判定逻辑（真实上移日 t ∈ `(prev_obs, obs]`，不假装知道精确日）：")
    A("")
    A("```")
    A("  conf ≤ prev_obs        → t > conf 必然成立        → 确定合规")
    A("  prev_obs < conf ≤ obs  → t 可能早于也可能晚于 conf → 相容但删失")
    A("  conf > obs             → t ≤ obs < conf 必然成立  → 确定违反")
    A("```")
    A("")
    A("但「新箱体」还有更严的一层：沿 `box.prev_box_id` 回溯能否到达入场箱体，"
      "即是否真是同一阶梯的后续箱体。可判时点的 %d 次事件（`C_OK` %d + `C_BOUNDARY` %d）中，"
      "**只有 %d 次（%.1f%%）**能回溯到入场箱体；其余虽时点相容，"
      "依据的却是与入场箱体无继承关系的箱体（`prev_box_id` 断链或指向别处）。"
      % (c_ok + cls.get("C_BOUNDARY", 0), c_ok, cls.get("C_BOUNDARY", 0),
         ok_new, pct(ok_new, c_ok + cls.get("C_BOUNDARY", 0))))
    A("")
    A("**反查命中口径分布**")
    A("")
    A("| match_type | 次数 | 说明 |")
    A("|---|---:|---|")
    for k, v in matches.most_common():
        note = {
            "bottom*disc": "引擎 raiseStops 的标准口径（bottom × sell.discount）",
            "bottom": "止损直接等于箱体下轨（无折扣）",
            "top*disc": "止损等于箱体上轨 × 折扣（初始止损公式）",
            "top": "止损直接等于箱体上轨",
            "none": "账本无对应箱体 → 可审计性缺口",
        }.get(k, "")
        A("| `%s` | %d | %s |" % (k, v, note))
    A("")
    A("### E_NO_BOX 缺口诊断（%d 次，占事件 %.1f%%）" % (len(no_box), pct(len(no_box), len(ev))))
    A("")
    A("这些事件的共同特征不是「小步上移」，而是**一次性大幅跳升**：")
    A("")
    A("| 组 | 次数 | 平均单次上移幅度 |")
    A("|---|---:|---:|")
    A("| 可归因箱体 | %d | %.1f%% |" % (len(boxed), mean_jump(boxed)))
    A("| `E_NO_BOX` | %d | **%.1f%%** |" % (len(no_box), mean_jump(no_box)))
    A("")
    if no_box:
        A("对每次 `E_NO_BOX`，取同 code 中 `bottom × sell.discount` 与新止损最接近的箱体"
          "（只用于诊断、不当命中）：**%d / %d 次（%.1f%%）该箱体的下轨确认日落在持仓窗口之外**，"
          "即对应的是同股票其他年份的箱体，而不是持仓当期的「下一个新箱体」。"
          % (near_out, len(no_box), pct(near_out, len(no_box))))
        A("")
        A("| 例 | 观测日 | 新止损 | 最接近箱体的下轨确认日 | 跳升 |")
        A("|---|---|---:|---|---:|")
        for e in no_box[:5]:
            A("| %s (pid %s) | %s | %.2f | %s | %.1f%% |"
              % (e["code"], e["pid"], e["obs"], e["new_stop"],
                 e["near_bot_c"] or "—", e["delta_pct"] or 0.0))
        A("")
        A("**性质与边界**：这说明部分上移的依据箱体在**终态 box 表**里已无法反查，"
          "或依据的是窗口外箱体（`LatestNonFormingBox` 按 `id DESC` 取「最近写入」而非「当前时间最新」）。"
          "根因可能牵涉 box 行后写 / upsert 覆盖，与 K1 全量前视同源；"
          "按用户 2026-11-08 裁定 **K1「继续暂停」**，本件只登记现象、不追根因、不补跑。")
        A("")
    A("### 硬证据：止损是否被抬到市价之上")
    A("")
    A("原著止损必在市价之下（「设在新箱体的下轨之下一点点」）。若 `new_stop > 观测日收盘`，"
      "常驻止损卖单会立即触发，等于无理由清仓——这是 O6 是否对齐最直接的物理判据，"
      "不依赖箱体反查。收盘取 `daily_bar_adj` 后复权价（观测日或之前最近一根）。")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| 可算市价的事件 | %d / %d |" % (
        sum(1 for e in ev if e.get("stop_vs_close_pct") is not None), len(ev)))
    A("| **新止损 > 当日收盘** | **%d 次（%.1f%%）** |"
      % (len(over), pct(len(over), len(ev))))
    A("| 其中可归因箱体 / `E_NO_BOX` | %d / %d |"
      % (sum(1 for e in over if e["cls"] != "E_NO_BOX"),
         sum(1 for e in over if e["cls"] == "E_NO_BOX")))
    if over:
        A("| 最大越过幅度 | %.1f%% |" % max(e["stop_vs_close_pct"] for e in over))
    A("")
    lags = [e["lag_td"] for e in ev if e["lag_td"] is not None and e["cls"] in ("C_OK", "C_BOUNDARY")]
    if lags:
        lags.sort()
        A("**及时性**：可判定事件的 `conf → 观测到上移` 间隔 = "
          "中位数 %.0f 交易日 / 均值 %.1f / 90 分位 %d / 最大 %d。"
          % (lags[len(lags) // 2], sum(lags) / len(lags),
             lags[int(len(lags) * 0.9)], lags[-1]))
        A("注意观测口径含快照缺口，此值是「观测到上移」的滞后上界，不是引擎真实滞后。")
        A("")
    if gb:
        A("## 5. 迟滞代价线索（浮盈回吐，非判据）")
        A("")
        A("观测区间内最高收盘 − 观测日收盘，× 持仓股数（后复权价 `daily_bar_adj`；"
      "`position.qty` 平仓后归零，股数取 `position_leg.qty` 之和）：")
        A("")
        A("| 项 | 值 |")
        A("|---|---|")
        A("| 可算事件 | %d / %d |" % (len(gb), len(ev)))
        A("| 出现正回吐 | %d 次 |" % len(gb_pos))
        A("| 正回吐合计 | %s 元（后复权口径，不等同已实现盈亏） |" % f"{sum(gb_pos):+,.2f}")
        A("| 单事件最大 | %s 元 |" % (f"{max(gb_pos):+,.2f}" if gb_pos else "—"))
        A("")
    A("## 6. 结论与待拍事项")
    A("")
    compliant = cls.get("C_OK", 0)
    boundary = cls.get("C_BOUNDARY", 0)
    violated = cls.get("C_EARLY", 0)
    anomalous = (cls.get("C_OFFWINDOW", 0) + cls.get("C_HIST", 0)
                 + cls.get("C_UNCONFIRMED", 0) + cls.get("E_NO_BOX", 0))
    A("1. **触发概率（`65 §4` 主判据）**：%d/%d 笔持仓（**%.1f%%**）至少上移一次，共 %d 次事件；"
      "首次上移集中在开仓后 %s 交易日档。即 `raiseStops` 不是死代码，"
      "「触发概率从未统计」这一欠账已销。"
      % (with_ev, n_trail, pct(with_ev, n_trail), len(ev),
         max(buckets, key=buckets.get) if firsts else "—"))
    A("2. **O6 时点三层结论**：确定合规 %d 次（%.1f%%）、相容但删失 %d 次（%.1f%%）、"
      "确定违反 %d 次（%.1f%%）；另有窗口外箱体 / 入场箱体 / 未确立 / 无法归因合计 %d 次（%.1f%%）。"
      % (compliant, pct(compliant, len(ev)), boundary, pct(boundary, len(ev)),
         violated, pct(violated, len(ev)), anomalous, pct(anomalous, len(ev))))
    A("3. **硬证据**：%d 次事件（%.1f%%）把新止损抬到当日收盘价之上（最大越过 %.1f%%），"
      "其中 %d 次无法归因到任何当期箱体；这些常驻止损卖单会立即触发、无理由清仓。"
      "另有 %d 次明确依据窗口外箱体、`E_NO_BOX` 中 %d 次最接近箱体也在窗口外；"
      "可判时点的 %d 次里只有 %d 次是入场箱体的阶梯后继。"
      "据此只能给出本账本的时点证据；O6 是否对齐由 `65 §4` 终验裁定，本脚本不裁定。"
      % (len(over), pct(len(over), len(ev)),
         max(e["stop_vs_close_pct"] for e in over) if over else 0.0,
         sum(1 for e in over if e["cls"] == "E_NO_BOX"),
         cls.get("C_OFFWINDOW", 0), near_out,
         c_ok + boundary, ok_new))
    A("4. **对 11-07 判断的修订**：`60 §6` 所记「须离线重算约 504 笔止损轨迹 / 根治必须新增 "
      "`stop_move` 表」应改为「账本已有逐日止损快照，直接还原即可；`stop_move` 降为可选审计增强」。"
      "样本由旧稿 504 笔改为当前段 A 账本全量 %d 笔。" % n_pos)
    A("5. **本件只交付证据，不裁定 O6 是否对齐、不改计划、不改引擎、不调参数、不接 lifespan**。"
      "按 `65 §1.1` 三段纪律，段 A 结论由用户拍板后回填；`65 §4` 终验已拍板并关门阶段 2，本件不重开。")
    A("6. **历史待拍四件（2026-11-09 `65 §4` 终验已拍板，本脚本不重开裁定）**：")
    A("   - ① O6 判定：判为「对齐」/「需修实现」/「补证据再判」；若判需修，"
      "修的是 `raiseStops` 取箱体的口径（属段 A 复刻，不是段 C 调参）。")
    A("   - ② 是否新增 `stop_move` 审计表（position_id / date / old / new / box_id / reason）；"
      "本件已证明非必需。")
    A("   - ③ `E_NO_BOX` 窗口外箱体现象是否追查；根因可能牵涉 box 行后写 / upsert，"
      "与 K1（全量前视验证，用户裁定「继续暂停」）同源，按裁定不补跑。")
    A("   - ④ 段 A 下一步是否转阶段 3 装油门（D1 金字塔加仓 / D2a / D11）；"
      "按 `65 §1.1` 起跑仍须用户另给一句明确指令（本轮未授权）。")
    A("")
    A("## 附：明细")
    A("")
    A("- 事件级 CSV：`%s`（%d 行）" % (args.csv, len(ev)))
    A("- 只读连接 `mode=ro`，脚本不写任何库；仅写上述报告与 CSV。")
    A("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="D5 raiseStops 事件研究（零回测/只读）")
    ap.add_argument("--db", default="var/bt/arms/bt_d3d4_off.db")
    ap.add_argument("--out", default="var/reports/d5_raise_stops.md")
    ap.add_argument("--csv", default="var/reports/d5_stop_moves.csv")
    ap.add_argument("--no-giveback", action="store_true",
                    help="跳过日线增强（浮盈回吐 + 市价证据），仅用于快速试跑")
    args = ap.parse_args()

    db = ro_connect(ROOT / args.db if not Path(args.db).is_absolute() else args.db)
    codes = sorted({r[0] for r in db.execute("SELECT DISTINCT code FROM position")})
    d = load_all(db, codes)
    r = build(db, d, with_giveback=not args.no_giveback)

    out = ROOT / args.out
    csvp = ROOT / args.csv
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(d, r, args), encoding="utf-8")
    fields = ["pid", "code", "opened", "closed", "prev_obs", "obs", "gap_td",
              "old_stop", "new_stop", "delta", "delta_pct", "match", "box_id",
              "box_top", "box_bottom", "top_c", "bot_c", "box_status", "cls",
              "conf", "lag_td", "first_td", "is_new_ladder",
              "near_box_id", "near_bot_c", "near_err_pct",
              "close_at_obs", "stop_vs_close_pct", "qty", "giveback"]
    with csvp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for e in r["events"]:
            w.writerow(e)
    db.close()

    cls = Counter(e["cls"] for e in r["events"])
    print("D5 事件研究完成（零回测 / 只读）")
    print("  持仓 %d 笔，有轨迹 %d 笔，快照 %d 张"
          % (len(d["positions"]), len(d["trail"]), sum(len(t) for t in d["trail"].values())))
    print("  上移事件 %d 次，涉及 %d 笔持仓（%.1f%%）"
          % (len(r["events"]), len(r["ev_per_pid"]),
             pct(len(r["ev_per_pid"]), len(d["trail"]))))
    print("  分类：" + " / ".join("%s=%d" % (k, cls.get(k, 0))
                              for k in ["C_OK", "C_BOUNDARY", "C_EARLY",
                                        "C_OFFWINDOW", "C_HIST",
                                        "C_UNCONFIRMED", "E_NO_BOX"]))
    over = [e for e in r["events"] if e.get("stop_vs_close_pct") is not None
            and e["stop_vs_close_pct"] > 0]
    print("  新止损越过当日收盘 %d / %d 次（%.1f%%）"
          % (len(over), len(r["events"]), pct(len(over), len(r["events"]))))
    print("  恒等式：首值 OK %d / 末值 OK %d / 单调 %d / 无轨迹 %d"
          % (r["ident"]["first_ok"], r["ident"]["last_ok"],
             r["ident"]["monotonic"], r["ident"]["no_trail"]))
    print("  报告 %s" % out)
    print("  明细 %s" % csvp)


if __name__ == "__main__":
    main()
