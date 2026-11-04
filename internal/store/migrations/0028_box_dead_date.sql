-- 0028: box 表加 dead_date 列（死亡日，docs/49 §五之二 特征一）
-- 背景：箱体生命周期原本靠 status + broken_date 隐式表达「死亡」，无独立字段。
--   broken_date 一个字段既记「向上突破」（broken_up，新生命周期开始）又记「向下破位」
--   （broken_down，箱体作废），死亡语义混在里面。
-- 本迁移（最小侵入，2026-11-04 用户拍板）：
--   - 新增 dead_date DATE，记箱体「死亡日」= broken_down（向下破位）或 invalid（宽度不足作废）当天；
--   - broken_date 语义**保持不动**（仍同时承载 broken_up/broken_down 事件日期），
--     以免破坏 regime 牛熊识别（LabelBoxes 读内存 BrokenDate）与 BreadthAgg 市场宽度法
--     （读 DB broken_date 列）这两处下游。
-- 语义承诺补齐（docs/49 §五之二 特征一）：
--   出生(bottom_confirmed) → 突破(broken_date, broken_up) → 死亡(dead_date) → 卖出(closed_date) → 继任(prev_box_id)。
-- 注：broken_up 箱体也有「死亡」（被下一箱体取代），但其生命周期终点即「突破日」，无需额外死亡日，
--   故 dead_date 只在 broken_down / invalid 两种「非突破性终结」时写值。

ALTER TABLE box ADD COLUMN dead_date DATE; -- 死亡日：broken_down / invalid 当天；NULL=未死亡

CREATE INDEX IF NOT EXISTS idx_box_dead_date ON box(dead_date);
