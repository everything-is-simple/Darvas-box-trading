#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D1 金字塔加仓（O1）结构／时序只读验收 CLI。

定位：`67 §4.1` S3 阶段 3 装油门的第一个子步（O1 金字塔加仓），判据与验收物见
`65 §1.2.2` O1 行、`67 §8.7` 起跑前预注册验收 CLI 清单（D1 行）。适用对象默认
为冻结的段 A 对照底座 `var/bt/arms/bt_clean_default_0033.db`，可用 `--arm`
指到任一 scratch 臂库。

判据（结构／时序，不含数值尺度）：
  C1 `trade_fill` 出现 leg≥2         —— 已成交买单关联 `plan_order.leg_no >= 2`
  C2 加仓价高于前腿                   —— 同一持仓内后腿 price 严格高于前腿
  C3 各腿独立 stop                    —— 每腿 `position_leg.stop_after` 与
                                        `plan_order.reason_json.stop` 各有独立记录
  C4 `capAmt` 取小机制生效且 ≤ 权益×20% —— 源码 `equityHardCapRatio` 取小结构 +
                                        直查 `reason_json.$.cap_amt{cap_for, hard_cap, applied}`
                                        一致性（applied == min(cap_for, hard_cap)、
                                        hard_cap == 0.20×equity、applied ≤ hard_cap）
  C5 恒等式 0.00                      —— `doc_anchors.compute()["values"]["identity_gap"]`
                                        ≤ 0.005（`doc_anchors.py:205` 口径）

落点说明（2026-11-10 用户「按建议办」拍板后更新）：
  ① `trade_fill` 无 leg / stop 列：腿号走 `trade_fill.plan_order_id → plan_order.leg_no`，
     逐腿价与 stop 走 `position_leg`，不用最终态 `box` 表反查。
  ② `capAmt` 已原生落 `plan_order.reason_json.$.cap_amt`（planner 装油门时新增，与
     `equity_budget` 同口径、仅 `pos.equity_gate=on` 时写入）——C4 直查该快照，**不采代理**。
     旁证（非判据）：每笔买单名义 `price×qty ≤ 当日 equity_snapshot×0.20`；并记录
     `hard_cap < cap_for` 的真实取小触发样本数。20% = `65 §1.2.4` 制度天花板
     （源码常量 `equityHardCapRatio`，非 param）；当前默认配置 `pos.single_limit=0.08`
     先于 20% 约束 → applied 恒等于 cap_for 属「只立机制」预期，放宽归段 C。

**时点归因铁律**：只用逐日快照 `plan_order.reason_json` / `signal.payload_json` 与
账本表 `trade_fill` / `position_leg` / `equity_snapshot`，**不读最终态 `box` 表反查**。

本 CLI 只逐条记录事实，不作完成线／臂验收裁定，不冻结账本、不回填看板、不改文档。
默认退出码 0 表示 CLI 跑通；`--strict` 时任一结构判据未过返回 2。
对 0033 冻结对照底座跑 = 结构基线冒烟（装油门未落地，C1/C2/C3 必未过），**非臂验收**。

用法：
  python scripts/arm_accept_d1_pyramid.py
  python scripts/arm_accept_d1_pyramid.py --arm var/bt/arms/bt_clean_default_0033.db
  python scripts/arm_accept_d1_pyramid.py --out var/bt/arms/accept_0033_d1_pyramid.md
  python scripts/arm_accept_d1_pyramid.py --strict
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from doc_anchors import compute  # noqa: E402

ARM = "var/bt/arms/bt_clean_default_0033.db"
PLANNER_GO = "internal/planner/planner.go"
IDENTITY_TOL = 0.005
CEILING = 0.20
EPS = 1e-6


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).resolve().as_posix(),
                           uri=True, timeout=30)


