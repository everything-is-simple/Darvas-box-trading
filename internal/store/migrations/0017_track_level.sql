-- 0017: 战绩分级制（2026-10-22 三方对照校订，用户拍板）
-- 动机：把「买多少」从达瓦斯腿号金字塔（1:3:10）改为「按战绩分级定首买单位」。
--   用户交易哲学（A股量化行情节奏快 +《简简单单做股票》双重仓位思路）：
--   「交易只发生在第一次信号；不论赢家池/普通池，一律不加码」。
--   下注大小由该票历史战绩等级决定：普通池 1 单位、赢家池 5 单位。
-- 分级状态机（单次马尔可夫，只看最近一笔盈亏）：
--   normal(普通) ──首买1单位──> 持仓周期盈利 → winner(赢家)
--                              持仓周期亏损 → blacklist(黑名单,冷却N月)
--   winner(赢家) ──首买5单位──> 再盈利 → winner
--                              亏损   → normal(降级)
--   blacklist ──冷却N月──> normal
-- 字段：security.track_level(normal/winner/blacklist) + blacklist_until(冷却截止日)。
-- 说明：替换旧「两亏拉黑」（LossCountByCode，永久拉黑）——旧机制只是本体系的特例
--   （亏损N次=永久），两套并存会互相打架，故统一到本三级制。
--   旧参数 pool.maxloss / risk.maxlegs / risk.addon_max_premium 等随腿号金字塔退役。
-- 同步：docs/00 §5.6、§6、docs/01 §6、docs/03、docs/04。

-- 1) security 表加战绩等级 + 冷却截止日（ALTER TABLE 幂等由迁移框架保证只跑一次）
ALTER TABLE security ADD COLUMN track_level TEXT NOT NULL DEFAULT 'normal'
    CHECK (track_level IN ('normal','winner','blacklist'));
ALTER TABLE security ADD COLUMN blacklist_until DATE;

-- 2) 新参数（首买单位档位 / 黑名单冷却 / 同时持仓上限）
INSERT OR IGNORE INTO param(key, value) VALUES
    ('pos.units.winner',       '5'),    -- 赢家池首买 5 单位（普通池恒 1 单位=pos.unit）
    ('pool.blacklist_months',  '12'),   -- 黑名单冷却月数
    ('pool.max_hold',          '7');    -- 同时最多持仓数（7×10%赢家池=70%，留30%现金）
