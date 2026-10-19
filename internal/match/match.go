// Package match 实现 M8.2 模拟撮合器（docs/08-m8.2-matcher.md）。
//
// 核心：把「人工贴成交单」替换成「当日 OHLC 模拟停损单触发」，触发则生成
// contract.Fill 喂给 ledger.ApplyFill——持仓状态机/金字塔/止损上移/两亏拉黑/
// 恒等式审计全复用，规则零重写。
//
// 复权日处理（§3.8，用户拍板 2026-10-19）：不复权原始价 + 复权日清仓。持仓股在
// 除权跳空日强制清仓（价=昨收，避开跳空损失）；watch 未持仓股不额外处理
// （signal 自然出池，复权后重新入池、箱体重算，属可接受行为）。
package match

import (
	"context"
	"math"
	"strings"

	"darvas/internal/contract"
	"darvas/internal/ledger"
	"darvas/internal/store"
)

// Matcher 模拟撮合器。
type Matcher struct {
	st *store.Store
	l  *ledger.Ledger
}

// New 构造。
func New(st *store.Store) *Matcher {
	return &Matcher{st: st, l: ledger.New(st)}
}

// Match 撮合 execute_date==date 的待成交订单 + 复权日强制清仓，返回当日成交笔数。
// 调用方须在当日六任务链 plan 之后、report 之前调用（docs/08 §5 时序）。
func (m *Matcher) Match(ctx context.Context, date string) (int, error) {
	fp := loadFeeParams(m.st)
	n := 0

	// 1. 复权跳空日：持仓股强制清仓（先于订单撮合，§3.8）
	cleared := map[string]bool{}
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
		if !isDividendGap(today, prev, gapThreshold(pos.Code)) {
			continue
		}
		fill := contract.Fill{
			Code: pos.Code, Date: date, Side: "sell",
			Price: prev.Close, Qty: pos.Qty,
			Fee: feeOf("sell", prev.Close, pos.Qty, fp), RawText: "sim:dividend",
		}
		if _, err := m.l.ApplyFill(fill); err != nil {
			return n, err
		}
		cleared[pos.Code] = true
		n++
		// 复权清仓后，在途订单（止损卖单/突破买单）同步作废：卖单不取消会次日触发
		// 「卖出成交但无持仓」（2018-05-23 sz300630 暴露）；买单不取消会以复权前
		// 箱体逻辑开幻影新仓。复权后箱体重算、重新出信号属正常流程。
		if _, err := m.st.TradeRepo.CancelOpenOrders(pos.Code); err != nil {
			return n, err
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

// gapThreshold 除权跳空检测阈值（涨跌停幅度减 1% 余量）。
// 初版不识别 ST（达瓦斯池不含 ST）；科创板/创业板 20% 涨跌停，主板 10%。
func gapThreshold(code string) float64 {
	num := strings.TrimPrefix(strings.TrimPrefix(code, "sh"), "sz")
	if strings.HasPrefix(num, "68") || strings.HasPrefix(num, "30") {
		return 0.19
	}
	return 0.09
}

// isDividendGap 判断当日是否除权跳空（close 相对昨收跌幅超过涨跌停限制）。
// 正常交易不可能单日跌超涨跌停，故超限即除权（一字跌停 ≈ 跌停幅度，不误判）。
func isDividendGap(today, prev contract.DailyBar, threshold float64) bool {
	if prev.Close <= 0 {
		return false
	}
	drop := (prev.Close - today.Close) / prev.Close
	return drop > threshold
}
