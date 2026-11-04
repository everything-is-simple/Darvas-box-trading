-- 0029: broken_date 语义收紧 —— 只表「向上突破」，破位只记 dead_date
-- 依据：docs/49 §五之二 特征一（用户 2026-11-04 明确要求「broken_date → 保留为向上突破日」）
--
-- 0028 只加了 dead_date 列，但引擎在 broken_down 分支同时写 broken_date 和 dead_date，
-- 导致 54.4% 的箱体在 broken_date 里存的其实是死亡日（实测 57,046/57,046 两列同值）。
-- 本迁移把存量数据的破位日从 broken_date 清掉，只留 dead_date：
--   broken_up   → broken_date 有值、dead_date 为空
--   broken_down → broken_date 为空、dead_date 有值
--   invalid     → 两列皆空 broken_date、dead_date 有值
--
-- 只清「dead_date 已存在」的行：0028 之前的老库 dead_date 全 NULL，
-- 若一并清空会永久丢失破位日期，故保留其原值（BreadthAgg 用 COALESCE 兜底）。
--
-- 下游一致性：BreadthAgg 已改为按状态取列（broken_up→broken_date，broken_down→dead_date），
-- 因 T 日两列同值，切换前后聚合结果完全一致；regime.LabelBoxes 读内存且按 status 分流；
-- signal/screener 均显式要求 status='broken_up'。无其他裸读依赖。

UPDATE box
   SET broken_date = NULL
 WHERE status = 'broken_down'
   AND dead_date IS NOT NULL;
