-- 0023_close_reason_enum.sql
-- T5 卖出出口六分类（docs/43）：给 position.close_reason 加 CHECK 枚举约束。
-- 六值：stop（首腿紧止损）/ stop_box（跌破箱体下轨）/ failed_breakout（突破失败）/
--       time_stop（无利时间止损）/ dividend_clear（除权除息强平）/ manual（人工）。
-- 兼容历史：旧库仅有的 stop / manual 两值均为合法值，重建后不丢数据。

-- 临时表名不带 IF NOT EXISTS，避免 C2 文本扫描误报为独立实体。
CREATE TABLE position_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL REFERENCES security(code),
    opened_date        DATE NOT NULL,
    entry_box_id       INTEGER REFERENCES box(id),
    qty                INTEGER NOT NULL DEFAULT 0,
    avg_cost           REAL NOT NULL DEFAULT 0,
    stop_price         REAL NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','closed')),
    closed_date        DATE,
    close_reason       TEXT CHECK (close_reason IS NULL OR close_reason IN
                       ('stop','stop_box','failed_breakout','time_stop','dividend_clear','manual')),
    realized_pnl       REAL,
    created_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO position_new (id, code, opened_date, entry_box_id, qty, avg_cost, stop_price,
                          status, closed_date, close_reason, realized_pnl, created_at)
SELECT id, code, opened_date, entry_box_id, qty, avg_cost, stop_price,
       status, closed_date, close_reason, realized_pnl, created_at
FROM position;

DROP TABLE position;
ALTER TABLE position_new RENAME TO position;

CREATE INDEX IF NOT EXISTS idx_position_status ON position(status);

-- 止损只升不降触发器（随表重建后重挂）
CREATE TRIGGER IF NOT EXISTS trg_position_stop_no_down
BEFORE UPDATE OF stop_price ON position
WHEN NEW.stop_price < OLD.stop_price AND OLD.stop_price > 0
BEGIN
    SELECT RAISE(ABORT, 'stop_price can only move up: ' || OLD.code || ' old=' || OLD.stop_price || ' new=' || NEW.stop_price);
END;
