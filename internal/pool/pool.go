// Package pool 实现跟踪池状态机（docs/03-erd.md §2.2 / docs/00 §6）。
//
// 状态迁移（contract.PoolManager.Sync 主导入池与出池）：
//   → watching     candidate 当日入池（初选四条件通过）
//   watching→exited 箱体 broken_down（跌破下轨，止损离场）/ 连续 N 箱不涨 / 人工移除
//   watching→pending 由 planner 生成停损买单计划时设置（M4）
//   pending→holding 由成交回录设置（M4）
//   holding→exited 止损成交 / 突破失败 / 人工移除
package pool

import (
	"context"
	"log"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// Manager 跟踪池状态机（contract.PoolManager）。
type Manager struct {
	st  *store.Store
	log *log.Logger
}

// New 构造。
func New(st *store.Store) *Manager {
	return &Manager{st: st, log: log.New(log.Writer(), "[poolsync] ", log.LstdFlags)}
}

// Sync 当日跟踪池同步：入池新候选 + 更新当前箱体 + 出池跌破者。
func (m *Manager) Sync(ctx context.Context, date string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "poolsync", Date: date}
	flatMax := m.st.ParamRepo.GetInt("pool.flatboxmax", 4)

	// 1) 入池：今日候选中尚未在池（或已退出）的 → watching
	cands, err := m.st.PoolRepo.CandidatesOn(date)
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	entered := 0
	for _, c := range cands {
		existing, _ := m.st.PoolRepo.WatchByCode(c.Code)
		if existing != nil && existing.Stage != contract.StageExited {
			continue // 已在池（未退出），不重复入
		}
		// 入池为 watching
		box, _ := m.st.BoxRepo.LatestBox(c.Code)
		var boxID int64
		if box != nil {
			boxID = box.ID
		}
		m.st.PoolRepo.UpsertWatch(contract.Watch{
			Code:         c.Code,
			CandidateID:  0,
			AddedDate:     date,
			Stage:        contract.StageWatching,
			CurrentBoxID:  boxID,
			BoxCount:     0,
		})
		entered++
	}

	// 2) 更新在池股票的当前箱体 + 出池跌破者
	watching, err := m.st.PoolRepo.WatchByStage(contract.StageWatching)
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	exited := 0
	for _, w := range watching {
		box, _ := m.st.BoxRepo.LatestBox(w.Code)
		if box == nil {
			continue
		}
		// 更新当前箱体
		w.CurrentBoxID = box.ID
		// 跌破下轨 → 出池（docs/00 §5.1 止损信号）
		if box.Status == contract.BoxBrokenDown {
			w.Stage = contract.StageExited
			w.ExitReason = "box_broken_down"
			m.st.PoolRepo.UpsertWatch(w)
			exited++
			continue
		}
		// 连续 flatMax 箱不涨（box_count 超限）→ 出池（docs/01 FR-303）
		if w.BoxCount >= flatMax {
			w.Stage = contract.StageExited
			w.ExitReason = "flat_too_long"
			m.st.PoolRepo.UpsertWatch(w)
			exited++
			continue
		}
		m.st.PoolRepo.UpsertWatch(w)
	}
	res.Status = "ok"
	res.AffectedRows = int64(entered + exited)
	res.DurationMS = time.Since(start).Milliseconds()
	m.log.Printf("poolsync %s: entered=%d exited=%d, %s", date, entered, exited, time.Since(start).Round(time.Millisecond))
	return res, nil
}

// MarkFundamental 人工复核基本面（yes/no）。
func (m *Manager) MarkFundamental(code, date, flag string) error {
	_, err := m.st.DB().Exec(`UPDATE candidate SET fundamental_flag = ?
		WHERE code = ? AND screen_date = (SELECT MAX(screen_date) FROM candidate WHERE code = ?)`,
		flag, code, code)
	return err
}

// Remove 人工移除（出池）。
func (m *Manager) Remove(code, reason string) error {
	w, err := m.st.PoolRepo.WatchByCode(code)
	if err != nil || w == nil {
		return err
	}
	if w.Stage == contract.StageExited {
		return nil
	}
	w.Stage = contract.StageExited
	w.ExitReason = reason
	return m.st.PoolRepo.UpsertWatch(*w)
}
