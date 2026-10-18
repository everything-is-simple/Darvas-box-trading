-- 0006: 模拟撮合费用参数（M8.2，2026-10-19）
-- 动机：M8.2 模拟撮合器需要 A股摩擦费用（docs/08-m8.2-matcher.md §3.6）。
-- 初版用当前费率常数，全部可配（历史费率变化列为已知偏差，校准时可临时覆盖）。
-- 规则：append-only，见 docs/06-consistency.md；同步 docs/01 §6 参数表。

INSERT OR IGNORE INTO param(key, value) VALUES
    ('fee.commission_rate', '0.00025'),  -- 佣金万2.5，双向
    ('fee.commission_min',   '5'),       -- 单笔最低佣金（元）
    ('fee.stamp_tax',        '0.001'),   -- 印花税千1，卖出单边
    ('fee.transfer_rate',    '0.00001'); -- 过户费万0.1，双向（简化，沪深同率）
