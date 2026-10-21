-- 0003: 跟踪池淘汰生命周期（B2，原书校对补全 2026-10-18）
-- 动机：原书四条池淘汰规则此前未实现——①太晚规则（screener 拦截）②两亏拉黑
--       ③条件挂起（"停传报价突破X再传"的A股等价）④跟踪5-8支上限；另有⑤不获利持仓
--       3周约束（planner 层）。watch 状态机文档宣称的 pending/holding 迁移（M4）也未接线。
-- 规则：append-only，见 docs/06-consistency.md；同步更新 docs/03-erd.md + docs/04 + docs/01。

-- 1) watch 表重建：stage 扩六值 + resume_trigger 列
--    （SQLite 无法 ALTER CHECK 约束，须重建表；watch 无被引用 FK，DROP 安全）
CREATE TABLE watch_rebuild (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL REFERENCES security(code),
    candidate_id       INTEGER REFERENCES candidate(id),
    added_date         DATE NOT NULL,
    stage              TEXT NOT NULL DEFAULT 'watching'
                       CHECK (stage IN ('watching','pending','holding','exited','suspended','blacklisted')),
    current_box_id     INTEGER REFERENCES box(id),
    box_count          INTEGER NOT NULL DEFAULT 0,
    exit_reason        TEXT,
    resume_trigger     REAL,                          -- 条件挂起激活价（stage=suspended 时有效）
    note               TEXT NOT NULL DEFAULT '',
    updated_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);
INSERT INTO watch_rebuild(id, code, candidate_id, added_date, stage, current_box_id,
                          box_count, exit_reason, resume_trigger, note, updated_at)
    SELECT id, code, candidate_id, added_date, stage, current_box_id,
           box_count, exit_reason, NULL, note, updated_at
    FROM watch;
DROP TABLE watch;
ALTER TABLE watch_rebuild RENAME TO watch;

CREATE INDEX IF NOT EXISTS idx_watch_stage ON watch(stage);
CREATE INDEX IF NOT EXISTS idx_watch_code ON watch(code);

-- 2) position 落库盈亏（两亏拉黑的统计基础；清仓时由 TradeRepo.ClosePosition 计算写入）
ALTER TABLE position ADD COLUMN realized_pnl REAL;

-- 3) 新参数（同步 docs/01 §6 参数表）
INSERT OR IGNORE INTO param(key, value) VALUES
    ('pool.maxwatch', '8'),        -- ⚠️ 2026-10-21 降级软参考：原书 L1074「节省电报费」倒逼，系统无此成本；不再拒绝入池，仅日志告警。资金预算（单位制）是唯一约束
    ('pool.maxloss', '2'),         -- 两亏拉黑阈值（同一股票亏损交易满N次→永久拉黑）
    ('screen.latedays', '4'),      -- 太晚规则：突破后第N个交易日（原书：四天收在突破点上方=太晚，放弃）
    ('position.staledays', '15');  -- 不获利持仓约束：开仓后N个交易日仍不获利→退出（原书3周）
