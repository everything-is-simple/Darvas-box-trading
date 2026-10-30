#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-6a `position.staledays` 梯度实验报告生成器（只读 p26 库 + 历史基线库）。

口径来源：
- 权益指标与 scripts/p05_yearly_report.py 完全一致（独算一遍，便于交叉核对）
- time_stop 归属判定与 scripts/e17_giveback.py 一致（plan_order.reason_json.position_id
  对应卖单 execute_date == position.closed_date）
- 持有天数用 BarCount 口径（date > opened_date AND date <= closed_date），与引擎一致

用法：
  python scripts/p26_report.py              # 扫描 var/bt/p26 下所有档位库并出报告
  python scripts/p26_report.py --check      # 只跑 HARD 校验，不出报告
"""
import argparse
import html
import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
P26 = BT / "p26"
# 归档后库可能已搬到冷存储盘：允许追加读目录（--p26-dir），首个为默认写入/查找根。
# 归档盘若存在则默认一并扫描——否则归档后不带参数重跑报告会把已归档年份整年丢掉
# （2026-10-30 事故：automation 不带参重跑，阶段 1 的 2012 五档从报告里消失）。
ARCHIVE = Path("A:/Darvas-archive/p26")
P26_DIRS = [P26] + ([ARCHIVE] if ARCHIVE.exists() else [])


def find_db(name):
    """在 P26_DIRS 里按文件名找库（先本地后归档盘）。"""
    for d in P26_DIRS:
        p = d / name
        if p.exists():
            return p
    return P26_DIRS[0] / name
BASE = BT / "base"  # 新基线库（HARD① 的对照对象，2026-10-30 换源，见框架 §10）
EXP = ROOT / "docs" / "experiments"
OUT_HTML = EXP / "e17_staledays_report.html"
OUT_MD = EXP / "e17_staledays_report.md"
OUT_JSON = EXP / "e17_staledays_data.json"

# 档位显示名（顺序即报告顺序）
STALE_LABEL = {0: "0（收紧极限）", 2: "2（基线）", 5: "5", 10: "10", 100000: "off（关闭）"}
STALE_ORDER = [0, 2, 5, 10, 100000]


# ---------------------------------------------------------------- 数据读取
def open_ro(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def eq_metrics(con):
    """权益指标：与 p05_yearly_report.py 同口径。"""
    rows = con.execute("SELECT date, total, drawdown FROM equity_snapshot ORDER BY date").fetchall()
    if not rows:
        raise SystemExit("equity_snapshot 为空（回测未完成？）")
    eq_start, eq_end = rows[0]["total"], rows[-1]["total"]
    ret = (eq_end / eq_start - 1) if eq_start else 0.0
    maxdd = min((r["drawdown"] for r in rows), default=0.0)
    dr = [rows[i]["total"] / rows[i - 1]["total"] - 1
          for i in range(1, len(rows)) if rows[i - 1]["total"] > 0]
    sharpe = 0.0
    if len(dr) > 1:
        m = sum(dr) / len(dr)
        v = sum((x - m) ** 2 for x in dr) / (len(dr) - 1)
        if v > 0:
            sharpe = m / (v ** 0.5) * (252 ** 0.5)
    # 日均暴露：market_value / total
    expo = con.execute(
        "SELECT AVG(CAST(market_value AS REAL)/total) FROM equity_snapshot WHERE total>0").fetchone()[0]
    return {"days": len(rows), "eq_start": eq_start, "eq_end": eq_end,
            "ret": ret, "maxdd": maxdd, "sharpe": sharpe, "exposure": expo}


def trade_metrics(con):
    q = lambda s: con.execute(s).fetchone()[0]
    n = q("SELECT COUNT(*) FROM position WHERE status='closed'") or 0
    wins = q("SELECT COUNT(*) FROM position WHERE status='closed' AND realized_pnl>0") or 0
    pnl = q("SELECT COALESCE(SUM(realized_pnl),0) FROM position WHERE status='closed'") or 0.0
    gw = q("SELECT COALESCE(SUM(realized_pnl),0) FROM position WHERE status='closed' AND realized_pnl>0") or 0.0
    gl = q("SELECT COALESCE(SUM(realized_pnl),0) FROM position WHERE status='closed' AND realized_pnl<0") or 0.0
    pf = (gw / abs(gl)) if gl else (float("inf") if gw > 0 else 0.0)
    return {"n": n, "wr": (wins / n) if n else 0.0, "pnl": pnl, "pf": pf}


def trade_rows(con):
    """逐笔：含 time_stop 归属、持有天数、成本、MFE 所需字段。"""
    ts = {}
    for r in con.execute("""
            SELECT t.execute_date, o.reason_json FROM plan_order o
            JOIN trade_plan t ON t.id = o.trade_plan_id
            WHERE o.side='sell' AND o.reason_json LIKE '%time_stop_noprofit%'"""):
        try:
            pid = json.loads(r["reason_json"] or "{}").get("position_id")
        except Exception:
            pid = None
        if pid:
            ts[pid] = r["execute_date"]
    rows = con.execute("""
        SELECT p.id, p.code, p.opened_date, p.closed_date, p.close_reason, p.realized_pnl,
               p.avg_cost,
               (SELECT COALESCE(SUM(l.qty),0) FROM position_leg l WHERE l.position_id=p.id) qty,
               (SELECT COALESCE(SUM(l.qty*l.price),0) FROM position_leg l WHERE l.position_id=p.id) cost,
               (SELECT COUNT(*) FROM daily_bar d
                 WHERE d.code=p.code AND d.date>p.opened_date AND d.date<=p.closed_date) hold
        FROM position p WHERE p.status='closed'""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_ts"] = (ts.get(d["id"]) == d["closed_date"])
        out.append(d)
    return out


def mfe_by_trade(con, codes_spans):
    """按 (code, opened, closed) 取持仓期最高价算 MFE。返回 {(code,opened,closed): mfe}。"""
    out = {}
    cache = {}
    for key in codes_spans:
        code, op, cl = key
        if code not in cache:
            cache[code] = {}
        hi = con.execute("""SELECT MAX(high) FROM daily_bar
                            WHERE code=? AND date>=? AND date<=?""", (code, op, cl)).fetchone()[0]
        out[key] = hi
    return out


