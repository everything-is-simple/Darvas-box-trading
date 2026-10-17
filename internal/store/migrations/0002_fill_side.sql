-- 0002: trade_fill 增加 side 列（M4.5 成交解析）
-- 动机：账本重算/审计需要每笔成交的买卖方向，不再依赖 plan_order 反查
--       或启发式推断（计划订单之外的成交也能正确入账）。
-- 规则：append-only，见 docs/06-consistency.md；同步更新 docs/03-erd.md。

ALTER TABLE trade_fill ADD COLUMN side TEXT NOT NULL DEFAULT '';

-- 存量数据回填：有关联订单的取订单方向
UPDATE trade_fill SET side = COALESCE(
    (SELECT po.side FROM plan_order po WHERE po.id = trade_fill.plan_order_id), 'buy');

CREATE INDEX IF NOT EXISTS idx_fill_date ON trade_fill(date);
