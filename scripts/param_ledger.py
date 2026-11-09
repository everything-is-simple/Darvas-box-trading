#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""param_ledger.py —— 基线参数权威台账生成器（唯一事实源 = 代码 + 迁移）

为什么要有这个
--------------
2026-11-03 之前，「基线包含哪些参数」有三个互相打架的口径：
  · docs/01 §6 活参数表（人工维护，容易漏新键）
  · docs/44 配置清单「39 项」（= baseA 快照 38 + max_growth_days，**漏了 7 个只走代码默认的键**）
  · migrations 种子键（26，check_consistency C3 只校这一个）
三者都对，但都不是「基线」的完整定义 → 看文档的人无法确定到底哪些参数在生效。

本脚本把口径钉死为**代码实际读取的键**（`GetFloat/GetInt/GetString` 的字符串字面量），
并算出「全新库跑完全部迁移后的生效值」：
    种子（INSERT OR IGNORE，先到先得） → UPDATE → INSERT ... ON CONFLICT DO UPDATE → DELETE
键若从未被种子化，生效值 = 代码默认值（活默认）。

用法
----
    python scripts/param_ledger.py                    # 打印台账（markdown）
    python scripts/param_ledger.py --check            # 与 docs/44 台账比对，漂移则退出码 1
    python scripts/param_ledger.py --write            # 用生成的表替换 docs/44 的生成区
    python scripts/param_ledger.py --db var/darvas.db # 额外对账真实库（多一列「库内实际」）
    python scripts/param_ledger.py --json out.json    # 导出结构化数据

维护约定
--------
新增/改参数时**不要手改 docs/44 的表**——改代码默认值或写 migration，然后跑 --write。
「验证状态」是唯一需要人工维护的部分（下面的 VERIFIED 表），键缺登记即显示「未验（待登记）」。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC44 = ROOT / "docs" / "44-new-baseline-config.md"
CODE_DIRS = [ROOT / "internal", ROOT / "cmd"]
MIG_DIR = ROOT / "internal" / "store" / "migrations"

BEGIN = "<!-- BEGIN GENERATED:params -->"
END = "<!-- END GENERATED:params -->"

