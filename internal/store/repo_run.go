package store

import (
	"database/sql"
	"time"

	"darvas/internal/contract"
)

// RunRepo daily_run 仓储：调度心跳与补跑判断（docs/04-api-contract.md §3.3）。
type RunRepo struct{ db *sql.DB }

// NewRunRepo 构造。
func NewRunRepo(db *sql.DB) *RunRepo { return &RunRepo{db: db} }

// StartRun 登记任务开始（UNIQUE(date,task) 冲突时视为重跑，重置状态）。
func (r *RunRepo) StartRun(date, task string) (int64, error) {
	_, err := r.db.Exec(`INSERT INTO daily_run(date, task, status, started_at)
		VALUES (?, ?, 'running', datetime('now','localtime'))
		ON CONFLICT(date, task) DO UPDATE SET
			status = 'running', started_at = datetime('now','localtime'),
			finished_at = NULL, error = NULL, affected_rows = NULL`,
		date, task)
	if err != nil {
		return 0, err
	}
	var id int64
	err = r.db.QueryRow(`SELECT id FROM daily_run WHERE date = ? AND task = ?`, date, task).Scan(&id)
	return id, err
}

// FinishRun 登记任务结果。
func (r *RunRepo) FinishRun(id int64, res contract.TaskResult) error {
	_, err := r.db.Exec(`UPDATE daily_run SET
		status = ?, finished_at = datetime('now','localtime'), error = ?, affected_rows = ?
		WHERE id = ?`,
		res.Status, nullString(res.Error), res.AffectedRows, id)
	return err
}

// LastOKRun 查询某日某任务是否已成功（补跑判断：CatchUp只补未ok的）。
func (r *RunRepo) LastOKRun(date, task string) (contract.TaskResult, bool, error) {
	var res contract.TaskResult
	var finished sql.NullTime
	err := r.db.QueryRow(`SELECT task, date, status, finished_at, COALESCE(affected_rows,0), COALESCE(error,'')
		FROM daily_run WHERE date = ? AND task = ? AND status = 'ok'`, date, task).
		Scan(&res.Task, &res.Date, &res.Status, &finished, &res.AffectedRows, &res.Error)
	if err == sql.ErrNoRows {
		return res, false, nil
	}
	if err != nil {
		return res, false, err
	}
	if finished.Valid {
		res.DurationMS = finished.Time.Sub(time.Time{}).Milliseconds()
	}
	return res, true, nil
}

func nullString(s string) any {
	if s == "" {
		return nil
	}
	return s
}
