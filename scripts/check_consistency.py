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
  C8 数字对账：docs/55 锚点区 == 锚点快照 == 账本重算；归档 48/60/README 引用的数字必须在册（2026-11-06 新增）
  C9 死链检查：docs/**/*.md 的 md 链接、反引号路径、`NN §x` 章节引用必须可达（2026-11-06 新增）

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


def check_c8_anchors():
    """数字对账（2026-11-06 新增，docs/55 §11.10）。

    链：账本 DB → docs/anchors/ledger_anchors.json（快照）→ docs/55 生成区 → 48/60/README 引用。
    三段各查一段，缺哪段都会有数字悄悄失真而不报错：
      ① docs/55 生成区 == 快照（防手改文档）
      ② 快照 == 账本重算（防快照与库脱钩；库不在则跳过并明示）
      ③ 跨文档「引基线收官数字」的句子逐数字在册（防手抄/口头改数）
    """
    import doc_anchors
    doc_anchors.ROOT = pathlib.Path(ROOT)
    doc_anchors.DOC55 = pathlib.Path(ROOT) / "docs" / "55-ledger-and-lessons.md"
    doc_anchors.SNAPSHOT = pathlib.Path(ROOT) / "docs" / "anchors" / "ledger_anchors.json"

    snap = doc_anchors.load_snapshot()
    if not snap:
        return fail("C8 锚点快照缺失 → 跑 python scripts/doc_anchors.py --write")

    rc = 0
    bad = doc_anchors.check_doc_vs_snapshot(snap)
    if bad:
        rc |= fail(f"C8 docs/55 锚点区漂移 {len(bad)} 处 → {'; '.join(bad[:4])}"
                   f"  → 跑 python scripts/doc_anchors.py --write")
    else:
        ok(f"C8 docs/55 锚点区 == 快照（{len(snap['values'])} 键）")

    db_bad, ran = doc_anchors.check_vs_db(snap)
    if not ran:
        print(f"  ℹ C8 账本不在（{snap.get('db')}），跳过重算——只校了文档↔快照")
    elif db_bad:
        rc |= fail(f"C8 快照与账本重算不一致 {len(db_bad)} 处 → {'; '.join(db_bad[:4])}"
                   f"  → 跑 python scripts/doc_anchors.py --write")
    else:
        ok(f"C8 快照 == 账本重算（边界 {snap['cut']}，引擎 {str(snap.get('engine_sha256'))[:12]}）")

    quotes = doc_anchors.check_quotes(snap)
    if quotes:
        rc |= fail(f"C8 跨文档未在册数字 {len(quotes)} 处 → {'; '.join(quotes[:3])}"
                   f"  → 改文档或补登锚点后 --write")
    else:
        ok(f"C8 跨文档引用在册（{len(doc_anchors.CROSS_DOCS)} 份）")

    near = doc_anchors.check_near_miss(snap)
    if near:
        rc |= fail(f"C8 疑似抄错 {len(near)} 处 → {'; '.join(near[:3])}"
                   f"  → 改回在册值或 --write 重算")
    else:
        ok(f"C8 全现役文档无近似错值（同形状 ±{doc_anchors.NEAR_REL:.1%} 阈值）")
    return rc


# ── C9 死链检查 ─────────────────────────────────────────────────────
MD_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\s]+?)(?:\s+\"[^\"]*\")?\)")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
PATHISH = re.compile(r"^(docs|scripts|internal|cmd)/[\w./-]+\.(md|py|go|sql|json|ps1|bat|html|csv|txt|yaml)$")
DOC_SHORT = re.compile(r"^docs/(\d{2})$")
SEC_REF = re.compile(
    r"(?:(?:`|docs/)?(\d{2})`?\s*(?:的\s*)?)?§\s*(\d+(?:\.\d+)*|[一二三四五六七八九十]+)")
HEAD_ARA = re.compile(r"^#{2,4}\s*§?\s*(\d+(?:\.\d+)*)")
HEAD_CN = re.compile(r"^#{2,4}\s*§?\s*([一二三四五六七八九十]+)\s*[、.·]")
HEAD_DI = re.compile(r"^#{2,4}\s*第\s*(\d+(?:\.\d+)*)\s*条")
FENCE = re.compile(r"^```.*?^```", re.S | re.M)


def _doc_index():
    """docs/NN-*.md → {NN: path}；archive 里的同号文档作为兜底。

    2026-11-09 治理：同号归档可能有多份（如 48 既有 11-05 旧稿、又有 11-09 整份归档）。
    旧实现按文件名排序覆盖，会把 `48 §十一` 解析到不含该节的旧稿，产生假死链。
    现改为「同号取文件名日期最新者」——整份归档稿才是历史章节引用的正确落点。
    """
    idx, archived, stamp = {}, {}, {}
    for p in sorted((pathlib.Path(ROOT) / "docs").glob("*.md")):
        m = re.match(r"(\d{2})-", p.name)
        if m:
            idx[m.group(1)] = p
    adir = pathlib.Path(ROOT) / "docs" / "archive"
    if adir.exists():
        for p in sorted(adir.glob("*.md")):
            m = re.match(r"(\d{2})-", p.name)
            if not m or m.group(1) in idx:
                continue
            num = m.group(1)
            d = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
            key = (d.group(1) if d else "", p.name)
            if num not in stamp or key > stamp[num]:
                stamp[num] = key
                archived[num] = p
    return idx, archived