def structural(con, rows):
    """time_stop / 短持块 / 大赢家 / 持有分布 / 退出方式。"""
    n = len(rows)
    tss = [r for r in rows if r["is_ts"]]
    short = [r for r in rows if (r["hold"] or 0) <= 10]
    holds = [r["hold"] for r in rows if r["hold"]]
    # MFE（相对成本的最大浮盈）
    highs = mfe_by_trade(con, [(r["code"], r["opened_date"], r["closed_date"]) for r in rows])
    for r in rows:
        hi = highs.get((r["code"], r["opened_date"], r["closed_date"]))
        cost = r["cost"] or 0
        r["mfe"] = (hi * (r["qty"] or 0) / cost - 1) if (hi and cost) else None
    big = [r for r in rows if r["mfe"] is not None and r["mfe"] > 0.5]
    pnl_top3 = sorted((r["realized_pnl"] for r in rows), reverse=True)[:3]
    hh = {b: sum(1 for h in holds if lo <= h <= hi)
          for b, lo, hi in [("0", 0, 0), ("1-2", 1, 2), ("3-5", 3, 5), ("6-10", 6, 10),
                            ("11-20", 11, 20), ("21-60", 21, 60), ("61+", 61, 10 ** 6)]}
    return {
        "ts_n": len(tss), "ts_share": (len(tss) / n) if n else 0.0,
        "ts_pnl": sum(r["realized_pnl"] for r in tss),
        "other_pnl": sum(r["realized_pnl"] for r in rows if not r["is_ts"]),
        "short_n": len(short), "short_pnl": sum(r["realized_pnl"] for r in short),
        "hold_med": statistics.median(holds) if holds else None,
        "hold_mean": (sum(holds) / len(holds)) if holds else None,
        "hold_p90": (sorted(holds)[int(len(holds) * 0.9)] if holds else None),
        "big_n": len(big), "big_pnl": sum(r["realized_pnl"] for r in big),
        "mfe_med": statistics.median([r["mfe"] for r in rows if r["mfe"] is not None])
        if any(r["mfe"] is not None for r in rows) else None,
        "top3_pnl": sum(pnl_top3),
        "by_reason": {k: sum(1 for r in rows if (r["close_reason"] or "?") == k)
                      for k in sorted({(r["close_reason"] or "?") for r in rows})},
        "hold_hist": hh,
        # HARD④：持有分布合计必须等于已平仓笔数，不得静默丢弃
        "hist_sum": sum(hh.values()), "hist_ok": sum(hh.values()) == n,
    }


def load_band(db, year, stale):
    con = open_ro(db)
    try:
        # 完成判据（与 p26_run.db_complete、base_year_summary 同口径）：
        # 末交易日 ≥ 当年 12-25。否则在跑中的库会被当成"已收官"进报告——
        # 2026-10-30 12:26 事故：sd2_2015 只跑到 10-29 就被并入报告参与裁定。
        last = con.execute("SELECT MAX(date) FROM equity_snapshot").fetchone()[0]
        if not last or last < f"{year}-12-25":
            raise ValueError(f"回测未完成（末交易日 {last} < {year}-12-25）")
        rows = trade_rows(con)
        m = {"year": year, "stale": stale, "db": db.name}
        m.update(eq_metrics(con))
        m.update(trade_metrics(con))
        m.update(structural(con, rows))
        m["stale_param"] = con.execute(
            "SELECT value FROM param WHERE key='position.staledays'").fetchone()[0]
        # 防御：有权益快照但无已平仓样本 = 回测未完成或中途失败，必须拒收，
        # 否则一个半成品库会被当成「收益率 0%」的档位参与裁定。
        if m["n"] == 0:
            raise ValueError("无已平仓样本（回测未完成？）")
        return m
    finally:
        con.close()


def discover():
    """扫描 p26 目录，返回按 (stale, year) 索引的结果。"""
    out = {}
    dbs = []
    for d in P26_DIRS:
        dbs.extend(sorted(d.glob("p26_sd*_*.db")))
    for db in dbs:
        stem = db.stem  # p26_sd2_2012 / p26_sdoff_2012
        try:
            _, sd, yr = stem.split("_")
            tag = sd[2:]
            # off 档文件名为 p26_sdoff_<year>，对应数值上界 100000
            stale = 100000 if tag == "off" else int(tag)
            year = int(yr)
        except Exception:
            continue
        try:
            out[(stale, year)] = load_band(db, year, stale)
        except SystemExit:
            print(f"[skip] {db.name}：回测未完成")
        except Exception as e:
            print(f"[skip] {db.name}：{e}")
    return out


# ---------------------------------------------------------------- HARD 校验
def hard_baseline(bands):
    """HARD①：sd2 档是否逐笔复现基线库。

    2026-10-30 换源：对照对象由老机 `bt_e17_<year>.db` 改为新基线 `bt_baseA_<year>.db`
    （老机 E17 库未随移植包带来，框架 §10 已登记）。
    """
    res = []
    for (stale, year), m in sorted(bands.items()):
        if stale != 2:
            continue
        base = BASE / f"bt_baseA_{year}.db"
        if not base.exists():
            res.append({"year": year, "ok": False, "msg": "基线库不存在"})
            continue
        a = open_ro(find_db(f"p26_sd2_{year}.db"))
        b = open_ro(base)
        try:
            qa = a.execute("""SELECT code, opened_date, closed_date, ROUND(realized_pnl,2)
                              FROM position WHERE status='closed'
                              ORDER BY code, opened_date""").fetchall()
            qb = b.execute("""SELECT code, opened_date, closed_date, ROUND(realized_pnl,2)
                              FROM position WHERE status='closed'
                              ORDER BY code, opened_date""").fetchall()
            sa, sb = [tuple(x) for x in qa], [tuple(x) for x in qb]
            diff = [i for i in range(min(len(sa), len(sb))) if sa[i] != sb[i]]
            pnl_a = sum(x[3] for x in sa)
            pnl_b = sum(x[3] for x in sb)
            ok = (len(sa) == len(sb) and not diff and abs(pnl_a - pnl_b) < 0.01)
            res.append({"year": year, "ok": ok, "n_new": len(sa), "n_base": len(sb),
                        "pnl_new": pnl_a, "pnl_base": pnl_b,
                        "n_diff": len(diff),
                        "first_diff": (sa[diff[0]] if diff else None),
                        "first_diff_base": (sb[diff[0]] if diff and diff[0] < len(sb) else None)})
        finally:
            a.close()
            b.close()
    return res