# ---------------------------------------------------------------- 人工知识层
# 验证状态与依据（唯一需要人维护的部分）。值 = (状态, 依据)
#   已验 / 维持 / 未验 / 回测注入
VERIFIED: dict[str, tuple[str, str]] = {
    "gate.ma60.mode": ("已验", "B-2 全 14 年：指数收盘 < MA60 停买，擂主；0025 种子化"),
    "gate.ma60.index": ("已验", "单镜上证定案；中证500 换镜未采纳（baseB 是历史对照锚）"),
    "pool.max_hold": ("已验", "B-4：14 只胜 7 只与 50% 权益闸门；0025 种子化。20 档待验"),
    "pos.equity_gate": ("已验", "B-4 ③ 否决权益预算闸门；0025 种子化 off"),
    "pos.equity_limit": ("地基", "闸门关时不生效，留作「牛冲熊守」动态仓地基"),
    "pos.single_limit": ("地基", "同上，单票 ≤8% 权益"),
    "position.staledays": ("原著规则", "O2：0033 固定 0（禁用 2 日强平）；v1 值 2 属反向刹车，干净默认已废（65 §1.2.5）"),
    "pos.newhigh_ladder": ("已验", "M9.0 第 5 维：距 52 周高点四档加码；0025 种子化 on"),
    "pos.newhigh.units.t1": ("已验", "M9.0：t1/t2 唯一稳定正贡献"),
    "pos.newhigh.units.t2": ("已验", "M9.0：t1/t2 唯一稳定正贡献"),
    "pos.newhigh.units.t3": ("已验", "M9.0 落地档位，未单独 A/B"),
    "pos.newhigh.units.t4": ("已验", "M9.0：>40% 净亏档，给 0.5 单位"),
    "pos.newhigh.lookback": ("已验", "52 周（交易日），随阶梯一并定案"),
    "pos.newhigh.cutoff": ("已验", ">40% 归 t4（十四年净亏档）"),
    "box.max_growth_days": ("已验", "LB-4：≤6 日最优（+626 超门槛、3/4 年改善）；0024 投产"),
    "box.volmult": ("已验", "P1-4：牛松熊紧，2.0 在震荡/牛/结构市与 1.5 相当、2018 更优，维持 2.0"),
    "sell.discount": ("维持", "T3 三档在噪声内，维持 0.97，不宣称最优"),
    "tdx.root": ("事实", "机器相关路径，**故意不入库**（0019 删行）；环境变量 DARVAS_TDX > 参数 > 自动探测，必须指到 vipdoc 一级"),
    "staledays": ("维持", ""),
    "pos.unit": ("已验", "1 单位 = 2% 权益（A M1 单位制定案）"),
    "fee.commission_rate": ("事实", "券商费率，非策略参数"),
    "fee.commission_min": ("事实", "券商最低收费"),
    "fee.stamp_tax": ("事实", "法定印花税"),
    "fee.transfer_rate": ("事实", "过户费"),
    "equity.base": ("事实", "初始权益 100 万（回放/复利起点）"),
    "buy.premium": ("原著规则", "类别 Ⅰ（O4 突破即买，65 §1.2.4）：0033 固定 1.00；旧 1.03 是 v1 生效值，非原著规则"),
    "match.max_entry_gap": ("未验", "P0-2 定 1.05，梯度未验"),
    "match.slippage": ("未验", "模拟成交买贵卖贱 0.2%"),
    "box.min_width": ("未验", "维 1，梯度未验（成本最大）"),
    "box.volwindow": ("未验", "放量参照基准窗口"),
    "screen.rswindow": ("未验", "相对强度窗口"),
    "screen.rs_excess_min": ("未验", "A1：20 日超额 ≥20pp 得 RS 分，梯度未验"),
    "screen.turnover": ("未验", "A 股流动性门槛 5000 万"),
    "screen.latedays": ("未验", "原书：突破后第 4 日没买=太晚"),
    "pool.flatboxmax": ("未验", "连续 4 箱不涨出池"),
    "pool.maxwatch": ("未验", "软参考：不拒入池，仅日志告警"),
    "pool.min_watch_days": ("未验", "G1：入池后 3 日内不产生首买"),
    "regime.breadth.window": ("未验", "宽度窗口（第 10 维归因原料）"),
    "regime.breadth.bull_thresh": ("未验", "牛触发 0.55（仅日志/归因）"),
    "regime.breadth.bear_thresh": ("未验", "熊标记 0.40（仅日志）"),
    "regime.breadth.confirm": ("未验", "连续确认 3 日"),
    "regime.breadth.min_history": ("未验", "次新过滤 60 根"),
    "regime.a.lookback": ("未验", "A 判据回看；**不再决定仓位单位**（阶梯制已接管）"),
    "regime.a.up_bias": ("未验", "A 判据升势阈值，从未调过"),
    "regime.a.down_bias": ("未验", "A 判据降势阈值，从未调过"),
    "pos.regime_scale.mode": ("未验", "攻防转换传导开关（0030 接口桩）：off 恒等；on 读 L1 状态机，留待后复权 + 新基线账本重算（docs/48 特征二）"),
    "pool.blacklist_months": ("原著规则", "O3：0033 固定 0（停用亏损拉黑）；0031 的 12 属 v1 历史值（65 §1.2.5）"),
    "buy.uptrend_only": ("否决", "P3-8 2026-11-03 定稿否决，仅回测注入，不种子化"),
    "buy.highest_box_mode": ("否决", "P3-8 2026-11-03 定稿否决（alltime/trend 三档全否）"),
    "buy.highest_box_window": ("否决", "随 highest_box_mode 否决；trend 模式专用"),
}

DOMAIN_ORDER = [
    ("数据/账户", ["equity.", "tdx."]),
    ("买入", ["buy."]),
    ("箱体", ["box."]),
    ("卖出", ["sell."]),
    ("持仓", ["position.", "pos."]),
    ("跟踪池", ["pool."]),
    ("筛选", ["screen."]),
    ("撮合", ["match."]),
    ("大盘闸门", ["gate."]),
    ("宽度/A 判据", ["regime."]),
    ("费用", ["fee."]),
]


# ---------------------------------------------------------------- 解析代码
CODE_RE = re.compile(r'\.(GetFloat|GetInt|GetString)\("([A-Za-z][\w.]*)"\s*,')
GO_TYPE = {"GetFloat": "float", "GetInt": "int", "GetString": "string"}


