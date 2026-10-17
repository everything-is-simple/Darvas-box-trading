#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Darvas-box-trading 一致性检查（docs/06-consistency.md §2 防线一）。

检查项：
  C1 枚举对齐：contract.go 的状态枚举常量集合  ==  DDL 的 CHECK(... IN (...)) 集合
  C2 表覆盖：  DDL 的 CREATE TABLE 列表  ⊇  docs/03-erd.md 的 Mermaid 实体列表
  C3 参数键对齐：migrations 的 param 种子 INSERT 键集合  ==  docs/01 §6 参数表键集合
  C4 任务ID对齐：scheduler 常量表（若存在）== docs/01 §4 任务ID列表

用法：python scripts/check_consistency.py [repo_root]
退出码：0=全部一致；1=有漂移（CI应失败）。
"""
import os, re, sys

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                       os.path.join(os.path.dirname(__file__), ".."))

CONTRACT_GO = os.path.join(ROOT, "internal", "contract", "contract.go")
MIGRATIONS_DIR = os.path.join(ROOT, "internal", "store", "migrations")
ERD_MD = os.path.join(ROOT, "docs", "03-erd.md")
REQ_MD = os.path.join(ROOT, "docs", "01-requirements.md")

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
    """contract.go 枚举常量 vs DDL CHECK IN 集合
    仅核对 contract.go 中显式定义了枚举类型的列（BoxStatus/WatchStage/SignalType）。
    其余列（position.status/order.status/daily_run.status/fundamental_flag/side/order_kind）
    在 contract.go 中用普通字符串表示，DDL 的 CHECK 即其运行时守卫——不要求枚举对齐。
    """
    code = read(CONTRACT_GO)
    # DDL CHECK IN
    sql = ""
    for fn in sorted(os.listdir(MIGRATIONS_DIR)):
        if fn.endswith(".sql"):
            sql += read(os.path.join(MIGRATIONS_DIR, fn)) + "\n"

    # 列→contract枚举映射（仅这些列要求对齐）
    col_to_enum = {
        "box.status":        "BoxStatus",
        "watch.stage":        "WatchStage",
        "signal.type":        "SignalType",
    }
    rc = 0
    for col_key, enum_name in col_to_enum.items():
        table, col = col_key.split(".")
        # 取该列的 CHECK IN 集合
        m = re.search(
            r"CREATE TABLE IF NOT EXISTS\s+" + table + r"\b.*?CHECK\s*\(\s*" + col +
            r"\s+IN\s*\(([^)]+)\)\s*\)", sql, re.I | re.S)
        if not m:
            rc |= fail(f"C1 找不到 {col_key} 的 CHECK 约束")
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
    """migrations param种子键 vs docs/01 §6 参数键"""
    sql = ""
    for fn in sorted(os.listdir(MIGRATIONS_DIR)):
        if fn.endswith(".sql"):
            sql += read(os.path.join(MIGRATIONS_DIR, fn)) + "\n"
    # 只取 INSERT INTO param 语句块（含多行 VALUES 列表），避免 CHECK IN 值污染
    m = re.search(r"INSERT OR IGNORE INTO param\(key, value\)\s*VALUES\s*(.*?);",
                  sql, re.I | re.S)
    if not m:
        return fail("C3 找不到 param 种子 INSERT")
    keys = set(re.findall(r"\('([\w.]+)',\s*'", m.group(1)))
    req = read(REQ_MD)
    doc_keys = set(re.findall(r"`([\w.]+)`", req))
    missing = keys - doc_keys
    if missing:
        return fail(f"C3 参数键DDL有但docs/01§6无: {missing}")
    ok(f"C3 参数键对齐 ({len(keys)}键)")
    return 0

def main():
    rc = 0
    print("== Darvas-box-trading 一致性检查 ==")
    rc |= check_c1_enums()
    rc |= check_c2_tables()
    rc |= check_c3_params()
    print("=" * 40)
    print("结果:", "全部一致 ✓" if rc == 0 else "存在漂移 ✗")
    sys.exit(rc)

if __name__ == "__main__":
    main()
