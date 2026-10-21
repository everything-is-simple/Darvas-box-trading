-- 0013: 进场跳空上限（2026-10-21 P0-2 落地）
-- 动机：match.go 成交规则「跳空即以开盘价成交」无追高上限，导致回测 71 笔买腿中
--       63.4% 高于计划买点 >5%、中位 +7.35%、8 笔超 +50%；五年利润 171% 来自 2 笔
--       跳空追高成交（docs/18 P0-2）。达瓦斯买的是「突破点+3%」，不是突破后追高。
-- 落地：
--   ① match 撮合买入单时，若 成交价/trigger - 1 > max_entry_gap，弃单并记 skipped_gap；
--   ② planner 生成买单前，若 trigger 与现价偏离超上限，拒绝生成并告警。
-- 参数：max_entry_gap 为「成交价允许超出触发价的比例上限」，默认 1.05（+5%）。
-- 同步：docs/01 §6、docs/04 §7.5、docs/19 P0-2。

INSERT OR IGNORE INTO param (key, value) VALUES ('match.max_entry_gap', '1.05');
