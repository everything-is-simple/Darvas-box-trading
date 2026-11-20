#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D11 多股并行试探淘汰（O12）结构／时序只读验收 CLI。

定位：`67 §4.1` S3 阶段 3 装油门的第三个子步（多股小仓试探、弱者止损淘汰强者加码），
判据与验收物见 `65 §1.2.2` O12 行「试探／淘汰事件序列对账」、`67 §8.7` 起跑前
预注册验收 CLI 清单（D11 行）、`65 §5` 施工表第 6 行「试探淘汰｜新增（现无）」。
适用对象默认为冻结的段 A 对照底座 `var/bt/arms/bt_clean_default_0033.db`，可用
`--arm` 指到任一 scratch 臂库。

**原生事件落点（2026-11-10 用户「按建议办」拍板，reason 码方案）**：不建表、不加
param 键、不写迁移——
  试探事件 = breakout 首买单 `plan_order.reason_json.$.probe = true`（planner 打标）；
  淘汰事件 = 试探入场持仓的卖单 `reason_json.$.eliminated = true`（planner 经
             `TradeRepo.OpenProbePositionIDs` 只读查询——leg1 买单带 $.probe 的未平仓
             持仓集合——在 stop/failed/time_stop/initial_stop 卖单上打标）；
  强者加码 = leg≥2 买单（D1 金字塔通道原生事件，reason.signal='pyramid'）。
事件序列对账全部用 `plan_order` 逐日快照（plan_date），不读最终态 `box` 表。
对 0033 冻结对照底座跑 = 装油门前结构基线冒烟（C1 必未过，预期），**非臂验收**。

**时点归因铁律**：只用逐日快照 `plan_order.reason_json` / `signal.payload_json` 与账本表
`trade_fill` / `position` / `plan_order`，**不读最终态 `box` 表反查**。

本 CLI 只逐条记录事实，不作完成线／臂验收裁定，不冻结账本、不回填看板、不改文档。
默认退出码 0 表示 CLI 跑通；`--strict` 时任一结构判据未过返回 2。

用法：
  python scripts/arm_accept_d11_probe.py
  python scripts/arm_accept_d11_probe.py --arm var/bt/arms/bt_clean_default_0033.db
  python scripts/arm_accept_d11_probe.py --out var/bt/arms/accept_0033_d11_probe.md
  python scripts/arm_accept_d11_probe.py --strict
