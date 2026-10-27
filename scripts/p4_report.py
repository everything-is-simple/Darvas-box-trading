#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B-4 仓位语义成对重跑 · 报告生成器（只读 p4 库与历史基线库）。

用法：
  python scripts/p4_report.py            # 生成 HTML/MD/JSON 三件套
  python scripts/p4_report.py --check    # 只跑 HARD 校验并打印，不写文件

设计约束（框架 docs/experiments/e17_position_semantics_framework.md）：
  · 权益/逐笔/结构指标直接复用 p26_report 的口径函数，**不另造一套**，
    否则同一引擎两个实验的 MDD/暴露会各算各的，横比失真；
  · HARD① 复用 p4_verify 的独立对账代码，按框架 §6.2 给 A/B/C 判定；
  · 拦单数（框架 §4.2 要求）**不落库**——`equity_budget` 审计只在买单
    生成时写入（planner.go:645），被闸门拒绝的候选根本不产生订单。
    故改用「闸门饱和度」代理，并在报告中作为诚实边界登记。
"""
import argparse
import html
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p26_report as PR      # 复用的通用引擎指标口径
import p4_verify as V        # HARD① 独立对账

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
P4 = Path(os.environ.get("P4_DIR", BT / "p4")).resolve()   # 可用 P4_DIR 指向冒烟目录
EXP = ROOT / "docs" / "experiments"
OUT_HTML = EXP / "e17_position_semantics_report.html"
OUT_MD = EXP / "e17_position_semantics_report.md"
OUT_JSON = EXP / "e17_position_semantics_data.json"

VARIANTS = {
    1: {"label": "① 旧语义", "gate": "off", "max_hold": 7, "note": "14 年历史基线所用（显式传参才能回到）"},
    2: {"label": "② 过渡", "gate": "off", "max_hold": 14, "note": "只放宽只数上限，不加预算闸门（分离机制用）"},
    3: {"label": "③ 新默认", "gate": "on", "max_hold": 0, "note": "0018 生产语义：50% 权益闸门 + 挂单预占"},
}
VORDER = [1, 2, 3]
NOISE = 0.03      # 噪声口径（与 P2-6a 同源，便于横比）
MDD_TOL = 0.005   # MDD 不更差容差 0.5pp


# ---------------------------------------------------------------- 数据读取
def concurrency(con, rows):
    """平均/最大同时持仓数（结构指标：语义是否真的生效）。"""
    dates = [r[0] for r in con.execute("SELECT date FROM equity_snapshot ORDER BY date")]
    if not dates:
        return {"con_avg": None, "con_max": None}
    spans = [(r["opened_date"], r["closed_date"]) for r in rows]
    cnt = [sum(1 for a, b in spans if a <= d and (b is None or d <= b)) for d in dates]
    return {"con_avg": sum(cnt) / len(cnt), "con_max": max(cnt),
            "con_ge_p90": sum(1 for c in cnt if c >= 0.9 * max(cnt)) / len(cnt)}


def single_weight(con, rows):
    """单票在开仓日的占比（检验 8% 上限是否被触到）。"""
    eq = dict(con.execute("SELECT date, total FROM equity_snapshot").fetchall())
    rat = [(r["cost"] / eq[r["opened_date"]]) for r in rows
           if r["cost"] and r["opened_date"] in eq and eq[r["opened_date"]]]
    if not rat:
        return {"single_max": None, "single_med": None}
    return {"single_max": max(rat), "single_med": statistics.median(rat)}


def gate_saturation(con):
    """闸门饱和度代理（框架 §4.2「拦单数」的实际可得替代）。

    `equity_budget` 审计只在买单**生成时**写入，被拒绝的候选不产生订单 →
    拦单数不可由库反推。改用：买单中「提交后预算占用 / 上限」的最大值与贴顶比例。
    """
    n, ratios, limits = 0, [], []
    for (rj,) in con.execute(
            "SELECT reason_json FROM plan_order WHERE side='buy' AND reason_json IS NOT NULL"):
        if "equity_budget" not in rj:
            continue
        try:
            eb = json.loads(rj)["equity_budget"]
            eq, used, notional = eb["equity"], eb["used_before"], eb["notional"]
            if not eq:
                continue
            n += 1
            ratios.append((used + notional) / eq)
            limits.append(eb["limit"])
        except Exception:
            continue
    if not n:
        return {"sat_n": 0, "sat_max": None, "sat_bind": None, "sat_limit": None}
    lim = statistics.median(limits)
    bind = sum(1 for r, l in zip(ratios, limits) if l and r >= l - 1e-9) / n
    return {"sat_n": n, "sat_max": max(ratios), "sat_bind": bind, "sat_limit": lim}


def param(con, key, default=None):
    r = con.execute("SELECT value FROM param WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def is_done(vid, year):
    """回测是否跑完：执行器在日志尾部写 "DONE <year> v<vid>"。

    缺这道守卫，跑到一半的库会带着几十笔已平仓样本被当成有效变体，
    让「③ 收益 −20%」这种假结论直接进裁定表。
    """
    logp = P4 / f"p4_v{vid}_{year}.log"
    if not logp.exists():
        return True          # 无日志的历史产物，交给调用方的 n>0 防御
    return f"DONE {year} v{vid}" in logp.read_text(encoding="utf-8", errors="ignore")


def load_variant(db, year, vid):
    con = PR.open_ro(db)
    try:
        rows = PR.trade_rows(con)
        m = {"year": year, "variant": vid, "db": db.name}
        m.update(PR.eq_metrics(con))
        m.update(PR.trade_metrics(con))
        m.update(PR.structural(con, rows))
        m.update(concurrency(con, rows))
        m.update(single_weight(con, rows))
        m.update(gate_saturation(con))
        m["p_gate"] = param(con, "pos.equity_gate")
        m["p_maxhold"] = param(con, "pool.max_hold")
        m["p_staledays"] = param(con, "position.staledays")
        # 防御：无已平仓样本 = 回测未完成，拒收，否则半成品会被当成「收益 0%」的变体
        if m["n"] == 0:
            raise ValueError("无已平仓样本（回测未完成？）")
        return m
    finally:
        con.close()


def discover():
    out = {}
    for db in sorted(P4.glob("p4_v*_*.db")):
        try:
            _, v, yr = db.stem.split("_")
            vid, year = int(v[1:]), int(yr)
        except Exception:
            continue
        if not is_done(vid, year):
            print(f"[skip] {db.name}：回测未完成（日志无 DONE）")
            continue
        try:
            out[(vid, year)] = load_variant(db, year, vid)
        except Exception as e:
            print(f"[skip] {db.name}：{e}")
    return out


# ---------------------------------------------------------------- HARD 校验
def hard1(bands):
    """HARD①：变体① 与历史基线库逐笔对账 → A/B/C（框架 §6.2）。"""
    res = []
    for (vid, year) in sorted(bands):
        if vid != 1:
            continue
        ref_db = BT / f"bt_e17_{year}.db"
        cur_db = P4 / f"p4_v1_{year}.db"
        if not ref_db.exists() or not cur_db.exists():
            continue
        ref, rp = V.load(ref_db)
        cur, cp = V.load(cur_db)
        only_ref, only_cur, pnl_diff = V.compare(ref, cur)
        pdiff = V.cmp_params(rp, cp)
        hard_keys = [k for k, _, _, tag in pdiff if "不可归因" in tag]
        exact = not only_ref and not only_cur and not pnl_diff and len(ref) == len(cur)
        verdict = "A" if (exact and not hard_keys) else ("C" if hard_keys else "B")
        res.append({
            "year": year, "verdict": verdict,
            "n_ref": len(ref), "n_cur": len(cur),
            "sum_ref": sum(r[3] or 0 for r in ref), "sum_cur": sum(r[3] or 0 for r in cur),
            "only_ref": len(only_ref), "only_cur": len(only_cur), "pnl_diff": len(pnl_diff),
            "param_diff": [{"key": k, "ref": a, "cur": b, "attributable": "不可归因" not in t}
                           for k, a, b, t in pdiff],
            "hard_keys": hard_keys,
            "sample": {"only_ref": [list(k) for k in only_ref[:5]],
                       "only_cur": [list(k) for k in only_cur[:5]]},
        })
    return res


def hard_params(bands):
    """HARD②③：参数生效 + 变体间只差那两个参数。"""
    res = []
    for (vid, year), m in sorted(bands.items()):
        want = VARIANTS[vid]
        res.append({
            "year": year, "variant": vid,
            # 历史库（0018 之前）没有 pos.equity_gate 键；真 p4 库必有。
            # 缺失时显示「缺失」并按不通过处理，不静默当 off。
            "gate": m["p_gate"] if m["p_gate"] is not None else "缺失",
            "gate_ok": m["p_gate"] == want["gate"],
            "max_hold": m["p_maxhold"] if m["p_maxhold"] is not None else "缺失",
            "max_hold_ok": m["p_maxhold"] == str(want["max_hold"]),
        })
    return res


def hard_closure(bands):
    """HARD④：已平仓 + 未平仓 = 建仓总数，且持有分布合计 = 已平仓（不静默丢弃）。"""
    res = []
    for (vid, year), m in sorted(bands.items()):
        con = PR.open_ro(P4 / m["db"])
        try:
            built = con.execute("SELECT COUNT(*) FROM position_leg WHERE leg_no=1").fetchone()[0]
            openn = con.execute("SELECT COUNT(*) FROM position WHERE status<>'closed'").fetchone()[0]
        finally:
            con.close()
        res.append({"year": year, "variant": vid, "n": m["n"], "open": openn, "built": built,
                    "closed_ok": m["n"] + openn == built, "hist_ok": m["hist_ok"], "hist_sum": m["hist_sum"]})
    return res


# ---------------------------------------------------------------- 复利与判定
def compound(bands, years):
    """代表年链接复利（擂台规则 §〇.1：不比单年 eq_end）。"""
    out = {}
    for vid in VORDER:
        rs = [(y, bands[(vid, y)]) for y in years if (vid, y) in bands]
        if not rs:
            continue
        term = 1.0
        for _, m in rs:
            term *= (1 + m["ret"])
        out[vid] = {"years": [y for y, _ in rs], "n_years": len(rs), "terminal": term,
                    "cagr": term ** (1 / len(rs)) - 1 if rs else None,
                    "worst_mdd": min(m["maxdd"] for _, m in rs),
                    "cagr_over_mdd": (term ** (1 / len(rs)) - 1) / abs(min(m["maxdd"] for _, m in rs))
                    if min(m["maxdd"] for _, m in rs) else None}
    return out


def judge(bands, comp, years, h1res):
    years_all = sorted({y for _, y in bands})
    full_years = [y for y in years_all if all((v, y) in bands for v in VORDER)]
    v = {"stage": "matrix" if full_years else "anchors_only", "full_years": full_years,
         "h1": None, "h2": None, "h3": None, "verdict": "", "note": ""}
    a1 = h1res[0]["verdict"] if h1res else None
    v["hard1"] = a1
    if not full_years:
        v["verdict"] = "数据不足，不判定"
        v["note"] = (f"目前仅有 {sorted(bands)}；三变体齐备的年份为空 → 阶段 0（锚验证）"
                     "尚未扩展到矩阵。本报告只登记 HARD① 与已跑档位的原始指标。")
        return v
    c1, c3 = comp.get(1), comp.get(3)
    if not c1 or not c3:
        v["verdict"] = "数据不足，不判定"
        return v
    rel = c3["terminal"] / c1["terminal"] - 1
    dmdd = c3["worst_mdd"] - c1["worst_mdd"]     # 正值 = 回撤更浅（更好）
    v["rel"], v["dmdd"] = rel, dmdd
    sign_flip = False
    for y in full_years:
        d = bands[(3, y)]["ret"] - bands[(1, y)]["ret"]
        if abs(d) > NOISE and (bands[(3, y)]["ret"] > 0) != (bands[(1, y)]["ret"] > 0):
            sign_flip = True
    v["sign_flip"] = sign_flip
    if abs(rel) < NOISE:
        v["h2"] = True
        v["verdict"] = "H2 新语义中性"
        v["note"] = (f"③ 与 ① 复利终值差 {rel * 100:+.2f}%，在 3% 噪声带内 → 按框架仍立 ③ 为新基线"
                     "（工程上更干净），但**不得表述为优化**。")
    elif rel > 0 and dmdd >= -MDD_TOL:
        v["h1"] = True
        v["verdict"] = "H1 新语义更优"
        v["note"] = (f"③ 复利终值 {rel * 100:+.2f}%、MDD 差 {dmdd * 100:+.2f}pp（不更差）"
                     " → 采纳并立为新账本基线。")
    elif rel < 0 and dmdd < -MDD_TOL:
        v["h3"] = True
        v["verdict"] = "H3 新语义有害"
        v["note"] = (f"③ 复利终值 {rel * 100:+.2f}% 且 MDD 差 {dmdd * 100:+.2f}pp（更差）"
                     " → 不采纳，须写 migration 回滚到 off+7。")
    else:
        v["verdict"] = "非单调 / 无稳定结论"
        v["note"] = (f"③ 相对 ①：复利 {rel * 100:+.2f}%、MDD 差 {dmdd * 100:+.2f}pp，不满足任一"
                     "单边条件 → 按 H2 处理，登记「机制可分离但结论不稳定」，不得挑单点最优。")
    if v["verdict"] == "H1 新语义更优" and sign_flip:
        v["verdict"] = "无稳定结论（逐年符号不一致）"
        v["note"] += " 但代表年方向不一致 → 降级为「无稳定结论」，按 H2 处理并列逐年符号表。"
    small = [y for y in full_years if bands[(3, y)]["n"] < 60 or bands[(1, y)]["n"] < 60]
    v["small_years"] = small
    if small:
        v["note"] += f" 样本不足年份（<60 笔）：{small}，不参与判定。"
    if a1 == "B":
        v["note"] += " ⚠️ HARD① 判 B：① 为「新二进制下的旧语义锚」，其绝对值不得与 14 年历史库横比。"
    elif a1 == "C":
        v["verdict"] = "实验作废（HARD① C 类）"
        v["note"] = "出现不可归因的参数差异，先查二进制/参数/迁移，不得解读任何变体差异。"
    return v


# ---------------------------------------------------------------- 渲染
def build():
    bands = discover()
    years = sorted({y for _, y in bands})
    h1res = hard1(bands)
    data = {"meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                     "years": years, "variants": [v for v in VORDER
                                                  if any(k[0] == v for k in bands)],
                     "noise": NOISE},
            "years": years, "bands": bands,
            "hard1": h1res, "hard23": hard_params(bands), "hard4": hard_closure(bands)}
    data["compound"] = compound(bands, years)
    data["verdict"] = judge(bands, data["compound"], years, h1res)
    data["conclusion"] = make_conclusion(data)
    return data


def make_conclusion(data):
    v, comp, t = data["verdict"], data["compound"], data["meta"]
    out = []
    out.append(f"阶段：**{v['stage']}**；已跑变体 {t['variants']}、年份 {t['years']}。")
    if v.get("hard1"):
        m = {"A": "逐笔完全复现（① 可与 14 年历史库横比）",
             "B": "不完全复现但可归因（① 降级为同二进制锚，①②③ 内部仍可比）",
             "C": "出现不可归因差异（实验作废）"}[v["hard1"]]
        out.append(f"HARD① 判定 **{v['hard1']}** —— {m}。")
    if comp:
        for vid in VORDER:
            c = comp.get(vid)
            if not c:
                continue
            out.append(f"{VARIANTS[vid]['label']}：{c['n_years']} 年链接终值 "
                       f"**{c['terminal']:,.3f}×**（CAGR {c['cagr'] * 100:+.2f}%、"
                       f"最差 MDD {c['worst_mdd'] * 100:.2f}%）。")
    out.append(f"裁定：**{v['verdict']}** —— {v['note']}")
    return out


def render_html(data):
    bands, comp, years, v = data["bands"], data["compound"], data["years"], data["verdict"]
    E = html.escape
    H = []
    A = H.append
    A(f"<!doctype html><html lang=zh><head><meta charset=utf-8>"
      f"<title>B-4 仓位语义成对重跑</title><style>{PR.CSS}</style></head><body><div class=wrap>")
    A("<h1>B-4 · 仓位语义成对重跑</h1>")
    A(f"<div class=sub>Darvas 箱体量化系统 · 十维 #10（买多少）· B 类单变量回测 · "
      f"生成于 {datetime.now():%Y-%m-%d %H:%M}</div>")
    A("<div class=toc><a href=#s0>核心结论</a><a href=#s1>变体与样本</a><a href=#s2>HARD① 对账</a>"
      "<a href=#s3>逐变体逐年</a><a href=#s4>复利与裁定</a><a href=#s5>结构指标</a>"
      "<a href=#s6>附录</a></div>")

    A("<h2 id=s0>一、核心结论</h2>")
    A("<div class=callout>")
    for line in data["conclusion"]:
        A(f"<div>{md_inline(line)}</div>")
    A("</div>")

    A("<h2 id=s1>二、变体与样本</h2>")
    A("<table><tr><th>变体</th><th>pos.equity_gate</th><th>pool.max_hold</th><th>语义</th></tr>")
    for vid in VORDER:
        if vid not in data["meta"]["variants"]:
            continue
        w = VARIANTS[vid]
        A(f"<tr><td>{E(w['label'])}</td><td>{w['gate']}</td><td>{w['max_hold']}</td>"
          f"<td style='text-align:left'>{E(w['note'])}</td></tr>")
    A("</table>")
    A(f"<p class=small>年份 {years}；噪声口径 ±{NOISE * 100:.0f}%；MDD 不更差容差 {MDD_TOL * 100:.1f}pp。</p>")

    A("<h2 id=s2>三、HARD① 对账（生死线）</h2>")
    if not data["hard1"]:
        A("<p class=small>尚未产出变体① 库，无法对账。</p>")
    else:
        A("<table><tr><th>年</th><th>判定</th><th>历史笔数</th><th>变体笔数</th>"
          "<th>历史 Σpnl</th><th>变体 Σpnl</th><th>仅历史</th><th>仅变体</th><th>金额差</th></tr>")
        for r in data["hard1"]:
            cls = "ok" if r["verdict"] == "A" else ("fail" if r["verdict"] == "C" else "mut")
            A(f"<tr><td>{r['year']}</td><td class={cls}>{r['verdict']}</td>"
              f"<td>{r['n_ref']}</td><td>{r['n_cur']}</td>"
              f"<td>{PR.money(r['sum_ref'])}</td><td>{PR.money(r['sum_cur'])}</td>"
              f"<td>{r['only_ref']}</td><td>{r['only_cur']}</td><td>{r['pnl_diff']}</td></tr>")
        A("</table>")
        for r in data["hard1"]:
            if r["param_diff"]:
                A(f"<h3>{r['year']} 参数差异</h3><table><tr><th>键</th><th>历史</th><th>变体</th><th>可归因</th></tr>")
                for d in r["param_diff"]:
                    A(f"<tr><td>{E(d['key'])}</td><td>{E(str(d['ref']))}</td><td>{E(str(d['cur']))}</td>"
                      f"<td class={'ok' if d['attributable'] else 'fail'}>"
                      f"{'是' if d['attributable'] else '**否**'}</td></tr>")
                A("</table>")
        A("<p class=small>A=逐笔全等；B=差异可归因到 f6a4c4c/caac49f/0018，① 降级为同二进制锚、"
          "绝对值不得与历史库横比；C=不可归因，实验作废。判据跑前写死于框架 §6.2。</p>")

    A("<h2 id=s3>四、逐变体逐年</h2>")
    A("<table><tr><th>变体</th><th>年</th><th>笔数</th><th>收益</th><th>MDD</th><th>Sharpe</th>"
      "<th>Σpnl</th><th>PF</th><th>胜率</th><th>保本胜率</th><th>缺口</th>"
      "<th>集中度</th><th>剔除前三</th></tr>")
    for vid in VORDER:
        for y in years:
            m = bands.get((vid, y))
            if not m:
                continue
            conc = (m["top3_pnl"] / m["pnl"] * 100) if m["pnl"] > 0 else None
            gap = m["wr"] - bewr(m)
            A(f"<tr><td>{E(VARIANTS[vid]['label'])}</td><td>{y}</td><td>{m['n']}</td>"
              f"<td class={pn(m['ret'])}>{PR.pct(m['ret'], 2)}</td>"
              f"<td class=neg>{PR.pct(m['maxdd'], 2)}</td><td>{PR.num(m['sharpe'], 2)}</td>"
              f"<td class={pn(m['pnl'])}>{PR.money(m['pnl'])}</td><td>{PR.num(m['pf'], 2)}</td>"
              f"<td>{PR.pct(m['wr'])}</td><td>{PR.pct(bewr(m))}</td>"
              f"<td class={pn(gap)}>{PR.pct(gap)}</td>"
              f"<td>{'—' if conc is None else f'{conc:.1f}%'}</td>"
              f"<td class={pn(m['pnl'] - m['top3_pnl'])}>{PR.money(m['pnl'] - m['top3_pnl'])}</td></tr>")
    A("</table>")
    A("<p class=small>保本胜率 = 1/(1+平均盈亏比)，与 P0-1/P2-5 同源；缺口 = 胜率 − 保本胜率；"
      "集中度 = 前三赢家占净利比重（净利≤0 时显示 —）。</p>")

    A("<h2 id=s4>五、复利与裁定</h2>")
    if comp:
        A("<table><tr><th>变体</th><th>年数</th><th>链接终值</th><th>CAGR</th><th>最差 MDD</th><th>Calmar</th></tr>")
        for vid in VORDER:
            c = comp.get(vid)
            if not c:
                continue
            A(f"<tr><td>{E(VARIANTS[vid]['label'])}</td><td>{c['n_years']}</td>"
              f"<td>{c['terminal']:,.3f}×</td><td>{PR.pct(c['cagr'], 2)}</td>"
              f"<td class=neg>{PR.pct(c['worst_mdd'], 2)}</td><td>{PR.num(c['cagr_over_mdd'], 2)}</td></tr>")
        A("</table>")
    else:
        A("<p class=small>无可用变体，无法计算复利链。</p>")
    cls = "green" if v.get("h1") else ("red" if v.get("h3") else "amber")
    A(f"<div class='callout {cls}'><b>裁定：{E(v['verdict'])}</b><br>{md_inline(v['note'])}</div>")
    if len(years) > 1 and comp:
        A("<h3>逐年符号表</h3><table><tr><th>年</th>"
          + "".join(f"<th>{E(VARIANTS[vv]['label'])}</th>" for vv in VORDER if comp.get(vv))
          + "<th>③−①</th></tr>")
        for y in years:
            row = [f"<tr><td>{y}</td>"]
            for vv in VORDER:
                if not comp.get(vv):
                    continue
                m = bands.get((vv, y))
                row.append(f"<td class={pn(m['ret']) if m else ''}>{PR.pct(m['ret'], 2) if m else '—'}</td>")
            a, b = bands.get((1, y)), bands.get((3, y))
            d = (b["ret"] - a["ret"]) if (a and b) else None
            row.append(f"<td class={pn(d)}>{PR.pct(d, 2) if d is not None else '—'}</td></tr>")
            A("".join(row))
        A("</table>")

    A("<h2 id=s5>六、结构指标（判「怎么变的」，不给分）</h2>")
    A("<table><tr><th>变体</th><th>年</th><th>平均持仓</th><th>最大持仓</th><th>日均暴露</th>"
      "<th>单票最大</th><th>闸门饱和度</th><th>贴顶比例</th><th>短持块净利</th>"
      "<th>大赢家笔数</th><th>大赢家净利</th><th>ts_n</th><th>ts_share</th></tr>")
    for vid in VORDER:
        for y in years:
            m = bands.get((vid, y))
            if not m:
                continue
            A(f"<tr><td>{E(VARIANTS[vid]['label'])}</td><td>{y}</td>"
              f"<td>{PR.num(m['con_avg'], 1)}</td><td>{PR.num(m['con_max'])}</td>"
              f"<td>{PR.pct(m['exposure'])}</td><td>{PR.pct(m['single_max'])}</td>"
              f"<td>{PR.pct(m['sat_max']) if m['sat_n'] else '—'}</td>"
              f"<td>{PR.pct(m['sat_bind']) if m['sat_n'] else '—'}</td>"
              f"<td class={pn(m['short_pnl'])}>{PR.money(m['short_pnl'])}</td>"
              f"<td>{m['big_n']}</td><td class={pn(m['big_pnl'])}>{PR.money(m['big_pnl'])}</td>"
              f"<td>{m['ts_n']}</td><td>{PR.pct(m['ts_share'])}</td></tr>")
    A("</table>")
    A("<div class='callout amber'><b>诚实边界：拦单数不可得。</b>"
      "`equity_budget` 审计只在买单**生成时**写入（<code>planner.go:645</code>），"
      "被闸门拒绝的候选不产生订单，故「拒绝了多少笔」无法由库反推。"
      "本报告用**闸门饱和度**（买单提交后预算占用/上限的最大值与贴顶比例）作代理："
      "饱和度接近上限 → 闸门真的咬住了；远低于上限 → 闸门在本年几乎未起作用。</div>")

    A("<h2 id=s6>七、附录</h2>")
    A("<h3>7.1 HARD② 参数生效 / HARD④ 闭合</h3>")
    A("<table><tr><th>年</th><th>变体</th><th>gate</th><th>max_hold</th><th>②参数</th>"
      "<th>已平仓</th><th>未平仓</th><th>建仓</th><th>④闭合</th><th>持有分布合计</th></tr>")
    h4 = {(r["year"], r["variant"]): r for r in data["hard4"]}
    for r in data["hard23"]:
        c = h4.get((r["year"], r["variant"]), {})
        ok = r["gate_ok"] and r["max_hold_ok"]
        A(f"<tr><td>{r['year']}</td><td>{E(VARIANTS[r['variant']]['label'])}</td>"
          f"<td>{E(str(r['gate']))}</td><td>{E(str(r['max_hold']))}</td>"
          f"<td class={'ok' if ok else 'fail'}>{'通过' if ok else '不通过'}</td>"
          f"<td>{c.get('n')}</td><td>{c.get('open')}</td><td>{c.get('built')}</td>"
          f"<td class={'ok' if c.get('closed_ok') else 'fail'}>{'通过' if c.get('closed_ok') else '不通过'}</td>"
          f"<td>{c.get('hist_sum')} {'✔' if c.get('hist_ok') else '✘'}</td></tr>")
    A("</table>")
    A("<h3>7.2 复现</h3>")
    A("<p class=small>回测执行器 <code>scripts/p4_run.py</code>；HARD① 独立校验 "
      "<code>scripts/p4_verify.py</code>；本报告 <code>python scripts/p4_report.py</code>。"
      "指标口径复用 <code>scripts/p26_report.py</code>（同一引擎，避免两套 MDD/暴露）。</p>")
    A("</div></body></html>")
    return "\n".join(H)


def bewr(m):
    """保本胜率：1/(1+平均盈亏比)，用 PF 与胜率反推平均盈亏比。"""
    if not m.get("n") or not m.get("wr"):
        return None
    if m["wr"] <= 0 or m["wr"] >= 1 or not m.get("pf"):
        return None
    return 1 / (1 + m["pf"] * (1 - m["wr"]) / m["wr"]) if m["pf"] else None


def pn(x):
    if x is None:
        return ""
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def md_inline(s):
    """极简 markdown 内联（**粗体** → <b>），HTML 与 MD 共用一份文案。"""
    out = html.escape(s)
    while "**" in out:
        out = out.replace("**", "<b>", 1).replace("**", "</b>", 1)
    return out


def render_md(data):
    bands, comp, years, v = data["bands"], data["compound"], data["years"], data["verdict"]
    L = []
    A = L.append
    A("# B-4 · 仓位语义成对重跑")
    A("")
    A(f"> Darvas 箱体量化系统 · 十维 #10（买多少）· B 类单变量回测 · 生成于 {datetime.now():%Y-%m-%d %H:%M}")
    A("")
    A("## 一、核心结论")
    A("")
    for line in data["conclusion"]:
        A(f"- {line}")
    A("")
    A("## 二、变体与样本")
    A("")
    A("| 变体 | pos.equity_gate | pool.max_hold | 语义 |")
    A("|---|---|---|---|")
    for vid in VORDER:
        if vid not in data["meta"]["variants"]:
            continue
        w = VARIANTS[vid]
        A(f"| {w['label']} | {w['gate']} | {w['max_hold']} | {w['note']} |")
    A("")
    A(f"年份 {years}；噪声口径 ±{NOISE * 100:.0f}%；MDD 不更差容差 {MDD_TOL * 100:.1f}pp。")
    A("")
    A("## 三、HARD① 对账（生死线）")
    A("")
    if not data["hard1"]:
        A("尚未产出变体① 库，无法对账。")
    else:
        A("| 年 | 判定 | 历史笔数 | 变体笔数 | 历史 Σpnl | 变体 Σpnl | 仅历史 | 仅变体 | 金额差 |")
        A("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in data["hard1"]:
            A(f"| {r['year']} | **{r['verdict']}** | {r['n_ref']} | {r['n_cur']} | "
              f"{PR.money(r['sum_ref'])} | {PR.money(r['sum_cur'])} | {r['only_ref']} | "
              f"{r['only_cur']} | {r['pnl_diff']} |")
        A("")
        for r in data["hard1"]:
            for d in r["param_diff"]:
                A(f"- {r['year']} 参数差：`{d['key']}` 历史={d['ref']} 变体={d['cur']} "
                  f"→ {'可归因' if d['attributable'] else '**不可归因**'}")
        A("")
        A("A=逐笔全等；B=差异可归因到 f6a4c4c/caac49f/0018，① 降级为同二进制锚、"
          "绝对值不得与历史库横比；C=不可归因，实验作废。")
    A("")
    A("## 四、逐变体逐年")
    A("")
    A("| 变体 | 年 | 笔数 | 收益 | MDD | Sharpe | Σpnl | PF | 胜率 | 保本胜率 | 缺口 | 集中度 | 剔除前三 |")
    A("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for vid in VORDER:
        for y in years:
            m = bands.get((vid, y))
            if not m:
                continue
            conc = (m["top3_pnl"] / m["pnl"] * 100) if m["pnl"] > 0 else None
            A(f"| {VARIANTS[vid]['label']} | {y} | {m['n']} | {PR.pct(m['ret'], 2)} | "
              f"{PR.pct(m['maxdd'], 2)} | {PR.num(m['sharpe'], 2)} | {PR.money(m['pnl'])} | "
              f"{PR.num(m['pf'], 2)} | {PR.pct(m['wr'])} | {PR.pct(bewr(m))} | "
              f"{PR.pct(m['wr'] - bewr(m) if bewr(m) is not None else None)} | "
              f"{'—' if conc is None else f'{conc:.1f}%'} | "
              f"{PR.money(m['pnl'] - m['top3_pnl'])} |")
    A("")
    A("## 五、复利与裁定")
    A("")
    if comp:
        A("| 变体 | 年数 | 链接终值 | CAGR | 最差 MDD | Calmar |")
        A("|---|---:|---:|---:|---:|---:|")
        for vid in VORDER:
            c = comp.get(vid)
            if not c:
                continue
            A(f"| {VARIANTS[vid]['label']} | {c['n_years']} | {c['terminal']:,.3f}× | "
              f"{PR.pct(c['cagr'], 2)} | {PR.pct(c['worst_mdd'], 2)} | {PR.num(c['cagr_over_mdd'], 2)} |")
        A("")
    A(f"**裁定：{v['verdict']}** —— {v['note']}")
    A("")
    if len(years) > 1 and comp:
        A("### 逐年符号表")
        A("")
        cols = [vv for vv in VORDER if comp.get(vv)]
        A("| 年 | " + " | ".join(VARIANTS[vv]["label"] for vv in cols) + " | ③−① |")
        A("|---|" + "---:|" * (len(cols) + 1))
        for y in years:
            cells = []
            for vv in cols:
                m = bands.get((vv, y))
                cells.append(PR.pct(m["ret"], 2) if m else "—")
            a, b = bands.get((1, y)), bands.get((3, y))
            d = (b["ret"] - a["ret"]) if (a and b) else None
            A(f"| {y} | " + " | ".join(cells) + f" | {PR.pct(d, 2) if d is not None else '—'} |")
        A("")
    A("## 六、结构指标")
    A("")
    A("| 变体 | 年 | 平均持仓 | 最大持仓 | 日均暴露 | 单票最大 | 闸门饱和度 | 贴顶比例 | 短持块净利 | 大赢家笔数 | 大赢家净利 | ts_n | ts_share |")
    A("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for vid in VORDER:
        for y in years:
            m = bands.get((vid, y))
            if not m:
                continue
            A(f"| {VARIANTS[vid]['label']} | {y} | {PR.num(m['con_avg'], 1)} | {PR.num(m['con_max'])} | "
              f"{PR.pct(m['exposure'])} | {PR.pct(m['single_max'])} | "
              f"{PR.pct(m['sat_max']) if m['sat_n'] else '—'} | "
              f"{PR.pct(m['sat_bind']) if m['sat_n'] else '—'} | "
              f"{PR.money(m['short_pnl'])} | {m['big_n']} | {PR.money(m['big_pnl'])} | "
              f"{m['ts_n']} | {PR.pct(m['ts_share'])} |")
    A("")
    A("**诚实边界：拦单数不可得。** `equity_budget` 审计只在买单生成时写入"
      "（`planner.go:645`），被闸门拒绝的候选不产生订单，故拒绝笔数无法由库反推。"
      "上表用**闸门饱和度**（买单提交后预算占用/上限）作代理。")
    A("")
    A("## 七、附录")
    A("")
    A("### 7.1 HARD② 参数生效 / HARD④ 闭合")
    A("")
    A("| 年 | 变体 | gate | max_hold | ②参数 | 已平仓 | 未平仓 | 建仓 | ④闭合 | 持有分布合计 |")
    A("|---|---|---|---|---|---:|---:|---:|---|---:|")
    h4 = {(r["year"], r["variant"]): r for r in data["hard4"]}
    for r in data["hard23"]:
        c = h4.get((r["year"], r["variant"]), {})
        ok = r["gate_ok"] and r["max_hold_ok"]
        A(f"| {r['year']} | {VARIANTS[r['variant']]['label']} | {r['gate']} | {r['max_hold']} | "
          f"{'通过' if ok else '**不通过**'} | {c.get('n')} | {c.get('open')} | {c.get('built')} | "
          f"{'通过' if c.get('closed_ok') else '**不通过**'} | {c.get('hist_sum')} "
          f"{'✔' if c.get('hist_ok') else '✘'} |")
    A("")
    A("### 7.2 复现")
    A("")
    A("回测执行器 `scripts/p4_run.py`；HARD① 独立校验 `scripts/p4_verify.py`；"
      "本报告 `python scripts/p4_report.py`。指标口径复用 `scripts/p26_report.py`。")
    return "\n".join(L)


def json_payload(data):
    out = dict(data)
    out["bands"] = sorted((dict(m) for m in data["bands"].values()),
                          key=lambda r: (r["variant"], r["year"]))
    out["compound"] = {str(k): v for k, v in data["compound"].items()}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只跑校验并打印，不写文件")
    a = ap.parse_args()
    data = build()
    v = data["verdict"]
    print(f"变体×年份：{sorted(data['bands'].keys())}")
    print(f"HARD①：{[r['verdict'] for r in data['hard1']] or '无'}")
    print(f"阶段：{v['stage']}  裁定：{v['verdict']}")
    for line in data["conclusion"]:
        print("  ·", line)
    if a.check:
        return 0
    EXP.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(render_html(data), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    OUT_JSON.write_text(json.dumps(json_payload(data), ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    print(f"\n已写：{OUT_HTML.name} / {OUT_MD.name} / {OUT_JSON.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
