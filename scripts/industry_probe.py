#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行业层零回测探针（只读，不改库、不改引擎）。

目的：回答「我们是否缺失行业兴衰这一环、行业信息能否提升胜率」的第一步——
先把**已完成的基线交易**按行业分组，看盈亏是否高度集中于少数行业、
行业间胜率是否存在结构性差异。

数据口径（必须显式标注）：
  - 交易样本：var/bt/base/bt_baseA_cont_adj.db（**adj 复权口径**基线，1,142 笔）
  - 行业映射：D:/new_tdx64/T0002/hq_cache/tdxhy.cfg（**当前快照**，2026-11-04）
  - 因此本探针结论属于「假设生成」，**不是验证**：
    当前行业归属回填到 2005–2018 年存在前视偏差（股票后来改行业、退市股缺失）。

用法：python scripts/industry_probe.py [--level 2|3] [--min-n 8]
"""
import argparse
import collections
import math
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_DB = os.path.join(ROOT, "var", "bt", "base", "bt_baseA_cont_adj.db")
TDX_HY = "D:/new_tdx64/T0002/hq_cache/tdxhy.cfg"
UNIT_PCT = 0.02


def load_industry(path=TDX_HY):
    """tdxhy.cfg: market|code|T<行业>|||<X...>；market 0=sz 1=sh 2=bj。"""
    raw = open(path, "rb").read().decode("gbk", errors="replace")
    m = {}
    for line in raw.splitlines():
        p = line.split("|")
        if len(p) < 3 or not p[1] or not p[2]:
            continue
        m[(p[0], p[1])] = p[2]
    return m


def key_of(code):
    """sh600000 -> ('1','600000')；sz000001 -> ('0','000001')。"""
    pre, num = code[:2], code[2:]
    return ({"sh": "1", "sz": "0", "bj": "2"}.get(pre, pre), num)


def load_trades(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    eq = {r[0]: r[1] for r in db.execute("SELECT date,total FROM equity_snapshot")}
    leg = {r[0]: r[1] for r in db.execute(
        "SELECT position_id, qty*price FROM position_leg WHERE leg_no=1")}
    rows = []
    for pid, code, od, cd, pnl in db.execute("""
            SELECT id, code, cast(opened_date as text), cast(closed_date as text),
                   coalesce(realized_pnl,0)
            FROM position WHERE status='closed' ORDER BY opened_date, id"""):
        e = eq.get(od[:10], 0.0)
        n = leg.get(pid, 0.0)
        rows.append(dict(pid=pid, code=code, od=od[:10], cd=(cd or "")[:10],
                         pnl=float(pnl), units=(n / (e * UNIT_PCT) if e > 0 else 0.0),
                         year=od[:4]))
    db.close()
    return rows


def metrics(rows):
    n = len(rows)
    if not n:
        return None
    pnl = [r["pnl"] for r in rows]
    w = [p for p in pnl if p > 0]
    units = sum(r["units"] for r in rows)
    ps = sorted(pnl)
    return dict(n=n, wr=len(w) / n, pnl=sum(pnl),
                ex3=(sum(ps[:-3]) if n > 3 else 0.0),
                unit=(sum(pnl) / units if units else 0.0))


def fmt(tag, m):
    if not m:
        return f"{tag} 无数据"
    return (f"{tag:<12} {m['n']:>4}笔 {m['wr']*100:>5.1f}% 总账 {m['pnl']:>+11,.0f} "
            f"剔前3 {m['ex3']:>+11,.0f} 每单位 {m['unit']:>+8,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=2, help="行业层级：1=T01 2=T0101 3=T010101")
    ap.add_argument("--min-n", type=int, default=8)
    a = ap.parse_args()

    ind = load_industry()
    trades = load_trades(BASE_DB)
    print(f"# 行业零回测探针｜样本 = adj 基线 {len(trades)} 笔（复权口径）")
    print(f"# 行业映射 = tdxhy.cfg 当前快照 {len(ind)} 条（前视偏差见文档）")
    print(f"# 行业层级 = T 第 {a.level} 级\n")

    miss = [t for t in trades if key_of(t["code"]) not in ind]
    print(f"未映射 {len(miss)}/{len(trades)} 笔（{len(miss)/len(trades)*100:.1f}%）")
    if miss:
        print("  未映射代码样例:", sorted({t["code"] for t in miss})[:12])

    groups = collections.defaultdict(list)
    for t in trades:
        tc = ind.get(key_of(t["code"]))
        if not tc:
            continue
        t["ind"] = tc[:2 + a.level * 2] if a.level <= 3 else tc
        groups[t["ind"]].append(t)

    print(f"\n覆盖行业数 {len(groups)}")
    rows = sorted(((metrics(v), k, v) for k, v in groups.items()),
                  key=lambda x: -(x[0]["pnl"] if x[0] else 0))
    print("\n== 按行业总账降序（n>=%d）==" % a.min_n)
    for m, k, v in rows:
        if m and m["n"] >= a.min_n:
            print(fmt(k, m))
    print("\n== 小样本行业（n<%d，合计）==" % a.min_n)
    small = [r for m, k, v in rows if m and m["n"] < a.min_n for r in v]
    print(fmt("小行业合计", metrics(small)))
    print(fmt("全样本", metrics(trades)))

    # R29 纪律：行业是「分组标签」，必须给年内对照，不能只看裸分组
    print("\n== R29 年内对照：当年最肥行业 vs 该年其他 ==")
    by_year = collections.defaultdict(list)
    for t in trades:
        if "ind" in t:
            by_year[t["year"]].append(t)
    for y in sorted(by_year):
        v = by_year[y]
        g = collections.defaultdict(list)
        for t in v:
            g[t["ind"]].append(t)
        best = max(g.items(), key=lambda kv: sum(x["pnl"] for x in kv[1]))
        rest = [x for k, vv in g.items() if k != best[0] for x in vv]
        bm, rm = metrics(best[1]), metrics(rest)
        print(f"  {y} 最强行业 {best[0]} {bm['n']:>3}笔 {bm['pnl']:>+10,.0f} "
              f"({bm['wr']*100:.0f}%) | 该年其他 {rm['n']:>3}笔 {rm['pnl']:>+10,.0f} "
              f"({rm['wr']*100:.0f}%)")

    # 行业集中度
    pnls = sorted(((sum(x["pnl"] for x in v), k) for k, v in groups.items()), reverse=True)
    tot = sum(p for p, _ in pnls)
    top5 = sum(p for p, _ in pnls[:5])
    print(f"\n== 集中度 == 前 5 行业 {top5:+,.0f} / 全样本 {tot:+,.0f} = {top5/tot*100:.1f}%")
    print("  前 5:", [(k, round(p)) for p, k in pnls[:5]])
    print("  后 5:", [(k, round(p)) for p, k in pnls[-5:]])


if __name__ == "__main__":
    main()
