// Package planner 实现达瓦斯六问 → 交易计划（docs/00 §4 / docs/04 §4.5）。
//
// 输入：当日 signal + 持仓 + param + equity_snapshot
// 输出：trade_plan + plan_order（停损买/卖单，含触发价、委托价、数量、腿号）
//
// 数量公式（单位制）：1 单位 = 权益 × pos.unit（默认 2%），
// 单位股数 = floor(equity × pos.unit / price / 100) × 100（A股整手）。
// A M1（2026-10-23，docs/23 §9 D0 + var/bt/regime_final_summary_and_prompt.md）：
// 不择时——首买单位由 A 判据（60 日低点相邻日比，regime.ClassifyDateA）决定：
//   升势日（指数 N 日低点逐级抬升）→ 首买 2 单位；其余 → 1 单位。
// 取消 winner 5x（D1：docs/23 §9 实证 3 年仅 2 笔全亏 -3,201，有害无益）。
// 不做熊市闸门、不引入 C 判据（2026-10-23 六轮回测硬结论：C 开仓滞后、A 在熊市不触发，
// 大盘择时实证不通，见 docs/00 第 0 条回滚标注）。track_level 保留作审计字段，不驱动 units。
// 一律不加码（0017）：已持仓股再突破不生成买单。组合约束 = 同时持仓数上限（pool.max_hold）。
//   - breakout → 腿1停损买单：trigger = box.top × buy.premium，stop = box.top × sell.discount
//     （B1修正2026-10-18：首腿紧止损=被突破箱体上轨/突破点下方，原书§4.1；旧实现误挂箱体下轨下方）
//   - pyramid → 一律不加码（0017）：已持仓不生成买单（防御性时滞分支随 0017 退役）
//   - stop/failed_breakout → 止损卖单：trigger = box.bottom × sell.discount，qty = 持仓全部（一次全出）
package planner

