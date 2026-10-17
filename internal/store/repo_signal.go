package store

import (
	"database/sql"
	"encoding/json"
	"fmt"

	"darvas/internal/contract"
)

// SignalRepo signal 仓储（docs/04-api-contract.md §3.3）。
type SignalRepo struct{ db *sql.DB }

// NewSignalRepo 构造。
func NewSignalRepo(db *sql.DB) *SignalRepo { return &SignalRepo{db: db} }

// AppendSignals 批量追加信号（append-only，docs/03 §2 signal 日志只插不更新）。
// Payload map 序列化为 payload_json。
func (r *SignalRepo) AppendSignals(sigs []contract.Signal) error {
	if len(sigs) == 0 {
		return nil
	}
	tx, err := r.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`INSERT INTO signal(date, code, type, box_id, close_px, box_level, payload_json)
		VALUES (?, ?, ?, ?, ?, ?, ?)`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, s := range sigs {
		j := "{}"
		if s.Payload != nil {
			if b, err := json.Marshal(s.Payload); err == nil {
				j = string(b)
			}
		}
		if _, err := stmt.Exec(s.Date, s.Code, string(s.Type),
			nullInt64(s.BoxID), s.ClosePx, s.BoxLevel, j); err != nil {
			return fmt.Errorf("append signal %s %s: %w", s.Code, s.Type, err)
		}
	}
	return tx.Commit()
}

// SignalsOn 读某日全部信号（按类型、代码排序）。
func (r *SignalRepo) SignalsOn(date string) ([]contract.Signal, error) {
	rows, err := r.db.Query(`SELECT CAST(date AS TEXT), code, type, COALESCE(box_id,0), close_px, box_level, payload_json
		FROM signal WHERE date = ? ORDER BY type, code`, date)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.Signal
	for rows.Next() {
		var s contract.Signal
		var t, pj string
		if err := rows.Scan(&s.Date, &s.Code, &t, &s.BoxID, &s.ClosePx, &s.BoxLevel, &pj); err != nil {
			return nil, err
		}
		s.Type = contract.SignalType(t)
		_ = json.Unmarshal([]byte(pj), &s.Payload)
		out = append(out, s)
	}
	return out, rows.Err()
}

// SignalsRange 读某闭区间内全部信号（按日期、类型、代码排序；周报纪律审计用）。
func (r *SignalRepo) SignalsRange(from, to string) ([]contract.Signal, error) {
	rows, err := r.db.Query(`SELECT CAST(date AS TEXT), code, type, COALESCE(box_id,0), close_px, box_level, payload_json
		FROM signal WHERE date BETWEEN ? AND ? ORDER BY date, type, code`, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.Signal
	for rows.Next() {
		var s contract.Signal
		var t, pj string
		if err := rows.Scan(&s.Date, &s.Code, &t, &s.BoxID, &s.ClosePx, &s.BoxLevel, &pj); err != nil {
			return nil, err
		}
		s.Type = contract.SignalType(t)
		_ = json.Unmarshal([]byte(pj), &s.Payload)
		out = append(out, s)
	}
	return out, rows.Err()
}

// LatestSignalDate 最近一次信号日（GUI /api/signals 缺省日期；无数据返回空）。
func (r *SignalRepo) LatestSignalDate() (string, error) {
	var d string
	err := r.db.QueryRow(`SELECT COALESCE(CAST(MAX(date) AS TEXT),'') FROM signal`).Scan(&d)
	return d, err
}
