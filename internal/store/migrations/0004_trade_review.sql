-- 0004: 已交易股票历史库（B3，原书校对补全 2026-10-18）
-- 动机：达瓦斯自称全书最重要的内容——对每笔已完成的交易做记录，避免重复犯错。
--       三记录制度：①买入理由（plan_order.reason_json 六问答案，M4 已有）
--                   ②卖出理由（position.close_reason + realized_pnl，0003 已有）
--                   ③亏损原因/教训（本表，人工复盘录入——不引入自动基本面模型）。
-- 再纳入闭环：poolsync 入池时查该股历史战绩，一亏未拉黑者再入池 → watch.note 留痕提示。
-- 规则：append-only，见 docs/06-consistency.md；同步 docs/03-erd.md + docs/04 §7 + docs/00 §6。

CREATE TABLE IF NOT EXISTS trade_review (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id    INTEGER NOT NULL UNIQUE REFERENCES position(id), -- 每笔交易一条复盘（重录覆盖）
    code           TEXT NOT NULL REFERENCES security(code),
    rule_followed  TEXT NOT NULL DEFAULT '',   -- 规则执行评价（followed/violated/自由文本）
    error_type     TEXT NOT NULL DEFAULT '',   -- 错误类型（追高/不止损/过早止盈/情绪化…自由文本）
    lesson         TEXT NOT NULL DEFAULT '',   -- 教训（人工复盘录入，原书核心）
    created_at     DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_trade_review_code ON trade_review(code);