def main():
    ap = argparse.ArgumentParser(description="D1 金字塔加仓（O1）只读验收 CLI")
    ap.add_argument("--arm", default=ARM, help="臂库路径（默认冻结的 0033 对照底座）")
    ap.add_argument("--out", default="", help="可选：同时把报告写入 md/txt 文件")
    ap.add_argument("--strict", action="store_true",
                    help="任一结构判据未过时返回退出码 2（默认只报告、总返回 0）")
    a = ap.parse_args()

    lines = []

    def emit(s=""):
        print(s)
        lines.append(s)

    checks = []

    def check(name, ok, detail):
        checks.append((name, bool(ok), detail))
        emit("  %s %-34s %s" % ("✅" if ok else "❌", name, detail))

    c = ro(a.arm)
    sha = c.execute("select engine_sha256 from baseline limit 1").fetchone()
    sha = sha[0] if sha else "(缺)"
    non_ok = c.execute("select count(*) from daily_run where status <> 'ok'").fetchone()[0]

    emit("=" * 100)
    emit("D1 金字塔加仓（O1）· 只读验收（`67 §4.1` S3 / `65 §1.2.2` O1 / `67 §8.7` D1 行）")
    emit("时间 %s | 臂库 %s | 引擎 sha %s" %
         (datetime.now().isoformat(timespec="seconds"), a.arm, sha[:16]))
    emit("daily_run 非 ok = %d | 只读 mode=ro | 不读最终态 box 表" % non_ok)
    emit("=" * 100)

    # ---------- 逐日快照 + 账本事实 ----------
    fills = c.execute("""
        select f.id, f.code, f.date, f.price, f.qty, p.leg_no, p.reason_json
        from trade_fill f
        join plan_order p on p.id = f.plan_order_id
        where p.side = 'buy'
        order by f.code, f.date, p.leg_no, f.id
    """).fetchall()
    leg_rows = c.execute("""
        select position_id, leg_no, date, price, qty, stop_after
        from position_leg order by position_id, leg_no
    """).fetchall()
    n_leg2 = sum(1 for r in fills if (r[5] or 1) >= 2)

    emit("\n## 账本规模")
    emit("  已成交买单 %d 笔（其中 leg≥2 = %d）｜position_leg %d 行" %
         (len(fills), n_leg2, len(leg_rows)))

    # ---------- C1 ----------
    emit("\n## C1 `trade_fill` 出现 leg≥2")
    check("C1 多腿成交存在", n_leg2 > 0,
          "leg≥2 的已成交买单 %d 笔（腿号来源 `trade_fill.plan_order_id → plan_order.leg_no`）"
          % n_leg2)

    # ---------- C2 ----------
    groups = {}
    for pid, leg_no, _d, price, _qty, stop_after in leg_rows:
        groups.setdefault(pid, []).append((leg_no, price, stop_after))
    pairs = bad_price = 0
    price_samples = []
    for pid, seq in groups.items():
        seq.sort(key=lambda x: x[0])
        for i in range(1, len(seq)):
            pairs += 1
            if seq[i][1] <= seq[i - 1][1] + EPS:
                bad_price += 1
                if len(price_samples) < 5:
                    price_samples.append((pid, seq[i - 1][0], seq[i - 1][1],
                                          seq[i][0], seq[i][1]))
    emit("\n## C2 加仓价高于前腿")
    check("C2 后腿价严格抬高", pairs > 0 and bad_price == 0,
          "同一持仓内可比腿对 %d 组；后腿价 ≤ 前腿价 %d 组" % (pairs, bad_price))
    for pid, l0, p0, l1, p1 in price_samples:
        emit("     违规样例 position=%s leg%s=%.4f → leg%s=%.4f" % (pid, l0, p0, l1, p1))

    # ---------- C3 ----------
    emit("\n## C3 各腿独立 stop")
    stop_missing = sum(1 for r in leg_rows if r[5] is None or r[5] <= 0)
    rj_missing = rj_bad = 0
    for _fid, _code, _date, _price, _qty, leg_no, rj in fills:
        if (leg_no or 1) < 2:
            continue
        try:
            d = json.loads(rj or "{}")
        except Exception:
            rj_missing += 1
            continue
        st = d.get("stop")
        if st is None or st <= 0:
            rj_missing += 1
        if d.get("leg") != leg_no:
            rj_bad += 1
    check("C3 逐腿止损记录齐全", n_leg2 > 0 and stop_missing == 0 and rj_missing == 0
          and rj_bad == 0,
          "`position_leg.stop_after` 缺失 %d／%d；leg≥2 买单 `reason_json.stop` 缺失 %d；"
          "`reason_json.leg` 与 `plan_order.leg_no` 不一致 %d"
          % (stop_missing, len(leg_rows), rj_missing, rj_bad))

    # ---------- C4 ----------
    emit("\n## C4 `capAmt` 取小机制 + 制度天花板 20%")
    planner = (ROOT / PLANNER_GO).read_text(encoding="utf-8", errors="replace")
    cap_src = ("equityHardCapRatio" in planner) and ("capAmt/trigger" in planner) \
        and ('reason["cap_amt"]' in planner)
    check("C4a 源码取小结构", cap_src,
          "`%s`：`equityHardCapRatio` 常量 + `capAmt/trigger` 缩量 + `reason[\"cap_amt\"]`"
          " 落点均存在 = %s" % (PLANNER_GO, cap_src))
    # C4b 直查 reason_json.$.cap_amt（原生落点，2026-11-10 拍板方案）。
    # 口径：与 reason_json.$.equity_budget 同规则——仅 pos.equity_gate=on 时落点；
    # 0033 干净默认臂 gate=off → 0 落点属**预期**，机制存在性由 C4a 源码结构＋C4c
    # 名义旁证覆盖；gate=on 却 0 落点＝机制未生效，判未过。
    gate = c.execute("select value from param where key = 'pos.equity_gate'").fetchone()
    gate_on = gate is not None and str(gate[0]).strip().lower() == "on"
    cap_rows = c.execute("""
        select p.id,
               json_extract(p.reason_json, '$.cap_amt.cap_for'),
               json_extract(p.reason_json, '$.cap_amt.hard_cap'),
               json_extract(p.reason_json, '$.cap_amt.applied'),
               json_extract(p.reason_json, '$.equity_budget.equity')
        from plan_order p
        where p.side = 'buy' and json_extract(p.reason_json, '$.cap_amt') is not null
    """).fetchall()
    n_bad_min = n_bad_ratio = n_bad_le = 0
    n_min_hit = 0
    for _oid, cap_for, hard_cap, applied, equity in cap_rows:
        if None in (cap_for, hard_cap, applied, equity):
            n_bad_min += 1
            continue
        if abs(applied - min(cap_for, hard_cap)) > max(1.0, abs(applied)) * 1e-9:
            n_bad_min += 1
        if abs(hard_cap - CEILING * equity) > max(1.0, abs(hard_cap)) * 1e-9:
            n_bad_ratio += 1
        if applied > hard_cap * (1 + 1e-9) + 1e-6:
            n_bad_le += 1
        if hard_cap < cap_for:
            n_min_hit += 1
    if not cap_rows and not gate_on:
        check("C4b 直查 cap_amt 一致性", True,
              "带 $.cap_amt 买单 0 笔——臂参数 pos.equity_gate=off，按 equity_budget 同口径"
              "不落点（预期）；机制存在性由 C4a 源码结构＋C4c 名义旁证覆盖")
    else:
        check("C4b 直查 cap_amt 一致性", len(cap_rows) > 0 and n_bad_min == 0
              and n_bad_ratio == 0 and n_bad_le == 0,
              "带 $.cap_amt 买单 %d 笔；applied≠min(cap_for,hard_cap) %d；"
              "hard_cap≠0.20×equity %d；applied>hard_cap %d；"
              "hard_cap<cap_for 真实触发 %d 笔（只立机制，默认配置下预期 0）" %
              (len(cap_rows), n_bad_min, n_bad_ratio, n_bad_le, n_min_hit))
    # C4c 旁证（非判据）：落账名义 ≤ 当日权益×20%
    n_buy, eq_missing, over_ceiling = c.execute("""
        select count(*),
               sum(case when e.total is null then 1 else 0 end),
               sum(case when e.total is not null
                         and f.price * f.qty > e.total * ? + 1e-6 then 1 else 0 end)
        from trade_fill f
        join plan_order p on p.id = f.plan_order_id and p.side = 'buy'
        left join equity_snapshot e on e.date = f.date
    """, (CEILING,)).fetchone()
    check("C4c 旁证：名义金额 ≤ 权益×20%", (eq_missing or 0) == 0 and (over_ceiling or 0) == 0,
          "买单 %d 笔；当日权益快照缺失 %d；超 20%% 天花板 %d（旁证，非判据）" %
          (n_buy, eq_missing or 0, over_ceiling or 0))

    # ---------- C5 ----------
    emit("\n## C5 恒等式 0.00")
    try:
        gap = float(compute(a.arm)["values"]["identity_gap"])
    except Exception as e:
        gap = float("nan")
        emit("     恒等式重算失败：%s" % e)
    check("C5 恒等式 0.00", gap <= IDENTITY_TOL,
          "`identity_gap` = %.2f（口径 `doc_anchors.py:205` max|total−(cash+market_value)|）"
          % gap)

    # ---------- 判定 ----------
    ok_all = all(ok for _n, ok, _d in checks)
    emit("\n## 判定")
    emit("  D1 结构判据：%s" % ("✅ 全过" if ok_all else "❌ 有未过项"))
    emit("  ⚠ 本 CLI 不裁定臂验收。对冻结的 0033 对照底座跑时，装油门尚未落地，")
    emit("     C1／C2／C3 未过属**预期**，只证明脚本能读真实账本、正确输出判据。")
    emit("     `67 §8.7` 契约：阶段 3 起跑前预注册；未跑通真实臂库前不得宣布 D1 已验收。")
    emit("\n  只读（mode=ro）、不冻结账本、不回填 `67 §8.7` 看板、不改文档、不替裁。")
    c.close()

    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n报告已写入 %s" % out.as_posix())

    return 2 if a.strict and not ok_all else 0


if __name__ == "__main__":
    raise SystemExit(main())
