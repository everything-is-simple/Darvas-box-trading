// Package planner 实现达瓦斯六问 → 交易计划（docs/00 §4 / docs/04 §4.5）。
//
// 输入：当日 signal + 持仓 + param + equity_snapshot
// 输出：trade_plan + plan_order（停损买/卖单，含触发价、委托价、数量、腿号）
//
// 数量公式（M8.8 单位制，archive/13 §3）：1 单位 = 权益 × pos.unit（默认 2%），
// 单位股数 = floor(equity × pos.unit / price / 100) × 100（A股整手）。
// 三层预算：试探 5 单位 + 主力 30 单位 + 预备 15 单位永不动；永远留 30% 现金。
// 方案 A（2026-10-21，第0条撤回连锁）：不择时——首腿统一 1 单位试探，任何市况
// 都允许金字塔加码；试探满后改吃主力预算（组合风控保留），体制标签仅作观察记录。
//   - breakout → 腿1停损买单：trigger = box.top × buy.premium，stop = box.top × sell.discount
//     （B1修正2026-10-18：首腿紧止损=被突破箱体上轨/突破点下方，原书§4.1；旧实现误挂箱体下轨下方）
//   - pyramid → 腿2/3停损买单：trigger = box.top × buy.premium，stop = box.bottom × sell.discount
//     （腿2 为 poolsync 时滞防御分支，见 buyOrder；原「熊猴不生成」已随方案A废止）
//   - stop/failed_breakout → 止损卖单：trigger = box.bottom × sell.discount，qty = 持仓全部（一次全出）
package planner

