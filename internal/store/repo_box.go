package store

import (
	"database/sql"
	"fmt"

	"darvas/internal/contract"
)

// BoxRepo box 仓储（docs/04-api-contract.md §3.3 / docs/03-erd.md §3）。
type BoxRepo struct{ db *sql.DB }

// NewBoxRepo 构造。
func NewBoxRepo(db *sql.DB) *BoxRepo { return &BoxRepo{db: db} }

// UpsertBoxes 批量写入箱体，重建阶梯链（prev_box_id）的 DB 真实 ID。
//
// 输入约定（来自 box.Engine.Scan）：
//   - boxes 按时间顺序排列，Box.ID 为切片内局部序号（1..N）；
//   - Box.PrevBoxID 引用同切片内的局部 ID（0=无前驱）。
//
// 幂等策略（docs/02 §6 boxscan「重算替换」语义）：
//   - 先删除该 code 的全部旧箱体（同一事务内），再插入新结果；
//   - M2 阶段 watch/signal/position 尚未引用 box，删除安全；
//     M3+ 引入引用后须改为「按 (code,top,bottom,top_confirmed) 匹配更新」以保 FK。
//
// 返回值：按输入顺序对应的 DB 分配 ID 列表。
func (r *BoxRepo) UpsertBoxes(boxes []contract.Box) ([]int64, error) {
	if len(boxes) == 0 {
		return nil, nil
	}
	tx, err := r.db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	// 局部 ID → DB ID 映射，用于修复 prev_box_id 链
	localToDB := make(map[int64]int64, len(boxes))
	ids := make([]int64, 0, len(boxes))

	// 同 code（扫描器单只调用），取首个 code 做删除
	code := boxes[0].Code
	if _, err := tx.Exec(`DELETE FROM box WHERE code = ?`, code); err != nil {
		return nil, fmt.Errorf("delete old boxes %s: %w", code, err)
	}

	// 预编译插入语句，复用以避免重编译
	stmt, err := tx.Prepare(`INSERT INTO box
		(code, top, bottom, top_confirmed, bottom_confirmed, status, prev_box_id, broken_date)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
	if err != nil {
		return nil, err
	}
	defer stmt.Close()

	for _, b := range boxes {
		// 修复 prev_box_id：局部 ID → DB ID（0 表示无前驱，DB 存 NULL）
		prevDB := int64(0)
		if b.PrevBoxID > 0 {
			if db, ok := localToDB[b.PrevBoxID]; ok {
				prevDB = db
			}
		}
		var prevVal any = nil
		if prevDB > 0 {
			prevVal = prevDB
		}
		res, err := stmt.Exec(
			b.Code, b.Top, b.Bottom,
			nullEmpty(b.TopConfirmed), nullEmpty(b.BottomConfirmed),
			string(b.Status), prevVal, nullEmpty(b.BrokenDate),
		)
		if err != nil {
			return nil, fmt.Errorf("insert box %s: %w", b.Code, err)
		}
		dbID, _ := res.LastInsertId()
		localToDB[b.ID] = dbID
		ids = append(ids, dbID)
	}

	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return ids, nil
}

// LatestBox 取某证券最新的箱体（按 id DESC，即最近写入）。
func (r *BoxRepo) LatestBox(code string) (*contract.Box, error) {
	var b contract.Box
	var topC, botC, broken sql.NullString
	var prev sql.NullInt64
	var status string
	err := r.db.QueryRow(`SELECT id, code, top, bottom,
		top_confirmed, bottom_confirmed, status, prev_box_id, broken_date
		FROM box WHERE code = ? ORDER BY id DESC LIMIT 1`, code).
		Scan(&b.ID, &b.Code, &b.Top, &b.Bottom, &topC, &botC, &status, &prev, &broken)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	b.TopConfirmed = topC.String
	b.BottomConfirmed = botC.String
	b.BrokenDate = broken.String
	if prev.Valid {
		b.PrevBoxID = prev.Int64
	}
	b.Status = contract.BoxStatus(status)
	return &b, nil
}

// Ladder 取某证券的全部箱体（时间顺序，含 invalid），供 GUI 画阶梯/回溯 prev_box_id。
// 调用方按需过滤 invalid（invalid 不计入可视阶梯，docs/00 §3.3）。
func (r *BoxRepo) Ladder(code string) ([]contract.Box, error) {
	rows, err := r.db.Query(`SELECT id, code, top, bottom,
		top_confirmed, bottom_confirmed, status, prev_box_id, broken_date
		FROM box WHERE code = ? ORDER BY id ASC`, code)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.Box
	for rows.Next() {
		var b contract.Box
		var status string
		var topC, botC, broken sql.NullString
		var prev sql.NullInt64
		if err := rows.Scan(&b.ID, &b.Code, &b.Top, &b.Bottom,
			&topC, &botC, &status, &prev, &broken); err != nil {
			return nil, err
		}
		b.TopConfirmed = topC.String
		b.BottomConfirmed = botC.String
		b.BrokenDate = broken.String
		if prev.Valid {
			b.PrevBoxID = prev.Int64
		}
		b.Status = contract.BoxStatus(status)
		out = append(out, b)
	}
	return out, rows.Err()
}

func nullEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
