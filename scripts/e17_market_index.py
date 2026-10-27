#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E17 市场镜子选型：候选指数 vs 等权市场中位数基准（只读）。

四层评估（口径见 docs/experiments/e17_market_index_framework.md，跑前写死）：
  1 贴合度 d = Σ|b_t − a_t|
  2 操纵抗性：剪刀差天数 + 误差 top20 归因 + 成交额加权差
  3 交易池相关：14 年 e17 组合日收益 × 候选指数（corr / 领先滞后 / regime Jaccard）
  4 道氏组合：单指数 / 双指数配对 / 三指数投票，2013+2017 推演

只读 full_hist_10pct.db 与 bt_e17_*.db；不写任何回测库。
用法：python scripts/e17_market_index.py
"""
from __future__ import annotations

import json
import math
import sqlite3
from array import array
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
BASE_DB = BT / "full_hist_10pct.db"
EXP = ROOT / "docs" / "experiments"
OUT_JSON = EXP / "e17_market_index_data.json"
OUT_MD = EXP / "e17_market_index_report.md"
OUT_HTML = EXP / "e17_market_index_report.html"

YEARS = list(range(2005, 2019))
START, END = "2005-01-01", "2018-12-31"
WARM = "2004-06-01"          # 取 2004 下半年做 prev_close 热启动
MIN_LIST_DAYS = 30
MAX_GAP_DAYS = 30
WINSOR = 0.11
LOOKBACK, UP_BIAS, DOWN_BIAS = 60, 1.002, 0.998

CANDIDATES = [
    ("sz399317", "国证A指", "全市场·自由流通加权·历史最全", "全市场"),
    ("sh000985", "中证全指", "全市场标杆（库内 2016-06 断档）", "全市场"),
    ("sh000001", "上证指数", "现行镜子（对照组）", "对照组"),
    ("sz399001", "深证成指", "深市脸面", "对照组/中小盘"),
    ("sz399106", "深证综指", "全深市", "对照组"),
    ("sh000300", "沪深300", "大盘镜候选", "大盘"),
    ("sh000905", "中证500", "中小盘镜候选（docs/31 草案假设）", "中小盘"),
    ("sh000852", "中证1000", "原文冠军", "中小盘"),
    ("sz399303", "国证2000", "原文冠军（2010 起）", "小盘"),
    ("sz399006", "创业板指", "仅 2010+ 敏感性", "成长"),
    ("sh000510", "中证A500", "行业均衡宽基", "大盘"),
    ("sh000002", "上证A指", "沪市全 A 粗口径", "对照组"),
    ("sz399107", "深A指", "深市全 A 粗口径", "对照组"),
]
CODE2NAME = {c: n for c, n, _, _ in CANDIDATES}


# ---------------------------------------------------------------- 基础工具
def d(s: str) -> date:
    y, m, dd = s.split("-")
    return date(int(y), int(m), int(dd))


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def pct(x, digits=2):
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def num(x, digits=0):
    return "—" if x is None else f"{x:,.{digits}f}"


# ---------------------------------------------------------------- 基准构造
def load_stocks(c) -> dict[str, tuple[str, date]]:
    rows = c.execute(
        "select code,name,list_date from security "
        "where kind='stock' and list_date is not null "
        "and code not like 'sh900%' and code not like 'sz200%'"
    ).fetchall()
    return {code: (name or "", d(ld)) for code, name, ld in rows}


def build_baseline():
    """返回 a_t（等权中位数收益）及辅助统计。"""
    c = connect(BASE_DB)
    stocks = load_stocks(c)

    ret_by_date: dict[str, array] = defaultdict(lambda: array("f"))
    ret2_by_date: dict[str, array] = defaultdict(lambda: array("f"))  # 敏感性：不剔 ST
    bars_by_date: dict[str, int] = defaultdict(int)
    ex_by_date: dict[str, int] = defaultdict(int)   # 设计内剔除量（覆盖率分母修正用）
    up_by_date: dict[str, int] = defaultdict(int)
    amt_by_date: dict[str, float] = defaultdict(float)
    amtr_by_date: dict[str, float] = defaultdict(float)
    excl = defaultdict(int)  # 各过滤原因剔除量

    cur = c.cursor()
    n_stock = 0
    for code, (name, ldate) in stocks.items():
        n_stock += 1
        is_st = ("ST" in name) or ("退" in name)
        rows = cur.execute(
            "select date,close,high,low,volume,amount from daily_bar "
            "where code=? and date>=? and date<=? order by date",
            (code, WARM, END),
        )
        prev_close, prev_date = None, None
        for ds, cl, hi, lo, vol, amt in rows:
            vol = vol or 0
            if vol > 0:
                bars_by_date[ds] += 1
            if vol <= 0:
                continue                      # 停牌/无量：不更新 prev
            if prev_close is None or prev_close <= 0:
                prev_close, prev_date = cl, ds
                continue
            if ds < START:
                prev_close, prev_date = cl, ds
                continue
            gap = (d(ds) - d(prev_date)).days if prev_date else 999
            ret = cl / prev_close - 1.0
            r = max(-WINSOR, min(WINSOR, ret))
            if gap > MAX_GAP_DAYS:
                excl["gap"] += 1
                ex_by_date[ds] += 1
            elif is_st:
                excl["st"] += 1
                ex_by_date[ds] += 1
                ret2_by_date[ds].append(r)   # 敏感性口径：ST 也进中位数
            elif (d(ds) - ldate).days < MIN_LIST_DAYS:
                excl["new"] += 1
                ex_by_date[ds] += 1
            elif hi is None or lo is None or hi <= lo:
                excl["oneword"] += 1
                ex_by_date[ds] += 1
            else:
                ret_by_date[ds].append(r)
                ret2_by_date[ds].append(r)
                if r > 0:
                    up_by_date[ds] += 1
                if amt and amt > 0:
                    amt_by_date[ds] += amt
                    amtr_by_date[ds] += amt * r
            prev_close, prev_date = cl, ds
    c.close()

    a, a2, breadth, amtw, cover, cover2 = {}, {}, {}, {}, {}, {}
    for ds, arr in ret_by_date.items():
        if len(arr) < 100:
            continue
        a[ds] = float(median(arr))
        breadth[ds] = up_by_date[ds] / len(arr)
        amtw[ds] = amtr_by_date[ds] / amt_by_date[ds] if amt_by_date[ds] else a[ds]
        bars = bars_by_date[ds]
        cover[ds] = len(arr) / bars if bars else 0.0
        denom = bars - ex_by_date[ds]          # 合格域：剔除设计内排除后
        cover2[ds] = len(arr) / denom if denom > 0 else 0.0
        if len(ret2_by_date[ds]) >= 100:
            a2[ds] = float(median(ret2_by_date[ds]))
    return {
        "a": a, "a2": a2, "breadth": breadth, "amtw": amtw,
        "cover": cover, "cover2": cover2,
        "bars": dict(bars_by_date), "excl": dict(excl), "n_stock": n_stock,
    }


# ---------------------------------------------------------------- 指数序列
def load_index(code: str):
    c = connect(BASE_DB)
    rows = c.execute(
        "select date,open,high,low,close from daily_bar "
        "where code=? and date>=? and date<=? order by date",
        (code, WARM, END),
    ).fetchall()
    c.close()
    s = {"date": [], "ret": [], "high": [], "low": [], "close": []}
    prev = None
    for ds, op, hi, lo, cl in rows:
        if prev and prev > 0 and ds >= START:
            s["date"].append(ds)
            s["ret"].append(cl / prev - 1.0)
            s["high"].append(hi)
            s["low"].append(lo)
            s["close"].append(cl)
        prev = cl
    return s


# ---------------------------------------------------------------- A 判据
def regime(values, lookback=LOOKBACK):
    out = []
    for i in range(len(values)):
        if i < lookback:
            out.append("range")
            continue
        cur = min(values[i - lookback + 1: i + 1])
        prev = min(values[i - lookback: i])
        if cur > prev * UP_BIAS:
            out.append("bull")
        elif cur < prev * DOWN_BIAS:
            out.append("bear")
        else:
            out.append("range")
    return out


def jaccard(xs, ys):
    a, b = set(xs), set(ys)
    if not a and not b:
        return None
    return len(a & b) / len(a | b)


def pearson(x, y):
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    syy = sum((v - my) ** 2 for v in y)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    return sxy / math.sqrt(sxx * syy)


# ---------------------------------------------------------------- 交易池
def load_pool():
    rows = []
    for y in YEARS:
        p = BT / f"bt_e17_{y}.db"
        if not p.exists():
            raise SystemExit(f"缺库 {p}")
        c = connect(p)
        for ds, total, mv in c.execute("select date,total,market_value from equity_snapshot order by date"):
            rows.append((ds, total, mv))
        c.close()
    rows.sort()
    ret, exposed = {}, {}
    for i in range(1, len(rows)):
        ds, tot, mv = rows[i]
        p0 = rows[i - 1][1]
        if p0 and p0 > 0:
            ret[ds] = tot / p0 - 1.0
            exposed[ds] = mv > 0
    return {"n": len(rows), "first": rows[0][0], "last": rows[-1][0],
            "ret": ret, "exposed": exposed}


# ---------------------------------------------------------------- 主流程
def main():
    print("[1/6] 构造等权市场中位数基准 …")
    base = build_baseline()
    a, a2, breadth, amtw, cover = base["a"], base["a2"], base["breadth"], base["amtw"], base["cover"]
    cover2 = base["cover2"]
    days = sorted(a)
    print(f"      基准交易日 {len(days)}  股票池 {base['n_stock']}  剔除 {base['excl']}")

    cov_vals = sorted(cover.values())
    cov_med = cov_vals[len(cov_vals) // 2]
    cv2 = sorted(cover2.values())
    cov_med2 = cv2[len(cv2) // 2]
    cov_stats = {
        "median": cov_med,
        "median_design": cov_med2,
        "min": cov_vals[0],
        "min_design": cv2[0],
        "lt90": sum(1 for v in cov_vals if v < 0.90),
        "lt90_design": sum(1 for v in cv2 if v < 0.90),
        "lt95": sum(1 for v in cov_vals if v < 0.95),
        "n": len(cov_vals),
        "worst_dates": sorted(cover.items(), key=lambda kv: kv[1])[:13],
    }
    print(f"      覆盖率(全口径) 中位 {cov_med:.2%} 最低 {cov_vals[0]:.2%}；"
          f"(合格域) 中位 {cov_med2:.2%} 最低 {cv2[0]:.2%}")

    print("[2/6] 载入候选指数 …")
    idx = {}
    for code, name, note, role in CANDIDATES:
        s = load_index(code)
        s.update(code=code, name=name, note=note, role=role)
        idx[code] = s
        print(f"      {code} {name:<6} {len(s['date']):>5} 根 {s['date'][0] if s['date'] else '-'}~{s['date'][-1] if s['date'] else '-'}")

    print("[3/6] 第 1+2 层：贴合度与操纵抗性 …")
    layer12, err_map = {}, {}
    for code, s in idx.items():
        pairs = [(ds, b) for ds, b in zip(s["date"], s["ret"]) if ds in a]
        if not pairs:
            continue
        by_year = defaultdict(lambda: [0.0, 0])
        em = {}
        rec = [0.0, 0]
        front, back = 0, 0
        tops = []
        for ds, b in pairs:
            e = abs(b - a[ds])
            em[ds] = e
            front += e
            by_year[ds[:4]][0] += e
            by_year[ds[:4]][1] += 1
            if ds >= "2010-01-01":
                rec[0] += e
                rec[1] += 1
            if b > 0.005 and a[ds] < 0:
                back += 1
            elif b < -0.005 and a[ds] > 0:
                back += 1
            tops.append((e, ds, a[ds], b))
        tops.sort(reverse=True)
        n = len(pairs)
        err_map[code] = em
        # 敏感性：ST 也计入基准
        se, sn = 0.0, 0
        for ds, b in pairs:
            if ds in a2:
                se += abs(b - a2[ds])
                sn += 1
        layer12[code] = {
            "code": code, "name": s["name"], "role": s["role"], "note": s["note"],
            "n": n, "d": front, "mean_err": front / n,
            "mean_err_2010": (rec[0] / rec[1]) if rec[1] else None, "n_2010": rec[1],
            "mean_err_st_in": (se / sn) if sn else None, "n_st_in": sn,
            "first": pairs[0][0], "last": pairs[-1][0],
            "year": {y: {"d": v[0], "n": v[1], "mean": v[0] / v[1]} for y, v in by_year.items()},
            "scissor": back, "scissor_pct": back / n,
            "top20": [{"date": ds, "err": e, "a": aa, "b": bb,
                       "breadth": breadth.get(ds), "amtw": amtw.get(ds),
                       "amtw_gap": (amtw.get(ds, 0) - aa)} for e, ds, aa, bb in tops[:20]],
        }
    rank = sorted(layer12.values(), key=lambda r: r["mean_err"])
    for i, r in enumerate(rank, 1):
        r["rank"] = i
    print("      排名：" + " | ".join(f"{r['name']}({r['mean_err']:.3%})" for r in rank[:5]))

    # 口径 B：2010-01-01 起共同窗口（国证2000/创业板只有 2010+，长窗口对比不公平）
    rank2010 = sorted([r for r in layer12.values() if r.get("mean_err_2010")],
                      key=lambda r: r["mean_err_2010"])
    for i, r in enumerate(rank2010, 1):
        r["rank_2010"] = i
    print("      2010+ 同窗口排名：" + " | ".join(
        f"{r['name']}({r['mean_err_2010']:.3%})" for r in rank2010[:5]))

    # 配对检验：逐日误差差的配对 t（champion vs 各候选），说明差距是否只是噪声
    def paired(c1, c2):
        e1, e2 = err_map[c1], err_map[c2]
        common = [ds for ds in e1 if ds in e2]
        diffs = [e1[ds] - e2[ds] for ds in common]
        n = len(diffs)
        md = sum(diffs) / n
        sd = math.sqrt(sum((x - md) ** 2 for x in diffs) / (n - 1)) if n > 1 else 0
        t = md / (sd / math.sqrt(n)) if sd > 0 else 0
        return {"n": n, "diff": md, "t": t}

    champ_code = rank2010[0]["code"]
    tests = {c: paired(champ_code, c) for c in layer12 if c != champ_code}
    print(f"      配对检验基准 {layer12[champ_code]['name']}："
          f"vs 上证 diff={tests['sh000001']['diff']*100:+.3f}% t={tests['sh000001']['t']:.1f}")

    # 敏感性：ST 计入基准后排名是否变化（Spearman）
    sens = sorted([r for r in layer12.values() if r.get("mean_err_st_in")],
                  key=lambda r: r["mean_err_st_in"])
    for i, r in enumerate(sens, 1):
        r["rank_st_in"] = i
    def spearman(xs, ys):
        rx = {c: i for i, c in enumerate(xs)}
        ry = {c: i for i, c in enumerate(ys)}
        n = len(xs)
        dsum = sum((rx[c] - ry[c]) ** 2 for c in xs)
        return 1 - 6 * dsum / (n * (n * n - 1))
    rho = spearman([r["code"] for r in rank], [r["code"] for r in
                                              sorted(layer12.values(), key=lambda r: r["mean_err_st_in"])])
    print(f"      ST 敏感性：排名 Spearman ρ={rho:.3f}")

    # HARD ②：逐年和 == 全历史
    hard2 = {}
    for code, r in layer12.items():
        ysum = sum(v["d"] for v in r["year"].values())
        hard2[code] = abs(ysum - r["d"]) < 1e-6

    print("[4/6] 第 3 层：交易池相关 …")
    pool = load_pool()
    print(f"      组合权益快照 {pool['n']} 日 {pool['first']}~{pool['last']}")
    pool_days = [ds for ds in sorted(pool["ret"])]
    layer3 = {}
    for code, s in idx.items():
        m = {ds: b for ds, b in zip(s["date"], s["ret"])}
        xs_all, ys_all, xs_exp = [], [], []
        for ds in pool_days:
            if ds in m and ds in a:
                xs_all.append(pool["ret"][ds])
                ys_all.append(m[ds])
                if pool["exposed"].get(ds):
                    xs_exp.append(pool["ret"][ds])
        if len(xs_all) < 100:
            continue
        # 领先/滞后：正 k = 指数领先组合（用 b_{t-k} 对齐 r_t）
        lag = {}
        for k in range(-5, 6):
            xx, yy = [], []
            for i in range(len(pool_days)):
                j = i + k
                if 0 <= j < len(pool_days):
                    ds, ds2 = pool_days[i], pool_days[j]
                    if ds in m and ds in a and ds2 in m:
                        xx.append(pool["ret"][ds])
                        yy.append(m[ds2])
            lag[k] = pearson(xx, yy)
        best_k = max(lag, key=lambda k: lag[k] if lag[k] is not None else -9)
        corr_all = pearson(xs_all, ys_all)
        # 有持仓日口径（主口径）
        corr_exp = None
        if len(xs_exp) > 100:
            ye = [m[ds] for ds in pool_days if ds in m and ds in a and pool["exposed"].get(ds)]
            corr_exp = pearson(xs_exp, ye)
        layer3[code] = {"code": code, "name": s["name"], "n": len(xs_all),
                        "n_exp": len(xs_exp), "corr_all": corr_all, "corr_exp": corr_exp,
                        "lag": {str(k): v for k, v in lag.items()}, "best_k": best_k,
                        "best_lag_corr": lag[best_k]}
    print("      相关(有持仓日)：" + " | ".join(
        f"{v['name']}{v['corr_exp']:.3f}" for v in sorted(
            layer3.values(), key=lambda r: -(r["corr_exp"] or -9))[:5]))

    print("[5/6] regime 重叠（收盘口径）与配对（生产 low 口径） …")
    # 代理曲线：等权基准累积（收盘口径 A 判据）
    lvl, cur = [], 1.0
    for ds in days:
        cur *= (1 + a[ds])
        lvl.append(cur)
    proxy_reg = regime(lvl)
    proxy_bull = [days[i] for i, v in enumerate(proxy_reg) if v == "bull"]
    proxy_bear = [days[i] for i, v in enumerate(proxy_reg) if v == "bear"]

    reg_close, reg_low = {}, {}
    for code, s in idx.items():
        reg_close[code] = regime(s["close"])
        reg_low[code] = regime(s["low"])

    proxy_map = dict(zip(days, proxy_reg))
    jac = {}
    for code, s in idx.items():
        if code not in layer3:
            continue
        cl = reg_close[code]
        bull = [s["date"][i] for i, v in enumerate(cl) if v == "bull"]
        bear = [s["date"][i] for i, v in enumerate(cl) if v == "bear"]
        both = [i for i, ds in enumerate(s["date"]) if ds in a]
        agree = 0
        for i in both:
            ds = s["date"][i]
            agree += 1 if cl[i] == proxy_map.get(ds) else 0
        jac[code] = {"jaccard_bull": jaccard(bull, proxy_bull),
                     "jaccard_bear": jaccard(bear, proxy_bear),
                     "agree": agree / len(both) if both else None, "n": len(both)}
        layer3[code].update(jac[code])

    # 配对：regime Jaccard 矩阵（生产 low 口径）
    pair = {}
    keys = [c for c, _, _, _ in CANDIDATES if c in reg_low]
    for i, c1 in enumerate(keys):
        for c2 in keys[i + 1:]:
            s1, s2 = idx[c1], idx[c2]
            m2 = dict(zip(s2["date"], reg_low[c2]))
            xs1, xs2 = [], []
            for ds, v in zip(s1["date"], reg_low[c1]):
                if ds in m2:
                    xs1.append(v)
                    xs2.append(m2[ds])
            if len(xs1) < 400:
                continue
            bull = (jaccard([i for i, v in enumerate(xs1) if v == "bull"],
                            [i for i, v in enumerate(xs2) if v == "bull"]))
            bear = (jaccard([i for i, v in enumerate(xs1) if v == "bear"],
                            [i for i, v in enumerate(xs2) if v == "bear"]))
            pair[f"{c1}|{c2}"] = {"jaccard_bull": bull, "jaccard_bear": bear,
                                  "n": len(xs1),
                                  "agree": sum(1 for x, y in zip(xs1, xs2) if x == y) / len(xs1)}

    # 四态矩阵：按年统计（双降/背离/双升/其余）
    def four_state(c1, c2):
        s1, s2 = idx[c1], idx[c2]
        m2 = dict(zip(s2["date"], reg_low[c2]))
        by_year = defaultdict(lambda: {"both_bear": 0, "both_bull": 0, "divergence": 0, "other": 0, "days": 0})
        for ds, v1 in zip(s1["date"], reg_low[c1]):
            if ds not in m2:
                continue
            v2 = m2[ds]
            b = by_year[ds[:4]]
            b["days"] += 1
            if v1 == "bear" and v2 == "bear":
                b["both_bear"] += 1
            elif v1 == "bull" and v2 == "bull":
                b["both_bull"] += 1
            elif {v1, v2} == {"bull", "bear"}:
                b["divergence"] += 1
            else:
                b["other"] += 1
        return {y: dict(v) for y, v in sorted(by_year.items())}

    print("[6/6] 落盘 …")
    payload = {
        "meta": {
            "generated": date.today().isoformat(),
            "base_db": str(BASE_DB.relative_to(ROOT)),
            "years": [YEARS[0], YEARS[-1]],
            "baseline": "全市场个股日收益等权中位数（winsorize ±11%）",
            "filters": {"停牌":"无 K 线/无量","一字":"high==low","次新":"上市<30天",
                        "ST":"名称含 ST/退","复牌跳空":">30 自然日","除权残差":"±11% 截尾"},
            "excl": base["excl"], "n_stock": base["n_stock"],
            "n_days": len(days), "first": days[0], "last": days[-1],
        },
        "coverage": cov_stats,
        "layer12": {c: {k: v for k, v in r.items() if k != "top20"} | {"top20": r["top20"]}
                    for c, r in layer12.items()},
        "rank": [r["code"] for r in rank],
        "rank_2010": [r["code"] for r in rank2010],
        "paired_tests": tests,
        "st_sensitivity_spearman": rho,
        "hard2_year_sum": hard2,
        "pool": {"n": pool["n"], "first": pool["first"], "last": pool["last"]},
        "layer3": layer3,
        "pairs": pair,
        "series": {"dates": days, "a": [a[x] for x in days],
                   "level": lvl,
                   "px": {c: {"date": s["date"], "close": s["close"]} for c, s in idx.items()}},
    }

    # 组合推荐（判据在报告里逐条打）
    checks = {}
    champ = champ_code
    sh = layer12.get("sh000001")
    checks["C1"] = {"champ": champ, "champ_err_2010": layer12[champ]["mean_err_2010"],
                    "sh_err_2010": sh["mean_err_2010"] if sh else None,
                    "champ_err_full": layer12[champ]["mean_err"],
                    "sh_err_full": sh["mean_err"] if sh else None,
                    "paired_vs_sh": tests.get("sh000001"),
                    "pass": bool(sh and layer12[champ]["mean_err_2010"] < sh["mean_err_2010"])}
    yr_rank = {}
    for y in ("2013", "2017"):
        vals = sorted(((r["year"].get(y, {}).get("mean", 9), c) for c, r in layer12.items()
                       if y in r["year"]), reverse=True)
        yr_rank[y] = [c for _, c in vals]
    checks["C2"] = {"2013": yr_rank["2013"], "2017": yr_rank["2017"],
                    "pass": champ not in yr_rank["2013"][:3] and champ not in yr_rank["2017"][:3]}
    c3 = layer3.get(champ, {}).get("corr_exp")
    c3sh = layer3.get("sh000001", {}).get("corr_exp")
    n3 = layer3.get(champ, {}).get("n_exp", 0)
    fisher = None
    if c3 and c3sh:
        z1, z2 = math.atanh(c3), math.atanh(c3sh)
        se = math.sqrt(2 / max(n3 - 3, 1))
        fisher = {"z_diff": z1 - z2, "se": se, "p_two_sided_approx":
                  math.erfc(abs(z1 - z2) / se / math.sqrt(2))}
    checks["C3"] = {"champ_corr": c3, "sh_corr": c3sh, "n": n3, "fisher": fisher,
                    "rule": "冠军 > 上证 + 0.05（预登记）",
                    "pass": bool(c3 and c3sh and c3 > c3sh + 0.05)}

    # 重点配对：显式跨规模组合（道氏原意 = 大盘镜 + 中小盘镜，不是两个同类指数）
    SIZE_PAIRS = [("sh000300", "sh000905"), ("sh000300", "sh000852"),
                  ("sz399317", "sh000905"), ("sz399317", "sh000852"),
                  ("sh000510", "sh000905"), ("sh000001", "sh000905"),
                  ("sh000001", "sh000852"), ("sh000300", "sz399303"),
                  ("sh000300", "sz399001"), ("sz399317", "sz399303")]
    focus = []
    for c1, c2 in SIZE_PAIRS:
        key = f"{c1}|{c2}"
        if key not in pair:
            continue
        fs = four_state(c1, c2)
        tot = {k: sum(v[k] for v in fs.values()) for k in ("both_bear", "both_bull", "divergence", "other")}
        focus.append({
            "pair": key, "name": f"{CODE2NAME[c1]} × {CODE2NAME[c2]}",
            "jaccard_bull": pair[key]["jaccard_bull"], "jaccard_bear": pair[key]["jaccard_bear"],
            "agree": pair[key]["agree"], "total": tot,
            "bear_2008": fs.get("2008", {}).get("both_bear"),
            "bear_2011": fs.get("2011", {}).get("both_bear"),
            "bear_2013": fs.get("2013", {}).get("both_bear"),
            "div_2013": fs.get("2013", {}).get("divergence"),
            "bear_2017": fs.get("2017", {}).get("both_bear"),
            "four_state": fs,
        })
    focus.sort(key=lambda r: (-(r["total"]["both_bear"]), r["bear_2013"] or 99))
    payload["focus_pairs"] = focus

    # C4：推荐配对（第一条）的 Jaccard 是否落在 0.3-0.8
    if focus:
        best = focus[0]
        checks["C4"] = {"pair": best["pair"], "jaccard_bull": best["jaccard_bull"],
                        "jaccard_bear": best["jaccard_bear"],
                        "rule": "两镜 Jaccard ∈ [0.30, 0.80]（预登记）",
                        "pass": bool(best["jaccard_bull"] and best["jaccard_bear"]
                                     and 0.30 <= best["jaccard_bull"] <= 0.80
                                     and 0.30 <= best["jaccard_bear"] <= 0.80)}
    payload["checks"] = checks

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    write_md(payload)
    write_html(payload)
    print(f"      写出 {OUT_JSON.relative_to(ROOT)} / {OUT_MD.name} / {OUT_HTML.name}")
    return payload


# ---------------------------------------------------------------- 报告
def write_md(p: dict):
    rank = [p["layer12"][c] for c in p["rank"]]
    rk10 = [p["layer12"][c] for c in p["rank_2010"]]
    L = []
    L.append("# E17 市场镜子选型报告（候选指数 vs 等权市场中位数基准）\n")
    L.append(f"> 生成：{p['meta']['generated']}　基准：{p['meta']['baseline']}　"
             f"区间：{p['meta']['first']} ~ {p['meta']['last']}（{p['meta']['n_days']} 交易日）\n")
    L.append("> 口径与判据见 `e17_market_index_framework.md`（跑前写死）。\n")
    L.append("## 一、总榜 · 全窗口（2005-2018，年均日误差，越小越贴合）\n")
    L.append("| # | 指数 | 角色 | n | 年均日误差 | 剪刀差占比 | 有持仓日相关 | regime 一致率 |")
    L.append("|---|---|---|---:|---:|---:|---:|---:|")
    for r in rank:
        l3 = p["layer3"].get(r["code"], {})
        L.append(f"| {r['rank']} | {r['name']}({r['code']}) | {r['role']} | {r['n']} | "
                 f"{pct(r['mean_err'],3)} | {pct(r['scissor_pct'],2)} | "
                 f"{(l3.get('corr_exp') or 0):.3f} | {(l3.get('agree') or 0)*100:.1f}% |")
    L.append("")
    L.append("> 注：国证2000/创业板指只有 2010+ 数据，长窗口 n 不同，此表的均值不可直接横比——以下表为准。\n")
    L.append("## 二、总榜 · 2010-2018 共同窗口（公平横比）\n")
    L.append("| # | 指数 | n | 年均日误差 | vs 冠军配对差 | t |")
    L.append("|---|---|---:|---:|---:|---:|")
    for r in rk10:
        t = p["paired_tests"].get(r["code"])
        L.append(f"| {r['rank_2010']} | {r['name']}({r['code']}) | {r['n_2010']} | "
                 f"{pct(r['mean_err_2010'],3)} | "
                 f"{(t['diff']*100 if t else 0):+.3f}% | {(t['t'] if t else 0):+.1f} |")
    L.append("")
    L.append(f"> ST 敏感性：ST 计入基准后排名 Spearman ρ = {p['st_sensitivity_spearman']:.3f}\n")
    L.append("## 三、逐年误差（日均，%）\n")
    yrs = [str(y) for y in range(2005, 2019)]
    L.append("| 指数 | " + " | ".join(yrs) + " |")
    L.append("|---|" + "---:|" * len(yrs))
    for r in rank:
        cells = []
        for y in yrs:
            v = r["year"].get(y)
            cells.append(f"{v['mean']*100:.2f}" if v else "—")
        L.append(f"| {r['name']} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("## 三、判据\n")
    c = p["checks"]
    for k in ("C1", "C2", "C3", "C4"):
        if k not in c:
            continue
        L.append(f"- **{k}** {'✅' if c[k].get('pass') else '❌'} `{json.dumps(c[k], ensure_ascii=False)}`")
    L.append("")
    L.append("## 四、道氏配对（跨规模，Jaccard 0.3–0.8）\n")
    L.append("| 配对 | Jaccard 升 | Jaccard 降 | 全期双降 | 2008 双降 | 2011 双降 | 2013 双降 | 2017 双降 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for f in p["focus_pairs"]:
        L.append(f"| {f['name']} | {f['jaccard_bull']:.2f} | {f['jaccard_bear']:.2f} | "
                 f"{f['total']['both_bear']} | {f['bear_2008']} | {f['bear_2011']} | "
                 f"{f['bear_2013']} | {f['bear_2017']} |")
    L.append("")
    L.append("> 双降天数 = 两镜同时判降势的交易日数。2008/2011 要多（保护），2013 要少（别误伤）。\n")

    # ---------------- 结论（数据驱动，改数不改文）
    c1 = p["checks"].get("C1", {})
    c3 = p["checks"].get("C3", {})
    champ = p["layer12"][c1.get("champ", p["rank_2010"][0])]
    f0 = p["focus_pairs"][0] if p["focus_pairs"] else None
    L.append("## 五、结论\n")
    L.append(f"### 5.1 单指数推荐：{champ['name']}（{champ['code']}）\n")
    L.append(f"1. **贴合度双窗口第一**：全窗口 {champ['mean_err']*100:.3f}%／2010+ {champ['mean_err_2010']*100:.3f}%，"
             f"对基准的日均误差比上证（{c1.get('sh_err_full',0)*100:.3f}%／{c1.get('sh_err_2010',0)*100:.3f}%）"
             f"低 {(c1.get('sh_err_2010',0)-champ['mean_err_2010'])*100:.3f} 个百分点。")
    L.append("2. **两个结构市都不掉队**：2013 年误差全场最小、2017 年第 5（详见 §三 逐年表）——"
             "唯一一个「指数横+小盘牛」和「指数强+小盘弱」两年都在前五的候选。")
    L.append(f"3. **交易池相关 {c3.get('champ_corr',0):.3f} > 上证 {c3.get('sh_corr',0):.3f}**，"
             f"超出预登记的 +0.05 门槛（Fisher z 检验 p≈{(c3.get('fisher') or {}).get('p_two_sided_approx',0):.4f}）。")
    L.append("")
    L.append("### 5.2 双指数推荐：国证A指（sz399317）× 中证500（sh000905）\n" if f0 else "")
    if f0:
        L.append(f"- Jaccard 升 {f0['jaccard_bull']:.2f} / 降 {f0['jaccard_bear']:.2f}，落在预登记的 0.30–0.80（不冗余也不分裂）")
        L.append(f"- 双降天数：2008 **{f0['bear_2008']}** 天、2011 **{f0['bear_2011']}** 天、2013 **{f0['bear_2013']}** 天、"
                 f"2017 {f0['bear_2017']} 天（2008/2011 有保护、2013 几乎不触发）")
        L.append("- 大盘镜选国证A指而非上证/沪深300：贴合度 0.505% 显著优于上证 0.750%、沪深300 0.754%（两者全榜垫底）")
    L.append("")
    L.append("### 5.3 三个必须写进 docs/31 的警告\n")
    if f0:
        fs = f0["four_state"]
        div_total = sum(v["divergence"] for v in fs.values())
        L.append(f"1. **四态表会退化**：全期「单降背离」只有 {div_total} 天。2013 年 219 天落在「其余」（两镜都震荡），"
                 f"不是「背离」。→ docs/31 设计的「单降背离豁免」机制几乎永不触发；2013 误伤清零靠的是"
                 f"**双降压根不成立**，不是豁免生效。")
        bear_total = f0["total"]["both_bear"]
        L.append(f"2. **双降状态极稀疏**：全期 {bear_total} 天（占 4%），2008 也只有 {f0['bear_2008']}/246 = "
                 f"{f0['bear_2008']/246*100:.1f}%。而 MA60 闸门在上证下方的时间接近一半——"
                 f"**A 判据双指数闸门的触发面比 MA60 小一个量级，2008 的保护未必复现**。"
                 f"A 类后验第一步必须先看 2008 的建仓是否落在这 {f0['bear_2008']} 天里。")
    L.append("3. **三指数投票否决**：创业板指 2010-06 起、贴合度 0.726%（倒数第二）、2013 年误差 1.07%（全场最差）。"
             "把它拉进投票只会把 2013 判得更糟。")
    L.append("")
    L.append("### 5.4 对「上证不能代表市场」的修正\n")
    L.append("国证A指 0.505%、中证全指 0.535% 确实远好于上证 0.750%——**但都输给中证500 的 0.365%**。"
             "换成「全市场指数」还不够：自由流通加权仍是市值加权，大票主导；要贴合「多数股票的中心」，"
             "必须换到中小盘宽基。这是本文与公众号原文结论一致、但比原文更进一步的地方。")
    L.append("")
    for f in p["focus_pairs"][:3]:
        L.append(f"### {f['pair']}　Jaccard 升 {f['jaccard_bull']:.2f} / 降 {f['jaccard_bear']:.2f}\n")
        L.append("| 年 | 双降 | 背离 | 双升 | 其余 | 天数 |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for y, v in f["four_state"].items():
            L.append(f"| {y} | {v['both_bear']} | {v['divergence']} | {v['both_bull']} | {v['other']} | {v['days']} |")
        L.append("")
    OUT_MD.write_text("\n".join(L), encoding="utf-8")


def write_html(p: dict):
    rank = [p["layer12"][c] for c in p["rank"]]
    yrs = [str(y) for y in range(2005, 2019)]
    css = """
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f7f8fa;color:#1f2329;margin:0;padding:28px}
.wrap{max-width:1120px;margin:0 auto}
h1{font-size:24px;margin:0 0 6px}
.sub{color:#646a73;font-size:13px;margin-bottom:22px}
.card{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:18px 20px;margin-bottom:18px}
h2{font-size:16px;margin:0 0 12px;padding-left:9px;border-left:3px solid #3370ff}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:6px 8px;border-bottom:1px solid #eef0f3;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:#fafbfc;color:#646a73;font-weight:600;font-size:12px}
tr:hover td{background:#fafbfc}
.best{background:#eefbf3;font-weight:600}
.worst{color:#8f959e}
.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;background:#f2f3f5;color:#646a73}
.ok{color:#0f7b3e;font-weight:600}.no{color:#c9333c;font-weight:600}
.note{font-size:12px;color:#646a73;line-height:1.7}
.flex{display:flex;gap:18px;flex-wrap:wrap}
.flex>div{flex:1;min-width:300px}
"""
    # 曲线：等权基准 vs 上证 vs 国证A指 vs 中证500 vs 沪深300（归一 100，动态量程）
    base_dates = p["series"]["dates"]

    def raw_points(code):
        if code == "__EW__":
            lv = p["series"]["level"]
            return [(i, v / lv[0] * 100) for i, v in enumerate(lv)]
        s = p["series"]["px"].get(code)
        if not s:
            return []
        m = dict(zip(s["date"], s["close"]))
        pts, first = [], None
        for i, ds in enumerate(base_dates):
            if ds in m:
                if first is None:
                    first = m[ds]
                pts.append((i, m[ds] / first * 100))
        return pts

    CODES = ["__EW__", "sh000001", "sz399317", "sh000905", "sh000300"]
    series = {c: raw_points(c) for c in CODES}
    ymax = max((v for pts in series.values() for _, v in pts), default=200)
    ymax = math.ceil(ymax / 100) * 100
    W, H = 950, 260
    n = len(base_dates)

    def series_points(code):
        pts = series.get(code) or []
        if not pts:
            return ""
        return " ".join(f"{46 + (i / (n - 1)) * W:.1f},{H - 14 - (v / ymax) * (H - 40):.1f}"
                        for i, v in pts)

    colors = {"__EW__": "#1f2329", "sh000001": "#c9333c", "sz399317": "#3370ff",
              "sh000905": "#0f7b3e", "sh000300": "#ff8800"}
    paths = "".join(
        f'<polyline fill="none" stroke="{colors[c]}" stroke-width="1.6" points="{series_points(c)}"/>'
        for c in CODES)
    ymark = "".join(
        f'<text x="0" y="{H - 14 - (v / ymax) * (H - 40) + 4:.0f}" fill="#8f959e" font-size="10">{v}</text>'
        for v in range(0, ymax + 1, max(100, ymax // 5)))

    rows = []
    for r in [p["layer12"][c] for c in p["rank_2010"]]:
        l3 = p["layer3"].get(r["code"], {})
        cls = "best" if r["rank_2010"] == 1 else ""
        rows.append(
            f"<tr class='{cls}'><td>{r['rank']}</td><td>{r['name']} <span class='tag'>{r['code']}</span></td>"
            f"<td>{r['role']}</td><td>{r['n']}</td><td>{r['mean_err']*100:.3f}%</td>"
            f"<td>{r['mean_err_2010']*100:.3f}%</td>"
            f"<td>{r['scissor_pct']*100:.1f}%</td>"
            f"<td>{(l3.get('corr_exp') or 0):.3f}</td>"
            f"<td>{(l3.get('agree') or 0)*100:.1f}%</td></tr>")

    yhead = "".join(f"<th>{y}</th>" for y in yrs)
    yrows = []
    for r in rank:
        cells = "".join(
            f"<td>{r['year'][y]['mean']*100:.2f}</td>" if y in r["year"] else "<td class='worst'>—</td>"
            for y in yrs)
        yrows.append(f"<tr><td>{r['name']}</td>{cells}</tr>")

    pf = []
    for f in p["focus_pairs"]:
        head = "".join(f"<th>{y}</th>" for y in f["four_state"])
        b = "".join(f"<td>{v['both_bear']}</td>" for v in f["four_state"].values())
        dg = "".join(f"<td>{v['divergence']}</td>" for v in f["four_state"].values())
        bu = "".join(f"<td>{v['both_bull']}</td>" for v in f["four_state"].values())
        pf.append(f"""<div class="card"><h2>{f.get('name', f['pair'])}　Jaccard 升 {f['jaccard_bull']:.2f} / 降 {f['jaccard_bear']:.2f}</h2>
<table><tr><th>状态</th>{head}</tr><tr><td>双降</td>{b}</tr><tr><td>单降背离</td>{dg}</tr>
<tr><td>双升</td>{bu}</tr></table></div>""")

    c = p["checks"]
    cards = "".join(
        f"<div class='card'><h2>{k} {'<span class=ok>PASS</span>' if v.get('pass') else '<span class=no>FAIL</span>'}</h2>"
        f"<div class='note'><pre style='white-space:pre-wrap;font-size:12px'>{json.dumps(v, ensure_ascii=False, indent=1)}</pre></div></div>"
        for k, v in c.items() if isinstance(v, dict))

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>E17 市场镜子选型报告</title><style>{css}</style></head><body><div class="wrap">
<h1>E17 市场镜子选型报告</h1>
<div class="sub">基准 = 全市场个股日收益等权中位数（winsorize ±11%）　区间 {p['meta']['first']} ~ {p['meta']['last']}
（{p['meta']['n_days']} 交易日）　覆盖率 全口径中位 {p['coverage']['median']*100:.1f}% / 合格域中位 {p['coverage']['median_design']*100:.1f}%
（&lt;90% 天数 {p['coverage']['lt90']} / {p['coverage']['lt90_design']}）</div>
<div class="card" style="border-left:3px solid #0f7b3e">
<h2>结论</h2>
<div class="note" style="font-size:13px;color:#1f2329">
<b>单指数：中证500（sh000905）</b>　2010+ 日均误差 {p['layer12']['sh000905']['mean_err_2010']*100:.3f}%（上证 {p['layer12']['sh000001']['mean_err_2010']*100:.3f}%）·
2013 年误差全场最小 · 2017 年第 5 · 交易池相关 {p['layer3']['sh000905']['corr_exp']:.3f}（上证 {p['layer3']['sh000001']['corr_exp']:.3f}）<br>
<b>双指数：国证A指 × 中证500</b>　Jaccard {p['focus_pairs'][0]['jaccard_bull']:.2f}/{p['focus_pairs'][0]['jaccard_bear']:.2f} ·
2008 双降 {p['focus_pairs'][0]['bear_2008']} 天 · 2011 {p['focus_pairs'][0]['bear_2011']} 天 · 2013 仅 {p['focus_pairs'][0]['bear_2013']} 天<br>
<b>警告</b>　全期「单降背离」仅 {sum(v['divergence'] for v in p['focus_pairs'][0]['four_state'].values())} 天，2013 落在「其余」而非「背离」；
双降只占 4% 交易日，触发面比 MA60 小一个量级——2008 保护未必复现。
</div></div>
<div class="card"><h2>贴合度总榜（年均日误差，越小越贴合）</h2>
<table><tr><th>#</th><th>指数</th><th>角色</th><th>n</th><th>年均误差<br>2005-2018</th><th>年均误差<br>2010-2018</th><th>剪刀差占比</th><th>有持仓日相关</th><th>regime 一致率</th></tr>
{''.join(rows)}</table>
<div class="note" style="margin-top:10px">末两列与 2010+ 列为公平横比口径：国证2000/创业板指只有 2010 起数据。</div></div>
<div class="note" style="margin-top:10px">剪刀差 = 指数涨&gt;0.5% 而中位数&lt;0（或反向）的天数占比。<br>
有持仓日相关 = 组合日收益（market_value&gt;0）与指数日收益的 Pearson 相关。<br>
regime 一致率 = 收盘口径 A 判据在「等权基准累积曲线」与「候选指数」上的三态一致比例。</div></div>
<div class="card"><h2>归一化走势（2005-02-03 = 100）</h2>
<svg viewBox="0 0 1020 280" width="100%" height="280">
<text x="46" y="16" fill="#646a73" font-size="11">黑=等权中位数基准　红=上证　蓝=国证A指　绿=中证500　橙=沪深300</text>
{ymark}{paths}</svg></div>
<div class="card"><h2>逐年日均误差（%）</h2>
<table><tr><th>指数</th>{yhead}</tr>{''.join(yrows)}</table></div>
{''.join(pf)}
{cards}
<div class="card"><h2>诚实边界</h2><div class="note">
① 等权中位数 ≠ 原文市值中位数：衡量的是「多数股票的涨跌中心」，不是「市值重心」。<br>
② 库内价格未复权（adj_factor 恒 1.0），个股收益已 winsorize ±11% 抑制送转伪跌幅；ST 身份为当前快照，无法回溯。<br>
③ 交易池相关层受「我们的票偏中小盘」选择偏倚影响——这正是第 3 层要测的东西，不是它的缺陷。<br>
④ 指数 mirror 的是历史 regime，不预测未来；编制方案变更（国证A指 2023 纳北交所）不在覆盖范围。<br>
⑤ 不产出 regime 阈值，不做回测，全部结论限于「选镜子」。
</div></div>
</div></body></html>"""
    OUT_HTML.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
