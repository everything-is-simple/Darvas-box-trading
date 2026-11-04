-- 0032: 后复权日线表 + 数据资产状态表 + 基线身份补数据口径
--
-- 规格来源：docs/53-data-spec-new-baseline.md §8（2026-11-04 用户确认）。
--
-- 背景：daily_bar 存的是原始价，除权日价格腰斩（实测 600008 2016-05-23
--   8.12 → 4.07），导致箱体识别、52 周新高、量比在除权日全部失真；
--   match.go 只能打「除权日强制清仓」补丁，而该补丁本身是 32 笔、531,767 元
--   假亏的来源（原始股数 × 除权后价，送转股被丢弃）。
--
-- 三张对象各自的角色：
--   1. daily_bar_adj：后复权价/量，**只存个股，不含指数**，与 daily_bar 同轴
--      （code/date 全集一致；数据起点 1990，供 rescanWindow=800 回溯）。
--   2. data_meta：数据资产状态，**不是参数**，故不进 param 表/台账（避免幽灵键）。
--      adj_ready=1 是「后复权数据已通过 §8.7 验收」的唯一标志。
--   3. baseline.data_mode：把数据口径写进基线身份，使 raw 库与 adj 库不可横比。
--
-- 为什么建独立表、而不给 daily_bar 加列：
--   daily_bar 的写入走 INSERT OR REPLACE（repo_bar.go），整行覆盖——
--   加列会在每次 ingest 时被清空，静默退回原价（与 0019 误杀 tdx.root 同型）。
--   独立表 + adj_ready 三态判定，把「没灌/灌一半/灌好」显式分开。

CREATE TABLE IF NOT EXISTS daily_bar_adj (
    code        TEXT NOT NULL,
    date        DATE NOT NULL,              -- YYYY-MM-DD
    adj_open    REAL NOT NULL,
    adj_high    REAL NOT NULL,
    adj_low     REAL NOT NULL,
    adj_close   REAL NOT NULL,
    adj_volume  INTEGER NOT NULL,           -- 股（= round(none_volume / factor_norm)）
    amount      REAL NOT NULL,              -- 元，原值拷贝不调整（B6）
    factor_norm REAL NOT NULL,              -- (back/none) / (back(T0)/none(T0))
    adj_status  TEXT NOT NULL DEFAULT 'ok', -- ok | no_factor（无因子，原价直通）
    ingested_at DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (code, date)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_daily_bar_adj_date ON daily_bar_adj(date);

-- 数据资产状态表（k/v 小表，承载 adj_ready 等装载状态）。
CREATE TABLE IF NOT EXISTS data_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- 基线身份补数据口径：raw | adj。旧行回填默认 raw，符合历史事实。
ALTER TABLE baseline ADD COLUMN data_mode TEXT NOT NULL DEFAULT 'raw';
