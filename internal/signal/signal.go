// Package signal 实现达瓦斯四类信号判定（docs/00 §4-5 / docs/04 §4.4）。
//
// 首次事件语义（M7.1 观察期回放发现重复触发 bug 后确立）：
// 信号是"状态迁移事件"，只在迁移发生当日发一次；条件持续满足不重复发。
//
//	breakout        箱体当日迁移为 broken_up（BrokenDate==当日）→ 买（watching 突破）
//	pyramid         持仓股新箱体当日再突破（leg_no<maxlegs）→ 加码（holding 再突破）
//	stop            收盘首次跌破最新箱体下轨（昨收≥下轨 且 今收<下轨）→ 卖
//	failed_breakout 突破后3日内收盘首次跌回箱体上轨之下（昨收≥上轨 且 今收<上轨）→ 卖
//
// 另：AppendSignals 同日幂等（同 date+code+type+box 已存在则跳过），重跑/调度重试不重复。
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

// evalOne 对单只在池股票判定信号（首次事件语义）。优先级：stop > failed_breakout > breakout/pyramid。
func (e *Engine) evalOne(w contract.Watch, date string, premium float64, maxLegs int) *contract.Signal {
	// 修复 docs/07 #1：用 LatestNonFormingBox 而非 LatestBox——突破日引擎输出
	// [broken_up箱体, forming(0,0)箱体]，LatestBox(id DESC) 必取 forming 致四类信号全死。
	box, err := e.st.BoxRepo.LatestNonFormingBox(w.Code)
	if err != nil || box == nil {
		return nil
	}
	// 最近2根K线：昨日收盘用于判定"首次"（今日满足条件且昨日不满足 → 迁移事件）
	bars, err := e.st.BarRepo.BarsWindow(w.Code, date, 2)
	if err != nil || len(bars) < 2 {
		return nil // 单根K线无昨日参照（有箱体的股票必然有历史，防御性返回）
	}
	today, prev := bars[len(bars)-1], bars[len(bars)-2]
	if today.Date != date {
		return nil // 当日无数据（停牌等）
	}
	close := today.Close

	// stop：今收<最新箱体下轨 且 昨收未破（首次跌破，docs/00 §5.1）
	if box.Bottom > 0 && close < box.Bottom && prev.Close >= box.Bottom {
		return &contract.Signal{
			Date: date, Code: w.Code, Type: contract.SignalStop,
			BoxID: box.ID, ClosePx: close, BoxLevel: box.Bottom,
			Payload: map[string]any{"stage": string(w.Stage), "box_bottom": box.Bottom,
				"close": close, "prev_close": prev.Close},
		}
	}

	// failed_breakout：3日内突破的箱体，今收首次跌回其上轨之下
	if s := e.checkFailedBreakout(w.Code, date, box, close, prev.Close); s != nil {
		return s
	}

	// breakout / pyramid：箱体今日刚迁移为 broken_up（BrokenDate==今日）→ 突破事件当日一次。
	// （boxscan 引擎已用当日窗口完成放量/非一字板等形态判定，此处只消费状态迁移。）
	if box.Status == contract.BoxBrokenUp && box.BrokenDate == date {
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

// checkFailedBreakout 突破失败：最近一个 broken_up 箱体在3个日历日内、
// 今收首次跌回其上轨之下（昨收≥上轨 且 今收<上轨 → 首次跌回事件）。
func (e *Engine) checkFailedBreakout(code, date string, latest *contract.Box, close, prevClose float64) *contract.Signal {
	if latest == nil || latest.Top <= 0 {
		return nil
	}
	if latest.Status == contract.BoxBrokenUp && close < latest.Top && prevClose >= latest.Top &&
		withinDays(latest.BrokenDate, date, 3) {
		return &contract.Signal{
			Date: date, Code: code, Type: contract.SignalFailed,
			BoxID: latest.ID, ClosePx: close, BoxLevel: latest.Top,
			Payload: map[string]any{"broken_date": latest.BrokenDate,
				"box_top": latest.Top, "close": close, "prev_close": prevClose},
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