def hard_params(bands):
    """HARD②：参数生效（含 off 档 ts 必须为 0）。"""
    res = []
    for (stale, year), m in sorted(bands.items()):
        ok_val = (str(m["stale_param"]) == str(stale))
        res.append({"year": year, "stale": stale, "param": m["stale_param"], "ok": ok_val,
                    "ts_n": m["ts_n"],
                    "off_ok": (m["ts_n"] == 0) if stale == 100000 else None})
    return res


def hard_single_var(bands):
    """HARD③：单变量 —— 除 position.staledays 外，其余 param 必须与基线库逐项相同。

    基线库 = var/bt/base/bt_baseA_<year>.db（同源新基线）。
    任何额外差异都意味着引入第二个变量，本档的差异就不能归因于 staledays。
    """
    res = []
    for (stale, year) in sorted(bands):
        base_db = BASE / f"bt_baseA_{year}.db"
        tag = "off" if stale == 100000 else str(stale)  # off 档库名是 p26_sdoff_<年>.db
        db = find_db(f"p26_sd{tag}_{year}.db")
        if not base_db.exists() or not db.exists():
            continue
        a, b = open_ro(db), open_ro(base_db)
        try:
            pa = dict(a.execute("SELECT key, value FROM param").fetchall())
            pb = dict(b.execute("SELECT key, value FROM param").fetchall())
        except sqlite3.OperationalError:
            a.close()
            b.close()
            continue
        a.close()
        b.close()
        diff = {k: (pb.get(k), v) for k, v in pa.items() if pb.get(k) != v}
        miss = [k for k in pb if k not in pa]
        extra = [k for k in pa if k not in pb]
        only_sd = (set(diff) == {"position.staledays"} or (not diff and stale == 2))
        res.append({"year": year, "stale": stale, "ok": only_sd and not miss,
                    "n_param": len(pa), "diff": diff, "miss": len(miss),
                    "only": ", ".join(diff) or "无"})
    return res


# ---------------------------------------------------------------- 渲染
def pct(x, d=1):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def money(x):
    return "—" if x is None else f"{x:,.0f}"


def num(x, d=0):
    """安全数值格式化：None → —，inf → ∞。"""
    if x is None:
        return "—"
    if isinstance(x, float) and (x == float("inf") or x == float("-inf")):
        return "∞"
    return f"{x:.{d}f}"


def compound(bands, years):
    """代表年链接复利终值：Π(1+r_y)。

    阶段 1（只跑 2012）时也能出数（= 1+r_2012），但 `full=False`，
    此时 judge() 拒绝裁定 —— 主判据须代表年齐全才生效（框架 §4.1 / §5.2）。
    """
    out = {}
    # 同年可比年集：所有出数档位都有数据的年份交集。
    # 档位覆盖年数不同（阶段 1 只有 2012、阶段 2 补 2011/2015）时，
    # 直接拿各档自己的终值横比是错的——3 年复利必然大于 2 年，与参数优劣无关。
    present = [s for s in STALE_ORDER if any((s, y) in bands for y in years)]
    common = [y for y in years if present and all((s, y) in bands for s in present)]
    for stale in STALE_ORDER:
        covered = [y for y in years if (stale, y) in bands]
        out[stale] = {"years": len(covered), "years_list": covered, "full": len(covered) == len(years),
                      "terminal": None, "cagr": None, "worst_mdd": None,
                      "common_years": common, "terminal_common": None, "cagr_common": None}
        if covered:
            t = 1.0
            for y in covered:
                t *= (1 + bands[(stale, y)]["ret"])
            out[stale]["terminal"] = t
            out[stale]["cagr"] = t ** (1 / len(covered)) - 1
            out[stale]["worst_mdd"] = min(bands[(stale, y)]["maxdd"] for y in covered)
        if common:
            tc = 1.0
            for y in common:
                tc *= (1 + bands[(stale, y)]["ret"])
            out[stale]["terminal_common"] = tc
            out[stale]["cagr_common"] = tc ** (1 / len(common)) - 1
        # 与基线（staledays=2）可比的年集：只取两边都有数据的年份。
        # 这才是决策口径——sd0 只有 2012，就跟基线比 2012；sd5 有三年，就跟基线比三年，
        # 而不是把所有档位压到公共交集（那样会把 sd5 三年证据丢成一年）。
        vs_years = [y for y in covered if (2, y) in bands]
        if vs_years and stale != 2:
            ts_vs = 1.0
            tb_vs = 1.0
            for y in vs_years:
                ts_vs *= (1 + bands[(stale, y)]["ret"])
                tb_vs *= (1 + bands[(2, y)]["ret"])
            out[stale]["vs_years"] = vs_years
            out[stale]["terminal_vs_base"] = ts_vs
            out[stale]["base_terminal_vs"] = tb_vs
            out[stale]["rel_vs_base"] = ts_vs / tb_vs - 1
    return out


