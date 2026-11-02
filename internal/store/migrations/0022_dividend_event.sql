-- 0022: 真实除权除息事件表（2026-11-02，docs/42 / T7 重定向「先修 bug」）
-- 动机：撮合层原先用 isDividendGap（价格跌幅反推）识别除权日，阈值 0.09 恰好
--   卡在主跌停 10% 上——四年代表年 44 笔 sim:dividend 里 37 笔（84%）是「一字跌停
--   被误判成除权」，只 7 笔是真除权（送股型，跌幅 31%~65%）。除权幅度本身是连续的
--   （0%~65%，纯派息日跌幅常 <3% 甚至上涨），无法用阈值干净二分。
-- 修复：除权识别从「价格反推」改为「查真实除权记录」。数据源 = TdxQuant
--   get_divid_factors（Type=1 除权除息），由 scripts/e17_t7_fetch_dividend.py 抽取。
-- 字段口径（钉死）：
--   bonus_10      派息（每 10 股，元）——不是每股，构造复权因子须 ÷10
--   share_bonus_10 送股（每 10 股）——不是每股
--   allot_10      配股（每 10 股）
--   allot_price   配股价（元/股）
--   只有 type=1 是除权除息；11=扩缩股、15=重新调整（非除权，本表不落）。
-- 注意：本迁移只建表，不灌数据、不改参数默认值（无 param 种子，不触发 docs/01 §6）。
--   数据灌入由抽取脚本负责（源库整库拷贝链路自动带入回测库）。

CREATE TABLE IF NOT EXISTS dividend_event (
    code           TEXT NOT NULL,
    date           TEXT NOT NULL,   -- 除权除息日（YYYY-MM-DD）
    type           INTEGER NOT NULL DEFAULT 1,  -- 1=除权除息（唯一落库类型）
    bonus_10       REAL NOT NULL DEFAULT 0,     -- 派息（每10股，元）
    share_bonus_10 REAL NOT NULL DEFAULT 0,     -- 送股（每10股）
    allot_10       REAL NOT NULL DEFAULT 0,     -- 配股（每10股）
    allot_price    REAL NOT NULL DEFAULT 0,     -- 配股价（元/股）
    PRIMARY KEY (code, date)
);

CREATE INDEX IF NOT EXISTS idx_dividend_event_date ON dividend_event(date);
