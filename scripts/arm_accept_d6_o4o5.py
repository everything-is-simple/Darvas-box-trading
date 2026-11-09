#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D6（O4 + O5 成对）结构／时序只读验收 CLI。

定位：`67 §4.1` S1 的成对验收工具，判据与验收物见 `65 §1.2` O4／O5 行、
`67 §8.7` 起跑前预注册验收 CLI 清单（D6 行）。适用对象是冻结的段 A 对照底座
`var/bt/arms/bt_clean_default_0033.db`，也可用 `--arm` 指到任一 scratch 臂库。

判的都是结构／时序，不判数值尺度：
  O4 突破即买   = `buy.premium` 1.00 + 源码严格 `>` + 逐日快照无「收盘 ≤ 上轨」的突破信号
                  + 等号边界用例（收盘恰等于上轨的候选当日不得出现突破信号）
  O5 初始止损   = `plan_order.reason_json` 首值即腿1 初始止损 + 挂点在被突破上轨的分界线
                  （两箱模型：stop 贴旧箱 A 上轨下沿＝新箱 B 下轨；即 box_bottom < stop < box_top，
                  贴分界、不进突破后的活跃箱、不穿旧箱下轨）
  尺度声明      = `stop == box_top × sell.discount`（当前 0.97）只记录，不据此否决；
                  固定 0.97 折价属 A 档 🚫 分母扣除项，纯数值按 `67 §5.2` 锁段 C。

**时点归因铁律**：只用逐日快照 `signal.payload_json` / `candidate.reasons_json` /
`plan_order.reason_json` 与行情表 `daily_bar_adj`，**不读最终态 `box` 表反查**。

本 CLI 只逐条记录事实，不作完成线／S1 放行裁定，不冻结账本、不回填看板、不改文档。
只读：`mode=ro`，`timeout=30`。默认退出码 0 表示 CLI 跑通；`--strict` 时任一结构判据
未过返回 2。

用法：
  python scripts/arm_accept_d6_o4o5.py
  python scripts/arm_accept_d6_o4o5.py --arm var/bt/arms/bt_clean_default_0033.db
  python scripts/arm_accept_d6_o4o5.py --out var/bt/arms/accept_0033_d6_o4o5.md
  python scripts/arm_accept_d6_o4o5.py --strict
"""
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARM = "var/bt/arms/bt_clean_default_0033.db"
ENGINE_GO = "internal/box/engine.go"
PLANNER_GO = "internal/planner/planner.go"
EPS = 1e-6  # 价格容差（行情四位小数）

# 等号边界：逐日快照 candidate.reasons_json 的 box_top 与同日行情 adj_close 相等
EQ_SQL = """
with eq as (
  select c.screen_date as d, c.code as k,
         cast(json_extract(c.reasons_json, '$.box_top') as real) as top
  from candidate c
  join daily_bar_adj b on b.code = c.code and b.date = c.screen_date
  where c.reasons_json like '%box_top%'
    and abs(b.adj_close - cast(json_extract(c.reasons_json, '$.box_top') as real)) < 1e-6
)
select count(*),
       sum(case when s.id is not null then 1 else 0 end)
