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

	// 既有箱体：身份键 → 可复用的 DB id 队列（按 id 序复用）。
	// 身份键 = (top, bottom, bottom_confirmed)（0029，docs/49 §五之二 特征一）：
	// 旧键只用 (top,bottom)，同一只股票在不同生命周期重现同价箱体时会复用到同一行，
	// 把买入当时的 status/broken_date 覆盖掉 —— 实测导致 34 笔持仓早于其箱体突破日、
	// 11 笔买入挂在 broken_down 箱体上。加入下轨确认日后，不同生命周期的同价箱体不再撞键，
	// 被 position 引用的旧行还会被 stale 删除逻辑永久保留，历史事实不再被重算改写。
	//
	// invalid 例外：作废箱体可能在下轨确认前就死（bottom=0、bottom_confirmed 为空），
	// 身份退化成只剩 top，不同年份同样 top 的作废箱会撞在一起（实测 124 组）。
	// 故 invalid 的身份再追加 dead_date——它是终态、重算不变，加入后唯一且稳定；
	// 其余状态（forming/confirmed/broken_up/broken_down）dead_date 或为空或将变，
	// 不参与键，避免「confirmed → broken_down」时键漂移导致旧行被误删。
	type exBox struct {
		id       int64
		top, bot float64
		botC     string
		dead     string
		status   string
	}
	rows, err := tx.Query(`SELECT id, top, bottom, COALESCE(CAST(bottom_confirmed AS TEXT),''),
		COALESCE(CAST(dead_date AS TEXT),''), status
		FROM box WHERE code = ? ORDER BY id`, code)
	if err != nil {
		return nil, err
	}
	var existing []exBox
	for rows.Next() {
		var e exBox
		if err := rows.Scan(&e.id, &e.top, &e.bot, &e.botC, &e.dead, &e.status); err != nil {
			rows.Close()
			return nil, err
		}
		existing = append(existing, e)
	}
	rows.Close()

	boxKey := func(top, bot float64, botC, dead, status string) string {
		if status == string(contract.BoxInvalid) {
			return fmt.Sprintf("%.6f|%.6f|%s|%s", top, bot, botC, dead)
		}
		return fmt.Sprintf("%.6f|%.6f|%s", top, bot, botC)
	}
	avail := map[string][]int64{}
	for _, e := range existing {
		k := boxKey(e.top, e.bot, e.botC, e.dead, e.status)
		avail[k] = append(avail[k], e.id)
	}

	// 局部 ID → DB ID 映射，用于修复 prev_box_id 链
	localToDB := make(map[int64]int64, len(boxes))
	ids := make([]int64, 0, len(boxes))
	reused := make(map[int64]bool, len(existing))

	updStmt, err := tx.Prepare(`UPDATE box SET top_confirmed=?, bottom_confirmed=?, status=?, prev_box_id=?, broken_date=?, dead_date=?
		WHERE id = ?`)
	if err != nil {
		return nil, err
	}
	defer updStmt.Close()
	insStmt, err := tx.Prepare(`INSERT INTO box
		(code, top, bottom, top_confirmed, bottom_confirmed, status, prev_box_id, broken_date, dead_date)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`)
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
		k := boxKey(b.Top, b.Bottom, b.BottomConfirmed, b.DeadDate, string(b.Status))
		if q := avail[k]; len(q) > 0 {
			// 复用既有箱体（保外键引用稳定）
			dbID = q[0]
			avail[k] = q[1:]
			reused[dbID] = true
			if _, err := updStmt.Exec(
				nullEmpty(b.TopConfirmed), nullEmpty(b.BottomConfirmed),
				string(b.Status), prevVal, nullEmpty(b.BrokenDate), nullEmpty(b.DeadDate), dbID); err != nil {
				return nil, fmt.Errorf("update box %s #%d: %w", b.Code, dbID, err)
			}
		} else {
			res, err := insStmt.Exec(
				b.Code, b.Top, b.Bottom,
				nullEmpty(b.TopConfirmed), nullEmpty(b.BottomConfirmed),
				string(b.Status), prevVal, nullEmpty(b.BrokenDate), nullEmpty(b.DeadDate),
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
	// position/watch/signal 仍引用的保留（FK 安全，历史事实不可删）；
	// 幸存箱体的 prev_box_id 前驱也保留（box 自引用 FK，archive/07 #6：
	// 漏保护曾致整个事务回滚、该股当日箱体更新全丢）
	var stale []string
	for _, e := range existing {
		if !reused[e.id] {
			stale = append(stale, strconv.FormatInt(e.id, 10))
		}
	}
	if len(stale) > 0 {
		del, err := tx.Exec(`DELETE FROM box WHERE id IN (`+strings.Join(stale, ",")+`)
			AND id NOT IN (SELECT entry_box_id FROM position WHERE code = ? AND entry_box_id IS NOT NULL)
			AND id NOT IN (SELECT current_box_id FROM watch WHERE code = ? AND current_box_id IS NOT NULL)
			AND id NOT IN (SELECT box_id FROM signal WHERE code = ? AND box_id IS NOT NULL)
			AND id NOT IN (SELECT prev_box_id FROM box WHERE code = ? AND prev_box_id IS NOT NULL)`,
			code, code, code, code)
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
// 含 forming 暂态行——供 GUI/报告展示「当前形成中状态」；决策逻辑请用 LatestNonFormingBox。
func (r *BoxRepo) LatestBox(code string) (*contract.Box, error) {
	var b contract.Box
	var topC, botC, broken, dead sql.NullString
	var prev sql.NullInt64
	var status string
	err := r.db.QueryRow(`SELECT id, code, top, bottom,
		CAST(top_confirmed AS TEXT), CAST(bottom_confirmed AS TEXT), status, prev_box_id, CAST(broken_date AS TEXT), CAST(dead_date AS TEXT)
		FROM box WHERE code = ? ORDER BY id DESC LIMIT 1`, code).
		Scan(&b.ID, &b.Code, &b.Top, &b.Bottom, &topC, &botC, &status, &prev, &broken, &dead)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	b.TopConfirmed = topC.String
	b.BottomConfirmed = botC.String
	b.BrokenDate = broken.String
	b.DeadDate = dead.String
	if prev.Valid {
		b.PrevBoxID = prev.Int64
	}
	b.Status = contract.BoxStatus(status)
	return &b, nil
}

// LatestNonFormingBox 取某证券最新的「已落定」箱体（confirmed/broken_up/broken_down；
// 排除 forming 暂态与 invalid 作废）。决策逻辑（signal/poolsync 出池/planner）专用——
// 修复 archive/07 #1：突破日引擎输出 [broken_up箱体, forming(0,0)箱体]，LatestBox(id DESC)
// 必取 forming → 四类信号判定全跳过（生产 signal 表 0 行佐证）。LatestNonFormingBox
// 跳过 forming，返回突破箱体本身，恢复事件判定。排除 invalid 避免（bottom 可能已填）
// spuriously 触发 stop 信号。
func (r *BoxRepo) LatestNonFormingBox(code string) (*contract.Box, error) {
	var b contract.Box
	var topC, botC, broken, dead sql.NullString
	var prev sql.NullInt64
	var status string
	err := r.db.QueryRow(`SELECT id, code, top, bottom,
		CAST(top_confirmed AS TEXT), CAST(bottom_confirmed AS TEXT), status, prev_box_id, CAST(broken_date AS TEXT), CAST(dead_date AS TEXT)
		FROM box WHERE code = ? AND status IN ('confirmed','broken_up','broken_down')
		ORDER BY id DESC LIMIT 1`, code).
		Scan(&b.ID, &b.Code, &b.Top, &b.Bottom, &topC, &botC, &status, &prev, &broken, &dead)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	b.TopConfirmed = topC.String
	b.BottomConfirmed = botC.String
	b.BrokenDate = broken.String
	b.DeadDate = dead.String
	if prev.Valid {
		b.PrevBoxID = prev.Int64
	}
	b.Status = contract.BoxStatus(status)
	return &b, nil
}

// LatestConfirmedBoxAsOf 取某证券在 planDate 时点「上下轨都已牢固确立」的最新箱体。
//
// 与 LatestNonFormingBox 的区别（O6，docs/65 §4 用户 2026-11-08 裁定「只修 (a) 时点隔离」）：
//   - 时点隔离：只取 top_confirmed / bottom_confirmed 均非空且均 ≤ planDate 的箱体，
//     不消费任何未来确认的箱体；
//   - 取最新按「完全确立日」= max(上轨确认日, 下轨确认日)，而不是 ORDER BY id DESC。
//     UpsertBoxes 会复用旧 id、新箱体又追加更大 id，id 序与时间序会背离
//     （D5 补证据实测：同一股票内相邻 id 的确认日倒退 20.82%），旧口径因此把
//     止损挂到历史箱体下轨上，造成 133 次止损越过当日收盘、131 次次日无理由清仓；
//   - 状态集合不变（confirmed / broken_up / broken_down；排除 forming / invalid），
//     避免顺带改动 signal / poolsync / screener 的判定口径。
//
// 仅供 planner.raiseStops 使用；其他决策逻辑仍走 LatestNonFormingBox。
func (r *BoxRepo) LatestConfirmedBoxAsOf(code, planDate string) (*contract.Box, error) {
	asOf := planDate
	if len(asOf) > 10 {
		asOf = asOf[:10]
	}
	// 两确认日的较晚者 = 上下轨「都牢固确立」之日；CASE 而非 max() 以免与聚合语义混淆。
	const confirmedAt = `CASE WHEN substr(CAST(top_confirmed AS TEXT),1,10) >= substr(CAST(bottom_confirmed AS TEXT),1,10)
		THEN substr(CAST(top_confirmed AS TEXT),1,10)
		ELSE substr(CAST(bottom_confirmed AS TEXT),1,10) END`
	q := `SELECT id, code, top, bottom,
		CAST(top_confirmed AS TEXT), CAST(bottom_confirmed AS TEXT), status, prev_box_id, CAST(broken_date AS TEXT), CAST(dead_date AS TEXT)
		FROM box WHERE code = ?
		AND status IN ('confirmed','broken_up','broken_down')
		AND top_confirmed IS NOT NULL AND TRIM(CAST(top_confirmed AS TEXT)) <> ''
		AND bottom_confirmed IS NOT NULL AND TRIM(CAST(bottom_confirmed AS TEXT)) <> ''
		AND substr(CAST(top_confirmed AS TEXT),1,10) <= ?
		AND substr(CAST(bottom_confirmed AS TEXT),1,10) <= ?
		ORDER BY ` + confirmedAt + ` DESC, id DESC LIMIT 1`

	var b contract.Box
	var topC, botC, broken, dead sql.NullString
	var prev sql.NullInt64
	var status string
	err := r.db.QueryRow(q, code, asOf, asOf).
		Scan(&b.ID, &b.Code, &b.Top, &b.Bottom, &topC, &botC, &status, &prev, &broken, &dead)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	b.TopConfirmed = topC.String
	b.BottomConfirmed = botC.String
	b.BrokenDate = broken.String
	b.DeadDate = dead.String
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
		CAST(top_confirmed AS TEXT), CAST(bottom_confirmed AS TEXT), status, prev_box_id, CAST(broken_date AS TEXT), CAST(dead_date AS TEXT)
		FROM box WHERE code = ? ORDER BY id ASC`, code)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.Box
	for rows.Next() {
		var b contract.Box
		var status string
		var topC, botC, broken, dead sql.NullString
		var prev sql.NullInt64
		if err := rows.Scan(&b.ID, &b.Code, &b.Top, &b.Bottom,
			&topC, &botC, &status, &prev, &broken, &dead); err != nil {
			return nil, err
		}
		b.TopConfirmed = topC.String
		b.BottomConfirmed = botC.String
		b.BrokenDate = broken.String
		b.DeadDate = dead.String
		if prev.Valid {
			b.PrevBoxID = prev.Int64
		}
		b.Status = contract.BoxStatus(status)
		out = append(out, b)
	}
	return out, rows.Err()
}

// BreadthAgg 聚合全市场个股箱体突破/破位事件（M8.8 市场宽度标定）。
// 返回按 broken_date 聚合的 up/down 事件数；只统计 kind='stock' 且事件发生时
// 已有 minHistory 根以上日线的个股（JULIANDAY 用 1.5×minHistory 过滤次新股噪）。
//
// 注意：本方法只读 box 表；boxscan 增量路径必须保证破位事件及时入库
//（box/scanner.go 的 confirmed 箱体下轨破位兜底），否则宽度法分母会慢性欠账。
func (r *BoxRepo) BreadthAgg(from, to string, minHistory int) (upByDate, downByDate map[string]int, err error) {
	upByDate, downByDate = map[string]int{}, map[string]int{}
	minDays := float64(minHistory) * 1.5
	// 次新股过滤用相关子查询按 code 求首根K线日期——借助 idx_daily_bar_code_date
	// （migration 0011）每次索引定位 O(1)；旧写法 JOIN+GROUP BY 会扫全表分组（11s+）。
	// 事件日期按状态取列（docs/49 §五之二 特征一，0029）：
	//   broken_up   → broken_date（向上突破日）
	//   broken_down → dead_date  （向下破位日；broken_date 已不再记破位）
	// COALESCE 兜底旧库（0029 之前 broken_down 只有 broken_date），保证口径切换前后结果一致。
	rows, err := r.db.Query(`WITH ev AS (
			SELECT b.code, b.status,
			       CAST(CASE WHEN b.status = 'broken_up' THEN b.broken_date
			                 ELSE COALESCE(b.dead_date, b.broken_date) END AS TEXT) AS d
			FROM box b
			JOIN security s ON s.code = b.code AND s.kind = 'stock'
			WHERE b.status IN ('broken_up','broken_down')
		)
		SELECT ev.d, ev.status, COUNT(*)
		FROM ev
		WHERE ev.d >= ? AND ev.d <= ?
		  AND (JULIANDAY(ev.d) - JULIANDAY((
			SELECT MIN(fd.date) FROM daily_bar fd WHERE fd.code = ev.code)) + 1) >= ?
		GROUP BY ev.d, ev.status`, from, to, minDays)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var d, status string
		var n int
		if err := rows.Scan(&d, &status, &n); err != nil {
			return nil, nil, err
		}
		switch contract.BoxStatus(status) {
		case contract.BoxBrokenUp:
			upByDate[d] = n
		case contract.BoxBrokenDown:
			downByDate[d] = n
		}
	}
	return upByDate, downByDate, rows.Err()
}

func nullEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