def _default_arg(text: str, start: int) -> str:
    """从 start（默认值起点）扫到与 Get*( 匹配的右括号，正确处理嵌套括号。"""
    depth = 1  # 已消费 GetX( 的左括号
    i = start
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i].strip()
        i += 1
    return text[start:].strip()


def scan_code() -> dict[str, dict]:
    """扫描 internal/ 与 cmd/ 下非测试 Go 源码，取 param 键与代码默认值。"""
    found: dict[str, dict] = {}
    for d in CODE_DIRS:
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.go")):
            if f.name.endswith("_test.go"):
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in CODE_RE.finditer(text):
                getter, key = m.group(1), m.group(2)
                default = _default_arg(text, m.end())
                if default.startswith('"') and default.endswith('"'):
                    default = default[1:-1]
                elif not re.fullmatch(r"-?\d+(\.\d+)?", default):
                    # 非字面量默认（如 tdxRoot() 这类函数调用）→ 标记为代码计算，值需人工确认
                    default = f"（代码计算 {default}）"
                line = text[: m.start()].count("\n") + 1
                rec = found.setdefault(key, {
                    "key": key, "type": GO_TYPE[getter], "default": default, "refs": [],
                })
                rec["refs"].append(f"{f.relative_to(ROOT).as_posix()}:{line}")
    return found


# ---------------------------------------------------------------- 解析迁移
SQL_INSERT = re.compile(
    r"INSERT\s+(OR\s+IGNORE\s+)?INTO\s+param\s*\(\s*key\s*,\s*value\s*\)\s*VALUES\s*(.*?)"
    r"(?:\s*ON\s+CONFLICT\s*\(\s*key\s*\)\s*DO\s+UPDATE\s+SET\s+value\s*=\s*'([^']*)')?\s*;",
    re.I | re.S)
SQL_UPDATE = re.compile(
    r"UPDATE\s+param\s+SET\s+value\s*=\s*'([^']*)'\s*WHERE\s+key\s*=\s*'([\w.]+)'", re.I)
SQL_DELETE = re.compile(r"DELETE\s+FROM\s+param\s+WHERE\s+key\s+IN\s*\((.*?)\)", re.I | re.S)
TUPLE = re.compile(r"\(\s*'([\w.]+)'\s*,\s*'([^']*)'\s*\)")


def scan_migrations() -> tuple[dict[str, dict], set[str], list[dict]]:
    """按迁移文件顺序模拟 param 表演化，返回 (键→{值,来源}, 已删键集, 事件流水)。

    三种写法的语义差异必须区分（踩过坑）：
      · INSERT OR IGNORE  → 键已存在则不覆盖（0001/0003/0010… 的种子）
      · INSERT（无 OR IGNORE）→ 会覆盖（0018 的 pool.max_hold / equity_limit / single_limit 用这种）
      · INSERT ... ON CONFLICT DO UPDATE → 覆盖（0025 的 4 个注入项）
    """
    eff: dict[str, dict] = {}
    dropped: set[str] = set()
    events: list[dict] = []
    if not MIG_DIR.exists():
        return eff, dropped, events

    for f in sorted(MIG_DIR.glob("*.sql")):
        sql = f.read_text(encoding="utf-8", errors="ignore")
        ops: list[tuple[int, str, tuple]] = []
        for m in SQL_INSERT.finditer(sql):
            or_ignore = bool(m.group(1))
            on_conflict = m.group(3)
            body = m.group(2)
            tuples = TUPLE.findall(body)
            if on_conflict is not None and len(tuples) == 1:
                ops.append((m.start(), "upsert", (tuples[0][0], on_conflict)))
                continue
            for k, v in tuples:
                ops.append((m.start(), "seed-ignore" if or_ignore else "seed-plain", (k, v)))
        for m in SQL_UPDATE.finditer(sql):
            ops.append((m.start(), "update", (m.group(2), m.group(1))))
        for m in SQL_DELETE.finditer(sql):
            for k in re.findall(r"'([\w.]+)'", m.group(1)):
                ops.append((m.start(), "delete", (k,)))
        for _, kind, args in sorted(ops, key=lambda x: x[0]):
            ver = f.name
            if kind == "delete":
                eff.pop(args[0], None)
                dropped.add(args[0])
                events.append({"file": ver, "op": "delete", "key": args[0]})
            elif kind in ("seed-ignore", "seed-plain"):
                k, v = args
                if kind == "seed-ignore" and k in eff:
                    continue
                eff[k] = {"value": v, "source": f"种子 {ver}", "origin": "seed"}
                events.append({"file": ver, "op": kind, "key": k, "value": v})
            elif kind == "upsert":
                k, v = args
                eff[k] = {"value": v, "source": f"迁移 {ver}", "origin": "migration"}
                events.append({"file": ver, "op": "upsert", "key": k, "value": v})
            else:  # update
                k, v = args
                if k in eff:
                    eff[k] = {"value": v, "source": f"迁移 {ver}", "origin": "migration"}
                else:
                    eff[k] = {"value": v, "source": f"迁移 {ver}（未种子，UPDATE 落空）", "origin": "migration-orphan"}
                events.append({"file": ver, "op": "update", "key": k, "value": v})
    return eff, dropped, events


