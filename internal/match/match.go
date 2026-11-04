// Package match 实现 M8.2 模拟撮合器（archive/08-m8.2-matcher.md）。
//
// 核心：把「人工贴成交单」替换成「当日 OHLC 模拟停损单触发」，触发则生成
// contract.Fill 喂给 ledger.ApplyFill——持仓状态机/金字塔/止损上移/两亏拉黑/
// 恒等式审计全复用，规则零重写。
//
// 复权日处理（§3.8，用户拍板 2026-10-19）：不复权原始价 + 复权日清仓。持仓股在
// 除权跳空日强制清仓（价=昨收×(1−滑点)，避开跳空损失）；watch 未持仓股不额外处理
// （signal 自然出池，复权后重新入池、箱体重算，属可接受行为）。
// 0032 豁免（docs/53 §8.7 判据⑧）：adj 模式下后复权价已含分红补偿，除权日不再是
// 「跳空损失」，强制清仓必须下线（sim:dividend 成交数=0）；仅 raw 模式保留清仓。
// 滑点（C4，2026-10-22）：全部模拟成交按 match.slippage（默认 0.2%）买贵卖贱，0 关闭。
package match

import (
	"context"
	"log"
	"math"

	"darvas/internal/contract"
	"darvas/internal/ledger"
	"darvas/internal/store"
)

// Matcher 模拟撮合器。
type Matcher struct {
	st  *store.Store
	l   *ledger.Ledger
	log *log.Logger
}

// New 构造。
func New(st *store.Store) *Matcher {
	return &Matcher{st: st, l: ledger.New(st), log: log.New(log.Writer(), "[match] ", log.LstdFlags)}
}

// Match 撮合 execute_date==date 的待成交订单 + 复权日强制清仓，返回当日成交笔数。
// 调用方须在当日六任务链 plan 之后、report 之前调用（archive/08 §5 时序）。
func (m *Matcher) Match(ctx context.Context, date string) (int, error) {
	fp := loadFeeParams(m.st)
	maxEntryGap := m.st.ParamRepo.GetFloat("match.max_entry_gap", 1.05)
	slippage := m.st.ParamRepo.GetFloat("match.slippage", 0.002)
	n := 0

	// 1. 除权除息日：持仓股强制清仓（先于订单撮合，§3.8）。
	// 识别口径（2026-11-02 修复）：查真实除权记录 dividend_event，不再用价格反推
	// isDividendGap——原阈值 0.09 恰卡在主跌停 10% 上，四年 44 笔 sim:dividend 里
	// 37 笔是「跌停误判」，只 7 笔是真除权。定价口径：除权后实际价 today.Close
	// （而非「昨收×滑点」——那在除权跳空日会按除权前价虚增成交）。
	// 0032：仅 raw 模式执行（见文件头豁免说明）。
	cleared := map[string]bool{}
	if m.st.DataMode() == "raw" {
		positions, err := m.st.TradeRepo.OpenPositions()
		if err != nil {
			return 0, err
		}
		for _, pos := range positions {
			bars, err := m.st.BarRepo.BarsWindow(pos.Code, date, 2)
			if err != nil || len(bars) < 2 {
				continue
			}
			prev, today := bars[0], bars[1]
			if today.Date != date {
				continue // 当日无数据（停牌）
			}
			_ = prev
			isDiv, err := m.st.DivRepo.IsDividendDay(pos.Code, date)
			if err != nil {
				return n, err
			}
			if !isDiv {
				continue
			}
			clearPx := today.Close // 除权后实际价格（保守、真实）
			fill := contract.Fill{
				Code: pos.Code, Date: date, Side: "sell",
				Price: clearPx, Qty: pos.Qty,
				Fee: feeOf("sell", clearPx, pos.Qty, fp), RawText: "sim:dividend",
			}
			if _, err := m.l.ApplyFill(fill); err != nil {
				return n, err
			}
			cleared[pos.Code] = true
			n++
			// 除权清仓后，在途订单（止损卖单/突破买单）同步作废：卖单不取消会次日触发
			// 「卖出成交但无持仓」（2018-05-23 sz300630 暴露）；买单不取消会以复权前
			// 箱体逻辑开幻影新仓。复权后箱体重算、重新出信号属正常流程。
			if _, err := m.st.TradeRepo.CancelOpenOrders(pos.Code); err != nil {
				return n, err
			}
		}
	}

	// 2. 撮合 execute_date==date 的待成交订单（先卖后买，满足 T+1）
	orders, err := m.st.TradeRepo.PendingOrdersForExecuteDate(date)
	if err != nil {
		return n, err
	}
	for _, o := range orders {
		if cleared[o.Code] && o.Side == "sell" {
			continue // 已跳空清仓，跳过卖出单（避免无持仓卖出）
		}
		bars, err := m.st.BarRepo.BarsWindow(o.Code, date, 2)
		if err != nil || len(bars) < 2 {
			continue
		}
		prev, today := bars[0], bars[1]
		if today.Date != date {
			continue // 当日无数据（停牌）
		}
		if up, down := isLimitBoard(today, prev.Close); (up && o.Side == "buy") || (down && o.Side == "sell") {
			continue // 一字板挡住方向
		}
		px, hit := matchOrder(o.Side, o.TriggerPrice, o.LimitPrice, today)
		if !hit {
			continue
		}
		// C4（2026-10-22，docs/21 第一段）：模拟成交加滑点（买贵卖贱），0=关闭。
		// 先加滑点再过跳空闸门——闸门校验的是「实际要付的价」，与真实委托一致。
		px = slip(o.Side, px, slippage)
		// P0-2（2026-10-21）：买入单进场跳空上限。跳空高开越过触发价按开盘价成交，
		// 但成交价/触发价 > max_entry_gap 视为「突破后追高」，弃单并记 cancelled。
		// 达瓦斯买的是「突破点+3%」，不是突破后 +63%/+162% 追高（docs/18 两笔假利润）。
		// 注意：max_entry_gap 存的是「成交价允许的触发价倍数上限」，默认 1.05（=+5%）。
		if o.Side == "buy" && o.TriggerPrice > 0 {
			if ratio := px / o.TriggerPrice; ratio > maxEntryGap {
				m.log.Printf("弃单 %s：成交价 %.2f / 触发价 %.2f = %.3f > max_entry_gap=%.3f（跳空追高，记 cancelled）",
					o.Code, px, o.TriggerPrice, ratio, maxEntryGap)
				// 弃单：置 cancelled（status CHECK 无 skipped_gap 值，用 cancelled 语义；
				// ratio 已在日志留痕，供 H1 统计被拒单事后表现）
				if err := m.st.TradeRepo.UpdateOrderStatus(o.ID, "cancelled"); err != nil {
					m.log.Printf("⚠ 弃单标记失败 %s: %v", o.Code, err)
				}
				continue
			}
		}
		fill := contract.Fill{
			PlanOrderID: o.ID, Code: o.Code, Date: date, Side: o.Side,
			Price: px, Qty: o.Qty, Fee: feeOf(o.Side, px, o.Qty, fp), RawText: "sim:order",
		}
		if _, err := m.l.ApplyFill(fill); err != nil {
			return n, err
		}
		n++
	}

	// 3. 覆盖当日权益快照（planner 末尾的 Snapshot 跑在撮合前，漏当日成交）
	if err := m.l.Snapshot(ctx, date); err != nil {
		return n, err
	}
	return n, nil
}

