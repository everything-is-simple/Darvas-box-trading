-- 0010: M8.8 市场宽度 + 单位制仓位参数（2026-10-20 用户拍板，M8.7 退役）
-- 动机：M8.7「箱体法标指数」证伪（docs/12）——sh000001 全历史 0 个 confirmed 箱体，
--       2016-2018 全熊死仓。M8.8 不向外求（不看指数/不用动量公式），一切靠系统自身
--       box.Scan 扫出的全市场 broken_up/broken_down 事件流标牛熊。
-- 宪法依据：docs/00 第 0 条「众人恐慌我更恐慌，众人贪婪我更贪婪」。
-- 退役约定：0009 旧 M8.7 参数（regime.mult.* / regime.indices）保留禁用作对照基准，
--          M8.8 若回测无效 → 回滚 M8.6 固定仓位版（当前生产 serve）。
-- 同步：docs/01 §6 + docs/13-m8.8-breadth-unit-sizing.md。

INSERT OR IGNORE INTO param(key, value) VALUES
    -- 市场宽度标定（替代 M8.7 指数版 ClassifyDate）
    ('regime.breadth.window',      '20'),    -- 滚动窗口交易日（与三日法则同构）
    ('regime.breadth.bull_thresh', '0.55'),  -- 牛触发阈值（连续 3 日 ≥ 触发牛）
    ('regime.breadth.bear_thresh', '0.40'),  -- 熊标记阈值（仅日志区分，仓位规则同猴）
    ('regime.breadth.confirm',     '3'),     -- 连续确认日数
    ('regime.breadth.min_history', '60'),    -- 个股最少 60 根日线才计事件（剔次新噪）

    -- 单位制仓位（替代旧 risk.probe_max_pct × mult / risk.maxpos × mult 模型）
    ('pos.unit',                            '0.02'),  -- 1 单位 = 2% 权益（总资金分 50 单位）
    ('pos.units_total',                     '50'),
    ('pos.units.probe',                     '5'),     -- 试探仓总顶 1/10
    ('pos.units.main',                      '30'),    -- 主力仓总顶 6/10（仅牛市启用）
    ('pos.units.reserve',                   '15'),    -- 预备仓永不动 3/10
    ('pos.units.per_stock_probe_bear',      '1'),     -- 熊/猴：单票 1 单位
    ('pos.units.per_stock_probe_bull',      '2'),     -- 牛：单票 2 单位
    ('pos.units.per_stock_main_max',        '10'),    -- 单票主力上限 = 20% = 对齐 risk.maxpos
    ('pos.units.per_stock_main_step',       '3');     -- 单次加码 ≥3 单位（为腿号递进提供下限）