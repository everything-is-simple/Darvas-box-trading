// Package planner 实现达瓦斯六问 → 交易计划（docs/00 §4 / docs/04 §4.5）。
//
// 输入：当日 signal + 持仓 + param + equity_snapshot
// 输出：trade_plan + plan_order（停损买/卖单，含触发价、委托价、数量、腿号）
//
// 数量公式（FR-404）：qty = floor(equity × risk% / (trigger − stop) / 100) × 100（A股整手）
// 动态仓位（B4 2026-10-18，原书§4.1「试探→放大→自设上限」）：腿1试探单钳制到
// 名义≤权益×risk.probe_max_pct 且 ≥risk.probe_min_shares 股；腿3加码=持仓×risk.scale
// 倍且加码后总名义≤risk.maxpos（单股上限）；参数见 migrations/0005，详见 buyOrder。
//   - breakout → 腿1停损买单：trigger = box.top × buy.premium，stop = box.top × sell.discount
//     （B1修正2026-10-18：首腿紧止损=被突破箱体上轨/突破点下方，原书§4.1；旧实现误挂箱体下轨下方）
//   - pyramid → 腿2/3停损买单：trigger = box.top × buy.premium，stop = box.bottom × sell.discount
//   - stop/failed_breakout → 止损卖单：trigger = box.bottom × sell.discount，qty = 持仓全部（一次全出）
package planner

import (
	"context"
	"log"
	"math"
	"time"

	"darvas/internal/contract"
	"darvas/internal/ledger"
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

// Build 生成下一交易日交易计划（六问答案；执行日按 ADR-2 取 sh000001 下一交易日，
// 实盘无未来数据时跳周末兜底，见 nextTradingDate）。
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

	// M4.4 止损上移（docs/00 §5.2：新箱体确认 → 止损上移到新箱下轨下方）。
	// DB 触发器保证只升不降（降则 ABORT），此处仅在候选值更高时调用。
	stopMoves := p.raiseStops(positions, sellDisc)

	// 已有在途卖出单的股票：避免重复生成（time_stop 持续条件每日重复挂单，
	// 撮合清仓后剩余单「卖出无持仓」——M8.3 暴露，docs/09）。
	pendingSell := map[string]bool{}
	if codes, err := p.st.TradeRepo.PendingSellCodes(); err == nil {
		for _, c := range codes {
			pendingSell[c] = true
		}
	}

	executeDate := p.nextTradingDate(planDate)
	var orders []contract.PlanOrder
	for _, s := range sigs {
		// 修复 docs/07 #1：用 LatestNonFormingBox——LatestBox 取 forming(0,0) 行致
		// box.Top/Bottom=0，buyOrder 因 box.Bottom<=0 直接 return nil，订单永不生成。
		box, _ := p.st.BoxRepo.LatestNonFormingBox(s.Code)
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
			if pendingSell[s.Code] {
				continue // 已有在途卖出单，不重复生成（M8.3）
			}
			o := p.sellOrder(s, box, sellDisc, posByCode, planDate, executeDate)
			if o != nil {
				orders = append(orders, *o)
			}
		}
	}

	// B2⑤ 不获利持仓约束（原书：买入后3周仍不获利→退出——资金效率纪律，
	// 非箱体事件，直接生成卖出计划；持有交易日数=BarCount(opened_date, planDate)）。
	staleDays := p.st.ParamRepo.GetInt("position.staledays", 15)
	for i := range positions {
		pos := &positions[i]
		if pos.Qty <= 0 {
			continue
		}
		if pendingSell[pos.Code] {
			continue // 已有在途卖出单，不重复生成（M8.3）
		}
		n, err := p.st.BarRepo.BarCount(pos.Code, pos.OpenedDate, planDate)
		if err != nil || n < staleDays {
			continue
		}
		px, ok, _ := p.st.BarRepo.CloseOn(pos.Code, planDate)
		if !ok || px > pos.AvgCost {
			continue
		}
		orders = append(orders, contract.PlanOrder{
			PlanDate: planDate, ExecuteDate: executeDate, Code: pos.Code,
			Side: "sell", OrderKind: "stop_sell",
			TriggerPrice: px, LimitPrice: px, Qty: pos.Qty, LegNo: 0,
			Reason: map[string]any{
				"signal": "time_stop_3weeks", "days_held": n,
				"avg_cost": pos.AvgCost, "last_close": px, "qty": pos.Qty, "position_id": pos.ID,
			},
		})
		p.log.Printf("3周不获利离场 %s（持有%d日 收盘%.2f≤成本%.2f）", pos.Code, n, px, pos.AvgCost)
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

	// B2 接线（docs/03 §2.2 状态机）：生成停损买单 → watch watching→pending。
	for i := range orders {
		if orders[i].Side != "buy" {
			continue
		}
		if w, _ := p.st.PoolRepo.WatchByCode(orders[i].Code); w != nil && w.Stage == contract.StageWatching {
			w.Stage = contract.StagePending
			p.st.PoolRepo.UpsertWatch(*w)
		}
	}

	// 收尾：过期未成交订单（执行日已过的 pending → expired）+ 当日权益快照
	if n, err := p.st.TradeRepo.ExpireStaleOrders(planDate); err == nil && n > 0 {
		p.log.Printf("过期订单 %d 笔（执行日已过未成交）", n)
	}
	// B2 接线：买单过期后 watch pending→watching（无在途买单即回观察态）
	if err := p.st.PoolRepo.RelaxPendingWatches(); err != nil {
		p.log.Printf("⚠ pending watch 回退失败: %v", err)
	}
	if err := ledger.New(p.st).Snapshot(ctx, planDate); err != nil {
		p.log.Printf("⚠ 权益快照失败: %v", err)
	}
	res.Status = "ok"
	res.AffectedRows = int64(len(orders))
	res.DurationMS = time.Since(start).Milliseconds()
	p.log.Printf("plan %s→%s: %d signals → %d orders, equity=%.0f, 止损上移=%d, %s",
		planDate, executeDate, len(sigs), len(orders), equity, stopMoves, time.Since(start).Round(time.Millisecond))
	return res, nil
}