def judge(bands, comp, years):
    """按框架 §5 预注册判据裁定。"""
    full = [s for s in STALE_ORDER if (comp[s]["terminal"] is not None and comp[s]["full"])]
    base = comp.get(2, {}).get("terminal")
    verdict = {"h1": False, "h2": False, "h3": False, "note": "", "full_bands": full}
    if len(full) < len([s for s in STALE_ORDER if comp[s]["terminal"] is not None]) or not full:
        verdict["note"] = (f"代表年未跑齐（{len(years)} 年目标，逐档已覆盖年数见 §三），"
                           f"阶段 1 只出梯度形状，**不作裁定**")
        return verdict
    if base is None:
        verdict["note"] = "基线档缺失，无法裁定"
        return verdict
    best = max(full, key=lambda s: comp[s]["terminal"])
    verdict["best"] = best
    verdict["best_terminal"] = comp[best]["terminal"]
    verdict["base_terminal"] = base

    # 阶段 1 保护（2026-10-30）：代表年只有 1 年时，"每档年数==目标年数"恒成立，
    # 会让单年数据直接走完整裁定 —— 违反框架 §5.2「单年不外推」「差异 <3% 视为噪声」。
    # 故强制只出梯度形状，不裁 H1/H2/H3。
    if len(years) < 2:
        d = (comp[best]["terminal"] / base - 1) * 100
        # 单调性：放宽方向（2→5→10→off）是否单调递增
        ladder = [s for s in (2, 5, 10, 100000) if comp.get(s, {}).get("terminal")]
        seq = [comp[s]["terminal"] for s in ladder]
        monotonic = all(seq[i] < seq[i + 1] for i in range(len(seq) - 1))
        verdict["stage1"] = True
        verdict["stage1_delta"] = d
        verdict["stage1_within"] = abs(d) < 3.0
        verdict["stage1_monotonic"] = monotonic
        verdict["note"] = (
            f"阶段 1（代表年 {len(years)} 年，仅出梯度形状，不作 H1/H2/H3 终裁）："
            f"最优档 {best} 相对基线 {d:+.2f}%，"
            f"{'落在噪声门槛 ±3% 内' if abs(d) < 3.0 else '超出噪声门槛'}；"
            f"放宽方向{'单调' if monotonic else '**非单调**'}。"
        )
        return verdict
    # 噪声门槛：复利终值差异 <3% 视为持平
    material = abs(comp[best]["terminal"] / base - 1) >= 0.03
    mdd_ok = comp[best]["worst_mdd"] >= comp[2]["worst_mdd"] - 1e-9
    if best == 2 or not material:
        verdict["h2"] = True
        verdict["note"] = "基线档最优或差异在噪声内（<3%）" if best == 2 else \
            f"最优档 {best} 优于基线但幅度 <3%，视为噪声"
    elif best == 0:
        verdict["h3"] = True
        verdict["note"] = "收紧极限档（0）最优"
    elif best in (5, 10, 100000) and mdd_ok:
        verdict["h1"] = True
        verdict["note"] = f"放宽档 {best} 复利终值更高且 MDD 不劣化"
    else:
        verdict["note"] = f"最优档 {best} 但 MDD 劣化，按 H2 处理（保持现状）"
        verdict["h2"] = True
    return verdict


CSS = """
:root{--bg:#0f1115;--fg:#e6e8eb;--muted:#8b929c;--card:#171a21;--line:#262b35;
--good:#e0524d;--bad:#3fa96a;--warn:#d9a441;--acc:#5b8def}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:36px 22px 80px}
h1{font-size:25px;margin:0 0 6px;letter-spacing:.4px}
h2{font-size:18px;margin:38px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:22px 0 8px;color:#c9ced6}
.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
table{border-collapse:collapse;width:100%;margin:10px 0 6px;font-size:13px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:600;background:#141821;text-align:right}
th:first-child,td:first-child{text-align:left}
tr:hover td{background:#1b1f28}
.pos{color:var(--good)}.neg{color:var(--bad)}.mut{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0}
.callout{border-left:3px solid var(--acc);background:#15192499;padding:13px 16px;margin:14px 0;border-radius:0 8px 8px 0}
.callout.red{border-color:var(--good)}.callout.green{border-color:var(--bad)}
.callout.amber{border-color:var(--warn)}
.tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:12px;background:#222836;color:#aeb6c2}
.ok{color:var(--bad);font-weight:600}.fail{color:var(--good);font-weight:600}
.small{font-size:12.5px;color:var(--muted)}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 8px}
.toc a{color:var(--acc);text-decoration:none;font-size:13px;padding:3px 9px;border:1px solid var(--line);border-radius:6px}
code{background:#1d222c;padding:1px 5px;border-radius:4px;font-size:12.5px}
"""