# ---------------------------------------------------------------- 合成台账
def build(db: str | None = None) -> dict:
    code = scan_code()
    eff, dropped, events = scan_migrations()
    dbvals: dict[str, str] = {}
    if db:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dbvals = dict(con.execute("SELECT key, value FROM param"))
        con.close()

    rows = []
    for key, rec in sorted(code.items()):
        seeded = key in eff
        value = eff[key]["value"] if seeded else rec["default"]
        source = eff[key]["source"] if seeded else "代码默认（未种子化）"
        status, basis = VERIFIED.get(key, ("未验（待登记）", ""))
        rows.append({
            "key": key,
            "value": value,
            "type": rec["type"],
            "source": source,
            "default": rec["default"],
            "status": status,
            "basis": basis,
            "ref": rec["refs"][0],
            "db_value": dbvals.get(key),
        })

    # 按「领域顺序 + 键名」排序，保证渲染时同一领域的键连续成块
    order = {name: i for i, (name, _) in enumerate(DOMAIN_ORDER)}
    rows.sort(key=lambda r: (order.get(domain_of(r["key"]), 99), r["key"]))

    code_keys = set(code)
    return {
        "rows": rows,
        "count": len(rows),
        "seeded": sorted(k for k in code_keys if k in eff),
        "default_only": sorted(k for k in code_keys if k not in eff),
        "dead": sorted(dropped),
        "dead_still_in_db": sorted(set(dbvals) & dropped) if dbvals else [],
        "db_extra": sorted(set(dbvals) - code_keys) if dbvals else [],
        "db_missing": sorted(code_keys - set(dbvals)) if dbvals else [],
        "db_mismatch": sorted(k for k in code_keys & set(dbvals) if str(dbvals[k]) != next(
            (r["value"] for r in rows if r["key"] == k), None)),
        "events": events,
    }


def domain_of(key: str) -> str:
    for name, prefixes in DOMAIN_ORDER:
        if any(key.startswith(p) for p in prefixes):
            return name
    return "其他"


def render_markdown(led: dict, with_db: bool) -> str:
    out: list[str] = []
    out.append(f"> 本表由 `scripts/param_ledger.py` 从 **代码实读键 + 迁移演化** 自动生成，"
               f"不要手改。共 **{led['count']} 键**"
               f"（种子 {len(led['seeded'])} / 只走代码默认 {len(led['default_only'])}）。")
    out.append("")
    head = "| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |"
    sep = "|---|---|---|---|---|---|---|"
    if with_db:
        head = "| 键 | 生效值 | 库内实际 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |"
        sep = "|---|---|---|---|---|---|---|---|"
    cur = None
    for r in led["rows"]:
        dom = domain_of(r["key"])
        if dom != cur:
            out.append("")
            out.append(f"**{dom}**")
            out.append("")
            out.append(head)
            out.append(sep)
            cur = dom
        cells = [f"`{r['key']}`", f"`{r['value']}`"]
        if with_db:
            actual = r["db_value"]
            if actual is None:
                cells.append("—（走默认）")
            elif str(actual) == str(r["value"]):
                cells.append(f"`{actual}` ✓")
            else:
                cells.append(f"`{actual}` ⚠ 不一致")
        cells += [r["type"], r["source"], f"`{r['default']}`", r["status"], r["basis"] or "—"]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def extract_doc_rows(text: str) -> dict[str, str]:
    """从 docs/44 的生成区里抠出 键→生效值，用于漂移检测。"""
    m = re.search(re.escape(BEGIN) + r"(.*?)" + re.escape(END), text, re.S)
    block = m.group(1) if m else text
    pairs: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip().startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = cells[0].strip("`")
        val = cells[1].strip("`")
        if re.fullmatch(r"[a-z][\w.]*", key):
            pairs[key] = val
    return pairs