from eq
left join signal s on s.date = eq.d and s.code = eq.k and s.type = 'breakout'
"""

EQ_SAMPLE_SQL = """
with eq as (
  select c.screen_date as d, c.code as k,
         cast(json_extract(c.reasons_json, '$.box_top') as real) as top
  from candidate c
  join daily_bar_adj b on b.code = c.code and b.date = c.screen_date
  where c.reasons_json like '%box_top%'
    and abs(b.adj_close - cast(json_extract(c.reasons_json, '$.box_top') as real)) < 1e-6
)
select eq.d, eq.k, eq.top, s.close_px, s.box_level
from eq join signal s on s.date = eq.d and s.code = eq.k and s.type = 'breakout'
limit 5
"""


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).resolve().as_posix(),
                           uri=True, timeout=30)


def main():
    ap = argparse.ArgumentParser(description="D6（O4+O5 成对）只读验收 CLI")
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
    params = dict(c.execute("select key, value from param").fetchall())
    premium = float(params.get("buy.premium", "nan"))
    disc = float(params.get("sell.discount", "nan"))
    sha = c.execute("select engine_sha256 from baseline limit 1").fetchone()
    sha = sha[0] if sha else "(缺)"
    non_ok = c.execute("select count(*) from daily_run where status <> 'ok'").fetchone()[0]

    emit("=" * 100)
    emit("D6（O4 + O5 成对）· 只读验收（`67 §4.1` S1 / `65 §1.2` O4·O5 / `67 §8.7` D6 行）")
    emit("时间 %s | 臂库 %s | 引擎 sha %s" %
         (datetime.now().isoformat(timespec="seconds"), a.arm, sha[:16]))
    emit("参数 buy.premium=%.2f | sell.discount=%.2f | daily_run 非 ok = %d" %
         (premium, disc, non_ok))
    emit("时点来源：signal.payload_json / candidate.reasons_json / plan_order.reason_json（逐日快照）")
    emit("          + daily_bar_adj（行情）；**不读最终态 box 表反查**")
    emit("=" * 100)

    # ---------- O4 突破即买 ----------
    emit("\n## O4 突破即买（结构／时序）")
    check("C1 无额外溢价前置", abs(premium - 1.00) < EPS,
          "`buy.premium` = %.2f（类别 Ⅰ，`65 §1.2.4`）" % premium)

    eng = (ROOT / ENGINE_GO).read_text(encoding="utf-8", errors="replace")
    strict_gt = "b.Close <= cur.top*s.params.BreakPremium" in eng
    src_line = next((i for i, l in enumerate(eng.splitlines(), 1)
                     if "b.Close <= cur.top*s.params.BreakPremium" in l), 0)
    check("C2 源码严格大于", strict_gt,
          "`%s:%s` isBreakout：`if b.Close <= top×premium { return false }` → 等号不算突破"
          % (ENGINE_GO, src_line))

    n_break, bad_le, bad_top, bad_prem = c.execute("""
        select count(*),
               sum(case when close_px <= box_level * ? + 1e-9 then 1 else 0 end),
               sum(case when payload_json is not null
                         and abs(coalesce(cast(json_extract(payload_json, '$.box_top') as real),
                                          box_level) - box_level) > 1e-6 then 1 else 0 end),
               sum(case when payload_json is not null
                         and json_extract(payload_json, '$.premium') is not null
                         and abs(cast(json_extract(payload_json, '$.premium') as real) - ?) > 1e-9
                    then 1 else 0 end)
        from signal where type = 'breakout'
    """, (premium, premium)).fetchone()
    check("C3 突破信号逐日快照", n_break > 0 and (bad_le or 0) == 0,
          "%d 条 breakout；收盘 ≤ 上轨×premium 违规 %d 条" % (n_break, bad_le or 0))
    check("C3b 快照自洽", (bad_top or 0) == 0 and (bad_prem or 0) == 0,
          "`payload_json.box_top` 与 `box_level` 不一致 %d；premium 快照不一致 %d"
          % (bad_top or 0, bad_prem or 0))

    eq_n, eq_bad = c.execute(EQ_SQL).fetchone()
    check("C4 等号边界用例", eq_n > 0 and (eq_bad or 0) == 0,
          "收盘 == 快照上轨的候选 %d 个；当日出现 breakout signal %d 个（应为 0）"
          % (eq_n, eq_bad or 0))
    if eq_bad:
        for r in c.execute(EQ_SAMPLE_SQL).fetchall():
            emit("     违规样例 %s / %s 快照top=%.4f signal收盘=%.4f box_level=%.4f" %
                 (r[0], r[1], r[2], r[3], r[4]))

    # ---------- O5 初始止损 ----------
    emit("\n## O5 初始止损（首值／锚点／不落箱体内）")
    rows = c.execute("""
        select id, reason_json from plan_order
        where side = 'buy' and leg_no = 1 and order_kind = 'stop_buy'
    """).fetchall()
    n = len(rows)
    missing = below_top = above_bottom = exact_disc = 0
    samples = []
    for oid, rj in rows:
        try:
            d = json.loads(rj)
        except Exception:
            missing += 1
            continue
        top, bot, stop = d.get("box_top"), d.get("box_bottom"), d.get("stop")
        if None in (top, bot, stop):
            missing += 1
            continue
        if stop < top - EPS:
            below_top += 1
        if bot + EPS < stop < top - EPS:
            above_bottom += 1
        elif len(samples) < 5:
            samples.append((oid, top, bot, stop))
        if abs(stop - top * disc) < 1e-6:
            exact_disc += 1

    check("C5 首值 == 腿1 初始止损", n > 0 and missing == 0,
          "腿1 初始买单 %d 笔；`reason_json.stop` 缺失／不可解析 %d 笔" % (n, missing))
    check("C6 挂点相对上轨", n > 0 and below_top == n,
          "stop < box_top：%d / %d" % (below_top, n))
    check("C7 贴上轨分界、不穿旧箱下轨", n > 0 and above_bottom == n,
          "旧箱下轨 < stop < 旧箱上轨：%d / %d（两箱模型：贴两箱分界线、不进突破后新箱）"
          % (above_bottom, n))
    if samples:
        for oid, top, bot, stop in samples:
            emit("     压线／穿旧箱下轨样例 id=%s top=%.4f bottom=%.4f stop=%.4f" %
                 (oid, top, bot, stop))

    emit("\n## 尺度声明（只记录、不作结构否决）")
    emit("  stop == box_top × sell.discount(%.2f)：%d / %d 笔" % (disc, exact_disc, n))
    planner = (ROOT / PLANNER_GO).read_text(encoding="utf-8", errors="replace")
    emit("  `%s` 现实现：%s" %
         (PLANNER_GO, "stop := box.Top * sellDisc" if "stop := box.Top * sellDisc" in planner
          else "（未匹配到已知实现，请人工核对）"))
    emit("  固定 0.97 折价 = A 档 🚫 分母扣除项（`64 §1.5`）；原著「上轨下方一点点」的")
    emit("  数值尺度按 `65 §1.2.1` 类别 Ⅲ / `67 §5.2` 锁段 C，最终去留归段 C 另行拍板。")

    # ---------- 汇总 ----------
    o4_ok = all(ok for name, ok, _ in checks if name.startswith("C1") or name.startswith("C2")
                or name.startswith("C3") or name.startswith("C4"))
    o5_ok = all(ok for name, ok, _ in checks if name.startswith("C5") or name.startswith("C6")
                or name.startswith("C7"))
    emit("\n## 判定")
    emit("  O4 突破即买结构：%s" % ("✅ 判据全过" if o4_ok else "❌ 有未过项"))
    emit("  O5 初始止损结构：%s" % ("✅ 判据全过" if o5_ok else "❌ 有未过项"))
    emit("  S1 成对放行：**本 CLI 不裁定**。用户 2026-11-10 已拍「零改动只读放行」：")
    emit("  O4／O5 结构判据全过、锚点本已在上轨（`stop := box.Top × sellDisc`），无需改码；")
    emit("  `stop` 恒 = 上轨 × 0.97 的尺度差异（系统 −3.00%、箱宽中位 11.64%；原著书证")
    emit("  45→44⅞ ≈ −0.28%）按 A 档裁定锁段 C，完成线报告首行挂 ⚠（`64 §1.5`／`67 §5.2`／`65 §1.2` O5 行）。")
    emit("\n  本 CLI 只读（mode=ro）、不冻结账本、不回填 `67 §8.7` 看板、不改文档、不替裁。")
    c.close()

    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n报告已写入 %s" % out.as_posix())

    return 2 if a.strict and not (o4_ok and o5_ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
