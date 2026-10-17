package store

import (
	"database/sql"
	"fmt"
	"strconv"
	"strings"

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
// 幂等策略（docs/02 §6 boxscan「重算替换」语义，M3+ FK 安全版）：
//   - 按 (top,bottom) 匹配既有箱体 → 复用其 DB ID（UPDATE 状态/确认日/突破日），
//     保证 watch.current_box_id / position.entry_box_id 等外键引用稳定；
//   - 未匹配上的旧箱体删除，但 position/watch 仍引用的箱体保留（历史事实不可删）；
//   - 新出现的箱体 INSERT。
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

	code := boxes[0].Code

	// 既有箱体：(top,bottom) → 可复用的 DB id 队列（同形箱体按 id 序复用）
	type exBox struct {
		id        int64
		top, bot  float64
	}
	rows, err := tx.Query(`SELECT id, top, bottom FROM box WHERE code = ? ORDER BY id`, code)
	if err != nil {
		return nil, err
	}
	var existing []exBox
	for rows.Next() {
		var e exBox
		if err := rows.Scan(&e.id, &e.top, &e.bot); err != nil {
			rows.Close()
			return nil, err
		}
		existing = append(existing, e)
	}
	rows.Close()

	boxKey := func(top, bot float64) string {
		return fmt.Sprintf("%.6f|%.6f", top, bot)
	}
	avail := map[string][]int64{}
	for _, e := range existing {
		k := boxKey(e.top, e.bot)
		avail[k] = append(avail[k], e.id)
	}

	// 局部 ID → DB ID 映射，用于修复 prev_box_id 链
	localToDB := make(map[int64]int64, len(boxes))
	ids := make([]int64, 0, len(boxes))
	reused := make(map[int64]bool, len(existing))

	updStmt, err := tx.Prepare(`UPDATE box SET top_confirmed=?, bottom_confirmed=?, status=?, prev_box_id=?, broken_date=?
		WHERE id = ?`)
	if err != nil {
		return nil, err
	}
	defer updStmt.Close()
	insStmt, err := tx.Prepare(`INSERT INTO box
		(code, top, bottom, top_confirmed, bottom_confirmed, status, prev_box_id, broken_date)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
	if err != nil {
		return nil, err
	}
	defer insStmt.Close()

	for _, b := range boxes {
		// 修复 prev_box_id：局部 ID → DB ID（0 表示无前驱，DB 存 NULL）
		var prevVal any = nil
		if b.PrevBoxID > 0 {
			if db, ok := localToDB[b.PrevBoxID]; ok {
				prevVal = db
			}
		}
		var dbID int64
		if q := avail[boxKey(b.Top, b.Bottom)]; len(q) > 0 {
			// 复用既有箱体（保外键引用稳定）
			dbID = q[0]
			avail[boxKey(b.Top, b.Bottom)] = q[1:]
			reused[dbID] = true
			if _, err := updStmt.Exec(
				nullEmpty(b.TopConfirmed), nullEmpty(b.BottomConfirmed),
				string(b.Status), prevVal, nullEmpty(b.BrokenDate), dbID); err != nil {
				return nil, fmt.Errorf("update box %s #%d: %w", b.Code, dbID, err)
			}
		} else {
			res, err := insStmt.Exec(
				b.Code, b.Top, b.Bottom,
				nullEmpty(b.TopConfirmed), nullEmpty(b.BottomConfirmed),
				string(b.Status), prevVal, nullEmpty(b.BrokenDate),
			)
			if err != nil {
				return nil, fmt.Errorf("insert box %s: %w", b.Code, err)
			}
			dbID, _ = res.LastInsertId()
		}
		localToDB[b.ID] = dbID
		ids = append(ids, dbID)
	}

	// 删除本次重算中消失的箱体（未被复用的旧箱）；
	// position/watch/signal 仍引用的保留（FK 安全，历史事实不可删）
	var stale []string
	for _, e := range existing {
		if !reused[e.id] {
			stale = append(stale, strconv.FormatInt(e.id, 10))
		}
	}
	if len(stale) > 0 {
		del, err := tx.Exec(`DELETE FROM box WHERE id IN (` + strings.Join(stale, ",") + `)
			AND id NOT IN (SELECT entry_box_id FROM position WHERE code = ? AND entry_box_id IS NOT NULL)
			AND id NOT IN (SELECT current_box_id FROM watch WHERE code = ? AND current_box_id IS NOT NULL)
			AND id NOT IN (SELECT box_id FROM signal WHERE code = ? AND box_id IS NOT NULL)`,
			code, code, code)
		if err != nil {
			return nil, fmt.Errorf("delete stale boxes %s: %w", code, err)
		}
		_ = del
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
		CAST(top_confirmed AS TEXT), CAST(bottom_confirmed AS TEXT), status, prev_box_id, CAST(broken_date AS TEXT)
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
		CAST(top_confirmed AS TEXT), CAST(bottom_confirmed AS TEXT), status, prev_box_id, CAST(broken_date AS TEXT)
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