import (
	"context"
	"log"
	"math"
	"sort"
	"time"

	"darvas/internal/contract"
	"darvas/internal/ledger"
	"darvas/internal/regime"
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

	// 参数（M8.8 单位制；旧 risk.leg1/leg23/maxpos/probe_* 保留禁用，不再读取）
	buyPrem := p.st.ParamRepo.GetFloat("buy.premium", 1.03)
	sellDisc := p.st.ParamRepo.GetFloat("sell.discount", 0.97)
	maxLegs := p.st.ParamRepo.GetInt("risk.maxlegs", 3)
	unitPct := p.st.ParamRepo.GetFloat("pos.unit", 0.02)
	probeCap := p.st.ParamRepo.GetFloat("pos.units.probe", 5)
	mainCap := p.st.ParamRepo.GetFloat("pos.units.main", 30)
	probeBear := p.st.ParamRepo.GetFloat("pos.units.per_stock_probe_bear", 1)
	// 方案A（2026-10-21）：首腿统一 1 单位试探，不分牛熊——per_stock_probe_bull 已废弃不读。
	mainMax := p.st.ParamRepo.GetFloat("pos.units.per_stock_main_max", 10)
	mainStep := p.st.ParamRepo.GetFloat("pos.units.per_stock_main_step", 3)
	// G1 观察期（2026-10-21）：入池后 N 个交易日内不产生首笔买入信号（原书 L1895/L1063）。
	minWatchDays := p.st.ParamRepo.GetInt("pool.min_watch_days", 3)
	// P0-2（2026-10-21）：进场跳空上限（成交/trigger 偏离上限，默认 +5%）。
	maxEntryGap := p.st.ParamRepo.GetFloat("match.max_entry_gap", 1.05)

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
	unitValue := equity * unitPct
	market := p.regimeLabel(planDate)

	// M8.8 组合级预算：试探满 5 不加新试探，主力满 30 不再加码。
	openProbe, openMain, err := p.st.TradeRepo.OpenExposureUnits(unitValue)
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	budget := &unitBudget{probeUsed: openProbe, mainUsed: openMain, probeCap: probeCap, mainCap: mainCap}
	plannedQty := map[string]int{} // 同日多信号同票时，避免重复超配

	// 信号
	sigs, err := p.st.SignalRepo.SignalsOn(planDate)
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	// G2 老朋友优先（2026-10-21，docs/00 §6 池4）：连续 2 笔盈利的"老朋友"再发
	// breakout 信号时排前，竞争试探预算时优先成交（止损不动）。稳定排序，其余顺序不变。
	oldFriends := map[string]bool{}
	for _, s := range sigs {
		if s.Type != contract.SignalBreakout {
			continue
		}
		if ok, err := p.st.TradeRepo.IsOldFriend(s.Code); err == nil && ok {
			oldFriends[s.Code] = true
		}
	}
	if len(oldFriends) > 0 {
		sort.SliceStable(sigs, func(i, j int) bool {
			return oldFriends[sigs[i].Code] && !oldFriends[sigs[j].Code]
		})
		p.log.Printf("老朋友优先 %d 支：%v", len(oldFriends), mapKeys(oldFriends))
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
	// 撮合清仓后剩余单「卖出无持仓」——M8.3 暴露，archive/09）。
	pendingSell := map[string]bool{}
	if codes, err := p.st.TradeRepo.PendingSellCodes(); err == nil {
		for _, c := range codes {
			pendingSell[c] = true
		}
	}

	executeDate := p.nextTradingDate(planDate)
	var orders []contract.PlanOrder
	for _, s := range sigs {
		// 修复 archive/07 #1：用 LatestNonFormingBox——LatestBox 取 forming(0,0) 行致
		// box.Top/Bottom=0，buyOrder 因 box.Bottom<=0 直接 return nil，订单永不生成。
		box, _ := p.st.BoxRepo.LatestNonFormingBox(s.Code)
		if box == nil {
			continue
		}
		switch s.Type {
		case contract.SignalBreakout, contract.SignalPyramid:
			// G1 观察期（2026-10-21）：仅首腿 breakout 受观察期约束——新票入池后
			// min_watch_days 个交易日内不产生首笔买单（原书 L1895「投资前仔细研究」+L1063「继续关注数日」）。
			// pyramid（持仓加码）不受限。
			if s.Type == contract.SignalBreakout && minWatchDays > 0 {
				w, _ := p.st.PoolRepo.WatchByCode(s.Code)
				if w != nil && w.AddedDate != "" {
					if n, err := p.st.BarRepo.BarCount(s.Code, w.AddedDate, planDate); err == nil && n < minWatchDays {
						p.log.Printf("观察期拦截 %s 首买：入池%d日 < min_watch_days=%d", s.Code, n, minWatchDays)
						continue
					}
				}
			}
			// M8.8：单位制仓位 + 组合级预算检查（熊猴 pyramid 在 buyOrder 内硬拦截）
			o := p.buyOrder(s, box, market, unitValue, probeBear, mainMax, mainStep,
				buyPrem, sellDisc, maxLegs, posByCode, budget, plannedQty, planDate, executeDate, maxEntryGap)
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
				// P0-3（2026-10-21）：生成卖单后立即回写 pendingSell，
				// 否则同批次后续（time_stop 循环）会对同一仓位再生成一张卖单，
				// 次日第一张成交清仓、第二张「卖出成交但无持仓」→ rc=1。
				pendingSell[s.Code] = true
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
		// P0-3（2026-10-21）：回写 pendingSell，防御同批次重复卖单（与信号循环同源）。
		pendingSell[pos.Code] = true
		p.log.Printf("3周不获利离场 %s（持有%d日 收盘%.2f≤成本%.2f）", pos.Code, n, px, pos.AvgCost)
	}

	// ST1（2026-10-21）：首腿初始止损单——持仓期每日挂常驻止损卖单（trigger=pos.StopPrice）。
	// 修复前 pos.StopPrice 只写不执行：sellOrder 触发价恒为箱体下轨×sellDisc，从不看 StopPrice，
	// 导致首腿紧止损（原书 L2526 箱顶45→止损44⅞，约-3%）形同虚设、单笔实际风险=到下轨距离(10-20%)。
	// 本单与箱体下轨止损单并存，match 撮合时按各自 trigger 先到先触发（成交后 CancelOpenOrders 撤另一张）。
	// 仅对"当日无卖出单的持仓"挂（避免与 stop/failed/time_stop 重复）。
	for i := range positions {
		pos := &positions[i]
		if pos.Qty <= 0 || pos.StopPrice <= 0 {
			continue
		}
		if pendingSell[pos.Code] {
			continue // 当日已有卖出单（stop/failed/time_stop），不再挂初始止损
		}
		orders = append(orders, contract.PlanOrder{
			PlanDate: planDate, ExecuteDate: executeDate, Code: pos.Code,
			Side: "sell", OrderKind: "stop_sell",
			TriggerPrice: pos.StopPrice, LimitPrice: pos.StopPrice, Qty: pos.Qty, LegNo: 0,
			Reason: map[string]any{
				"signal": "initial_stop", "stop_price": pos.StopPrice,
				"avg_cost": pos.AvgCost, "qty": pos.Qty, "position_id": pos.ID,
			},
		})
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
	p.log.Printf("plan %s→%s: %d signals → %d orders, equity=%.0f, 止损上移=%d, 体制=%s, 单位预算=试探%.2f/%.0f+主力%.2f/%.0f, %s",
		planDate, executeDate, len(sigs), len(orders), equity, stopMoves, regimeName(market),
		budget.probeUsed, probeCap, budget.mainUsed, mainCap, time.Since(start).Round(time.Millisecond))
	return res, nil
}

// regimeLabel 标注当日市场体制（M8.8 内生宽度法，零未来信息）。
// params/indices 传空即可：ClassifyDate 保留签名仅为向后兼容，内部忽略。
func (p *Planner) regimeLabel(date string) regime.Regime {
	return regime.ClassifyDate(p.st, date, contract.BoxParams{}, nil)
}

// regimeName 日志中文名。
func regimeName(r regime.Regime) string {
	switch r {
	case regime.Bull:
		return "牛"
	case regime.Bear:
		return "熊"
	default:
		return "猴"
	}
}

// unitBudget M8.8 组合级单位预算（试探 5 / 主力 30；预备 15 永不动）。
type unitBudget struct {
	probeUsed, mainUsed float64
	probeCap, mainCap   float64
}

func (b *unitBudget) takeProbe(units float64) bool {
	if b.probeUsed+units > b.probeCap+1e-9 {
		return false
	}
	b.probeUsed += units
	return true
}

func (b *unitBudget) takeMain(units float64) bool {
	if b.mainUsed+units > b.mainCap+1e-9 {
		return false
	}
	b.mainUsed += units
	return true
}

// unitShares 1 单位股数 = floor(unitValue/price/100)×100；不足 1 手返回 0。
func unitShares(unitValue, price float64) int {
	if unitValue <= 0 || price <= 0 {
		return 0
	}
	return int(math.Floor(unitValue/price/100)) * 100
}

// raiseStops 止损上移（M4.4）：对每只持仓，若最新箱体下轨已3日确认
// 且 新止损 = 下轨×sellDisc 高于现止损 → UpdateStop（触发器兜底拒绝下降）。
// 入场箱体本身不参与（B1修正后：腿1初始止损=其上轨下方、腿2/3初始止损=其下轨下方，
// 均不随入场箱体自身重算）。返回上移笔数。
//
// 修复 archive/07 #1：用 LatestNonFormingBox——LatestBox 取 forming(bottom=0) 致
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
// M8.8 单位制仓位（archive/13 §3.3）+ 方案 A（2026-10-21，不择时）：
//   - 腿1 试探单：统一 1 单位（per_stock_probe_bear，不分牛熊），占用试探预算，
//     试探 5 单位用满后改占主力预算（archive/13 §3.4，组合风控不择时保留）；
//   - 腿2/3 主力单：任何市况均可生成（原「仅牛市」已随第0条撤回废止）；
//     腿2 起步=per_stock_main_step 单位，腿3 单次 3-5 单位并递进至单票 10 单位；
//   - 组合预算：试探满 5 / 主力满 30 即跳过；单票上限=per_stock_main_max 单位。
func (p *Planner) buyOrder(s contract.Signal, box *contract.Box, market regime.Regime, unitValue,
	probeBear, mainMax, mainStep float64,
	buyPrem, sellDisc float64, maxLegs int, posByCode map[string]*contract.Position,
	budget *unitBudget, plannedQty map[string]int, planDate, execDate string, maxEntryGap float64) *contract.PlanOrder {
	trigger := box.Top * buyPrem
	// 初始止损（B1修正，原书§4.1）：腿1=被突破箱体上轨（突破点）下方——紧止损；
	// 腿2/3=新箱体下轨下方（新箱体确立后止损上移至其下轨，符合原书阶梯规则）。
	stop := box.Bottom * sellDisc
	if s.Type == contract.SignalBreakout {
		stop = box.Top * sellDisc
	}
	if trigger <= 0 || stop <= 0 || box.Bottom <= 0 {
		return nil // 无下轨或触发/止损非法，不生成买单
	}
	// P0-2（2026-10-21）：生成买单前校验 trigger 与信号现价偏离。
	// 正常 breakout 时 ClosePx ≈ 箱顶、trigger ≈ 箱顶×1.03（偏离约3%）；
	// 陈旧箱体（连板盲区 R7 致箱体未及时更新，如 sh600825 箱顶5.10 vs 现价15.58）
	// 会产出 trigger 严重偏离现价的灾难订单，必须拦截。
	// 注意：max_entry_gap 存的是「现价允许的 trigger 倍数上限」，默认 1.05（=+5%）。
	if s.ClosePx > 0 && trigger > 0 {
		if ratio := s.ClosePx / trigger; ratio > maxEntryGap {
			p.log.Printf("拒单 %s：trigger %.2f 与现价 %.2f 比值 %.3f > max_entry_gap=%.3f（陈旧箱体，跳过）",
				s.Code, trigger, s.ClosePx, ratio, maxEntryGap)
			return nil
		}
	}
	shares := unitShares(unitValue, trigger)
	if shares <= 0 {
		p.log.Printf("跳过 %s：1单位=%.0f元在触发价%.2f下不足1手", s.Code, unitValue, trigger)
		return nil
	}

	// 腿号：breakout=第1腿（试探单）；pyramid=第2/3腿（加码）
	leg := 1
	var pos *contract.Position
	if s.Type == contract.SignalPyramid {
		if pp, ok := posByCode[s.Code]; ok {
			leg = 3 // 已有持仓的加码 → 第3腿
			pos = pp
		} else {
			// 腿2 = 时滞防御分支（F5 裁决，docs/21）：watch.stage=holding 但 position
			// 已清仓的 poolsync 日更滞后场景走到这里；非旧注释的「确认单」（D2 语义错标已更正）。
			leg = 2
		}
	}
	if leg > maxLegs {
		return nil
	}
	if pos == nil {
		if pp, ok := posByCode[s.Code]; ok {
			pos = pp // 防御：breakout 信号若已有持仓，也纳入单票上限
		}
	}

	units := 0.0
	bucket := ""
	qty := 0
	switch {
	case s.Type == contract.SignalBreakout:
		// 方案 A（2026-10-21，第0条撤回连锁）：首腿统一 1 单位试探，不分牛熊。
		// 达瓦斯「不预测市场」（L1206），仓位只跟个股箱体走，不跟大盘牛熊走。
		units = probeBear // =1 单位（熊猴牛统一）
		qty = int(math.Floor(units)) * shares
		if qty <= 0 {
			return nil
		}
		// 单票上限（主力上限=10单位；试探腿1也计入该票总敞口）
		maxQty := int(math.Floor(mainMax*unitValue/trigger/100)) * 100
		current := plannedQty[s.Code]
		if pos != nil {
			current += pos.Qty
		}
		if current+qty > maxQty {
			p.log.Printf("跳过 %s 腿1：单票上限 %d 股", s.Code, maxQty)
			return nil
		}
		// 预算：先吃试探；试探满后改吃主力（组合风控，不择时——方案A保留）。
		if budget.probeUsed+units > budget.probeCap+1e-9 {
			if !budget.takeMain(units) {
				p.log.Printf("跳过 %s 腿1：主力预算不足（已用%.2f/%.0f）", s.Code, budget.mainUsed, budget.mainCap)
				return nil
			}
			bucket = "main"
		} else {
			if !budget.takeProbe(units) {
				p.log.Printf("跳过 %s 腿1：试探预算不足（已用%.2f/%.0f）", s.Code, budget.probeUsed, budget.probeCap)
				return nil
			}
			bucket = "probe"
		}

	default: // pyramid 主力仓腿2/3（方案A：任何市况都允许金字塔加码，不择时）
		current := plannedQty[s.Code]
		if pos != nil {
			current += pos.Qty
		}
		maxQty := int(math.Floor(mainMax*unitValue/trigger/100)) * 100
		if current >= maxQty {
			p.log.Printf("跳过 %s 腿%d：单票已达主力上限 %d 股", s.Code, leg, maxQty)
			return nil
		}
		desiredUnits := mainStep
		if leg == 3 {
			// 正常路径（方案A后）：腿1=1单位 + 腿2=3单位=4 → 腿3补 max(3, 10−4)=6，
			// 封顶 5 单位。若腿2缺单，最多也只补 5 单位，不一次跨档硬凑。
			currentUnits := float64(current) * trigger / unitValue
			desiredUnits = math.Max(mainStep, mainMax-currentUnits)
			desiredUnits = math.Min(desiredUnits, 5)
		}
		qty = int(math.Floor(desiredUnits)) * shares
		if minStepQty := int(math.Floor(mainStep)) * shares; qty < minStepQty {
			p.log.Printf("跳过 %s 腿%d：剩余空间不足单次最小加码 %d 股", s.Code, leg, minStepQty)
			return nil
		}
		if current+qty > maxQty {
			qty = maxQty - current
		}
		if qty < int(math.Floor(mainStep))*shares || qty <= 0 {
			p.log.Printf("跳过 %s 腿%d：单票上限裁剪后不足最小加码", s.Code, leg)
			return nil
		}
		units = float64(qty) / float64(shares)
		if !budget.takeMain(units) {
			p.log.Printf("跳过 %s 腿%d：主力预算不足（已用%.2f/%.0f）", s.Code, leg, budget.mainUsed, budget.mainCap)
			return nil
		}
		bucket = "main"
	}

	plannedQty[s.Code] += qty
	return &contract.PlanOrder{
		PlanDate: planDate, ExecuteDate: execDate, Code: s.Code,
		Side: "buy", OrderKind: "stop_buy",
		TriggerPrice: trigger, LimitPrice: trigger, Qty: qty, LegNo: leg,
		Reason: map[string]any{
			"signal": string(s.Type), "box_top": box.Top, "box_bottom": box.Bottom,
			"trigger": trigger, "stop": stop, "leg": leg,
			"regime": string(market), "budget_bucket": bucket,
			"unit_value": unitValue, "unit_shares": shares, "units": units,
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

// mapKeys 返回 map 的键切片（仅日志用，顺序不定）。
func mapKeys(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	return keys
}