import (
	"context"
	"fmt"
	"log"
	"math"
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

	// 参数（A M1：首买单位由 A 判据升势决定，winner 5x 退役）。
	buyPrem := p.st.ParamRepo.GetFloat("buy.premium", 1.03)
	sellDisc := p.st.ParamRepo.GetFloat("sell.discount", 0.97)
	unitPct := p.st.ParamRepo.GetFloat("pos.unit", 0.02) // 1 单位 = 2% 权益
	maxHold := p.st.ParamRepo.GetInt("pool.max_hold", 0) // 0 = 废止（docs/00 §5.6.9：改由 50% 权益预算约束）
	ladder := p.newHighLadder()
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

	// LB-4 B 类（2026-10-31）：Box 成长速度买入准入门槛，参数 box.max_growth_days（种子化，
	// migration 0020，默认 0=关闭）。成长速度 = 入场 Box 上轨确认(top_confirmed)到下轨确认
	// (bottom_confirmed)的交易日数；超过阈值则不产生买单。0 时必须完全短路——不读行情、
	// 不改 Reason、不改任何输出，保证 off 档与历史基线逐单等价。
	maxGrowthDays := p.st.ParamRepo.GetInt("box.max_growth_days", 0)

	// P0-3 B 类（2026-10-26）：MA60 闸门实验，参数 gate.ma60.mode = off|half|stop（未种子化，
	// 回测时经 --set 注入；取不到时默认 off）。off 时必须完全短路——不读指数、不改 Reason、
	// 不改任何输出，保证 bt_b12_off 与 bt_e17 历史基线逐单等价（等价性由 2014 对照跑验证）。
	// half：上证收盘 < MA60 时首买降为 0.5 单位；stop：上证收盘 < MA60 时不生成买单。
	// 判断时点 = planDate（与 reason_json.regime 同口径）；零未来信息（date ≤ planDate）；
	// 历史不足（CloseVsMA ok=false）保守放行，与 A 判据历史不足返 Range 同口径，并写入 reason_json 供审计。
	gateMode := p.st.ParamRepo.GetString("gate.ma60.mode", "off")
	// 2026-10-28 双镜对照：MA60 闸门镜子指数（默认 sh000001 向后兼容 B-2；双跑时 B 组传 sh000905）。
	gateIndex := p.st.ParamRepo.GetString("gate.ma60.index", "sh000001")
	gate := gateState{mode: gateMode}
	if gateMode != "off" {
		if close, ma, ok := regime.CloseVsMA(p.st, planDate, 60, gateIndex); ok {
			gate.ok, gate.close, gate.ma = true, close, ma
			gate.below = close < ma
		}
	}

	// 组合级约束：同时持仓数上限（A M1 升势 2 单位 × maxHold 只 ≈ 权益 28%，远低于 70%）。
	// 不再有「试探/主力」分桶预算——不加码，仓位只有首买一档，组合风险由「持仓数」管。
	// docs/33 §2.2（2026-10-27）：预算必须含未成交买单——挂单在生成时预占名额，
	// 否则连续多日挂单全部成交后持仓冲出上限（2007-01-12 实测 12 只）。
	openCount, err := p.st.TradeRepo.OpenPositionCount()
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	pendingBuys, err := p.st.TradeRepo.PendingBuyCount(planDate)
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	budget := &holdBudget{open: openCount + pendingBuys, max: maxHold}
	// A′ 50% 权益预算（docs/00 §5.6.9，2026-10-27 拍板）：持仓市值 + 挂单预占 → 剩余额度；
	// 下单金额 = min(单位仓, 剩余额度, 单票 8%)。max_hold 废止后这是唯一的组合级约束。
	eqb := p.equityBudget(planDate, equity, unitValue)
	plannedQty := map[string]int{} // 同日多信号同票时，避免重复超配

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
			// A M1（2026-10-23）：首买单位由 A 判据升势决定（升势 2 / 其余 1），winner 5x 退役。
			o := p.buyOrder(s, box, market, unitValue,
				buyPrem, sellDisc, posByCode, budget, eqb, plannedQty, planDate, executeDate, maxEntryGap, ladder, gate, maxGrowthDays)
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
	// B3 校订（2026-10-22，用户拍板）：原书 3 周(21自然日)≈15 交易日是 1960 年代
	// T+0 无涨跌停的节奏；A 股行情发动常 10 日内见顶回落，15 日强平熬出的是「已死仓位」。
	// 实测 M8.8 五年 53 笔平仓中，16 日强平 17 笔全亏（平均 -7363）。
	// 改「尽快斩断亏损」：staledays 15→2（次日收盘仍 ≤ 成本即走，第3日卖出）。
	// 依据《简简单单做股票》L755「不赚钱立刻出局」+ A股 T+1 最早次日可卖。
	staleDays := p.st.ParamRepo.GetInt("position.staledays", 2)
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
				"signal": "time_stop_noprofit", "days_held": n,
				"avg_cost": pos.AvgCost, "last_close": px, "qty": pos.Qty, "position_id": pos.ID,
			},
		})
		// P0-3（2026-10-21）：回写 pendingSell，防御同批次重复卖单（与信号循环同源）。
		pendingSell[pos.Code] = true
		p.log.Printf("不获利离场 %s（持有%d日 收盘%.2f≤成本%.2f）", pos.Code, n, px, pos.AvgCost)
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
	p.log.Printf("plan %s→%s: %d signals → %d orders, equity=%.0f, 止损上移=%d, 体制=%s, 持仓数=%d/%d, %s",
		planDate, executeDate, len(sigs), len(orders), equity, stopMoves, regimeName(market),
		budget.open, budget.max, time.Since(start).Round(time.Millisecond))
	return res, nil
}

