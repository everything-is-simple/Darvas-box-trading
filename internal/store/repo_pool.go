package store

import (
	"database/sql"
	"encoding/json"
	"fmt"

	"darvas/internal/contract"
)

// PoolRepo candidate + watch 仓储（docs/04-api-contract.md §3.3）。
type PoolRepo struct{ db *sql.DB }

// NewPoolRepo 构造。
func NewPoolRepo(db *sql.DB) *PoolRepo { return &PoolRepo{db: db} }

// AppendCandidates 批量追加候选快照（append-only，docs/03 §2 候选池只插不更新）。
// reasons map 序列化为 JSON 存 reasons_json。
func (r *PoolRepo) AppendCandidates(date string, cands []contract.Candidate) error {
	if len(cands) == 0 {
		return nil
	}
	tx, err := r.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`INSERT INTO candidate(screen_date, code, score, reasons_json, fundamental_flag)
		VALUES (?, ?, ?, ?, ?)`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, c := range cands {
		j := "{}"
		if b, err := json.Marshal(c.Reasons); err == nil {
			j = string(b)
		}
		if _, err := stmt.Exec(date, c.Code, c.Score, j, c.FundamentalFlag); err != nil {
			return fmt.Errorf("append candidate %s: %w", c.Code, err)
		}
	}
	return tx.Commit()
}

// CandidatesOn 读某日全部候选（按分数降序）。
func (r *PoolRepo) CandidatesOn(date string) ([]contract.Candidate, error) {
	rows, err := r.db.Query(`SELECT CAST(screen_date AS TEXT), code, score, reasons_json, fundamental_flag
		FROM candidate WHERE screen_date = ? ORDER BY score DESC`, date)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.Candidate
	for rows.Next() {
		var c contract.Candidate
		var rj string
		if err := rows.Scan(&c.ScreenDate, &c.Code, &c.Score, &rj, &c.FundamentalFlag); err != nil {
			return nil, err
		}
		_ = json.Unmarshal([]byte(rj), &c.Reasons)
		out = append(out, c)
	}
	return out, rows.Err()
}

// UpsertWatch 维护「每只股票一行活跃 watch」状态机（ERD §2.2）：
//   - 存在未退出行 → 更新 stage/current_box_id/box_count 等（保留 id/added_date/candidate_id）；
//   - 已退出或不存在 → 插入新行（支持退出后重新入池）。
func (r *PoolRepo) UpsertWatch(w contract.Watch) error {
	// 查最近一行（含已退出）
	var existingID int64
	var existingStage string
	err := r.db.QueryRow(`SELECT id, stage FROM watch WHERE code = ? ORDER BY id DESC LIMIT 1`, w.Code).Scan(&existingID, &existingStage)
	if err == sql.ErrNoRows {
		// 新入池
		_, err := r.db.Exec(`INSERT INTO watch(code, candidate_id, added_date, stage, current_box_id, box_count, note)
			VALUES (?, ?, ?, ?, ?, ?, ?)`,
			w.Code, nullInt64(w.CandidateID), w.AddedDate, string(w.Stage),
			nullInt64(w.CurrentBoxID), w.BoxCount, w.Note)
		return err
	}
	if err != nil {
		return err
	}
	// 已退出 → 重新入池（插入新行）
	if contract.WatchStage(existingStage) == contract.StageExited {
		_, err := r.db.Exec(`INSERT INTO watch(code, candidate_id, added_date, stage, current_box_id, box_count, note)
			VALUES (?, ?, ?, ?, ?, ?, ?)`,
			w.Code, nullInt64(w.CandidateID), w.AddedDate, string(w.Stage),
			nullInt64(w.CurrentBoxID), w.BoxCount, w.Note)
		return err
	}
	// 未退出 → 更新现有行
	_, err = r.db.Exec(`UPDATE watch SET stage = ?, current_box_id = ?, box_count = ?,
		exit_reason = ?, note = ?, updated_at = datetime('now','localtime') WHERE id = ?`,
		string(w.Stage), nullInt64(w.CurrentBoxID), w.BoxCount, nullEmpty(w.ExitReason), w.Note, existingID)
	return err
}

// WatchByStage 按状态查跟踪池（GUI 池列表视图）。
func (r *PoolRepo) WatchByStage(stage contract.WatchStage) ([]contract.Watch, error) {
	rows, err := r.db.Query(`SELECT id, code, COALESCE(candidate_id,0), CAST(added_date AS TEXT), stage,
		COALESCE(current_box_id,0), box_count, COALESCE(exit_reason,''), note
		FROM watch WHERE stage = ? ORDER BY code`, string(stage))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.Watch
	for rows.Next() {
		var w contract.Watch
		var st string
		if err := rows.Scan(&w.ID, &w.Code, &w.CandidateID, &w.AddedDate, &st, &w.CurrentBoxID, &w.BoxCount, &w.ExitReason, &w.Note); err != nil {
			return nil, err
		}
		w.Stage = contract.WatchStage(st)
		out = append(out, w)
	}
	return out, rows.Err()
}

// WatchByCode 取某证券最新的 watch 行（无则 nil）。
func (r *PoolRepo) WatchByCode(code string) (*contract.Watch, error) {
	var w contract.Watch
	var st string
	var cid, boxID sql.NullInt64
	err := r.db.QueryRow(`SELECT id, code, candidate_id, CAST(added_date AS TEXT), stage, current_box_id, box_count, exit_reason, note
		FROM watch WHERE code = ? ORDER BY id DESC LIMIT 1`, code).
		Scan(&w.ID, &w.Code, &cid, &w.AddedDate, &st, &boxID, &w.BoxCount, &w.ExitReason, &w.Note)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if cid.Valid {
		w.CandidateID = cid.Int64
	}
	if boxID.Valid {
		w.CurrentBoxID = boxID.Int64
	}
	w.Stage = contract.WatchStage(st)
	return &w, nil
}

func nullInt64(n int64) any {
	if n == 0 {
		return nil
	}
	return n
}