// feeParams 撮合费用参数（param 可配，初版默认当前费率）。
type feeParams struct {
	commissionRate float64
	commissionMin  float64
	stampTax       float64
	transferRate   float64
}

func loadFeeParams(st *store.Store) feeParams {
	return feeParams{
		commissionRate: st.ParamRepo.GetFloat("fee.commission_rate", 0.00025),
		commissionMin:  st.ParamRepo.GetFloat("fee.commission_min", 5),
		stampTax:       st.ParamRepo.GetFloat("fee.stamp_tax", 0.001),
		transferRate:   st.ParamRepo.GetFloat("fee.transfer_rate", 0.00001),
	}
}

// matchOrder 判定停损单是否触发，返回成交价（未触发 hit=false）。
// 上穿/下穿触发；跳空按开盘价成交（最坏价格），盘中触及按委托价 limit（当前==trigger）。
func matchOrder(side string, trigger, limit float64, bar contract.DailyBar) (px float64, hit bool) {
	switch side {
	case "buy":
		if bar.Open >= trigger {
			return bar.Open, true // 跳空高开越过触发价
		}
		if bar.High >= trigger && bar.Open < trigger {
			return limit, true // 盘中上穿
		}
	case "sell":
		if bar.Open <= trigger {
			return bar.Open, true // 跳空低开跌破触发价
		}
		if bar.Low <= trigger && bar.Open > trigger {
			return limit, true // 盘中下穿
		}
	}
	return 0, false
}

// isLimitBoard 判定一字板（H==L 单一价格），方向由相对昨收判断。
// 初版简化：不精确算涨停价，只挡「一字板」最恶劣的假成交。
func isLimitBoard(bar contract.DailyBar, prevClose float64) (limitUp, limitDown bool) {
	if bar.High != bar.Low {
		return false, false
	}
	if bar.Close > prevClose {
		return true, false
	}
	if bar.Close < prevClose {
		return false, true
	}
	return false, false
}

// slip 按滑点调整成交价：买贵卖贱（C4，2026-10-22）。slippage≤0 或价格非法时原样返回。
// 默认 0.002（0.2%）：docs/18 R5 估算 20-40 笔/年换手下，滑点缺失约让年化乐观 0.3-0.8%。
func slip(side string, px, slippage float64) float64 {
	if slippage <= 0 || px <= 0 {
		return px
	}
	if side == "buy" {
		return px * (1 + slippage)
	}
	return px * (1 - slippage)
}

// feeOf 计算单笔成交费用（买：佣金+过户费；卖：佣金+印花税+过户费）。
func feeOf(side string, px float64, qty int, p feeParams) float64 {
	turnover := px * float64(qty)
	fee := math.Max(turnover*p.commissionRate, p.commissionMin)
	if side == "sell" {
		fee += turnover * p.stampTax
	}
	fee += turnover * p.transferRate
	return fee
}

