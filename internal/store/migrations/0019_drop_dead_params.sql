-- 0019: 清理死参数（2026-10-28 全量清点，对照 internal/ 代码引用）
-- 动机：param 表 55 键中 29 键代码零引用（死参数），误导后续实验与一致性检查。
--   分类：
--   · risk.*   = 金字塔加码整套（量化时代废除，docs/00 §4.1 已标废止）
--   · pos.units.* = 旧单位制（已被「1 单位=2%」A M1 取代）
--   · regime.mult.* / regime.bull/bear_thresh / regime.window / regime.indices = 牛熊乘数（已退役）
--     ※ 「牛冲熊守」目标已升级为待立项功能（docs/00 §5.6.9），不再是参数
--   · pool.blacklist_months / pool.maxloss / screen.maxboxage / screen.rsthreshold / tdx.root = 单个退役
-- 同步：docs/01 §6（死参数清单）、docs/06 C3 参数键对齐、docs/00 §4.1/§5.6.9。

DELETE FROM param WHERE key IN (
    -- 金字塔加码整套（9）
    'risk.leg1', 'risk.leg23', 'risk.maxpos', 'risk.scale', 'risk.maxlegs',
    'risk.addon_max_premium', 'risk.probe_min_shares', 'risk.probe_max_pct',
    -- 旧单位制（9）
    'pos.units.main', 'pos.units_total', 'pos.units.reserve', 'pos.units.winner',
    'pos.units.probe', 'pos.units.per_stock_main_max', 'pos.units.per_stock_main_step',
    'pos.units.per_stock_probe_bear', 'pos.units.per_stock_probe_bull',
    -- 牛熊乘数 / 动量法（7）
    'regime.bull_thresh', 'regime.bear_thresh', 'regime.window',
    'regime.mult.bull', 'regime.mult.bear', 'regime.mult.range', 'regime.indices',
    -- 单个退役（4）
    'pool.blacklist_months', 'pool.maxloss', 'screen.maxboxage',
    'screen.rsthreshold', 'tdx.root'
);
