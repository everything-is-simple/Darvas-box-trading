-- 0027: 基线身份 —— 建 baseline 表（F9 的补法，docs/49 §四）
--
-- 动机（docs/49 §四，2026-11-04 用户拍板，阶段 B）：
--   当前"同一套账本换了参数根本认不出来"。重建前必须先有身份，否则 C（连续单库）
--   /D（断点续跑）/E（日追加）跨年续跑时"续错了都不知道"。基线身份是它们共同的
--   "对账前提"。
--
-- 三样身份（docs/49 §四）：
--   1. 参数指纹 param_fingerprint：param 表跑完迁移后，把 (key, value) 排序序列做 sha256。
--   2. baseline_id：每次重建生成（如 2026-11-04-newhigh-ladder-v1）。
--   3. 引擎 hash engine_sha256：跑出这批账本的二进制（darvas_base.exe / darvas.exe）的
--      sha256，呼应 HARD①（跨二进制不可横比，必须先有身份才能谈对账）。
--
-- 为什么只有 DDL、没有 INSERT：
--   现代 SQLite（modernc.org/sqlite，纯 Go 驱动）没有内置 sha256，且本工程未注册任何
--   自定义 SQL 函数——参数指纹的 sha256 无法在纯 SQL 里算，引擎 hash 也必须在 Go 侧
--   读 os.Executable() 才算得出。故本迁移只负责建表结构；指纹/hash 的计算与写入由
--   store.RecordBaseline()（Go 侧，crypto/sha256 标准库）在迁移跑完后调用完成。
--
-- 语义：
--   · 一张 baseline 表 = 一次重建的身份锚，同库多次重建则多行（latest=1 标记最新）。
--   · param_fingerprint / engine_sha256 是 64 位十六进制小写（sha256）。
--   · source_hash：源库（full_hist / 上一期账本）的 sha256，串起"这份账本从哪来"。
--   · params_json：参数快照（key:value 排序 JSON），即使 param 表后续被改也能回溯
--     本次重建时的参数全貌。
--   · engine_version：与 cmd/darvas/main.go 的 const version 对应（0.5.0-m8），
--     sha256 之外的语义版本锚。

CREATE TABLE IF NOT EXISTS baseline (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    baseline_id         TEXT    NOT NULL UNIQUE,      -- 如 2026-11-04-newhigh-ladder-v1
    engine_version      TEXT    NOT NULL,             -- 语义版本 0.5.0-m8
    engine_sha256       TEXT    NOT NULL,             -- 跑出账本的二进制 sha256（HARD① 锚）
    source_hash         TEXT,                         -- 源库 sha256（从哪来）
    param_fingerprint   TEXT    NOT NULL,             -- (key,value) 排序序列的 sha256
    params_json         TEXT    NOT NULL,             -- 参数快照（key:value 排序 JSON）
    created_at          DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    latest              INTEGER NOT NULL DEFAULT 0    -- 1 = 本库最新一次重建
);

CREATE INDEX IF NOT EXISTS idx_baseline_latest ON baseline(latest);
