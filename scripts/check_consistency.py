#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Darvas-box-trading 一致性检查（docs/06-consistency.md §2 防线一）。

检查项：
  C1 枚举对齐：contract.go 的状态枚举常量集合  ==  DDL 的 CHECK(... IN (...)) 集合
  C2 表覆盖：  DDL 的 CREATE TABLE 列表  ⊇  docs/03-erd.md 的 Mermaid 实体列表
  C3 参数键对齐：migrations 的 param 种子键集合  ⊆  docs/01 §6 参数表键集合
  C4 时刻表对齐：scheduler.go Schedule/WeeklyClock == docs/01 §4.1/§4.3 时刻表
     （2026-10-18 v1.3 起：时刻集中一处管理——§4 为唯一事实源，
      调度器/CLI/GUI/测试/一致性检查均由其派生，本项锁定代码与文档不漂移。）
  C6 参数台账：docs/44 生成区台账  ==  代码实读键 + 迁移演化后的生效值（2026-11-03 新增）
  C7 研究纪律：判据窗口 ≤2018-12-31（2019+ 不解冻）+ raw 报告口径横幅齐全（2026-11-05 新增，docs/55）

用法：python scripts/check_consistency.py [repo_root]
退出码：0=全部一致；1=有漂移（CI应失败）。
"""
import os, pathlib, re, sys

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                       os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import param_ledger  # noqa: E402  （C3/C6 共用同一套 param 解析，避免两处口径打架）
import apply_caliber_banner  # noqa: E402  （C7 横幅清单唯一事实源，避免两处口径打架）

CONTRACT_GO = os.path.join(ROOT, "internal", "contract", "contract.go")
SCHEDULER_GO = os.path.join(ROOT, "internal", "scheduler", "scheduler.go")
MIGRATIONS_DIR = os.path.join(ROOT, "internal", "store", "migrations")
ERD_MD = os.path.join(ROOT, "docs", "03-erd.md")
REQ_MD = os.path.join(ROOT, "docs", "01-requirements.md")
DOC44_MD = os.path.join(ROOT, "docs", "44-new-baseline-config.md")

# ── C7 配置（docs/55 §6.4）──────────────────────────────────────────
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
WINDOW_MAX_YEAR = 2018
# 只认「整行赋值给窗口型名称」的日期字面量；不碰注释、不碰决策日期
WINDOW_PAT = re.compile(
    r"\b(START|END|FROM|TO|BEGIN|FINISH|SAMPLE_START|SAMPLE_END|DATA_START|DATA_END"
    r"|start_time|end_time|start_date|end_date|from_date|to_date)"
    r"\s*=\s*[\"'](\d{4})-\d{2}-\d{2}[\"']")
# 白名单：生产实时链路，窗口本就是「当前日期」，不属于判据计算
WINDOW_WHITELIST = {
    "backfill_window.py": "生产补数，窗口=当前交易日",
}

def fail(msg):
    print("✗", msg)
    return 1

def ok(msg):
    print("✓", msg)
    return 0

def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()

def check_c1_enums():
    """contract.go 枚举常量 vs 最终生效 DDL 的 CHECK IN 集合
    仅核对 contract.go 中显式定义了枚举类型的列（BoxStatus/WatchStage/SignalType）。
    其余列（position.status/order.status/daily_run.status/fundamental_flag/side/order_kind）
    在 contract.go 中用普通字符串表示，DDL 的 CHECK 即其运行时守卫——不要求枚举对齐。
    2026-10-18（0003起）：迁移可重建表改 CHECK（如 watch 六值），纯文本拼接会取到旧定义；
    改为内存库按序执行全部迁移后，从 sqlite_master 读最终生效 DDL。
    """
    import sqlite3
    code = read(CONTRACT_GO)
    parts = []
    for fn in sorted(os.listdir(MIGRATIONS_DIR)):
        if fn.endswith(".sql"):
            parts.append(read(os.path.join(MIGRATIONS_DIR, fn)))
    try:
        con = sqlite3.connect(":memory:")
        con.executescript("\n".join(parts))
        ddl = " ".join(r[0] for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall())
        con.close()
    except Exception as e:
        return fail(f"C1 迁移无法在内存库执行: {e}")

    # 列→contract枚举映射（仅这些列要求对齐）
    col_to_enum = {
        "box.status":        "BoxStatus",
        "watch.stage":        "WatchStage",
        "signal.type":        "SignalType",
    }
    rc = 0
    for col_key, enum_name in col_to_enum.items():
        table, col = col_key.split(".")
        # sqlite_master 存储的建表语句（表名可能带引号：RENAME 重建的表存为 "watch"，原建表无引号）
        m = re.search(
            r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?[\"'`]?" + table + r"[\"'`]?\s*\(.*?" +
            r"CHECK\s*\(\s*" + col + r"\s+IN\s*\(([^)]+)\)\s*\)", ddl, re.I | re.S)
        if not m:
            rc |= fail(f"C1 找不到 {col_key} 的 CHECK 约束（最终生效 schema）")
            continue
        ddl_vals = set(re.findall(r"'(\w+)'", m.group(1)))
        # 取 contract.go 中该枚举类型的常量值
        go_vals = set(re.findall(
            r"\b" + enum_name + r"\b\s*=\s*\"(\w+)\"", code))
        if not go_vals:
            # 形如 ConstName EnumType = "val"
            go_vals = set(re.findall(
                r"\w+\s+" + enum_name + r'\s*=\s*"(\w+)"', code))
        missing = ddl_vals - go_vals
        extra = go_vals - ddl_vals
        if missing:
            rc |= fail(f"C1 {col_key}: DDL有但contract.go无 {missing}")
        elif extra:
            rc |= fail(f"C1 {col_key}: contract.go有但DDL无 {extra}")
        else:
            ok(f"C1 {col_key} 枚举对齐 ({len(ddl_vals)}值)")
    return rc

def check_c2_tables():
    """DDL建表 vs ERD Mermaid实体"""
    sql = ""
    for fn in sorted(os.listdir(MIGRATIONS_DIR)):
        if fn.endswith(".sql"):
            sql += read(os.path.join(MIGRATIONS_DIR, fn)) + "\n"
    ddl_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", sql, re.I))
    erd = read(ERD_MD)
    # Mermaid实体名: 行首大写单词后跟 { 
    erd_entities = set(re.findall(r"^\s*(\w+)\s*\{", erd, re.M))
    erd_entities = {e for e in erd_entities if e and not e.startswith(("erDiagram",))}
    # DDL用snake_case, ERD用camelCase——做粗略匹配：小写后比较
    ddl_lower = {t.lower() for t in ddl_tables}
    erd_lower = {e.lower() for e in erd_entities}
    missing_in_erd = ddl_lower - erd_lower - {"schema_version"}  # schema_version内部表可不进ERD
    if missing_in_erd:
        return fail(f"C2 DDL有表但ERD Mermaid无: {missing_in_erd}")
    ok(f"C2 表覆盖对齐 ({len(ddl_lower)}表)")
    return 0

def check_c3_params():
    """migrations param 种子键 vs docs/01 §6 参数键

    2026-11-03 起改用 param_ledger 的统一解析：迁移有四种写法
    （`INSERT OR IGNORE` / 裸 `INSERT` / `ON CONFLICT DO UPDATE` / `UPDATE`），
    旧版只匹配第一种，导致 0018 的 3 个种子、0025/0026 的 12 个注入项全被漏掉
    （旧版一直报「26键」，真值是 40）。
    """
    eff, dropped, _ = param_ledger.scan_migrations()
    keys = set(eff)
    if not keys:
        return fail("C3 找不到 param 种子")
    req = read(REQ_MD)
    doc_keys = set(re.findall(r"`([\w.]+)`", req))
    missing = keys - doc_keys
    if missing:
        return fail(f"C3 参数键DDL有但docs/01§6无: {missing}")
    ok(f"C3 参数键对齐 ({len(keys)}键，已删死参数 {len(dropped)} 键)")
    return 0


def check_c6_ledger():
    """docs/44 生成区台账 vs 代码实读键+迁移生效值（2026-11-03 新增）。

    动机：参数清单一直靠人维护，历史上一度出现 26/38/39 三个口径同时存在。
    C6 把「基线包含哪些参数、值是多少」钉成机器可校验的事实。
    """
    led = param_ledger.build()
    doc_path = doc44_path()
    if not os.path.exists(doc_path):
        return fail(f"C6 找不到 {doc_path}")
    doc = param_ledger.extract_doc_rows(read(doc_path))
    live = {r["key"]: r["value"] for r in led["rows"]}
    missing = sorted(set(live) - set(doc))
    extra = sorted(set(doc) - set(live))
    diff = sorted(k for k in set(live) & set(doc) if str(live[k]) != str(doc[k]))
    if missing:
        return fail(f"C6 docs/44 漏 {len(missing)} 键: {', '.join(missing)}"
                    f"  → 跑 python scripts/param_ledger.py --write")
    if extra:
        return fail(f"C6 docs/44 多 {len(extra)} 键（代码不读）: {', '.join(extra)}"
                    f"  → 跑 python scripts/param_ledger.py --write")
    if diff:
        detail = "; ".join(f"{k}: 生效={live[k]} docs={doc[k]}" for k in diff)
        return fail(f"C6 参数值漂移 → {detail}"
                    f"  → 跑 python scripts/param_ledger.py --write")
    ok(f"C6 参数台账对齐 ({len(live)}键 = 代码实读，docs/44 与迁移生效值一致)")
    return 0


def doc44_path():
    if os.path.exists(DOC44_MD):
        return DOC44_MD
    # 允许用另一个根目录跑（CI/子目录场景）
    return os.path.join(param_ledger.ROOT, "docs", "44-new-baseline-config.md")

def check_c4_schedule():
    """scheduler.go Schedule/WeeklyClock vs docs/01 §4.1/§4.3 时刻表
    时刻集中一处管理：§4 为唯一事实源，本项锁定代码与文档同步（顺序+时刻+任务名全等）。
    """
    src = read(SCHEDULER_GO)
    m = re.search(r"var Schedule = \[\]TaskTime\{(.*?)\n\}", src, re.S)
    if not m:
        return fail("C4 scheduler.go 找不到 Schedule 表")
    go_pairs = re.findall(r'\{\s*"(\d{2}:\d{2})"\s*,\s*"(\w+)"\s*\}', m.group(1))
    if not go_pairs:
        return fail("C4 scheduler.go Schedule 表解析为空")
    wm = re.search(r'const WeeklyClock\s*=\s*"(\d{2}:\d{2})"', src)
    if not wm:
        return fail("C4 scheduler.go 找不到 WeeklyClock 常量")
    req = read(REQ_MD)
    sec1 = re.search(r"### 4\.1 .*?\n(.*?)(?:### 4\.2|## )", req, re.S)
    if not sec1:
        return fail("C4 docs/01 找不到 §4.1 小节")
    doc_pairs = re.findall(r"^\|\s*(\d{2}:\d{2})\s*\|\s*`(\w+)`", sec1.group(1), re.M)
    rc = 0
    # 任务时刻序列须顺序、时刻、任务名全等
    if [list(p) for p in go_pairs] != [list(p) for p in doc_pairs]:
        rc |= fail(f"C4 §4.1 时刻表漂移\n  scheduler.go: {go_pairs}\n  docs/01 §4.1:   {doc_pairs}")
    sec3 = re.search(r"### 4\.3 .*?\n(.*?)(?:### 4\.4|## )", req, re.S)
    wdoc = re.findall(r"^\|\s*(\d{2}:\d{2})\s*\|\s*`weekly`", sec3.group(1), re.M) if sec3 else []
    if not wdoc:
        rc |= fail("C4 docs/01 §4.3 找不到 weekly 时刻行")
    elif wdoc[0] != wm.group(1):
        rc |= fail(f"C4 weekly 时刻漂移: scheduler.go={wm.group(1)} docs/01§4.3={wdoc[0]}")
    if rc == 0:
        ok(f"C4 时刻表对齐 ({len(go_pairs)}任务 + weekly {wm.group(1)})")
    return rc

def check_c7_research_discipline():
    """C7 研究窗口纪律 + 口径横幅完整性（docs/55 §6.1 / §6.4，2026-11-05 新增）。

    两条都是「会静默失效」的规则，所以必须机检：
    ① 窗口纪律：2019+ 不解冻 → 任何脚本把判据窗口端点硬编码到 2018 之后即违约
       （docs/55 §6.4）。只查窗口型变量/参数，不查注释里的「2026-11-03 定案」这类日期。
    ② 口径横幅：raw 口径报告必须带「已作废」横幅（docs/55 §6.1）。报告由 e17_*.py
       生成、重生成即回退，靠人记必漏 → 交给 apply_caliber_banner.py 统一施加。
    """
    rc = 0
    bad = []
    for name in sorted(os.listdir(SCRIPTS_DIR)):
        if not name.endswith(".py") or name in WINDOW_WHITELIST:
            continue
        path = os.path.join(SCRIPTS_DIR, name)
        if not os.path.exists(path):
            continue
        text = read(path)
        for m in WINDOW_PAT.finditer(text):
            if int(m.group(2)) > WINDOW_MAX_YEAR:
                line = text[: m.start()].count("\n") + 1
                bad.append(f"{name}:{line} {m.group(0)}")
    if bad:
        detail = "; ".join(bad[:6]) + (" …" if len(bad) > 6 else "")
        rc = fail(f"C7 判据窗口越界 {len(bad)} 处（2019+ 不解冻）→ {detail}"
                  f"  → 见 docs/55 §6.4")
    else:
        ok(f"C7 判据窗口 ≤{WINDOW_MAX_YEAR}-12-31（{len(WINDOW_WHITELIST)} 个生产脚本白名单）")

    # 允许用另一个根目录跑（CI/子目录场景）：横幅清单的目录随之切换
    apply_caliber_banner.EXP = pathlib.Path(ROOT) / "docs" / "experiments"
    miss = apply_caliber_banner.missing_banners()
    if miss:
        rc = fail(f"C7 口径横幅缺失 {len(miss)} 份 → {', '.join(miss[:6])}"
                  f"{' …' if len(miss) > 6 else ''}"
                  f"  → 跑 python scripts/apply_caliber_banner.py")
    else:
        ok(f"C7 口径横幅齐全（{len(apply_caliber_banner.TARGETS)} 份 raw 报告族）")
    return rc


def main():
    rc = 0
    print("== Darvas-box-trading 一致性检查 ==")
    rc |= check_c1_enums()
    rc |= check_c2_tables()
    rc |= check_c3_params()
    rc |= check_c4_schedule()
    rc |= check_c6_ledger()
    rc |= check_c7_research_discipline()
    print("=" * 40)
    print("结果:", "全部一致 ✓" if rc == 0 else "存在漂移 ✗")
    sys.exit(rc)

if __name__ == "__main__":
    main()