def cmd_check(led: dict) -> int:
    if not DOC44.exists():
        print(f"✗ C6 找不到 {DOC44}")
        return 1
    doc = extract_doc_rows(DOC44.read_text(encoding="utf-8"))
    live = {r["key"]: r["value"] for r in led["rows"]}
    missing = sorted(set(live) - set(doc))
    extra = sorted(set(doc) - set(live))
    diff = sorted(k for k in set(live) & set(doc) if str(live[k]) != str(doc[k]))
    bad = False
    if missing:
        bad = True
        print(f"✗ C6 docs/44 缺 {len(missing)} 键: {', '.join(missing)}")
    if extra:
        bad = True
        print(f"✗ C6 docs/44 多 {len(extra)} 键（代码不读）: {', '.join(extra)}")
    if diff:
        bad = True
        for k in diff:
            print(f"✗ C6 值漂移 {k}: 代码/迁移={live[k]}  docs={doc[k]}")
    if bad:
        print("  → 跑 `python scripts/param_ledger.py --write` 重新生成")
        return 1
    print(f"✓ C6 参数台账对齐 ({len(live)} 键，docs/44 与代码+迁移一致)")
    return 0


def print_db_diff(led: dict):
    """docs/archive/54 #13：库差异提示（--check --db 与非 check 的默认输出共用）。"""
    if led["db_mismatch"]:
        print(f"⚠ 库内与生效值不一致: {led['db_mismatch']}")
    if led["db_extra"]:
        print(f"⚠ 库内多出（代码不读/已删死参数残留）{len(led['db_extra'])} 键: {led['db_extra']}")
    if led["db_missing"]:
        print(f"ℹ 库内缺失（走代码默认）{len(led['db_missing'])} 键: {led['db_missing']}")


def cmd_write(led: dict, with_db: bool) -> int:
    if not DOC44.exists():
        print(f"✗ 找不到 {DOC44}")
        return 1
    text = DOC44.read_text(encoding="utf-8")
    body = render_markdown(led, with_db)
    block = f"{BEGIN}\n{body}\n{END}"
    if BEGIN in text and END in text:
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    else:
        print("✗ docs/44 里没有生成区标记，请先手工插入标记")
        return 1
    DOC44.write_text(new, encoding="utf-8")
    print(f"✓ 已写入 docs/44（{led['count']} 键）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="对账真实库（只读）")
    ap.add_argument("--check", action="store_true", help="与 docs/44 比对，漂移退出码 1")
    ap.add_argument("--write", action="store_true", help="用生成结果替换 docs/44 生成区")
    ap.add_argument("--json", default="", help="导出结构化数据")
    a = ap.parse_args()

    led = build(a.db or None)
    if a.json:
        Path(a.json).write_text(json.dumps(led, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写 {a.json}")
    if a.check:
        # docs/archive/54 #13：--check 不再提前 return——带了 --db 时一并输出库差异
        # （db_mismatch/db_extra/db_missing），让 --check 与库对账可同次完成。
        # 退出码仍只反映 docs/44 与代码/迁移的对齐，库差异以 ⚠ 提示呈现。
        rc = cmd_check(led)
        if a.db:
            print_db_diff(led)
        return rc
    if a.write:
        return cmd_write(led, bool(a.db))
    print(render_markdown(led, bool(a.db)))
    print()
    print(f"合计 {led['count']} 键：种子 {len(led['seeded'])} / 只走代码默认 {len(led['default_only'])}"
          f" / 死参数已删 {len(led['dead'])}")
    if a.db:
        print_db_diff(led)
    return 0


if __name__ == "__main__":
    sys.exit(main())
