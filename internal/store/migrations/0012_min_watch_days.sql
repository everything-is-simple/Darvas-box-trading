-- 0012: 观察期参数（2026-10-21 G1 落地）
-- 动机：原书 L1895「每次只增加一支，投资前仔细研究」+ L1063「确认还运行在箱体里
--       才继续关注数日，观察是否突破才下停损买单」——从入池到首买之间有数日观察缓冲。
--       系统此前流水线当晚一气呵成（screen→poolsync→signal→plan），新票入池次日即出买单，
--       缺"继续关注"这一步（docs/19 G1）。
-- 落地：planner 生成首腿买单（breakout）前，检查 watch.added_date 距 planDate 的
--       交易日数 < pool.min_watch_days 则跳过（条件挂起，不占名额）；pyramid 加码不受限。
-- 同步：docs/00 §6 G1、docs/01 §6、docs/19 R1。

INSERT OR IGNORE INTO param (key, value) VALUES ('pool.min_watch_days', '3');