// raiseStops 止损上移（M4.4）：对每只持仓，若最新箱体下轨已3日确认
// 且 新止损 = 下轨×sellDisc 高于现止损 → UpdateStop（触发器兜底拒绝下降）。
// 入场箱体本身不参与（B1修正后：腿1初始止损=其上轨下方、腿2/3初始止损=其下轨下方，
// 均不随入场箱体自身重算）。返回上移笔数。
//
// 修复 docs/07 #1：用 LatestNonFormingBox——LatestBox 取 forming(bottom=0) 致
// `lb.Bottom <= 0 → continue`，止损上移永不触发。
func (p *Planner) raiseStops(positions []contract.Position, sellDisc float64) int {
	moves := 0
	for i := range positions {
		pos := &positions[i]
		lb, err := p.st.BoxRepo.LatestNonFormingBox(pos.Code)
		if err != nil || lb == nil {
			continue
		}
		// 下轨未3日确认（forming/未确认）不作为止损依据
		if lb.Bottom <= 0 || lb.BottomConfirmed == "" {
			continue
		}
		// 入场箱体不算上移
		if lb.ID == pos.EntryBoxID {
			continue
		}
		cand := lb.Bottom * sellDisc
		if cand > pos.StopPrice {
			if err := p.st.TradeRepo.UpdateStop(pos.ID, cand); err != nil {
				p.log.Printf("⚠ 止损上移被拒(触发器): pos#%d %s: %v", pos.ID, pos.Code, err)
				continue
			}
			p.log.Printf("止损上移 %s: %.2f → %.2f（新箱体 #%d 下轨 %.2f）",
				pos.Code, pos.StopPrice, cand, lb.ID, lb.Bottom)
			moves++
		}
	}
	return moves
}

