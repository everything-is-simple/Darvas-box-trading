-- ============================================================================
-- Darvas-box-trading · SQLite Schema v1
-- 单一事实源（Single Source of Truth）
-- 修改规则：改此文件必须同步修改 docs/03-erd.md，并新增 000N_xxx.sql 迁移
-- 严禁：直接修改已发布的迁移文件（append-only）
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. 证券基础
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS security (
    code        TEXT PRIMARY KEY,          -- sh600519 / sz000001 / sh000001(指数)
    market      TEXT NOT NULL,             -- sh / sz
    name        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'stock',   -- stock / index
    board       TEXT NOT NULL DEFAULT 'main',    -- main / gem / star / st
    industry    TEXT NOT NULL DEFAULT '',
    list_date   DATE,
    status      TEXT NOT NULL DEFAULT 'active', -- active / delisted
    updated_at  DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ---------------------------------------------------------------------------
-- 2. 日线行情（幂等：复合主键，重导不重复）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_bar (
    code        TEXT NOT NULL REFERENCES security(code),
    date        DATE NOT NULL,             -- YYYY-MM-DD
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      INTEGER NOT NULL,          -- 手
    amount      REAL NOT NULL,             -- 元
    adj_factor  REAL NOT NULL DEFAULT 1.0, -- 预留：复权因子
    ingested_at DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (code, date)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_daily_bar_date ON daily_bar(date);

-- ---------------------------------------------------------------------------
-- 3. 箱体（阶梯链：prev_box_id 自引用）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS box (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL REFERENCES security(code),
    top                REAL,               -- 上轨（forming时为候选值）
    bottom             REAL,               -- 下轨（未确立时为NULL）
    top_confirmed      DATE,               -- 上轨3日确认日
    bottom_confirmed   DATE,               -- 下轨3日确认日
    status             TEXT NOT NULL DEFAULT 'forming'
                       CHECK (status IN ('forming','confirmed','broken_up','broken_down','invalid')),
    prev_box_id        INTEGER REFERENCES box(id),  -- 阶梯：前一箱体
    broken_date        DATE,               -- 突破/跌破日期
    created_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_box_code_status ON box(code, status);
CREATE INDEX IF NOT EXISTS idx_box_code_id ON box(code, id);

-- ---------------------------------------------------------------------------
-- 4. 候选池（append-only 快照）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidate (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_date        DATE NOT NULL,
    code               TEXT NOT NULL REFERENCES security(code),
    score              REAL NOT NULL DEFAULT 0,
    reasons_json       TEXT NOT NULL DEFAULT '{}',   -- 四条件明细
    fundamental_flag   TEXT NOT NULL DEFAULT 'unknown' -- yes/no/unknown 人工复核
                       CHECK (fundamental_flag IN ('yes','no','unknown')),
    created_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_candidate_date ON candidate(screen_date);

-- ---------------------------------------------------------------------------
-- 5. 跟踪池（状态机）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watch (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL REFERENCES security(code),
    candidate_id       INTEGER REFERENCES candidate(id),  -- 入池来源
    added_date         DATE NOT NULL,
    stage              TEXT NOT NULL DEFAULT 'watching'
                       CHECK (stage IN ('watching','pending','holding','exited')),
    current_box_id     INTEGER REFERENCES box(id),
    box_count          INTEGER NOT NULL DEFAULT 0,       -- 已走完的箱体数
    exit_reason        TEXT,
    note               TEXT NOT NULL DEFAULT '',
    updated_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_watch_stage ON watch(stage);
CREATE INDEX IF NOT EXISTS idx_watch_code ON watch(code);

-- ---------------------------------------------------------------------------
-- 6. 信号日志（append-only）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               DATE NOT NULL,
    code               TEXT NOT NULL REFERENCES security(code),
    type               TEXT NOT NULL
                       CHECK (type IN ('breakout','pyramid','stop','failed_breakout')),
    box_id             INTEGER REFERENCES box(id),
    close_px           REAL NOT NULL,
    box_level          REAL NOT NULL,          -- 触发参照轨
    payload_json       TEXT NOT NULL DEFAULT '{}',
    created_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_signal_date_type ON signal(date, type);
CREATE INDEX IF NOT EXISTS idx_signal_code_date ON signal(code, date);

-- ---------------------------------------------------------------------------
-- 7. 持仓与金字塔腿
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS position (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL REFERENCES security(code),
    opened_date        DATE NOT NULL,
    entry_box_id       INTEGER REFERENCES box(id),
    qty                INTEGER NOT NULL DEFAULT 0,
    avg_cost           REAL NOT NULL DEFAULT 0,
    stop_price         REAL NOT NULL DEFAULT 0,   -- 只升不降（应用层+触发器双重防护）
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','closed')),
    closed_date        DATE,
    close_reason       TEXT,
    created_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_position_status ON position(status);

CREATE TABLE IF NOT EXISTS position_leg (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id        INTEGER NOT NULL REFERENCES position(id),
    leg_no             INTEGER NOT NULL CHECK (leg_no IN (1,2,3)),
    date               DATE NOT NULL,
    qty                INTEGER NOT NULL,
    price              REAL NOT NULL,
    stop_after         REAL NOT NULL,            -- 本腿成交后止损位
    plan_order_id      INTEGER,                  -- 关联订单（成交回录时填）
    created_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (position_id, leg_no)
);

-- 止损只升不降触发器
CREATE TRIGGER IF NOT EXISTS trg_position_stop_no_down
BEFORE UPDATE OF stop_price ON position
WHEN NEW.stop_price < OLD.stop_price AND OLD.stop_price > 0
BEGIN
    SELECT RAISE(ABORT, 'stop_price can only move up: ' || OLD.code || ' old=' || OLD.stop_price || ' new=' || NEW.stop_price);
END;

-- ---------------------------------------------------------------------------
-- 8. 交易计划（六问的书面答案）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_plan (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date          DATE NOT NULL,            -- 生成日 T
    execute_date       DATE NOT NULL,            -- 执行日 T+1
    equity_base        REAL NOT NULL,            -- 仓位计算基准权益
    open_position_count INTEGER NOT NULL DEFAULT 0,
    created_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (plan_date)
);

CREATE TABLE IF NOT EXISTS plan_order (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_plan_id      INTEGER NOT NULL REFERENCES trade_plan(id),
    code               TEXT NOT NULL REFERENCES security(code),
    side               TEXT NOT NULL CHECK (side IN ('buy','sell')),
    order_kind         TEXT NOT NULL CHECK (order_kind IN ('stop_buy','stop_sell')),
    trigger_price      REAL NOT NULL,            -- 触发价（停损单）
    limit_price        REAL NOT NULL,            -- 触发后委托价
    qty                INTEGER NOT NULL,
    leg_no             INTEGER CHECK (leg_no IN (1,2,3)),
    reason_json        TEXT NOT NULL DEFAULT '{}', -- 六问答案引用（信号/箱体/仓位推导）
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','placed','filled','cancelled','expired')),
    updated_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_plan_order_plan ON plan_order(trade_plan_id, status);

-- ---------------------------------------------------------------------------
-- 9. 成交回录
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_fill (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_order_id      INTEGER REFERENCES plan_order(id),
    code               TEXT NOT NULL REFERENCES security(code),
    date               DATE NOT NULL,
    price              REAL NOT NULL,
    qty                INTEGER NOT NULL,
    fee                REAL NOT NULL DEFAULT 0,
    raw_text           TEXT,                    -- 粘贴原文（审计）
    created_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_fill_code_date ON trade_fill(code, date);

-- ---------------------------------------------------------------------------
-- 10. 账本
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS equity_snapshot (
    date               DATE PRIMARY KEY,
    cash               REAL NOT NULL,
    market_value       REAL NOT NULL,
    total              REAL NOT NULL,
    peak_total         REAL NOT NULL,
    drawdown           REAL NOT NULL            -- (peak-total)/peak，负数
);

-- ---------------------------------------------------------------------------
-- 11. 调度运行日志（心跳/补跑依据）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_run (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               DATE NOT NULL,
    task               TEXT NOT NULL,   -- ingest/boxscan/screen/poolsync/signal/plan/report/weekly
    status             TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running','ok','failed','skipped')),
    started_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at        DATETIME,
    error              TEXT,
    affected_rows      INTEGER,
    UNIQUE (date, task)
);

-- ---------------------------------------------------------------------------
-- 12. 参数（热调 + 审计）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS param (
    key                TEXT PRIMARY KEY,   -- risk.leg1 / buy.premium / box.volmult ...
    value              TEXT NOT NULL,
    old_value          TEXT,
    changed_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 初始默认参数（与 docs/01-requirements §6 一致）
INSERT OR IGNORE INTO param(key, value) VALUES
    ('risk.leg1',            '0.01'),
    ('risk.leg23',           '0.02'),
    ('risk.maxpos',          '0.20'),
    ('risk.maxlegs',         '3'),
    ('buy.premium',          '1.03'),
    ('sell.discount',        '0.97'),
    ('box.volmult',          '2.0'),
    ('box.volwindow',        '50'),
    ('screen.rswindow',      '20'),
    ('screen.rsthreshold',   '1.2'),
    ('screen.turnover',      '50000000'),
    ('screen.maxboxage',     '60'),
    ('pool.flatboxmax',      '4'),
    ('tdx.root',             'H:/new_tdx64/vipdoc');

-- ---------------------------------------------------------------------------
-- 13. Schema版本（由 store.migrate() 管理，迁移文件内禁止自行INSERT版本号）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version            INTEGER PRIMARY KEY,
    applied_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    description        TEXT
);
