// Package planner 实现达瓦斯六问 → 交易计划（docs/00 §4 / docs/04 §4.5）。
//
// 输入：当日 signal + 持仓 + param + equity_snapshot
// 输出：trade_plan + plan_order（停损买/卖单，含触发价、委托价、数量、腿号）
//
// 数量公式（FR-404）：qty = floor(equity × risk% / (trigger − stop) / 100) × 100（A股整手）
//   - breakout/pyramid → 停损买单：trigger = box.top × buy.premium，stop = box.bottom × sell.discount
//   - stop/failed_breakout → 止损卖单：trigger = box.bottom × sell.discount，qty = 持仓全部（一次全出）
package planner

import (
	"context"
	"log"
	"math"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// defaultEquity 无权益快照时的默认本金（可被 param equity.base 覆盖）。
const defaultEquity = 1000000.0

// Planner 计划生成器（contract.Planner）。
type Planner struct {
	st  *store.Store
	log *log.Logger
}

// New 构造。
func New(st *store.Store) *Planner {
	return &Planner{st: st, log: log.New(log.Writer(), "[plan] ", log.LstdFlags)}
}

// Build 生成次日交易计划（六问答案）。
func (p *Planner) Build(ctx context.Context, planDate string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "plan", Date: planDate}

	// 参数
	pLeg1 := p.st.ParamRepo.GetFloat("risk.leg1", 0.01)
	pLeg23 := p.st.ParamRepo.GetFloat("risk.leg23", 0.02)
	maxPos := p.st.ParamRepo.GetFloat("risk.maxpos", 0.20)
	buyPrem := p.st.ParamRepo.GetFloat("buy.premium", 1.03)
	sellDisc := p.st.ParamRepo.GetFloat("sell.discount", 0.97)
	maxLegs := p.st.ParamRepo.GetInt("risk.maxlegs", 3)

	// 权益基准
	eq, _ := p.st.TradeRepo.LatestEquity()
	equity := defaultEquity
	if eq != nil {
		equity = eq.Total
	}
	// 若无任何快照，初始化一个默认基准（首次运行）
	if eq == nil {
		p.st.TradeRepo.SetEquityDefault(planDate, equity)
	}
	// 信号
	sigs, err := p.st.SignalRepo.SignalsOn(planDate)
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	// 持仓（决定卖出数量 + 加码腿号）
	positions, err := p.st.TradeRepo.OpenPositions()
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	posByCode := map[string]*contract.Position{}
	for i := range positions {
		posByCode[positions[i].Code] = &positions[i]
	}

	executeDate := nextDay(planDate)
	var orders []contract.PlanOrder
	for _, s := range sigs {
		box, _ := p.st.BoxRepo.LatestBox(s.Code)
		if box == nil {
			continue
		}
		switch s.Type {
		case contract.SignalBreakout, contract.SignalPyramid:
			o := p.buyOrder(s, box, equity, pLeg1, pLeg23, maxPos, buyPrem, sellDisc, maxLegs, posByCode, planDate, executeDate)
			if o != nil {
				orders = append(orders, *o)
			}
		case contract.SignalStop, contract.SignalFailed:
			o := p.sellOrder(s, box, sellDisc, posByCode, planDate, executeDate)
			if o != nil {
				orders = append(orders, *o)
			}
		}
	}

	// 创建计划
	planID, err := p.st.TradeRepo.CreatePlan(contract.TradePlan{
		PlanDate:          planDate,
		ExecuteDate:       executeDate,
		EquityBase:        equity,
		OpenPositionCount: len(positions),
	})
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	if err := p.st.TradeRepo.AddOrders(planID, orders); err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	res.Status = "ok"
	res.AffectedRows = int64(len(orders))
	res.DurationMS = time.Since(start).Milliseconds()
	p.log.Printf("plan %s→%s: %d signals → %d orders, equity=%.0f, %s",
		planDate, executeDate, len(sigs), len(orders), equity, time.Since(start).Round(time.Millisecond))
	return res, nil
}

// buyOrder 构造停损买单（breakout=leg1, pyramid=leg2/3）。
func (p *Planner) buyOrder(s contract.Signal, box *contract.Box, equity, leg1, leg23, maxPos, buyPrem, sellDisc float64,
	maxLegs int, posByCode map[string]*contract.Position, planDate, execDate string) *contract.PlanOrder {
	trigger := box.Top * buyPrem
	stop := box.Bottom * sellDisc
	riskPerUnit := trigger - stop
	if riskPerUnit <= 0 || box.Bottom <= 0 {
		return nil // 无下轨或触发≤止损，不生成买单
	}
	// 腿号：breakout=第1腿（测试单）；pyramid=第2/3腿（确认/加码）
	leg := 1
	riskPct := leg1
	if s.Type == contract.SignalPyramid {
		if _, ok := posByCode[s.Code]; ok {
			leg = 3 // 已有持仓的加码 → 第3腿
		} else {
			leg = 2 // 第2腿（确认单）
		}
		riskPct = leg23
	}
	if leg > maxLegs {
		return nil
	}
	// 数量公式 + 单股仓位上限（FR-404）
	qty := int(math.Floor(equity*riskPct/riskPerUnit/100)) * 100
	maxQty := int(math.Floor(equity*maxPos/trigger/100)) * 100
	if qty > maxQty {
		qty = maxQty
	}
	if qty <= 0 {
		return nil
	}
	return &contract.PlanOrder{
		PlanDate: planDate, ExecuteDate: execDate, Code: s.Code,
		Side: "buy", OrderKind: "stop_buy",
		TriggerPrice: trigger, LimitPrice: trigger, Qty: qty, LegNo: leg,
		Reason: map[string]any{
			"signal": string(s.Type), "box_top": box.Top, "box_bottom": box.Bottom,
			"trigger": trigger, "stop": stop, "risk_pct": riskPct, "equity": equity, "leg": leg,
		},
	}
}

// sellOrder 构造止损卖单（一次全出）。
func (p *Planner) sellOrder(s contract.Signal, box *contract.Box, sellDisc float64,
	posByCode map[string]*contract.Position, planDate, execDate string) *contract.PlanOrder {
	pos, ok := posByCode[s.Code]
	if !ok {
		return nil // 无持仓不生成卖单（watching 的 stop 由 poolsync 出池处理）
	}
	trigger := box.Bottom * sellDisc
	return &contract.PlanOrder{
		PlanDate: planDate, ExecuteDate: execDate, Code: s.Code,
		Side: "sell", OrderKind: "stop_sell",
		TriggerPrice: trigger, LimitPrice: trigger, Qty: pos.Qty, LegNo: 0,
		Reason: map[string]any{
			"signal": string(s.Type), "box_bottom": box.Bottom,
			"trigger": trigger, "qty": pos.Qty, "position_id": pos.ID,
		},
	}
}

// nextDay 下一日历日（T+1 执行日；精确交易日历由调度器/日历模块处理，此处近似）。
func nextDay(date string) string {
	t, err := time.Parse("2006-01-02", date)
	if err != nil {
		return date
	}
	return t.AddDate(0, 0, 1).Format("2006-01-02")
}
