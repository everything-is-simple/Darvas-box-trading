// Package pool 实现跟踪池状态机（docs/03-erd.md §2.2 / docs/00 §6）。
//
// 状态迁移（contract.PoolManager.Sync 主导入池与出池）：
//
//	→ watching     candidate 当日入池（初选四条件通过；黑名单/两亏/超上限拦截）
//	watching→pending 由 planner 生成停损买单计划时设置（M4，B2接线）
//	pending→holding 由成交回录设置（M4，B2接线）
//	holding→exited 止损成交（清仓）/ 人工移除
//	watching→exited 箱体 broken_down（跌破下轨，止损离场）/ 连续 N 箱不涨 / 人工移除
//	*→blacklisted   两亏拉黑（同一股票亏损交易满 pool.maxloss 次，永久；B2）
//	*→suspended     条件挂起（"停传报价"：人工暂停，收盘≥resume_trigger 再激活；B2）
//	suspended→watching 激活（poolsync 每日检查最新收盘≥激活价）
package pool

import (
	"context"
	"fmt"
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

// Sync 当日跟踪池同步：入池新候选 + 更新当前箱体 + 出池跌破者 + 淘汰生命周期（B2）。
func (m *Manager) Sync(ctx context.Context, date string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "poolsync", Date: date}
	flatMax := m.st.ParamRepo.GetInt("pool.flatboxmax", 4)
	maxWatch := m.st.ParamRepo.GetInt("pool.maxwatch", 8)
	maxLoss := m.st.ParamRepo.GetInt("pool.maxloss", 2)

	// 1) 入池：今日候选中尚未在池（或已退出）的 → watching
	cands, err := m.st.PoolRepo.CandidatesOn(date)
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	entered := 0
	for _, c := range cands {
		// B2③ 两亏拉黑：亏损交易次数已达阈值 → 永久不入池
		if n, err := m.st.TradeRepo.LossCountByCode(c.Code); err == nil && n >= maxLoss {
			m.log.Printf("两亏拉黑拦截入池 %s（历史亏损%d次≥%d）", c.Code, n, maxLoss)
			continue
		}
		existing, _ := m.st.PoolRepo.WatchByCode(c.Code)
		if existing != nil && existing.Stage != contract.StageExited {
			continue // 已在池（未退出），不重复入（blacklisted/suspended 也在此列）
		}
		// B2④ 跟踪5-8支上限：活跃池（watching+pending+holding）满则不再入
		active, _ := m.st.PoolRepo.ActiveWatchCount()
		if active >= maxWatch {
			m.log.Printf("跟踪池满（%d≥pool.maxwatch=%d），跳过入池 %s", active, maxWatch, c.Code)
			break
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
			AddedDate:    date,
			Stage:        contract.StageWatching,
			CurrentBoxID: boxID,
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

	// 3) B2 淘汰生命周期：
	//   ③两亏拉黑：活跃池股票亏损次数达标 → blacklisted
	//   ⑤条件挂起激活：suspended 股票收盘≥激活价 → watching
	blacklisted, resumed := m.lifecycle(date, maxLoss)

	res.Status = "ok"
	res.AffectedRows = int64(entered + exited + blacklisted + resumed)
	res.DurationMS = time.Since(start).Milliseconds()
	m.log.Printf("poolsync %s: entered=%d exited=%d blacklisted=%d resumed=%d, %s",
		date, entered, exited, blacklisted, resumed, time.Since(start).Round(time.Millisecond))
	return res, nil
}

// lifecycle 淘汰生命周期巡检（B2）：
//  1. 两亏拉黑——watching/pending/holding 股票若历史亏损交易次数 ≥ maxLoss → blacklisted
//     （清仓由 ledger 完成，poolsync 次日兜底拉黑；pending/holding 一般不会有两亏仍留池，防御性处理）
//  2. 条件挂起激活——suspended 股票最新收盘 ≥ resume_trigger → 回 watching（"突破X再传"）
func (m *Manager) lifecycle(date string, maxLoss int) (blacklisted, resumed int) {
	for _, stage := range []contract.WatchStage{contract.StageWatching, contract.StagePending, contract.StageHolding} {
		ws, err := m.st.PoolRepo.WatchByStage(stage)
		if err != nil {
			continue
		}
		for _, w := range ws {
			if n, err := m.st.TradeRepo.LossCountByCode(w.Code); err == nil && n >= maxLoss {
				w.Stage = contract.StageBlacklisted
				w.ExitReason = fmt.Sprintf("two_losses_%d", n)
				m.st.PoolRepo.UpsertWatch(w)
				m.log.Printf("两亏拉黑 %s（历史亏损%d次）", w.Code, n)
				blacklisted++
			}
		}
	}
	sus, err := m.st.PoolRepo.WatchByStage(contract.StageSuspended)
	if err != nil {
		return blacklisted, 0
	}
	for _, w := range sus {
		if w.ResumeTrigger <= 0 {
			continue
		}
		px, ok, _ := m.st.BarRepo.CloseOn(w.Code, date)
		if !ok {
			continue // 停牌/无行情，顺延
		}
		if px >= w.ResumeTrigger {
			trigger := w.ResumeTrigger
			w.Stage = contract.StageWatching
			w.ResumeTrigger = 0
			w.Note = fmt.Sprintf("%s 挂起激活（收盘%.2f≥%.2f）", date, px, trigger)
			m.st.PoolRepo.UpsertWatch(w)
			m.log.Printf("挂起激活 %s（收盘%.2f ≥ 激活价%.2f）", w.Code, px, trigger)
			resumed++
		}
	}
	return blacklisted, resumed
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

// Suspend 条件挂起（B2⑤，"停传报价"的A股等价）：暂停跟踪，收盘≥resumeTrigger 再激活。
// GUI 只读原则：本操作走 CLI（darvas pool suspend <code> --resume <价>）。
func (m *Manager) Suspend(code string, resumeTrigger float64) error {
	w, err := m.st.PoolRepo.WatchByCode(code)
	if err != nil || w == nil {
		return fmt.Errorf("%s 不在跟踪池", code)
	}
	if w.Stage == contract.StageExited || w.Stage == contract.StageBlacklisted {
		return fmt.Errorf("%s 当前为 %s，无需挂起", code, w.Stage)
	}
	w.Stage = contract.StageSuspended
	w.ResumeTrigger = resumeTrigger
	return m.st.PoolRepo.UpsertWatch(*w)
}
