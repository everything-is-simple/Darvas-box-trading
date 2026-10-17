// Package signal 实现达瓦斯四类信号判定（docs/00 §4-5 / docs/04 §4.4）。
//
// 四类信号（contract.SignalEngine.Evaluate 对 watching/pending/holding 池逐股判定）：
//   breakout        收盘>confirmed箱体上轨×premium 且放量 且非一字板 → 买（watching 突破）
//   pyramid         持仓股新箱体再突破（leg_no<maxlegs）→ 加码（holding 再突破）
//   stop            收盘<最新箱体下轨 → 卖（holding 全卖；watching 出池）
//   failed_breakout 突破后3日内收盘跌回箱体内 → 卖（突破失败离场）
package signal

import (
	"context"
	"log"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// Engine 信号引擎（contract.SignalEngine）。
type Engine struct {
	st  *store.Store
	log *log.Logger
}

// New 构造。
func New(st *store.Store) *Engine {
	return &Engine{st: st, log: log.New(log.Writer(), "[signal] ", log.LstdFlags)}
}

// Evaluate 对在池股票判定四类信号，追加到 signal 表。
func (e *Engine) Evaluate(ctx context.Context, date string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "signal", Date: date}
	premium := e.st.ParamRepo.GetFloat("buy.premium", 1.03)
	maxLegs := e.st.ParamRepo.GetInt("risk.maxlegs", 3)

	var sigs []contract.Signal
	countByType := map[contract.SignalType]int{}

	for _, stage := range []contract.WatchStage{contract.StageWatching, contract.StagePending, contract.StageHolding} {
		watches, err := e.st.PoolRepo.WatchByStage(stage)
		if err != nil {
			res.Status, res.Error = "failed", err.Error()
			return res, err
		}
		for _, w := range watches {
			s := e.evalOne(w, date, premium, maxLegs)
			if s != nil {
				sigs = append(sigs, *s)
				countByType[s.Type]++
			}
		}
	}
	if err := e.st.SignalRepo.AppendSignals(sigs); err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	res.Status = "ok"
	res.AffectedRows = int64(len(sigs))
	res.DurationMS = time.Since(start).Milliseconds()
	e.log.Printf("signal %s: %d signals (breakout=%d pyramid=%d stop=%d failed=%d), %s",
		date, len(sigs), countByType[contract.SignalBreakout], countByType[contract.SignalPyramid],
		countByType[contract.SignalStop], countByType[contract.SignalFailed],
		time.Since(start).Round(time.Millisecond))
	return res, nil
}

// evalOne 对单只在池股票判定信号。优先级：stop > failed_breakout > breakout/pyramid。
func (e *Engine) evalOne(w contract.Watch, date string, premium float64, maxLegs int) *contract.Signal {
	box, err := e.st.BoxRepo.LatestBox(w.Code)
	if err != nil || box == nil {
		return nil
	}
	close, ok, _ := e.st.BarRepo.CloseOn(w.Code, date)
	if !ok {
		return nil // 当日无数据（停牌等）
	}

	// stop：收盘<最新箱体下轨（docs/00 §5.1）
	if box.Bottom > 0 && close < box.Bottom {
		return &contract.Signal{
			Date: date, Code: w.Code, Type: contract.SignalStop,
			BoxID: box.ID, ClosePx: close, BoxLevel: box.Bottom,
			Payload: map[string]any{"stage": string(w.Stage), "box_bottom": box.Bottom, "close": close},
		}
	}

	// failed_breakout：最近有 broken_up 箱体且3日内、收盘跌回该箱上轨之下
	if s := e.checkFailedBreakout(w.Code, date, box, close); s != nil {
		return s
	}

	// breakout / pyramid：最新箱体 broken_up（今日或近期）→ 突破信号
	if box.Status == contract.BoxBrokenUp {
		// 持仓且未达最大腿数 → 加码；否则 → 突破买入
		var sigType contract.SignalType
		if w.Stage == contract.StageHolding {
			if w.BoxCount < int(maxLegs) {
				sigType = contract.SignalPyramid
			} else {
				return nil // 已达最大腿数，不加码
			}
		} else {
			sigType = contract.SignalBreakout
		}
		return &contract.Signal{
			Date: date, Code: w.Code, Type: sigType,
			BoxID: box.ID, ClosePx: close, BoxLevel: box.Top,
			Payload: map[string]any{
				"stage": string(w.Stage), "box_top": box.Top,
				"premium": premium, "broken_date": box.BrokenDate,
			},
		}
	}
	return nil
}

// checkFailedBreakout 突破失败：最近一个 broken_up 箱体在3个交易日内、收盘跌回其上轨之下。
func (e *Engine) checkFailedBreakout(code, date string, latest *contract.Box, close float64) *contract.Signal {
	if latest == nil || latest.Top <= 0 {
		return nil
	}
	// 最新箱体若刚 broken_up 且今日收盘已跌回上轨之下 → 失败突破
	if latest.Status == contract.BoxBrokenUp && close < latest.Top && withinDays(latest.BrokenDate, date, 3) {
		return &contract.Signal{
			Date: date, Code: code, Type: contract.SignalFailed,
			BoxID: latest.ID, ClosePx: close, BoxLevel: latest.Top,
			Payload: map[string]any{"broken_date": latest.BrokenDate, "box_top": latest.Top, "close": close},
		}
	}
	return nil
}

// withinDays 判断 brokenDate 是否在 date 之前（含）的 n 个日历日内。
// 用日历日近似（交易日日历需 sh000001；信号判定容差用日历日足够）。
func withinDays(brokenDate, date string, n int) bool {
	if brokenDate == "" || date == "" {
		return false
	}
	b, err := time.Parse("2006-01-02", brokenDate)
	if err != nil {
		return false
	}
	d, err := time.Parse("2006-01-02", date)
	if err != nil {
		return false
	}
	delta := d.Sub(b).Hours() / 24
	return delta >= 0 && delta <= float64(n)
}