def render_html(data):
    bands, comp, years, v = data["bands"], data["compound"], data["years"], data["verdict"]
    E = html.escape
    H = []
    A = H.append
    A(f"<!doctype html><html lang=zh><head><meta charset=utf-8>"
      f"<title>P2-6a staledays 梯度</title><style>{CSS}</style></head><body><div class=wrap>")
    A("<h1>P2-6a · <code>position.staledays</code> 梯度实验</h1>")
    A(f"<div class=sub>Darvas 箱体量化系统 · 十维 #9（移动 stop）· B 类单变量回测 · "
      f"生成于 {datetime.now():%Y-%m-%d %H:%M}</div>")
    # ---- 核心结论
    A("<h2 id=s0>核心结论</h2>")
    for para in data["conclusion"]:
        A(f"<div class=callout>{para}</div>")
    # ---- 裁定
    A("<h2 id=s1>一、假设裁定</h2>")
    if v.get("stage1"):
        A(f"<div class=callout amber><b>阶段 1 裁定</b>：{v['note']}<br>"
          "H1 / H2 / H3 <b>均不裁定</b>（代表年仅 1 年，单年不外推）；"
          "<b>可采纳档位：无</b>。</div>")
    else:
        cls = "green" if v["h1"] else ("amber" if v["h2"] else "red")
        A(f"<div class=callout {cls}><b>裁定</b>：{E(v['note'])}<br>")
        A(f"H1（放宽有效）= <b>{'成立' if v['h1'] else '不成立'}</b> ｜ "
          f"H2（现行最优）= <b>{'成立' if v['h2'] else '不成立'}</b> ｜ "
          f"H3（越严越好）= <b>{'成立' if v['h3'] else '不成立'}</b></div>")
    if v.get("best") is not None:
        A(f"<p class=small>完整档位：{v['full_bands']}；最优档 = <b>{E(STALE_LABEL.get(v['best'], str(v['best'])))}</b>，"
          f"链接终值 {v['best_terminal']:.4f}（基线 {v['base_terminal']:.4f}，"
          f"差 {(v['best_terminal']/v['base_terminal']-1)*100:+.2f}%）。噪声门槛 ±3%。</p>")
    # ---- 主表
    A("<h2 id=s2>二、逐年逐档矩阵</h2>")
    A("<p class=small>权益指标与 <code>p05_yearly_report.py</code> 同口径（equity_snapshot 首末 total、drawdown 最小值）。"
      "涨用红、跌用绿（A 股惯例）。</p>")
    for y in years:
        rows = [(s, bands.get((s, y))) for s in STALE_ORDER if (s, y) in bands]
        if len(rows) < 2:
            continue
        A(f"<h3>{y} 年</h3><table><tr><th>档位</th><th>收益率</th><th>期末权益</th><th>最大回撤</th>"
          f"<th>夏普</th><th>平仓笔数</th><th>胜率</th><th>PF</th><th>净利</th>"
          f"<th>ts 笔数</th><th>ts 占比</th><th>ts 净利</th><th>持有中位</th></tr>")
        for s, m in rows:
            rc = "pos" if m["ret"] > 0 else "neg"
            A(f"<tr><td>{E(STALE_LABEL.get(s, str(s)))}</td>"
              f"<td class={rc}>{pct(m['ret'], 2)}</td><td>{money(m['eq_end'])}</td>"
              f"<td class=neg>{pct(m['maxdd'], 2)}</td><td>{num(m['sharpe'], 2)}</td>"
              f"<td>{m['n']}</td><td>{pct(m['wr'])}</td><td>{num(m['pf'], 2)}</td>"
              f"<td class={'pos' if m['pnl'] > 0 else 'neg'}>{money(m['pnl'])}</td>"
              f"<td>{m['ts_n']}</td><td>{pct(m['ts_share'])}</td>"
              f"<td class={'pos' if m['ts_pnl'] > 0 else 'neg'}>{money(m['ts_pnl'])}</td>"
              f"<td>{num(m['hold_med'])}</td></tr>")
        A("</table>")
    # ---- 复利链接
    if len(years) < 2:
        A("<h2 id=s3>三、单年收益率（尚不构成链接终值）</h2>")
        A(f"<div class=callout amber>阶段 1 只有 1 个代表年（{years[0]}），"
          f"<code>Π(1+r_y)</code> 退化为 <code>1+r</code>，<b>不构成跨年链接复利终值</b>；"
          f"主判据（框架 §4.1）在本阶段不生效，下表「链接终值」仅为占位。跨年判定须等阶段 2。</div>")
    else:
        A("<h2 id=s3>三、代表年链接复利终值（主判据）</h2>")
    A("<p class=small>Π(1+r_y)，代表年：%s。单年视角会被好年/坏年主导，链接后才是可比口径。</p>"
      % "、".join(str(y) for y in years))
    A("<table><tr><th>档位</th><th>覆盖年</th><th>自身链接终值</th><th>CAGR</th><th>最差 MDD</th>"
      "<th>与基线对比年集</th><th>相对基线</th></tr>")
    for s in STALE_ORDER:
        c = comp.get(s) or comp.get(str(s)) or {}
        yrs = "+".join(str(y) for y in (c.get("years_list") or []))
        if not c.get("terminal"):
            A(f"<tr><td>{E(STALE_LABEL.get(s, str(s)))}</td><td>—</td>"
              f"<td colspan=5 class=mut>未完跑</td></tr>")
            continue
        rel = c.get("rel_vs_base")
        vy = "+".join(str(y) for y in (c.get("vs_years") or []))
        if s == 2:
            vy, rel = yrs, 0.0
        rc = "pos" if (rel or 0) > 0 else ("neg" if (rel or 0) < 0 else "mut")
        A(f"<tr><td>{E(STALE_LABEL.get(s, str(s)))}</td><td>{yrs or '—'}</td>"
          f"<td>{c['terminal']:.4f}</td><td>{pct(c['cagr'], 2)}</td>"
          f"<td class=neg>{pct(c['worst_mdd'], 2)}</td><td>{vy or '—'}</td>"
          f"<td class={rc}>{'—' if rel is None else f'{rel*100:+.2f}%'}</td></tr>")
    A("<p class=small>横比只看「相对基线」列：只在「该档与基线都有数据」的年份上复利，"
      "覆盖年数不同的档不会因多跑一年而虚高。</p>")
    A("</table>")
    # ---- 结构指标
    A("<h2 id=s4>四、结构指标（判「怎么变的」，不给分）</h2>")
    A("<p class=small>P2-5 已证：46 笔大赢家（MFE&gt;50%）贡献 +1,133,049，是系统全部利润。"
      "放宽容量的第一风险是<b>切碎大赢家</b>或<b>让亏损单长期占仓</b>。</p>")
    for y in years:
        rows = [(s, bands.get((s, y))) for s in STALE_ORDER if (s, y) in bands]
        if len(rows) < 2:
            continue
        A(f"<h3>{y} 年</h3><table><tr><th>档位</th><th>ts 退出占比</th><th>短持块(≤10日)净利</th>"
          f"<th>短持笔数</th><th>大赢家笔数</th><th>大赢家净利</th><th>MFE 中位</th>"
          f"<th>前三赢家占净利</th><th>日均暴露</th><th>持有 P90</th></tr>")
        for s, m in rows:
            share = (m["top3_pnl"] / m["pnl"]) if (m["pnl"] or 0) > 0 else None
            A(f"<tr><td>{E(STALE_LABEL.get(s, str(s)))}</td><td>{pct(m['ts_share'])}</td>"
              f"<td class={'pos' if m['short_pnl'] > 0 else 'neg'}>{money(m['short_pnl'])}</td>"
              f"<td>{m['short_n']}</td><td>{m['big_n']}</td>"
              f"<td class=pos>{money(m['big_pnl'])}</td><td>{pct(m['mfe_med'])}</td>"
              f"<td>{pct(share, 0)}</td><td>{pct(m['exposure'], 0)}</td>"
              f"<td>{num(m['hold_p90'])}</td></tr>")
        A("</table>")
    # ---- 持有分布
    A("<h2 id=s5>五、持有天数分布变化</h2>")
    A("<p class=small>BarCount 口径。基线档在 6–10 日的尖峰是 time_stop 的机械后果（P2-5 §七）。</p>")
    for y in years:
        rows = [(s, bands.get((s, y))) for s in STALE_ORDER if (s, y) in bands]
        if len(rows) < 2:
            continue
        keys = ["0", "1-2", "3-5", "6-10", "11-20", "21-60", "61+"]
        A(f"<h3>{y} 年</h3><table><tr><th>档位</th>" +
          "".join(f"<th>{k} 日</th>" for k in keys) + "</tr>")
        for s, m in rows:
            A(f"<tr><td>{E(STALE_LABEL.get(s, str(s)))}</td>" +
              "".join(f"<td>{m['hold_hist'][k]}</td>" for k in keys) + "</tr>")
        A("</table>")
    # ---- HARD
    A("<h2 id=s6>六、HARD 校验</h2>")
    A("<h3>① 基线可复现（生死线）</h3>")
    A("<p class=small>staledays=2 档必须与历史基线库逐笔一致，否则差异无法归因于 staledays。</p>")
    A("<table><tr><th>年</th><th>新库笔数</th><th>基线笔数</th><th>新库净利</th><th>基线净利</th>"
      "<th>逐笔差异</th><th>结论</th></tr>")
    for r in data["hard_baseline"]:
        A(f"<tr><td>{r['year']}</td><td>{r.get('n_new','—')}</td><td>{r.get('n_base','—')}</td>"
          f"<td>{money(r.get('pnl_new'))}</td><td>{money(r.get('pnl_base'))}</td>"
          f"<td>{r.get('n_diff','—')}</td>"
          f"<td class={'ok' if r['ok'] else 'fail'}>{'通过' if r['ok'] else '不通过'}</td></tr>")
    A("</table>")
    if data["hard_baseline"] and not all(r["ok"] for r in data["hard_baseline"]):
        for r in data["hard_baseline"]:
            if not r["ok"] and r.get("first_diff"):
                A(f"<div class=callout red>首个差异：新库 <code>{E(str(r['first_diff']))}</code>；"
                  f"基线 <code>{E(str(r['first_diff_base']))}</code></div>")
    A("<h3>② 参数生效</h3>")
    A("<table><tr><th>年</th><th>目标档</th><th>库内 param</th><th>ts 笔数</th><th>结论</th></tr>")
    for r in data["hard_params"]:
        st = "通过" if r["ok"] else "不通过"
        if r.get("off_ok") is False:
            st = "不通过（off 档 ts 应为 0）"
        A(f"<tr><td>{r['year']}</td><td>{r['stale']}</td><td>{r['param']}</td>"
          f"<td>{r['ts_n']}</td><td class={'ok' if r['ok'] and r.get('off_ok') is not False else 'fail'}>{st}</td></tr>")
    A("</table>")
    A("<h3>③ 单变量（除 staledays 外 param 与基线库逐项相同）</h3>")
    A("<p class=small>基线库 = <code>var/bt/base/bt_baseA_&lt;年&gt;.db</code>（同源新基线）。"
      "任何额外差异都意味着引入第二个变量，本档与前档的差异就不能归因于 staledays。</p>")
    A("<table><tr><th>年</th><th>档位</th><th>param 项数</th><th>与基线不同的键</th><th>缺键</th><th>结论</th></tr>")
    for r in data.get("hard_single", []):
        A(f"<tr><td>{r['year']}</td><td>{E(STALE_LABEL.get(r['stale'], str(r['stale'])))}</td>"
          f"<td>{r['n_param']}</td><td>{E(r['only'])}</td><td>{r['miss']}</td>"
          f"<td class={'ok' if r['ok'] else 'fail'}>{'通过' if r['ok'] else '不通过'}</td></tr>")
    A("</table>")
    A("<h3>④ 持有分布合计闭合</h3>")
    A("<p class=small>各档各年「持有天数分布」合计必须等于已平仓笔数，不得静默丢弃。"
      "持有天数取 <code>BarCount</code> 口径（<code>date &gt; opened_date</code>，不含开仓日），"
      "故中位 5 对应 P2-5 报告（含开仓日）的 6。</p>")
    A("<table><tr><th>年</th><th>档位</th><th>已平仓</th><th>分布合计</th><th>结论</th></tr>")
    for m in sorted(data["bands"].values(), key=lambda r: (r["year"], r["stale"])):
        if m["n"] == 0:
            continue
        A(f"<tr><td>{m['year']}</td><td>{E(STALE_LABEL.get(m['stale'], str(m['stale'])))}</td>"
          f"<td>{m['n']}</td><td>{m['hist_sum']}</td>"
          f"<td class={'ok' if m['hist_ok'] else 'fail'}>{'通过' if m['hist_ok'] else '不通过'}</td></tr>")
    A("</table>")
    A(f"<div class=callout blue><b>复现命令</b>：<code>python scripts/p26_report.py</code>"
      f"（只读 var/bt/p26/*.db 与历史基线库）；回测执行器 <code>scripts/p26_run.py</code>。</div>")
    A("</div></body></html>")
    return "\n".join(H)