"""
import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from doc_anchors import compute  # noqa: E402

ARM = "var/bt/arms/bt_clean_default_0033.db"


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).resolve().as_posix(),
                           uri=True, timeout=30)


def main():
    ap = argparse.ArgumentParser(description="D11 多股并行试探淘汰只读验收 CLI")
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
    emit("D11 多股并行试探淘汰（O12）· 只读验收（`67 §4.1` S3 / `65 §1.2.2` O12 / `67 §8.7` D11 行）")
    emit("时间 %s | 臂库 %s | 引擎 sha %s" %
         (datetime.now().isoformat(timespec="seconds"), a.arm, sha[:16]))
    emit("daily_run 非 ok = %d | 只读 mode=ro | 不读最终态 box 表" % non_ok)
    emit("=" * 100)

    # ---------- C1 原生事件落点（唯一正式判据） ----------
    emit("\n## C1 试探／淘汰事件原生落点（reason 码，2026-11-10 拍板方案）")
    probe_n = c.execute("""
        select count(*) from plan_order
        where side = 'buy' and json_extract(reason_json, '$.probe') is not null
    """).fetchone()[0]
    elim_n = c.execute("""
        select count(*) from plan_order
        where side = 'sell' and json_extract(reason_json, '$.eliminated') is not null
    """).fetchone()[0]
    add_n = c.execute("""
        select count(*) from plan_order where side = 'buy' and leg_no >= 2
    """).fetchone()[0]
    evt_tables = [r[0] for r in c.execute("""
        select name from sqlite_master where type = 'table'
          and (lower(name) like '%probe%' or lower(name) like '%eliminat%')
    """).fetchall()]
    param_keys = [r[0] for r in c.execute("""
        select key from param where lower(key) like '%probe%' or lower(key) like '%eliminat%'
    """).fetchall()]
    has_evt = probe_n > 0 and elim_n > 0
    check("C1 事件落点存在（试探+淘汰双事件）", has_evt,
          "$.probe 首买单 %d 笔｜$.eliminated 卖单 %d 笔｜leg≥2 加码单 %d 笔｜"
          "事件表 %d 张（不要求）｜param 键 %d 个（不要求）" %
          (probe_n, elim_n, add_n, len(evt_tables), len(param_keys)))
    emit("     落点出处：planner breakout 首买打 $.probe；OpenProbePositionIDs 命中的持仓其")
    emit("     卖单打 $.eliminated；强者加码 = leg≥2 买单（D1 通道）。均逐日快照 plan_order。")

    # ---------- C2–C4 原生事件序列对账 ----------
    emit("\n## C2–C4 事件序列对账（原生 reason 码 + leg≥2，逐日快照 plan_order）")
    probes = c.execute("""
        select tp.plan_date, p.code from plan_order p
        join trade_plan tp on tp.id = p.trade_plan_id
        where p.side = 'buy' and json_extract(p.reason_json, '$.probe') is not null
        order by tp.plan_date, p.code
    """).fetchall()
    elims = c.execute("""
        select tp.plan_date, p.code from plan_order p
        join trade_plan tp on tp.id = p.trade_plan_id
        where p.side = 'sell' and json_extract(p.reason_json, '$.eliminated') is not null
        order by tp.plan_date, p.code
    """).fetchall()
    adds = c.execute("""
        select tp.plan_date, p.code from plan_order p
        join trade_plan tp on tp.id = p.trade_plan_id
        where p.side = 'buy' and p.leg_no >= 2
        order by tp.plan_date, p.code
    """).fetchall()

    adds_by_code = defaultdict(list)
    for d, k in adds:
        adds_by_code[k].append(d)
    elims_by_code = defaultdict(list)
    for d, k in elims:
        elims_by_code[k].append(d)

    batches = defaultdict(set)
    for d, k in probes:
        batches[d].add(k)
    batch_days = sorted((d, ks) for d, ks in batches.items() if len(ks) >= 2)

    n_weak = n_strong = n_after = 0
    samples = []
    for d, codes in batch_days:
        weak = [k for k in codes
                if any(sd >= d for sd in elims_by_code.get(k, []))
                and not any(ad >= d for ad in adds_by_code.get(k, []))]
        strong = [k for k in codes if any(ad >= d for ad in adds_by_code.get(k, []))]
        n_weak += len(weak)
        n_strong += len(strong)
        # 只计试探日当日／之后的事件；同票更早批次的事件按口径排除，避免重复计入
        n_after += sum(sum(1 for sd in elims_by_code.get(k, []) if sd >= d)
                       + sum(1 for ad in adds_by_code.get(k, []) if ad >= d)
                       for k in codes)
        if len(samples) < 5:
            samples.append((d, len(codes), len(weak), len(strong)))

    emit("  C2 试探事件：%d 笔 $.probe 首买；同日 ≥2 只的试探批次 %d 个交易日" %
         (len(probes), len(batch_days)))
    emit("  C3 弱者淘汰事件：$.eliminated 卖单共 %d 笔；批次内后续被淘汰 %d 票次" %
         (len(elims), n_weak))
    emit("  C4 强者加码事件：leg≥2 买单共 %d 笔；批次内后续加码 %d 票次；"
         "日期 ≥ 试探日的后续事件 %d 例" % (len(adds), n_strong, n_after))
    for d, n, w, s in samples:
        emit("     批次样例 %s：试探 %d 只 / 淘汰 %d 只 / 加码 %d 只" % (d, n, w, s))
    emit("  ⚠ 序列统计只记录、不裁定：D11 臂验收须事件序列完整且时序正确，由人工判读。")

    # ---------- 账本体检（非判据） ----------
    emit("\n## 账本体检（非 D11 判据）")
    try:
        gap = float(compute(a.arm)["values"]["identity_gap"])
        emit("  恒等式 `identity_gap` = %.2f（`doc_anchors.py:205` 口径）" % gap)
    except Exception as e:
        emit("  恒等式重算失败：%s" % e)

    # ---------- 判定 ----------
    ok_all = all(ok for _n, ok, _d in checks)
    emit("\n## 判定")
    emit("  D11 结构判据：%s" % ("✅ 全过" if ok_all else "❌ 有未过项（C1 原生事件落点缺失）"))
    emit("  ⚠ 本 CLI 不裁定臂验收。对冻结的 0033 对照底座跑时 C1 未过属**预期**")
    emit("     （0033 是装油门前底座，无 $.probe/$.eliminated 标记）；")
    emit("     `67 §8.7` 契约：阶段 3 起跑前预注册；未跑通真实臂库前不得宣布 D11 已验收。")
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
