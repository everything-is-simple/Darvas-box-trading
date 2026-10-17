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

// LatestCandidateDate 最近一次候选快照日（GUI /api/candidates 缺省日期；无数据返回空）。
func (r *PoolRepo) LatestCandidateDate() (string, error) {
	var d string
	err := r.db.QueryRow(`SELECT COALESCE(CAST(MAX(screen_date) AS TEXT),'') FROM candidate`).Scan(&d)
	return d, err
}

// UpsertWatch 维护「每只股票一行活跃 watch」状态机（ERD §2.2）：
//   - 存在未退出行 → 更新 stage/current_box_id/box_count 等（保留 id/added_date/candidate_id）；
//   - 已退出或不存在 → 插入新行（支持退出后重新入池；blacklisted/suspended 走更新分支，
//     重新入池拦截在 pool.Sync 层——黑名单永不入池）。
func (r *PoolRepo) UpsertWatch(w contract.Watch) error {
	// 查最近一行（含已退出）
	var existingID int64
	var existingStage string
	err := r.db.QueryRow(`SELECT id, stage FROM watch WHERE code = ? ORDER BY id DESC LIMIT 1`, w.Code).Scan(&existingID, &existingStage)
	if err == sql.ErrNoRows {
		// 新入池
		_, err := r.db.Exec(`INSERT INTO watch(code, candidate_id, added_date, stage, current_box_id, box_count, resume_trigger, note)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
			w.Code, nullInt64(w.CandidateID), w.AddedDate, string(w.Stage),
			nullInt64(w.CurrentBoxID), w.BoxCount, nullFloat64(w.ResumeTrigger), w.Note)
		return err
	}
	if err != nil {
		return err
	}
	// 已退出 → 重新入池（插入新行）
	if contract.WatchStage(existingStage) == contract.StageExited {
		_, err := r.db.Exec(`INSERT INTO watch(code, candidate_id, added_date, stage, current_box_id, box_count, resume_trigger, note)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
			w.Code, nullInt64(w.CandidateID), w.AddedDate, string(w.Stage),
			nullInt64(w.CurrentBoxID), w.BoxCount, nullFloat64(w.ResumeTrigger), w.Note)
		return err
	}
	// 未退出（watching/pending/holding/suspended/blacklisted）→ 更新现有行
	_, err = r.db.Exec(`UPDATE watch SET stage = ?, current_box_id = ?, box_count = ?,
		exit_reason = ?, resume_trigger = ?, note = ?, updated_at = datetime('now','localtime') WHERE id = ?`,
		string(w.Stage), nullInt64(w.CurrentBoxID), w.BoxCount,
		nullEmpty(w.ExitReason), nullFloat64(w.ResumeTrigger), w.Note, existingID)
	return err
}

// ActiveWatchCount 活跃跟踪池数量（watching+pending+holding；suspended 不占跟踪额度，B2 5-8上限）。
func (r *PoolRepo) ActiveWatchCount() (int, error) {
	var n int
	err := r.db.QueryRow(`SELECT COUNT(*) FROM watch
		WHERE stage IN ('watching','pending','holding')`).Scan(&n)
	return n, err
}

// RelaxPendingWatches 买单过期后 watch pending→watching（B2 接线：无在途买单即回观察态）。
func (r *PoolRepo) RelaxPendingWatches() error {
	_, err := r.db.Exec(`UPDATE watch SET stage='watching', updated_at=datetime('now','localtime')
		WHERE stage='pending' AND code NOT IN (
			SELECT DISTINCT code FROM plan_order WHERE side='buy' AND status IN ('pending','placed'))`)
	return err
}