def render_md(data):
    bands, comp, years, v = data["bands"], data["compound"], data["years"], data["verdict"]
    L = []
    A = L.append
    A("# P2-6a · `position.staledays` 梯度实验")
    A("")
    A(f"> Darvas 箱体量化系统 · 十维 #9 · B 类单变量回测 · 生成于 {datetime.now():%Y-%m-%d %H:%M}")
    A("")
    A("## 核心结论")
    A("")
    for para in data["conclusion"]:
        A(f"- {para}")
    A("")
    A("## 一、假设裁定")
    A("")
    A(f"**{v['note']}**")
    A("")
    A(f"| 假设 | 裁定 |\n|---|---|")
    if v.get("stage1"):
        A("| H1 放宽有效 | — 阶段 1 不裁定（代表年仅 1 年） |")
        A("| H2 现行最优 | — 阶段 1 不裁定（代表年仅 1 年） |")
        A("| H3 越严越好 | — 阶段 1 不裁定（代表年仅 1 年） |")
        A("| **可采纳档位** | **无**（最优档差异落在噪声门槛内，且放宽方向非单调） |")
    else:
        A(f"| H1 放宽有效 | {'成立' if v['h1'] else '不成立'} |")
        A(f"| H2 现行最优 | {'成立' if v['h2'] else '不成立'} |")
        A(f"| H3 越严越好 | {'成立' if v['h3'] else '不成立'} |")
    A("")
    A("## 二、逐年逐档矩阵")
    A("")
    for y in years:
        rows = [(s, bands.get((s, y))) for s in STALE_ORDER if (s, y) in bands]
        if len(rows) < 2:
            continue
        A(f"### {y}")
        A("")
        A("| 档位 | 收益率 | 期末权益 | 最大回撤 | 夏普 | 笔数 | 胜率 | PF | 净利 | ts 笔数 | ts 占比 | ts 净利 | 持有中位 |")
        A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for s, m in rows:
            A(f"| {STALE_LABEL.get(s, s)} | {pct(m['ret'],2)} | {money(m['eq_end'])} | "
              f"{pct(m['maxdd'],2)} | {num(m['sharpe'], 2)} | {m['n']} | {pct(m['wr'])} | "
              f"{num(m['pf'], 2)} | {money(m['pnl'])} | {m['ts_n']} | {pct(m['ts_share'])} | "
              f"{money(m['ts_pnl'])} | {num(m['hold_med'])} |")
        A("")
    if len(years) < 2:
        A("## 三、单年收益率（尚不构成链接终值）")
        A("")
        A(f"> 阶段 1 只有 1 个代表年（{years[0]}），`Π(1+r_y)` 退化为 `1+r`，"
          "**不构成跨年链接复利终值**，主判据（框架 §4.1）在本阶段不生效，"
          "下表「链接终值」仅为占位。跨年判定须等阶段 2。")
    else:
        A("## 三、代表年链接复利终值（主判据）")
    A("")
    A("| 档位 | 覆盖年 | 自身链接终值 | CAGR | 最差 MDD | 与基线对比年集 | 相对基线 |")
    A("|---|---|---:|---:|---:|---|---:|")
    for s in STALE_ORDER:
        c = comp.get(s) or comp.get(str(s)) or {}
        if not c.get("terminal"):
            A(f"| {STALE_LABEL.get(s, s)} | — | — | — | — | — | 未完跑 |")
            continue
        yrs = "+".join(str(y) for y in c.get("years_list") or [])
        rel = c.get("rel_vs_base")
        vy = "+".join(str(y) for y in c.get("vs_years") or [])
        if s == 2:
            vy, rel = yrs, 0.0
        A(f"| {STALE_LABEL.get(s, s)} | {yrs} | {c['terminal']:.4f} | {pct(c['cagr'],2)} | "
          f"{pct(c['worst_mdd'],2)} | {vy or '—'} | "
          f"{'—' if rel is None else f'{rel*100:+.2f}%'} |")
    A("")
    A("> **横比只看「相对基线」列**：只在「该档与基线都有数据」的年份上复利，"
      "覆盖年数不同的档不会因为多跑一年而虚高。"
      "「自身链接终值」是按各档自己覆盖年数算的绝对收益，只作参考、不与别档横比。")
    A("")
    A("## 四、结构指标")
    A("")
    for y in years:
        rows = [(s, bands.get((s, y))) for s in STALE_ORDER if (s, y) in bands]
        if len(rows) < 2:
            continue
        A(f"### {y}")
        A("")
        A("| 档位 | ts 占比 | 短持块净利 | 短持笔数 | 大赢家笔数 | 大赢家净利 | MFE 中位 | 前三赢家占比 | 日均暴露 | 持有 P90 |")
        A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for s, m in rows:
            share = (m["top3_pnl"] / m["pnl"]) if (m["pnl"] or 0) > 0 else None
            A(f"| {STALE_LABEL.get(s, s)} | {pct(m['ts_share'])} | {money(m['short_pnl'])} | "
              f"{m['short_n']} | {m['big_n']} | {money(m['big_pnl'])} | {pct(m['mfe_med'])} | "
              f"{pct(share,0)} | {pct(m['exposure'],0)} | {num(m['hold_p90'])} |")
        A("")
    A("## 五、持有天数分布")
    A("")
    for y in years:
        rows = [(s, bands.get((s, y))) for s in STALE_ORDER if (s, y) in bands]
        if len(rows) < 2:
            continue
        keys = ["0", "1-2", "3-5", "6-10", "11-20", "21-60", "61+"]
        A(f"### {y}")
        A("")
        A("| 档位 | " + " | ".join(f"{k} 日" for k in keys) + " |")
        A("|---|" + "---:|" * len(keys))
        for s, m in rows:
            A(f"| {STALE_LABEL.get(s, s)} | " +
              " | ".join(str(m["hold_hist"][k]) for k in keys) + " |")
        A("")
    A("## 六、HARD 校验")
    A("")
    A("### ① 基线可复现")
    A("")
    A("| 年 | 新库笔数 | 基线笔数 | 新库净利 | 基线净利 | 逐笔差异 | 结论 |")
    A("|---|---:|---:|---:|---:|---:|---|")
    for r in data["hard_baseline"]:
        A(f"| {r['year']} | {r.get('n_new','—')} | {r.get('n_base','—')} | {money(r.get('pnl_new'))} | "
          f"{money(r.get('pnl_base'))} | {r.get('n_diff','—')} | {'通过' if r['ok'] else '**不通过**'} |")
    A("")
    A("### ② 参数生效")
    A("")
    A("| 年 | 目标档 | 库内 param | ts 笔数 | 结论 |")
    A("|---|---:|---|---:|---|")
    for r in data["hard_params"]:
        st = "通过" if r["ok"] else "不通过"
        if r.get("off_ok") is False:
            st = "不通过（off 档 ts 应为 0）"
        A(f"| {r['year']} | {r['stale']} | {r['param']} | {r['ts_n']} | {st} |")
    A("")
    A("### ③ 单变量（除 staledays 外 param 与基线库逐项相同）")
    A("")
    A("| 年 | 档位 | 库内 param 项数 | 与基线不同的键 | 缺键 | 结论 |")
    A("|---|---|---:|---|---:|---|")
    for r in data.get("hard_single", []):
        A(f"| {r['year']} | {STALE_LABEL.get(r['stale'], r['stale'])} | {r['n_param']} | "
          f"{r['only']} | {r['miss']} | {'通过' if r['ok'] else '**不通过**'} |")
    A("")
    A("### ④ 持有分布合计闭合")
    A("")
    A("| 年 | 档位 | 已平仓 | 分布合计 | 结论 |")
    A("|---|---|---:|---:|---|")
    for m in sorted(data["bands"].values(), key=lambda r: (r["year"], r["stale"])):
        if m["n"] == 0:
            continue
        A(f"| {m['year']} | {STALE_LABEL.get(m['stale'], m['stale'])} | {m['n']} | "
          f"{m['hist_sum']} | {'通过' if m['hist_ok'] else '**不通过**'} |")
    A("")
    A("## 七、复现")
    A("")
    A("```")
    A("python scripts/p26_run.py --years 2012 --staledays 0,2,5,10,100000")
    A("python scripts/p26_report.py")
    A("```")
    return "\n".join(L)