def _cn2arabic(text):
    """把「六」「十一」这类中文序号归一成阿拉伯数字串；无法解析则原样返回。

    44 用「## 六、」而 60 引 `44 §6`；48 用「## 五、」而 55 引 `48 §五`——
    不归一就会把「存在」误报成「不存在」。
    """
    if not text:
        return text
    d = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
         "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if re.fullmatch(r"\d+(\.\d+)*", text):
        return text
    if "十" in text:
        a, _, b = text.partition("十")
        tens = d.get(a, 1) if a else 1
        ones = d.get(b, 0) if b else 0
        return str(tens * 10 + ones)
    if len(text) == 1 and text in d:
        return str(d[text])
    return text


def _sections(path):
    got = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        for pat in (HEAD_ARA, HEAD_CN, HEAD_DI):
            m = pat.match(line)
            if m:
                got.add(_cn2arabic(m.group(1)))
    return got


def scan_deadlinks():
    """返回死链清单（供 check 与调试/导出共用）。"""
    docs = pathlib.Path(ROOT) / "docs"
    # 只查现役文档（docs/*.md）。docs/archive 与 docs/experiments 是冻结稿：
    # 归档稿按原样保留历史编号与旧路径，实验报告由脚本重生成（禁手改），
    # 对它们查「今天的路径」只会产生噪声。现役文档才是导航入口，必须零死链。
    files = sorted(p for p in docs.glob("*.md"))
    idx, archived = _doc_index()
    sec_cache = {}
    problems = []

    def sections(num):
        p = idx.get(num) or archived.get(num)
        if p is None:
            return None
        if p not in sec_cache:
            sec_cache[p] = _sections(p)
        return sec_cache[p]

    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        body = FENCE.sub(" ", text)          # 代码块里的示例链接不算
        rel = f.relative_to(pathlib.Path(ROOT)).as_posix()
        is_archive = "docs/archive/" in rel

        # ① markdown 链接
        for m in MD_LINK.finditer(body):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            if not (f.parent / target).exists():
                problems.append(f"{rel}: 链接目标不存在 → {target}")

        # ② 反引号里的仓库路径（只查代码/文档产物；var/ 是运行产物，不入库不查）
        for m in CODE_SPAN.finditer(body):
            token = m.group(1).strip().split()[0] if m.group(1).strip() else ""
            if not token or "*" in token or "<" in token or token.endswith("/"):
                continue
            if DOC_SHORT.match(token):
                if token.split("/")[1] not in idx:
                    problems.append(f"{rel}: 反引号路径不存在 → {token}")
                continue
            if PATHISH.match(token) and not (pathlib.Path(ROOT) / token).exists():
                problems.append(f"{rel}: 反引号路径不存在 → {token}")

        # ③ `NN §x` 章节引用（归档稿冻结，只查链接不查章节）
        if is_archive:
            continue
        for m in SEC_REF.finditer(body):
            num, sec = m.group(1), m.group(2)
            if not num:
                continue
            # 「原 55 §3」「原引 docs/23 §9」是明确的历史引用（编号已归档/文档已删），不查
            ctx = body[max(0, m.start() - 6):m.start()]
            if "原" in ctx and "原始" not in ctx:
                continue
            got = sections(num)
            sec_n = _cn2arabic(sec)
            if got is None:
                problems.append(f"{rel}: 引用了不存在的文档 {num}（§{sec}）")
            elif sec_n not in got:
                problems.append(f"{rel}: {num} 无 §{sec} 章节")
    return problems


def check_c9_deadlinks():
    """死链检查（2026-11-06 新增）：链接目标 / 反引号路径 / `NN §x` 章节引用必须可达。

    为什么机检：文档治理时改文件名、归档旧稿、重编号，都会留下「点进去 404」或
    「`55 §11.9` 指向已不存在的节」——读者不会报错，只会读到过期内容。
    """
    problems = scan_deadlinks()
    if problems:
        detail = "; ".join(problems[:6]) + (f" …（共 {len(problems)} 处）" if len(problems) > 6 else "")
        return fail(f"C9 死链 {len(problems)} 处 → {detail}")
    n = len(list((pathlib.Path(ROOT) / "docs").glob("*.md")))
    ok(f"C9 死链 0（现役 {n} 份 md：链接 / 反引号路径 / 章节引用；archive+experiments 冻结不检）")
    return 0


def main():
    rc = 0
    print("== Darvas-box-trading 一致性检查 ==")
    rc |= check_c1_enums()
    rc |= check_c2_tables()
    rc |= check_c3_params()
    rc |= check_c4_schedule()
    rc |= check_c6_ledger()
    rc |= check_c7_research_discipline()
    rc |= check_c8_anchors()
    rc |= check_c9_deadlinks()
    print("=" * 40)
    print("结果:", "全部一致 ✓" if rc == 0 else "存在漂移 ✗")
    sys.exit(rc)

if __name__ == "__main__":
    main()