// buyOrder 构造停损买单（breakout=leg1, pyramid=leg2/3）。
// 动态仓位（B4 2026-10-18，原书§4.1「试探→放大→自设上限」，参数见 0005 migration）：
//   - 腿1 试探单：风险公式基准钳制到名义≤权益×probe_max_pct/触发价，且不低于
//     probe_min_shares 股（上限允许时）——原书"首批200-500股小仓"的A股适配；
//   - 腿2 确认单（pyramid 但无持仓=新入场）：风险公式(2%) + 单股上限；
//   - 腿3 加码单（pyramid 且有持仓）：max(风险公式(2%), 持仓股数×scale)，且加码后
//     总名义不超过单股上限（room=maxQty−持仓；不足1手则不加码）——原书"确认后放大3-10倍"。
func (p *Planner) buyOrder(s contract.Signal, box *contract.Box, equity, leg1, leg23, maxPos, buyPrem, sellDisc float64,
	maxLegs int, posByCode map[string]*contract.Position, planDate, execDate string) *contract.PlanOrder {
	trigger := box.Top * buyPrem
	// 初始止损（B1修正，原书§4.1）：腿1=被突破箱体上轨（突破点）下方——紧止损；
	// 腿2/3=新箱体下轨下方（新箱体确立后止损上移至其下轨，符合原书阶梯规则）。
	stop := box.Bottom * sellDisc
	if s.Type == contract.SignalBreakout {
		stop = box.Top * sellDisc
	}
	riskPerUnit := trigger - stop
	if riskPerUnit <= 0 || box.Bottom <= 0 {
		return nil // 无下轨或触发≤止损，不生成买单
	}
	// 腿号：breakout=第1腿（测试单）；pyramid=第2/3腿（确认/加码）
	leg := 1
	riskPct := leg1
	var pos *contract.Position
	if s.Type == contract.SignalPyramid {
		if pp, ok := posByCode[s.Code]; ok {
			leg = 3 // 已有持仓的加码 → 第3腿
			pos = pp
		} else {
			leg = 2 // 第2腿（确认单）
		}
		riskPct = leg23
	}
	if leg > maxLegs {
		return nil
	}
	// 动态仓位参数（B4）
	probeMin := p.st.ParamRepo.GetInt("risk.probe_min_shares", 200)
	probeMaxPct := p.st.ParamRepo.GetFloat("risk.probe_max_pct", 0.05)
	scale := p.st.ParamRepo.GetInt("risk.scale", 5)
	// 数量公式 + 单股仓位上限（FR-404）；maxQty=自设单股名义上限
	qty := int(math.Floor(equity*riskPct/riskPerUnit/100)) * 100
	maxQty := int(math.Floor(equity*maxPos/trigger/100)) * 100
	// 腿3 加码：原书"确认后放大"——以持仓股数为基放大 scale 倍（不低于风险公式）
	if leg == 3 && pos != nil && pos.Qty > 0 {
		if scaled := pos.Qty / 100 * scale * 100; scaled > qty {
			qty = scaled
		}
	}
	// 上限：腿1=试探仓名义上限；腿3=加码后总名义≤单股上限（room=maxQty−持仓）；其余=maxQty
	upper := maxQty
	if leg == 1 {
		if probeUpper := int(math.Floor(equity*probeMaxPct/trigger/100)) * 100; probeUpper < upper {
			upper = probeUpper
		}
	} else if leg == 3 && pos != nil {
		if room := maxQty - pos.Qty; room < upper {
			upper = room
		}
	}
	if qty > upper {
		qty = upper
	}
	// 下限：试探仓不低于原书绝对下限（上限允许时）；其余腿 A股整手 100 股
	if leg == 1 && qty < probeMin && probeMin <= upper {
		qty = probeMin
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
			"probe_min_shares": probeMin, "probe_max_pct": probeMaxPct, "scale": scale, "max_qty": maxQty,
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

// nextTradingDate 下一交易日（ADR-2 交易日历来自数据本身）：优先取 sh000001 日线中
// planDate 之后的最近日期（回放/补跑场景未来K线已入库，跨节假日的执行日精确）；
// 无未来数据时（实盘当日 plan 任务时，DB 仅含至 planDate；时点见 scheduler.Schedule）退化为跳过周末的下一工作日
// ——节假日无法从历史数据预判（ADR-2 不内置日历库），假日偏差由 ExpireStaleOrders
// 过期机制兜底，不产生错误成交。
// 修复（2026-10-17）：此前用日历日+1 近似，周五信号会产生周六死单（sh600052 2026-10-09 → exec 10-10）。
func (p *Planner) nextTradingDate(planDate string) string {
	if d, err := p.st.BarRepo.NextDate("sh000001", planDate); err == nil && d != "" {
		return d
	}
	return nextWeekday(planDate)
}

// nextWeekday 下一工作日（跳过周六日；仅作实盘无未来数据时的兜底）。
func nextWeekday(date string) string {
	t, err := time.Parse("2006-01-02", date)
	if err != nil {
		return date
	}
	for t = t.AddDate(0, 0, 1); ; t = t.AddDate(0, 0, 1) {
		if wd := t.Weekday(); wd != time.Saturday && wd != time.Sunday {
			return t.Format("2006-01-02")
		}
	}
}