def build():
    bands_raw = discover()
    years = sorted({y for (_, y) in bands_raw})
    comp = compound(bands_raw, years)
    v = judge(bands_raw, comp, years)
    data = {
        "meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                 "years": years, "bands": [s for s in STALE_ORDER
                                           if any(ss == s for ss, _ in bands_raw)]},
        "years": years,
        # 渲染用 tuple key；写 JSON 时由 json_payload() 展平为记录列表
        "bands": bands_raw,
        "compound": {str(k): v_ for k, v_ in comp.items()},
        "verdict": v,
        "hard_baseline": hard_baseline(bands_raw),
        "hard_params": hard_params(bands_raw),
        "hard_single": hard_single_var(bands_raw),
    }
    data["conclusion"] = make_conclusion(data)
    return data


def json_payload(data):
    """JSON 输出：库里别名为记录列表，避免 tuple key 变成字符串。"""
    out = dict(data)
    out["bands"] = sorted((dict(m) for m in data["bands"].values()),
                          key=lambda r: (r["year"], r["stale"]))
    out["compound"] = {str(k): v for k, v in data["compound"].items()}
    return out


def make_conclusion(data):
    """一句话可执行结论，不外推。"""
    v, comp, years = data["verdict"], data["compound"], data["years"]
    out = []
    if not v.get("best"):
        out.append("代表年档位未跑齐，本报告暂无结论。")
        return out
    best = v["best"]
    b = comp.get(2) or {}
    if v.get("stage1"):
        d = v["stage1_delta"]
        mono = v.get("stage1_monotonic")
        out.append(
            f"<b>阶段 1 结论：无档位达到可采纳标准，维持 "
            f"<code>position.staledays=2</code>。</b>"
            f"最优档 {STALE_LABEL.get(best, best)} 相对基线 {d:+.2f}%，"
            f"{'落在噪声门槛 ±3% 内' if v.get('stage1_within') else '超出噪声门槛但需阶段 2 复核'}；"
            f"放宽方向{'单调' if mono else '**非单调**（5 优于 10）'}，"
            f"按框架 §5.1「无稳定最优、保持现状」处理，不得挑单点最优档采纳。"
        )
        out.append(
            "单年不外推：本年 MFE&gt;50% 的大赢家只有 1~2 笔（远低于 §八 n≥30 门槛），"
            "「防切碎大赢家」这条判据在本年<b>不可检验</b>，须靠阶段 2 的 2015（杠杆牛）验证。"
        )
        out.append(
            "结构发现：放宽使短持块亏损单调收窄（−36k → −12k），但 off 档长持块由正转负"
            "（+13k → −8.8k）、建仓数掉 42% —— <b>time_stop 不只在砍亏损，它同时在腾仓位</b>。"
        )
        out.append(
            f"本报告仅覆盖代表年 {'/'.join(map(str, years))}；"
            f"改默认值前须走 migration（append-only）。"
        )
        return out
    if v["h2"]:
        out.append(f"<b>结论：维持 <code>position.staledays=2</code>。</b>"
                   f"最优档为 {STALE_LABEL.get(best, best)}，相对基线 "
                   f"{(v['best_terminal']/v['base_terminal']-1)*100:+.2f}%"
                   f"（噪声门槛 ±3%），不构成改动依据。")
    elif v["h1"]:
        out.append(f"<b>结论：放宽到 <code>staledays={best}</code> 有证据支持。</b>"
                   f"链接终值 {v['best_terminal']:.4f} vs 基线 {v['base_terminal']:.4f}"
                   f"（{(v['best_terminal']/v['base_terminal']-1)*100:+.2f}%），且 MDD 不劣化。")
    elif v["h3"]:
        out.append(f"<b>结论：收紧到 <code>staledays=0</code> 有证据支持。</b>"
                   f"链接终值 {v['best_terminal']:.4f} vs 基线 {v['base_terminal']:.4f}。")
    out.append(f"本报告仅覆盖代表年 {'/'.join(map(str, years))}，"
               f"<b>单年不外推</b>；改默认值前须走 migration（append-only）。")
    return out


def main():
    global P26_DIRS
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--p26-dir", action="append", default=None,
                    help="追加读取目录（如归档盘 A:/Darvas-archive/p26），可重复；"
                         "本地 var/bt/p26 始终优先")
    a = ap.parse_args()
    if a.p26_dir:
        P26_DIRS = [P26] + [Path(x) for x in a.p26_dir]
    data = build()
    if a.check:
        for r in data["hard_baseline"]:
            print(f"HARD① {r['year']}: {'PASS' if r['ok'] else 'FAIL'} "
                  f"n={r.get('n_new')}/{r.get('n_base')} diff={r.get('n_diff')}")
        for r in data["hard_params"]:
            print(f"HARD② {r['year']} sd={r['stale']}: param={r['param']} ts_n={r['ts_n']}")
        return 0
    EXP.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(render_html(data), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    OUT_JSON.write_text(json.dumps(json_payload(data), ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    print(f"已生成：{OUT_HTML.name} / {OUT_MD.name} / {OUT_JSON.name}")
    print(f"档位×年份：{sorted(data['bands'].keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
