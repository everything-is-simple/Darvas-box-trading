package store

import (
	"database/sql"
	"fmt"
)

// DivRepo dividend_event 仓储（migration 0022，真实除权除息事件）。
// 供撮合层查表识别除权日，替代价格反推 isDividendGap（docs/42 / T7「先修 bug」）。
type DivRepo struct{ db *sql.DB }

// NewDivRepo 构造。
func NewDivRepo(db *sql.DB) *DivRepo { return &DivRepo{db: db} }

// DividendEvent 单条除权除息事件（migration 0022 口径）。
// Bonus10/ShareBonus10/Allot10 均为「每 10 股」单位，构造复权因子须 ÷10。
type DividendEvent struct {
	Code         string
	Date         string
	Type         int
	Bonus10      float64 // 派息（每10股，元）
	ShareBonus10 float64 // 送股（每10股）
	Allot10      float64 // 配股（每10股）
	AllotPrice   float64 // 配股价（元/股）
}

// IsDividendDay 判断 (code, date) 是否为除权除息日（type=1）。
// 未命中（无记录或非 type=1）返回 false——跌停等非除权跳空不再误判。
func (r *DivRepo) IsDividendDay(code, date string) (bool, error) {
	var typ int
	err := r.db.QueryRow(
		`SELECT type FROM dividend_event WHERE code = ? AND date = ?`,
		code, date).Scan(&typ)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return typ == 1, nil
}

// Get 取 (code, date) 的除权事件；无记录返回 (DividendEvent{}, false, nil)。
func (r *DivRepo) Get(code, date string) (DividendEvent, bool, error) {
	var e DividendEvent
	err := r.db.QueryRow(
		`SELECT code, date, type, bonus_10, share_bonus_10, allot_10, allot_price
		 FROM dividend_event WHERE code = ? AND date = ?`,
		code, date).Scan(&e.Code, &e.Date, &e.Type,
		&e.Bonus10, &e.ShareBonus10, &e.Allot10, &e.AllotPrice)
	if err == sql.ErrNoRows {
		return DividendEvent{}, false, nil
	}
	if err != nil {
		return DividendEvent{}, false, err
	}
	return e, true, nil
}

// Upsert 批量幂等写入除权事件（INSERT OR REPLACE，供抽取脚本/灌库用）。
func (r *DivRepo) Upsert(events []DividendEvent) (int64, error) {
	if len(events) == 0 {
		return 0, nil
	}
	tx, err := r.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	var total int64
	for _, e := range events {
		res, err := tx.Exec(`INSERT OR REPLACE INTO dividend_event
			(code, date, type, bonus_10, share_bonus_10, allot_10, allot_price)
			VALUES (?, ?, ?, ?, ?, ?, ?)`,
			e.Code, e.Date, e.Type, e.Bonus10, e.ShareBonus10, e.Allot10, e.AllotPrice)
		if err != nil {
			return total, fmt.Errorf("upsert dividend %s %s: %w", e.Code, e.Date, err)
		}
		if n, err := res.RowsAffected(); err == nil {
			total += n
		}
	}
	return total, tx.Commit()
}
