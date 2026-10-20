-- 0011: daily_bar(code, date) 复合索引（2026-10-20 M8.8 回测性能修复）
-- 动机：daily_bar 原本只有 date 单列索引，M8.8 新增的两条每日常规查询——
--       regime.ClassifyDate 的 BarsRange(sh000001) 与 BoxRepo.BreadthAgg 的
--       first_date CTE（按 code 求 MIN(date)）——都按 code 过滤，
--       无索引时 SQLite 每次对 ~2900 万行做自动索引/全扫，
--       plan 步从 M8.6 的毫秒级劣化到 3m22s/日（实测 BreadthAgg 55.7s）。
-- 修复后：BreadthAgg 毫秒级；boxscan 每股K线加载等所有按 code 查询同享加速。
-- 代价：索引一次性构建约 2 分钟（2900 万行）；ingest 重灌 daily_bar 时维护成本可忽略。
-- 同步：docs/01 §6（参数与 schema 演进记录）。

CREATE INDEX IF NOT EXISTS idx_daily_bar_code_date ON daily_bar(code, date);
