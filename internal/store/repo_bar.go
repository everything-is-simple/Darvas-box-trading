package store

import (
	"database/sql"
	"fmt"

	"darvas/internal/contract"
)

// BarRepo daily_bar 仓储（docs/04-api-contract.md §3.3）。
type BarRepo struct{ db *sql.DB }

// NewBarRepo 构造。
func NewBarRepo(db *sql.DB) *BarRepo { return &BarRepo{db: db} }

// UpsertBars 批量幂等写入（INSERT OR REPLACE：同code+date重导即更新）。
func (r *BarRepo) UpsertBars(bars []contract.DailyBar) (int64, error) {
	if len(bars) == 0 {
		return 0, nil
	}
	tx, err := r.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`INSERT OR REPLACE INTO daily_bar
		(code, date, open, high, low, close, volume, amount)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
	if err != nil {
		return 0, err
	}
	defer stmt.Close()
	for _, b := range bars {
		if _, err := stmt.Exec(b.Code, b.Date, b.Open, b.High, b.Low, b.Close, b.Volume, b.Amount); err != nil {
			return 0, fmt.Errorf("upsert bar %s %s: %w", b.Code, b.Date, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return int64(len(bars)), nil
}

// BarsWindow 读取 code 截至 endDate（含）的最近 n 根K线，升序返回。
// boxscan 主路径：三日法则只需一个有限窗口（docs/02-architecture.md §6）。
func (r *BarRepo) BarsWindow(code string, endDate string, n int) ([]contract.DailyBar, error) {
	rows, err := r.db.Query(`SELECT date, open, high, low, close, volume, amount
		FROM daily_bar WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?`, code, endDate, n)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.DailyBar
	for rows.Next() {
		var b contract.DailyBar
		b.Code = code
		if err := rows.Scan(&b.Date, &b.Open, &b.High, &b.Low, &b.Close, &b.Volume, &b.Amount); err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	// 反转为升序
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out, rows.Err()
}

// CloseOn 查某日收盘价。
func (r *BarRepo) CloseOn(code, date string) (float64, bool, error) {
	var c float64
	err := r.db.QueryRow(`SELECT close FROM daily_bar WHERE code = ? AND date = ?`, code, date).Scan(&c)
	if err == sql.ErrNoRows {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return c, true, nil
}

// AvgVolume 截至 endDate 的 n 日均成交量（手）。
func (r *BarRepo) AvgVolume(code string, endDate string, n int) (float64, error) {
	var v float64
	err := r.db.QueryRow(`SELECT COALESCE(AVG(volume), 0) FROM (
		SELECT volume FROM daily_bar WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?)`,
		code, endDate, n).Scan(&v)
	return v, err
}

// LatestDate 某证券的最新交易日（交易日历与新鲜度校验依据，ADR-2）。
func (r *BarRepo) LatestDate(code string) (string, error) {
	var d string
	err := r.db.QueryRow(`SELECT MAX(date) FROM daily_bar WHERE code = ?`, code).Scan(&d)
	return d, err
}

// HasBar 是否存在该日数据。
func (r *BarRepo) HasBar(code, date string) (bool, error) {
	var one int
	err := r.db.QueryRow(`SELECT 1 FROM daily_bar WHERE code = ? AND date = ? LIMIT 1`, code, date).Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	return err == nil, err
}