// WatchByStage 按状态查跟踪池（GUI 池列表视图）。
func (r *PoolRepo) WatchByStage(stage contract.WatchStage) ([]contract.Watch, error) {
	rows, err := r.db.Query(`SELECT id, code, COALESCE(candidate_id,0), CAST(added_date AS TEXT), stage,
		COALESCE(current_box_id,0), box_count, COALESCE(exit_reason,''), COALESCE(resume_trigger,0), note
		FROM watch WHERE stage = ? ORDER BY code`, string(stage))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.Watch
	for rows.Next() {
		var w contract.Watch
		var st string
		if err := rows.Scan(&w.ID, &w.Code, &w.CandidateID, &w.AddedDate, &st, &w.CurrentBoxID, &w.BoxCount, &w.ExitReason, &w.ResumeTrigger, &w.Note); err != nil {
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
	var rt sql.NullFloat64
	err := r.db.QueryRow(`SELECT id, code, candidate_id, CAST(added_date AS TEXT), stage, current_box_id, box_count,
		COALESCE(exit_reason,''), resume_trigger, COALESCE(note,'')
		FROM watch WHERE code = ? ORDER BY id DESC LIMIT 1`, code).
		Scan(&w.ID, &w.Code, &cid, &w.AddedDate, &st, &boxID, &w.BoxCount, &w.ExitReason, &rt, &w.Note)
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
	if rt.Valid {
		w.ResumeTrigger = rt.Float64
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

// nullFloat64 0 → NULL（resume_trigger 无值时）。
func nullFloat64(f float64) any {
	if f == 0 {
		return nil
	}
	return f
}

// WatchOverview 跟踪池看板行（GUI 跟踪池视图）：
// watch 最新行 JOIN security（名称）/ box（当前箱体）/ 最新收盘，一次查询成型。
type WatchOverview struct {
	Code            string  `json:"code"`
	Name            string  `json:"name"`
	Stage           string  `json:"stage"`
	AddedDate       string  `json:"added_date"`
	BoxCount        int     `json:"box_count"`
	BoxTop          float64 `json:"box_top"`
	BoxBottom       float64 `json:"box_bottom"`
	BoxStatus       string  `json:"box_status"`
	TopConfirmed    string  `json:"top_confirmed"`
	BottomConfirmed string  `json:"bottom_confirmed"`
	BrokenDate      string  `json:"broken_date"`
	LastClose       float64 `json:"last_close"`
	ExitReason      string  `json:"exit_reason"`    // 仅 exited 行有值（出池轨迹展示）
	ResumeTrigger   float64 `json:"resume_trigger"` // 仅 suspended 行有值（B2 条件挂起激活价）
}

// WatchOverview 全部跟踪池看板行（每 code 取最新 watch 行，含 exited）。
// 最新收盘走 daily_bar PK 倒扫相关子查询（PK(code,date) WITHOUT ROWID，O(log n)/行）。
func (r *PoolRepo) WatchOverview() ([]WatchOverview, error) {
	rows, err := r.db.Query(`SELECT w.code, COALESCE(s.name,''), w.stage, CAST(w.added_date AS TEXT), w.box_count,
			COALESCE(b.top,0), COALESCE(b.bottom,0), COALESCE(b.status,''),
			CAST(COALESCE(b.top_confirmed,'') AS TEXT), CAST(COALESCE(b.bottom_confirmed,'') AS TEXT),
			CAST(COALESCE(b.broken_date,'') AS TEXT),
			COALESCE((SELECT d.close FROM daily_bar d WHERE d.code = w.code ORDER BY d.date DESC LIMIT 1), 0),
			COALESCE(w.exit_reason,''), COALESCE(w.resume_trigger,0)
		FROM watch w
		JOIN (SELECT code, MAX(id) AS mid FROM watch GROUP BY code) latest ON w.id = latest.mid
		LEFT JOIN security s ON s.code = w.code
		LEFT JOIN box b ON b.id = w.current_box_id
		ORDER BY CASE w.stage
			WHEN 'holding' THEN 1 WHEN 'pending' THEN 2 WHEN 'watching' THEN 3
			WHEN 'suspended' THEN 4 WHEN 'blacklisted' THEN 5 ELSE 6 END, w.code`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []WatchOverview
	for rows.Next() {
		var w WatchOverview
		if err := rows.Scan(&w.Code, &w.Name, &w.Stage, &w.AddedDate, &w.BoxCount,
			&w.BoxTop, &w.BoxBottom, &w.BoxStatus,
			&w.TopConfirmed, &w.BottomConfirmed, &w.BrokenDate, &w.LastClose,
			&w.ExitReason, &w.ResumeTrigger); err != nil {
			return nil, err
		}
		out = append(out, w)
	}
	return out, rows.Err()
}