// regimeLabel 标注当日市场趋势（A 判据：60 日低点相邻日比，零未来信息）。
// A M1 仓位规则用：升势日首买 2 单位、其余 1 单位。M8.8 宽度法（ClassifyDate）
// 已不再被 planner 调用（大盘择时 2026-10-23 实验证伪回滚，见 docs/00 第 0 条），
// 仅分体制报告（regime.Run）保留宽度法，与仓位决策解耦。
func (p *Planner) regimeLabel(date string) regime.Regime {
	return regime.ClassifyDateA(p.st, date)
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

// holdBudget 0017 组合级持仓数预算（不加码后，组合风险由「同时持仓数」约束）。
// open = 已开仓数 + 未成交买单数（docs/33 §2.2 挂单预占；历史 70%/30% 算术已废，见 docs/33）。
type holdBudget struct {
	open int // 已开仓 + 未成交买单（挂单预占）
	max  int // 上限 pool.max_hold
}

// gateState P0-3 B 类 MA60 闸门在 planDate 的状态（gate.ma60.mode != off 时填充）。
// mode=off 时全零值且 buyOrder 完全不触碰——保证与历史基线逐单等价。
type gateState struct {
	mode  string // off | half | stop
	ok    bool   // 指数历史是否足够计算 MA60（不足则保守放行）
	below bool   // 上证收盘 < MA60
	close float64
	ma    float64
}

func (b *holdBudget) canOpen() bool { return b.max <= 0 || b.open < b.max }

func (b *holdBudget) take() { b.open++ }

// equityBudget A′ 权益预算（docs/00 §5.6.9，2026-10-27 拍板）：
// 永久留 50% 现金，总仓位（持仓市值 + 未成交买单名义）≤ 50% 权益；
// 单票名义 ≤ 8% 权益。下单金额 = min(单位仓, 剩余额度, 单票上限) 三层取小。
// 取代 pool.max_hold=7（7 × 实测均值 2.6% ≈ 18%，吃不满 50%，与预算不兼容）。
type equityBudget struct {
	on     bool    // pos.equity_gate=on 时生效；off 用于复现历史基线
	equity float64 // 权益基准（LatestEquity）
	limit  float64 // 总仓位上限（pos.equity_limit，默认 0.5）
	single float64 // 单票上限（pos.single_limit，默认 0.08）
	used   float64 // 已用 = 持仓市值 + 挂单预占名义
}

// capFor 本单可用的名义上限（剩余总额度与单票上限取小）；off 时无上限。
func (b *equityBudget) capFor() float64 {
	if b == nil || !b.on {
		return math.Inf(1)
	}
	rem := b.limit*b.equity - b.used
	single := b.single * b.equity
	if single < rem {
		return single
	}
	return rem
}

// take 预占额度（同日多信号共享累计，防一日内叠加突破 50%）。
func (b *equityBudget) take(notional float64) {
	if b != nil {
		b.used += notional
	}
}

// newHighLadder E17：按信号日收盘价距52周新高的距离分档首买单位。
// 仅当 pos.newhigh_ladder=on 时启用；默认 off 保持 A M1。
type newHighLadder struct {
	enabled  bool
	lookback int
	cutoff   float64
	units    [4]float64
}

func (p *Planner) newHighLadder() newHighLadder {
	return newHighLadder{
		enabled:  p.st.ParamRepo.GetString("pos.newhigh_ladder", "off") == "on",
		lookback: p.st.ParamRepo.GetInt("pos.newhigh.lookback", 252),
		cutoff:   p.st.ParamRepo.GetFloat("pos.newhigh.cutoff", 40),
		units: [4]float64{
			p.st.ParamRepo.GetFloat("pos.newhigh.units.t1", 3),
			p.st.ParamRepo.GetFloat("pos.newhigh.units.t2", 2),
			p.st.ParamRepo.GetFloat("pos.newhigh.units.t3", 1),
			p.st.ParamRepo.GetFloat("pos.newhigh.units.t4", 0.5),
		},
	}
}

// tierFor 返回距离所属档位（1-4）和首买单位；超过 cutoff 拒绝。
func (l newHighLadder) tierFor(distance float64) (int, float64, bool) {
	switch {
	case distance > l.cutoff:
		return 0, 0, false
	case distance < 5:
		return 1, l.units[0], true
	case distance < 10:
		return 2, l.units[1], true
	case distance < 20:
		return 3, l.units[2], true
	default:
		return 4, l.units[3], true
	}
}

// high52w 返回截至 through（含）最近 lookback 根K线的最高价。
func (p *Planner) high52w(code, through string, lookback int) float64 {
	bars, err := p.st.BarRepo.BarsWindow(code, through, lookback)
	if err != nil {
		return 0
	}
	high := 0.0
	for _, bar := range bars {
		if bar.High > high {
			high = bar.High
		}
	}
	return high
}

// unitShares 1 单位股数 = floor(unitValue/price/100)×100；不足 1 手返回 0。
func unitShares(unitValue, price float64) int {
	if unitValue <= 0 || price <= 0 {
		return 0
	}
	return int(math.Floor(unitValue/price/100)) * 100
}

// lotQty 按单位数缩放整手股数；整数单位与 unitShares×units 完全一致。
func lotQty(shares int, units float64) int {
	if shares <= 0 || units <= 0 {
		return 0
	}
	return int(math.Floor(float64(shares)*units/100)) * 100
}

// raiseStops 止损上移（M4.4）：对每只持仓，若最新箱体下轨已3日确认
// 且 新止损 = 下轨×sellDisc 高于现止损 → UpdateStop（触发器兜底拒绝下降）。
// 入场箱体本身不参与（B2 校订后：腿1/2/3 初始止损一律=入场箱体上轨下方，
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

// equityBudget 计算计划日的已用额度：持仓市值（最新收盘，无行情回退成本）
// + 未成交买单名义（挂单预占）。pos.equity_gate=off 时原样返回（on=false，无上限），
// 供 B-4 实验复现历史基线（旧语义 = 只有 pool.max_hold 只数约束）。
func (p *Planner) equityBudget(planDate string, equity, unitValue float64) *equityBudget {
	b := &equityBudget{
		on:     p.st.ParamRepo.GetString("pos.equity_gate", "on") == "on",
		equity: equity,
		limit:  p.st.ParamRepo.GetFloat("pos.equity_limit", 0.5),
		single: p.st.ParamRepo.GetFloat("pos.single_limit", 0.08),
	}
	if !b.on {
		return b
	}
	probe, main, err := p.st.TradeRepo.OpenExposureUnits(unitValue)
	if err != nil {
		p.log.Printf("⚠ 权益预算：持仓市值读取失败，按 0 计（%v）", err)
	} else {
		b.used += (probe + main) * unitValue
	}
	n, err := p.st.TradeRepo.PendingBuyNotional(planDate)
	if err != nil {
		p.log.Printf("⚠ 权益预算：挂单预占读取失败，按 0 计（%v）", err)
	} else {
		b.used += n
	}
	return b
}

// buyOrder 构造停损买单（A M1：首买，一律不加码）。
// 规则（2026-10-23，var/bt/regime_final_summary_and_prompt.md + docs/23 §9 D0/D1）：
//   - 交易只发生在「第一次信号」——同一只票持仓期间一律不加码（0017）。
//   - 默认首买单位由 A 判据（60 日低点相邻日比，regime.ClassifyDateA）决定：
//     升势日 2 单位、其余 1 单位。取消 winner 5x（D1：实证有害无益）。
//   - E17 实验开关 pos.newhigh_ladder=on 时改为按信号收盘距52周新高分档：
//     0-5/5-10/10-20/20-40% 分别取参数单位，>cutoff 拒买；默认 off。
//   - 组合约束（docs/00 §5.6.9，2026-10-27 拍板）＝ 50% 权益预算：下单金额 = min(单位仓,
//     50% 剩余额度, 单票 8%)，挂单生成即预占；pool.max_hold 废止（0）。
//     旧「同时持仓数上限 7 只 / 留 30% 现金」已废——7 × 实测均值 2.6% ≈ 18%，吃不满 50%。
//   - 不引入 C 判据、不做熊市闸门（2026-10-23 六轮回测硬结论：大盘择时不通）。
//   - track_level 保留作审计字段（记录该票战绩等级），不再驱动 units。
// 腿号金字塔（leg1=1/leg2=3/leg3补到10）、第②通道加码、单票上限10单位、主力预算30单位
//   全部随「不加码」退役（见 docs/00 §5.6）。
func (p *Planner) buyOrder(s contract.Signal, box *contract.Box, market regime.Regime, unitValue,
	buyPrem, sellDisc float64, posByCode map[string]*contract.Position,
	budget *holdBudget, eqb *equityBudget, plannedQty map[string]int, planDate, execDate string, maxEntryGap float64,
	ladder newHighLadder, gate gateState, maxGrowthDays int) *contract.PlanOrder {
	trigger := box.Top * buyPrem
	// 初始止损（B2 校订 2026-10-22，原书 §4.1 + §3.4）：=被突破箱体上轨（突破点）下方
	// ——紧止损。原书 §3.4「新箱体下轨 ≈ 前一箱体上轨（±3%容差）」，故「新箱体下轨下方」
	// 等价于「被突破旧箱体上轨下方」。
	stop := box.Top * sellDisc
	if trigger <= 0 || stop <= 0 || box.Top <= 0 {
		return nil // 无上轨或触发/止损非法，不生成买单
	}
	// LB-4 B 类（2026-10-31）：Box 成长速度准入门槛。成长速度 = top_confirmed → bottom_confirmed
	// 的交易日数（daily_bar 闭区间，与 LB-4 A 类口径一致）；超过阈值则拒绝买入。
	// maxGrowthDays <= 0 时完全短路（保 off 档与历史基线逐单等价）。
	if maxGrowthDays > 0 && box.TopConfirmed != "" && box.BottomConfirmed != "" {
		if gs, err := p.st.BarRepo.BarCountClosed(s.Code, box.TopConfirmed, box.BottomConfirmed); err == nil && gs > maxGrowthDays {
			p.log.Printf("成长速度拦截 %s：%d 日 > max_growth_days=%d（top_confirmed %s → bottom_confirmed %s）",
				s.Code, gs, maxGrowthDays, box.TopConfirmed, box.BottomConfirmed)
			return nil
		}
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

	// 0017：只首买，不加码。已有持仓（pos != nil）→ 不再买入。
	if _, hasPos := posByCode[s.Code]; hasPos {
		p.log.Printf("跳过 %s：已持仓，一律不加码（首买后持仓期间不再追加）", s.Code)
		return nil
	}

	// A M1 仓位规则：首买单位由 A 判据升势决定（升势 2 / 其余 1），winner 5x 退役。
	// track_level 仍记录（审计），但不进入下注档位。
	lv, _ := p.st.SecRepo.TrackLevel(s.Code)
	units := 1.0
	basis := "default"
	high52w := 0.0
	distance := 0.0
	if ladder.enabled && s.ClosePx > 0 {
		high52w = p.high52w(s.Code, planDate, ladder.lookback)
		if high52w > 0 {
			distance = (high52w - s.ClosePx) / high52w * 100
			tier, tierUnits, ok := ladder.tierFor(distance)
			if !ok {
				p.log.Printf("跳过 %s：距52周新高 %.1f%% > cutoff %.1f%%", s.Code, distance, ladder.cutoff)
				return nil
			}
			units = tierUnits
			basis = fmt.Sprintf("newhigh_t%d", tier)
		} else {
			basis = "newhigh_unavailable"
		}
	} else if market == regime.Bull { // A 判据升势（60 日低点逐级抬升）
		units = 2.0
		basis = "A_regime_up"
	}

	// P0-3 B 类 MA60 闸门（gate.mode）：仅 off 以外模式生效；ok=false（历史不足）保守放行。
	if gate.mode == "stop" && gate.ok && gate.below {
		p.log.Printf("闸门拦截 %s：上证 %.2f < MA60 %.2f（gate.ma60.mode=stop）", s.Code, gate.close, gate.ma)
		return nil
	}
	if gate.mode == "half" && gate.ok && gate.below && units > 0.5 {
		units = 0.5
		basis = basis + "|ma60_half"
	}

	// 组合约束：同时持仓数上限（含未成交买单预占）。
	if !budget.canOpen() {
		p.log.Printf("跳过 %s：持仓+挂单已达上限 %d 只（含未成交买单预占）", s.Code, budget.max)
		return nil
	}
	qty := lotQty(shares, units)
	if qty <= 0 {
		return nil
	}
	// A′ 三层取小（docs/00 §5.6.9）：实际下单 = min(单位仓, 50% 剩余额度, 单票 8%)。
	// 挂单在生成时即预占额度（eqb.take），撤单/过期由下次 Build 重新计算释放。
	if eqb != nil && eqb.on {
		capAmt := eqb.capFor()
		if capAmt <= 0 {
			p.log.Printf("跳过 %s：50%% 权益预算已用满（已用 %.0f / 上限 %.0f）",
				s.Code, eqb.used, eqb.limit*eqb.equity)
			return nil
		}
		if maxQty := lotQty(int(math.Floor(capAmt/trigger)), 1.0); qty > maxQty {
			if maxQty <= 0 {
				p.log.Printf("跳过 %s：剩余额度 %.0f 元不足 1 手（触发价 %.2f）", s.Code, capAmt, trigger)
				return nil
			}
			p.log.Printf("缩量 %s：%d → %d 股（剩余额度 %.0f 元，单票上限 %.0f 元）",
				s.Code, qty, maxQty, capAmt, eqb.single*eqb.equity)
			qty = maxQty
			basis = basis + "|eq_cap"
		}
	}
	// 同日同票多信号防重复超配（defense）
	if plannedQty[s.Code] > 0 {
		p.log.Printf("跳过 %s：同日已生成买单，不重复", s.Code)
		return nil
	}
	budget.take()
	eqb.take(float64(qty) * trigger)
	plannedQty[s.Code] = qty

	reason := map[string]any{
		"signal": string(s.Type), "box_top": box.Top, "box_bottom": box.Bottom,
		"trigger": trigger, "stop": stop, "leg": 1,
		"regime": string(market), "sizing_basis": basis,
		"track_level": string(lv), "budget_bucket": "normal",
		"unit_value": unitValue, "unit_shares": shares, "units": units,
		"high_52w": high52w, "newhigh_distance_pct": distance,
	}
	// 仅闸门开启时写入 gate_ma60（off 模式不写，保 bt_b12_off 与历史基线逐单等价）。
	if gate.mode != "off" {
		reason["gate_ma60"] = map[string]any{
			"mode": gate.mode, "below": gate.below,
			"close": gate.close, "ma": gate.ma, "ok": gate.ok,
		}
	}
	// A′ 预算审计（仅 on 时写；off 保持历史基线逐单等价）。
	if eqb != nil && eqb.on {
		reason["equity_budget"] = map[string]any{
			"limit": eqb.limit, "single": eqb.single,
			"equity": eqb.equity, "used_before": eqb.used - float64(qty)*trigger,
			"notional": float64(qty) * trigger,
		}
	}

	return &contract.PlanOrder{
		PlanDate: planDate, ExecuteDate: execDate, Code: s.Code,
		Side: "buy", OrderKind: "stop_buy",
		TriggerPrice: trigger, LimitPrice: trigger, Qty: qty, LegNo: 1,
		Reason: reason,
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
