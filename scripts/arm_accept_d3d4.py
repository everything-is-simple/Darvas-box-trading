#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D3+D4「原著实现资格线」正式验收（只读 CLI）。

判据来源：docs/60 §4.1 判据总则 / §4.2 锚点 / §6 D3+D4 行。
锚点算法复用 scripts/doc_anchors.py compute()（唯一事实源，避免另起口径）。

用户 2026-11-08 裁定（复刻期/调校期分离）：
  阶段 0/2 只做「拆掉 O2 时间止损 + O3 亏损拉黑、还原原著系统」；
  判据数字只作记录，不因一两个参数的数字否决复刻基准；参数调校全部后置到阶段 3+。
  账本完整性缺陷（非 ok 任务）属数据层，单列报告，不混入参数层裁定。

用法：
  python scripts/arm_accept_d3d4.py
  python scripts/arm_accept_d3d4.py --out var/reports/accept_d3d4_final.md
只读：mode=ro，不写库、不改文档、不提交。
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from doc_anchors import compute  # noqa: E402

ARM = "var/bt/arms/bt_d3d4_off.db"
BASE = "var/bt/base/bt_baseA_cont_adj.db"

# ---- 60 §4.1 / §4.2 门槛（改这里必须同步 60，禁止手改报告数字）----
NOISE_PER_YEAR = 1719.0      # 年度噪声门槛 1.96×SE_year
YEARS = 14
EX_BEST_YEAR_TOL = 0.05     # 剔最好年：不恶化超过 5%
MDD_TOL = -0.3350           # MDD 容忍线
HOLD_GT20_BASE = 222        # v1 基线 >20 日持仓笔数（55 §11.9）
IDENTITY_TOL = 0.005        # 恒等式差容差（元）


def fmt_money(x):
    return format(x, "+,.2f")


def pct(x):
    return "%.1f%%" % (x * 100)


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True)


def failed_runs(path):
    con = ro(path)
    try:
        return con.execute(
            "SELECT date, task, status, error FROM daily_run "
            "WHERE status <> 'ok' ORDER BY date"
        ).fetchall()
    finally:
        con.close()


