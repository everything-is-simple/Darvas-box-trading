# -*- coding: utf-8 -*-
"""清理 perf_test.db 中非A股存量箱体（B股/沪B/指数）。

安全前检查：
  1. box.prev_box_id 自引用（阶梯链）——B股箱体之间可能互相引用，
     先解除外键再 DELETE 避免循环引用失败；
  2. 先用 VACUUM INTO 做时间点备份；
  3. 预查删除范围行数，与执行结果对比；
  4. 执行后 VACUUM 回收空间（WAL+VACUUM 安全）。
"""
import sqlite3, os, sys, time

DB = "var/perf_test.db"
BACKUP = f"var/backup_pre_cleanup_{time.strftime('%Y%m%d_%H%M%S')}.db"

if not os.path.exists(DB):
    sys.exit(f"源库不存在: {DB}")

# ---------- 1. 预查删除范围 ----------
s = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
total_box = s.execute("SELECT COUNT(*) FROM box").fetchone()[0]
b_codes = [
    ("B股sz200", "sz200%"),
    ("沪Bsh900", "sh900%"),
    ("指数sh000", "sh000%"),
    ("指数sz399", "sz399%"),
]
print(f"=== 清理前 box 表总量: {total_box} ===")
to_delete = 0
for label, pat in b_codes:
    n = s.execute(f"SELECT COUNT(*) FROM box WHERE code LIKE '{pat}'").fetchone()[0]
    to_delete += n
    print(f"  {label} ({pat}): {n} 行")

# 外键引用检查：是否有 A 股箱体 prev_box_id 指向 B 股箱体
cross = s.execute("""
    SELECT COUNT(*) FROM box b
    JOIN box p ON b.prev_box_id = p.id
    WHERE p.code LIKE 'sz200%' OR p.code LIKE 'sh900%'
       OR p.code LIKE 'sh000%' OR p.code LIKE 'sz399%'
""").fetchone()[0]
s.close()
print(f"\n  → 合计待删: {to_delete} 行；A股→B股外键交叉引用: {cross}")

# ---------- 2. VACUUM INTO 备份 ----------
print(f"\n=== VACUUM INTO → {BACKUP} ===")
tgt = sqlite3.connect(BACKUP)
src = sqlite3.connect(DB)
src.backup(tgt)
src.close(); tgt.close()
sz = os.path.getsize(BACKUP)
print(f"  备份完成, {sz:,} bytes")

# ---------- 3. 执行清理 ----------
print(f"\n=== 执行清理 ===")
db = sqlite3.connect(DB)
db.execute("PRAGMA foreign_keys = OFF")
db.execute("PRAGMA journal_mode = WAL")

# 先清空所有指向待删箱体的 prev_box_id（A股也可能指到 B 股历史箱体）
db.execute("""
    UPDATE box SET prev_box_id = 0 WHERE prev_box_id IN (
        SELECT id FROM box WHERE code LIKE 'sz200%' OR code LIKE 'sh900%'
           OR code LIKE 'sh000%' OR code LIKE 'sz399%'
    )
""")
print(f"  已解除 prev_box_id 引用")

for label, pat in b_codes:
    cur = db.execute(f"DELETE FROM box WHERE code LIKE '{pat}'")
    print(f"  DELETE {label}: {cur.rowcount} 行")

db.commit()

# ---------- 4. 清理后复核 ----------
remaining = db.execute("SELECT COUNT(*) FROM box").fetchone()[0]
print(f"\n=== 清理后 box 表: {remaining} 行（删 {total_box-remaining}）===")
print(f"  清理效率: {(total_box-remaining)/total_box*100:.1f}%")

# 验证非A股残留
for label, pat in b_codes:
    n = db.execute(f"SELECT COUNT(*) FROM box WHERE code LIKE '{pat}'").fetchone()[0]
    mark = "✅" if n == 0 else "⚠️ 残留!"
    print(f"  {label}: {n} {mark}")

db.close()
print(f"\n✅ 完成。时间点备份: {BACKUP}")