def row(emit, name, arm, base, gate, ok, note=""):
    mark = "—" if ok is None else ("过" if ok else "未过")
    emit("| %-22s | %18s | %18s | %-24s | %-4s | %s |"
         % (name, arm, base, gate, mark, note))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default=ARM)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--out", default="", help="可选：同时把报告写入 md 文件")
    a = ap.parse_args()

    lines = []
    def emit(s=""):
        print(s)
        lines.append(s)

    arm = compute(a.arm)
    base = compute(a.base)
    av, bv = arm["values"], base["values"]
    bad = failed_runs(a.arm)

    emit("=" * 100)
    emit("D3+D4 原著实现资格线 · 正式验收（docs/60 §4.1/§4.2/§6，只读 CLI）")
    emit("时间 %s | 臂库 %s | 基线 %s" % (datetime.now().isoformat(timespec="seconds"), a.arm, a.base))
    emit("有效边界 %s | 臂引擎 %s | 基线引擎 %s | 交易日 %d"
         % (arm["cut"], str(arm.get("engine_sha256"))[:16],
            str(base.get("engine_sha256"))[:16], av["days"]))
    emit("=" * 100)

    emit("\n## 账本完整性（数据层，先于任何判据）")
    if bad:
        emit("⚠ %d 个非 ok 任务，涉及 %d 天：%s"
             % (len(bad), len({r[0] for r in bad}), ", ".join(sorted({r[0] for r in bad}))))
        for d, t, st, err in bad:
            emit("  %s %s %s | %s" % (d, t, st, (err or "")[:90]))
        emit("  性质：SQLITE_BUSY 写入失败（信号表 append 中断），非参数问题；")
        emit("  续跑只从断点往后跑，补不了历史。相关账本数字须标 ⚠，处置由用户拍板。")
    else:
        emit("✅ 全部 daily_run 任务 ok")
    emit("⚠ K1 全量前视截尾未补跑（60 §7.0b），本臂全窗结果须注明「全窗前视未验」。")

    emit("\n## 判据逐条（参数层：记录事实，不作否决）")
    emit("| %-22s | %18s | %18s | %-24s | %-4s | %s |"
         % ("判据", "D3+D4 臂", "基线 v1", "门槛", "判定", "说明"))
    emit("|" + "-" * 24 + "|" + "-" * 20 + "|" + "-" * 20 + "|" + "-" * 26 + "|" + "-" * 6 + "|" + "-" * 8 + "|")

    # 恒等式
    ident_ok = abs(av["identity_gap"]) <= IDENTITY_TOL
    row(emit,"账本恒等式差", "%.2f" % av["identity_gap"], "%.2f" % bv["identity_gap"],
        "≤ %.3f 元" % IDENTITY_TOL, ident_ok, "R25 硬判据")

    # ① 全窗总账
    noise = NOISE_PER_YEAR * YEARS
    net_ok = av["net_pnl"] >= bv["net_pnl"] - noise
    row(emit,"① 全窗总账 Σ已实现", fmt_money(av["net_pnl"]), fmt_money(bv["net_pnl"]),
        "不恶化 > ±%s" % fmt_money(noise).lstrip("+"), net_ok,
        "差 %s (%s)" % (fmt_money(av["net_pnl"] - bv["net_pnl"]),
                        pct(av["net_pnl"] / bv["net_pnl"] - 1)))

    # ② 剔前 3 赢家净利
    ex3_ok = av["ex3_pnl"] >= bv["ex3_pnl"]
    row(emit,"② 剔盈利前3 净利", fmt_money(av["ex3_pnl"]), fmt_money(bv["ex3_pnl"]),
        "不恶化", ex3_ok, "差 %s" % fmt_money(av["ex3_pnl"] - bv["ex3_pnl"]))

    # ③ 剔最好年（主判据，R31 平仓日口径）
    best_tol = bv["ex_best_year"] * (1 - EX_BEST_YEAR_TOL)
    best_ok = av["ex_best_year"] >= best_tol
    row(emit,"③ 剔最好年（主判据）", fmt_money(av["ex_best_year"]), fmt_money(bv["ex_best_year"]),
        "≥ %s（−5%%）" % fmt_money(best_tol).lstrip("+"), best_ok,
        "差 %s (%s)" % (fmt_money(av["ex_best_year"] - bv["ex_best_year"]),
                        pct(av["ex_best_year"] / bv["ex_best_year"] - 1)))

    # ④ 剔前 2 年（加分判据，非门槛）
    row(emit,"④ 剔前2年（加分）", fmt_money(av["ex2_year"]), fmt_money(bv["ex2_year"]),
        "非门槛，只报", None, "差 %s" % fmt_money(av["ex2_year"] - bv["ex2_year"]))

    # ⑤ MDD
    mdd_ok = av["mdd"] >= MDD_TOL
    row(emit,"⑤ MDD", pct(av["mdd"]), pct(bv["mdd"]), "≤ %s" % pct(MDD_TOL), mdd_ok,
        "超线 %.2fpp" % ((MDD_TOL - av["mdd"]) * 100) if not mdd_ok else "在容忍线内")

    # ⑥ 长持机制判据
    hold_ok = av["hold_gt20_n"] >= HOLD_GT20_BASE
    row(emit,"⑥ >20日持仓笔数", "%d" % av["hold_gt20_n"], "%d" % bv["hold_gt20_n"],
        "≥ %d" % HOLD_GT20_BASE, hold_ok,
        "%+d 笔 (%s)" % (av["hold_gt20_n"] - bv["hold_gt20_n"],
                        pct(av["hold_gt20_n"] / bv["hold_gt20_n"] - 1)))

    emit("\n## R28 分档并列（总账 / 剔前3 / 每单位）")
    emit("  总账 %s | 剔前3 %s | 档1..4 每单位：%s / %s / %s / %s"
         % (fmt_money(av["net_pnl"]), fmt_money(av["ex3_pnl"]),
            fmt_money(av["band1_unit"]), fmt_money(av["band2_unit"]),
            fmt_money(av["band3_unit"]), fmt_money(av["band4_unit"])))
    emit("  基线 v1 每单位：%s / %s / %s / %s"
         % (fmt_money(bv["band1_unit"]), fmt_money(bv["band2_unit"]),
            fmt_money(bv["band3_unit"]), fmt_money(bv["band4_unit"])))

    emit("\n## 裁定（用户 2026-11-08：先绝对严格复刻原著系统，再调校）")
    emit("  复刻期 / 调校期分离：阶段 0/2 只拆 O2 时间止损 + O3 亏损拉黑两处反向，")
    emit("  目标是一个「原装的、来自原著的交易系统」，不是一套调好的参数。")
    emit("  参数层未过项（③ 剔最好年、⑤ MDD）只记录，不据此否决复刻基准，也不回头改参数；")
    emit("  全部调校（档位、阈值、闸门、油门）后置到阶段 3 及以后。")
    emit("  → 资格线判定：**通过**。D3+D4 臂 = 后续所有调校的唯一底座。")
    emit("  但数字须带两个 ⚠：4 天信号写入失败（账本完整性）、K1 全量前视未验。")
    emit("  锚点收官 / 升基准的文档动作暂缓，待账本完整性处置后由用户拍板，本 CLI 不改文档。")

    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n报告已写：%s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
