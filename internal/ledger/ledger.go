// Package ledger 实现账本：成交解析（M4.5）、权益快照与恒等式审计（M4.6）。
//
// 职责（docs/02 §3 / docs/04 §4.6，ADR-4 人工成交录入）：
//   - ParseFill: 券商成交回报文本粘贴 → 结构化 Fill（2-3种常见格式）
//   - ApplyFill: fill → 匹配 pending 订单 → 订单 filled → 持仓状态机 → 入库
//   - Snapshot:  现金+持仓市值=总权益，每笔成交后重算
//   - AuditEquation: 恒等式校验（权益恒等式 + 持仓↔成交数量闭合）
//
// 现金不做增量字段，永远从初始本金+全部成交重算——fill 是唯一事实源，审计可复现。
package ledger

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// Ledger 账本服务（contract.Ledger）。
type Ledger struct {
	st *store.Store
}

// New 构造。
func New(st *store.Store) *Ledger { return &Ledger{st: st} }

// ---------------------------------------------------------------------------
// 订单状态机（plan_order.status：pending→placed→filled / cancelled / expired）
// ---------------------------------------------------------------------------

// orderTransitions 合法状态迁移（封闭集合，docs/03 §2.7）。
var orderTransitions = map[string][]string{
	"pending":   {"placed", "filled", "cancelled", "expired"},
	"placed":    {"filled", "cancelled", "expired"},
	"filled":    {}, // 终态：成交不可逆
	"cancelled": {},
	"expired":   {},
}

// CanTransition 判断订单状态迁移是否合法。
func CanTransition(from, to string) bool {
	if from == to {
		return false
	}
	for _, t := range orderTransitions[from] {
		if t == to {
			return true
		}
	}
	return false
}

// SetOrderStatus 带状态机校验的订单状态更新（非法迁移返回错误）。
func (l *Ledger) SetOrderStatus(orderID int64, from, to string) error {
	if !CanTransition(from, to) {
		return fmt.Errorf("非法订单状态迁移 %s→%s (order #%d)", from, to, orderID)
	}
	return l.st.TradeRepo.UpdateOrderStatus(orderID, to)
}

// ---------------------------------------------------------------------------
// M4.5 成交解析（ADR-4：粘贴解析，每笔30秒）
// ---------------------------------------------------------------------------

var (
	reDate      = regexp.MustCompile(`(\d{4})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})`)
	reCode      = regexp.MustCompile(`(?i)\b(sh|sz)?\s*([03468]\d{5})\b`)
	reSideBuy   = regexp.MustCompile(`买入|证券买入|担保品买入`)
	reSideSell  = regexp.MustCompile(`卖出|证券卖出|担保品卖出`)
	reSideBuyB  = regexp.MustCompile(`(?i)\bB\b`)
	reSideSellS = regexp.MustCompile(`(?i)\bS\b`)
	reHeader    = regexp.MustCompile(`成交日期|证券代码|操作|成交价格|成交数量|发生金额|手续费|佣金|--`)
	reNum       = regexp.MustCompile(`^-?\d[\d,]*(\.\d+)?$`)
)

// ParseFill 把券商成交回报文本解析为结构化 Fill（contract.Ledger 接口）。
// 支持三种常见格式（字段顺序固定、分隔符为空白/Tab/逗号均可）：
//
//	格式1 同花顺类: 20261016 600519 买入 1700.50 100 170050.00
//	格式2 通达信类: 2026-10-16 15:00:01 sh600519 贵州茅台 证券买入 1700.50 100
//	格式3 CSV类:    2026/10/16,SZ000001,S,12.34,500,6170.00,5.00
//
// 表头行/空行自动跳过；fee 可选（缺省0）；解析失败的行返回错误（宁可拒绝不可错账）。
func (l *Ledger) ParseFill(rawText string) ([]contract.Fill, error) {
	var fills []contract.Fill
	for i, line := range strings.Split(rawText, "\n") {
		line = strings.TrimSpace(strings.ReplaceAll(line, "\r", ""))
		if line == "" || reHeader.MatchString(line) {
			continue
		}
		f, err := parseLine(line)
		if err != nil {
			return nil, fmt.Errorf("第%d行解析失败: %w\n原文: %s", i+1, err, line)
		}
		f.RawText = line
		fills = append(fills, f)
	}
	if len(fills) == 0 {
		return nil, fmt.Errorf("未解析出任何成交记录")
	}
	return fills, nil
}

// parseLine 解析单行。
func parseLine(line string) (contract.Fill, error) {
	var f contract.Fill

	// 1. 日期（行内第一处日期）
	m := reDate.FindStringSubmatchIndex(line)
	if m == nil {
		return f, fmt.Errorf("未找到日期")
	}
	date, err := normalizeDate(line[m[0]:m[1]])
	if err != nil {
		return f, err
	}
	f.Date = date
	rest := line[m[1]:]

	// 2. 方向：中文词优先，其次单字母 B/S
	side := ""
	if reSideSell.MatchString(rest) && !buyBeforeSell(rest) {
		side = "sell"
	} else if reSideBuy.MatchString(rest) {
		side = "buy"
	} else if reSideBuyB.MatchString(rest) {
		side = "buy"
	} else if reSideSellS.MatchString(rest) {
		side = "sell"
	}
	if side == "" {
		return f, fmt.Errorf("未找到买卖方向（买入/卖出/B/S）")
	}
	f.Side = side

	// 3. 代码（日期之后：可选 sh/sz 前缀 + 6位数字）
	cm := reCode.FindStringSubmatchIndex(rest)
	if cm == nil {
		return f, fmt.Errorf("未找到6位证券代码")
	}
	code, err := normalizeCode(rest[cm[0]:cm[1]])
	if err != nil {
		return f, err
	}
	f.Code = code
	tail := rest[cm[1]:]

	// 4. 代码之后的数字序列：剔除股票中文名后按语义取值
	tokens := tokenize(stripChinese(tail))
	var price, fee float64
	qty := 0
	var nums []float64
	var ints []int
	for _, t := range tokens {
		if !reNum.MatchString(t) {
			continue
		}
		v, err := strconv.ParseFloat(strings.ReplaceAll(t, ",", ""), 64)
		if err != nil {
			continue
		}
		if strings.Contains(t, ".") {
			nums = append(nums, v)
		} else {
			ints = append(ints, int(v))
		}
	}
	switch {
	case len(nums) >= 1 && len(ints) >= 1:
		price = nums[0]
		qty = ints[0]
		if len(nums) >= 3 {
			fee = nums[2] // price, amount, fee
		} else if len(nums) >= 2 && nums[1] < price {
			fee = nums[1] // 第二个小数更小 → fee（amount 通常更大）
		}
	case len(nums) >= 1:
		return f, fmt.Errorf("有价格无数量，记录不完整")
	case len(ints) >= 2:
		// 整数价格（低价股/ETF）：第一个=价格，第二个=数量
		price = float64(ints[0])
		qty = ints[1]
	default:
		return f, fmt.Errorf("数值字段不足（需价格+数量）")
	}
	if price <= 0 || qty <= 0 {
		return f, fmt.Errorf("价格/数量非法: price=%v qty=%v", price, qty)
	}
	f.Price, f.Qty, f.Fee = price, qty, fee
	return f, nil
}

// buyBeforeSell 一行同时含买卖字样时取先出现者。
func buyBeforeSell(s string) bool {
	b := reSideBuy.FindStringIndex(s)
	sl := reSideSell.FindStringIndex(s)
	if b == nil {
		return false
	}
	if sl == nil {
		return true
	}
	return b[0] < sl[0]
}

// stripChinese 去掉中文字符（股票名/方向词残留），保留数字与字母。
func stripChinese(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= 0x4E00 && r <= 0x9FFF { // CJK 统一表意文字区
			b.WriteRune(' ')
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

// tokenize 按空白/逗号/制表符等切分。
func tokenize(s string) []string {
	return strings.FieldsFunc(s, func(r rune) bool {
		return r == ' ' || r == '\t' || r == ',' || r == '|' || r == ';' || r == '，' || r == '、'
	})
}

// normalizeDate 统一为 YYYY-MM-DD。
func normalizeDate(s string) (string, error) {
	m := reDate.FindStringSubmatch(s)
	if m == nil {
		return "", fmt.Errorf("日期格式无法识别: %q", s)
	}
	y, _ := strconv.Atoi(m[1])
	mo, _ := strconv.Atoi(m[2])
	d, _ := strconv.Atoi(m[3])
	t := time.Date(y, time.Month(mo), d, 0, 0, 0, 0, time.Local)
	if t.Year() < 2000 || t.Year() > 2100 {
		return "", fmt.Errorf("日期超出合理范围: %q", s)
	}
	return t.Format("2006-01-02"), nil
}

// normalizeCode 统一为 shXXXXXX / szXXXXXX（有前缀用前缀，无前缀按首位推断）。
func normalizeCode(s string) (string, error) {
	s = strings.ToLower(strings.TrimSpace(s))
	m := reCode.FindStringSubmatch(s)
	if m == nil {
		return "", fmt.Errorf("证券代码无法识别: %q", s)
	}
	prefix, num := strings.ToLower(m[1]), m[2]
	if prefix == "" {
		switch num[0] {
		case '6':
			prefix = "sh"
		case '0', '3':
			prefix = "sz"
		default:
			return "", fmt.Errorf("代码 %s 无法推断市场（需 sh/sz 前缀）", num)
		}
	}
	return prefix + num, nil
}

// ---------------------------------------------------------------------------
// 成交应用（fill → 订单流转 → 持仓状态机）
// ---------------------------------------------------------------------------

// ApplyResult 单笔成交的应用结果。
type ApplyResult struct {
	Fill        contract.Fill
	Matched     bool // 是否匹配到计划订单
	OrderID     int64
	PositionID  int64
	StopUpdated bool // 买入成交是否上移了止损
	Duplicate   bool // 幂等：与已有成交重复，跳过
	Note        string
}

// ApplyFills 批量应用成交（幂等：同 code+date+side+price+qty 跳过）。
// 买入：匹配 stop_buy 订单 → 开仓/加码（腿记录+止损上移）；
// 卖出：匹配 stop_sell 订单 → 减仓/清仓。
func (l *Ledger) ApplyFills(fills []contract.Fill) ([]ApplyResult, error) {
	var results []ApplyResult
	for _, f := range fills {
		r, err := l.ApplyFill(f)
		if err != nil {
			return results, fmt.Errorf("apply %s %s: %w", f.Code, f.Date, err)
		}
		results = append(results, r)
	}
	return results, nil
}

// ApplyFill 应用单笔成交。
func (l *Ledger) ApplyFill(f contract.Fill) (ApplyResult, error) {
	res := ApplyResult{Fill: f}
	if f.Side != "buy" && f.Side != "sell" {
		return res, fmt.Errorf("成交方向非法: %q", f.Side)
	}

	// 幂等检查
	dup, err := l.st.TradeRepo.FillExists(f.Code, f.Date, f.Side, f.Price, f.Qty)
	if err != nil {
		return res, err
	}
	if dup {
		res.Duplicate = true
		res.Note = "已存在同 code+date+side+price+qty 的成交，跳过"
		return res, nil
	}

	// 证券行兜底（INSERT OR IGNORE，不覆盖既有元数据；避免新库/冷门代码 FK 失败）
	if err := l.st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(f.Code, "")}); err != nil {
		return res, err
	}

	// 匹配待成交订单（执行日≤成交日，触发价最接近者优先）
	orders, err := l.st.TradeRepo.PendingOrdersFor(f.Code, f.Side, f.Date)
	if err != nil {
		return res, err
	}
	var best *store.PendingOrder
	for i := range orders {
		o := &orders[i]
		if best == nil || math.Abs(o.LimitPrice-f.Price) < math.Abs(best.LimitPrice-f.Price) {
			best = o
		}
	}
	if best != nil {
		res.Matched, res.OrderID = true, best.ID
		f.PlanOrderID = best.ID
		// 状态机流转 pending/placed → filled
		cur, err := l.st.TradeRepo.OrderStatus(best.ID)
		if err != nil {
			return res, err
		}
		if err := l.SetOrderStatus(best.ID, cur, "filled"); err != nil {
			return res, fmt.Errorf("订单状态流转: %w", err)
		}
	}

	// 入库留痕
	if err := l.st.TradeRepo.AppendFill(f); err != nil {
		return res, err
	}

	// 持仓状态机
	if f.Side == "buy" {
		posID, stopUp, err := l.applyBuy(f, best)
		if err != nil {
			return res, err
		}
		res.PositionID, res.StopUpdated = posID, stopUp
	} else {
		posID, err := l.applySell(f, best)
		if err != nil {
			return res, err
		}
		res.PositionID = posID
	}
	return res, nil
}

// applyBuy 买入成交：开仓或加码（金字塔腿 + 止损上移）。
func (l *Ledger) applyBuy(f contract.Fill, order *store.PendingOrder) (posID int64, stopUp bool, err error) {
	pos, err := l.st.TradeRepo.PositionByCode(f.Code)
	if err != nil {
		return 0, false, err
	}
	newStop := reasonStop(order) // 计划止损价（来自订单 reason.stop）
	legNo := 0
	if order != nil && order.LegNo > 0 {
		legNo = order.LegNo
	}
	if pos != nil {
		// 加码：腿号取既有最大腿号+1（手动加码无订单时）；计划腿号冲突同样顺延
		legs, _ := l.st.TradeRepo.LegsByPosition(pos.ID)
		maxLeg := 0
		for _, lg := range legs {
			if n, _ := lg["leg_no"].(int64); int(n) > maxLeg {
				maxLeg = int(n)
			}
			if legNo > 0 && int(lg["leg_no"].(int64)) == legNo {
				legNo = 0 // 与已有腿冲突 → 顺延
			}
		}
		if legNo == 0 {
			legNo = maxLeg + 1
		}
	}
	if legNo == 0 {
		legNo = 1 // 开仓首腿
	}

	if pos == nil {
		// 开仓
		var entryBox int64
		if order != nil {
			entryBox = reasonBoxID(order)
		}
		posID, err = l.st.TradeRepo.CreatePosition(contract.Position{
			Code: f.Code, OpenedDate: f.Date, EntryBoxID: entryBox,
			Qty: f.Qty, AvgCost: f.Price, StopPrice: newStop,
		})
		if err != nil {
			return 0, false, err
		}
		stopUp = newStop > 0
	} else {
		// 加码：摊薄成本
		posID = pos.ID
		newQty := pos.Qty + f.Qty
		avg := (pos.AvgCost*float64(pos.Qty) + f.Price*float64(f.Qty)) / float64(newQty)
		if err := l.st.TradeRepo.UpdatePositionQtyCost(posID, newQty, avg); err != nil {
			return 0, false, err
		}
		// 止损上移（只升不降，触发器兜底 ABORT）
		if newStop > pos.StopPrice {
			if err := l.st.TradeRepo.UpdateStop(posID, newStop); err != nil {
				return 0, false, fmt.Errorf("止损上移被拒绝: %w", err)
			}
			stopUp = true
		}
	}
	// 腿记录（含成交后止损位，审计）；超过 maxlegs=3 的手动加码不记腿（CHECK约束）
	if legNo >= 1 && legNo <= 3 {
		if err := l.st.TradeRepo.AddLeg(posID, legNo, f.Date, f.Qty, f.Price, newStop, f.PlanOrderID); err != nil {
			return posID, stopUp, err
		}
	}
	// B2 接线（docs/03 §2.2）：买入成交 → watch 持仓化（watching/pending → holding）。
	if w, _ := l.st.PoolRepo.WatchByCode(f.Code); w != nil &&
		(w.Stage == contract.StagePending || w.Stage == contract.StageWatching) {
		w.Stage = contract.StageHolding
		l.st.PoolRepo.UpsertWatch(*w)
	}
	return posID, stopUp, nil
}

// applySell 卖出成交：减仓或清仓。
func (l *Ledger) applySell(f contract.Fill, order *store.PendingOrder) (int64, error) {
	pos, err := l.st.TradeRepo.PositionByCode(f.Code)
	if err != nil {
		return 0, err
	}
	if pos == nil {
		return 0, fmt.Errorf("卖出成交但无持仓: %s（请核对方向或先录入买入）", f.Code)
	}
	newQty := pos.Qty - f.Qty
	if newQty < 0 {
		return pos.ID, fmt.Errorf("卖出数量 %d 超过持仓 %d: %s", f.Qty, pos.Qty, f.Code)
	}
	if newQty == 0 {
		reason := "stop" // 计划内止损离场
		if order == nil {
			reason = "manual" // 计划外人工卖出
		}
		if err := l.st.TradeRepo.ClosePosition(pos.ID, f.Date, reason); err != nil {
			return pos.ID, err
		}
		// B2 接线：清仓 → watch 持仓态出池；止损离场后即时两亏拉黑判定。
		l.onPositionClosed(f.Code, reason)
		return pos.ID, nil
	}
	return pos.ID, l.st.TradeRepo.UpdatePositionQtyCost(pos.ID, newQty, pos.AvgCost)
}

// onPositionClosed 清仓后的 watch 状态迁移（B2）：holding/pending → exited；
// 计划内止损离场（reason=stop）且该股历史亏损交易满 pool.maxloss → blacklisted（poolsync 次日兜底双保险）。
func (l *Ledger) onPositionClosed(code, reason string) {
	w, err := l.st.PoolRepo.WatchByCode(code)
	if err != nil || w == nil {
		return
	}
	if w.Stage == contract.StageExited || w.Stage == contract.StageBlacklisted {
		return
	}
	if reason == "stop" {
		maxLoss := l.st.ParamRepo.GetInt("pool.maxloss", 2)
		if n, err := l.st.TradeRepo.LossCountByCode(code); err == nil && n >= maxLoss {
			w.Stage = contract.StageBlacklisted
			w.ExitReason = fmt.Sprintf("two_losses_%d", n)
			l.st.PoolRepo.UpsertWatch(*w)
			return
		}
	}
	w.Stage = contract.StageExited
	w.ExitReason = "position_closed_" + reason
	l.st.PoolRepo.UpsertWatch(*w)
}

// reasonStop 从订单 reason 提取计划止损价（无则0）。
func reasonStop(o *store.PendingOrder) float64 {
	if o == nil {
		return 0
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(o.ReasonJSON), &m); err != nil {
		return 0
	}
	if v, ok := m["stop"].(float64); ok {
		return v
	}
	return 0
}

// reasonBoxID 从订单 reason 提取箱体ID。
func reasonBoxID(o *store.PendingOrder) int64 {
	if o == nil {
		return 0
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(o.ReasonJSON), &m); err != nil {
		return 0
	}
	if v, ok := m["box_id"].(float64); ok {
		return int64(v)
	}
	return 0
}

// ---------------------------------------------------------------------------
// M4.6 权益快照 + 恒等式审计
// ---------------------------------------------------------------------------

// baseEquity 初始本金（param equity.base，默认100万）。
func (l *Ledger) baseEquity() float64 {
	return l.st.ParamRepo.GetFloat("equity.base", 1000000.0)
}

// computeEquity 从成交+持仓重算某日权益：
// 现金 = 本金 + Σ卖出所得 − Σ买入支出（≤date 的全部成交）；市值 = open持仓 × 当日收盘。
func (l *Ledger) computeEquity(date string) (cash, mv float64, positions []contract.Position, err error) {
	fills, err := l.st.TradeRepo.FillsAll()
	if err != nil {
		return 0, 0, nil, err
	}
	cash = l.baseEquity()
	for _, f := range fills {
		if f.Date > date {
			continue
		}
		if f.Side == "buy" {
			cash -= f.Price*float64(f.Qty) + f.Fee
		} else {
			cash += f.Price*float64(f.Qty) - f.Fee
		}
	}
	allPos, err := l.st.TradeRepo.AllPositions()
	if err != nil {
		return 0, 0, nil, err
	}
	for _, p := range allPos {
		if p.Status != "open" || p.OpenedDate > date {
			continue
		}
		positions = append(positions, p)
		px, ok, _ := l.st.BarRepo.CloseOn(p.Code, date)
		if !ok {
			px = l.lastCloseBefore(p.Code, date, p.AvgCost)
		}
		mv += px * float64(p.Qty)
	}
	return cash, mv, positions, nil
}

// lastCloseBefore 取 date 当日或之前最近收盘价（停牌兜底），无则 def。
func (l *Ledger) lastCloseBefore(code, date string, def float64) float64 {
	bars, err := l.st.BarRepo.BarsWindow(code, date, 1)
	if err != nil || len(bars) == 0 {
		return def
	}
	return bars[len(bars)-1].Close
}

// Snapshot 生成某日权益快照（contract.Ledger 接口）。幂等：重跑覆盖。
// peak/drawdown 与此前最新快照衔接（首日 peak=total）。
func (l *Ledger) Snapshot(ctx context.Context, date string) error {
	cash, mv, _, err := l.computeEquity(date)
	if err != nil {
		return err
	}
	total := cash + mv
	peak := total
	var prev *contract.EquitySnapshot
	all, err := l.st.TradeRepo.EquityAll()
	if err != nil {
		return err
	}
	for i := len(all) - 1; i >= 0; i-- {
		if all[i].Date < date {
			prev = &all[i]
			break
		}
	}
	if prev != nil && prev.PeakTotal > peak {
		peak = prev.PeakTotal
	}
	dd := 0.0
	if peak > 0 {
		dd = (total - peak) / peak // 负数或0
	}
	return l.st.TradeRepo.UpsertEquity(contract.EquitySnapshot{
		Date: date, Cash: cash, MarketValue: mv, Total: total, PeakTotal: peak, Drawdown: dd,
	})
}

// AuditEquation 恒等式审计（contract.Ledger 接口）：
//  1. 权益恒等式: cash + market_value == total（对指定日或最新快照）
//  2. 持仓闭合:   Σ买入qty − Σ卖出qty == open持仓qty（逐股）
//  3. 订单闭合:   filled 订单必有对应 fill
//
// 返回 (是否通过, 详细说明, 错误)。
func (l *Ledger) AuditEquation(ctx context.Context, date string) (bool, string, error) {
	var problems []string

	// 目标日：参数为空 → 最新快照日
	if date == "" {
		eq, err := l.st.TradeRepo.LatestEquity()
		if err != nil {
			return false, "", err
		}
		if eq == nil {
			return true, "无任何权益快照，无事可审", nil
		}
		date = eq.Date
	}

	// 1. 权益恒等式（重算 vs 快照存储 + 快照内部自洽）
	cash, mv, positions, err := l.computeEquity(date)
	if err != nil {
		return false, "", err
	}
	var stored *contract.EquitySnapshot
	all, err := l.st.TradeRepo.EquityAll()
	if err != nil {
		return false, "", err
	}
	for i := len(all) - 1; i >= 0; i-- {
		if all[i].Date <= date {
			stored = &all[i]
			break
		}
	}
	if stored == nil {
		problems = append(problems, fmt.Sprintf("%s 无权益快照", date))
	} else {
		reTotal := cash + mv
		if math.Abs(reTotal-stored.Total) > 0.01 {
			problems = append(problems, fmt.Sprintf(
				"权益恒等式不平: 快照 total=%.2f, 重算 cash+mv=%.2f (diff=%.2f)",
				stored.Total, reTotal, reTotal-stored.Total))
		}
		if math.Abs((stored.Cash+stored.MarketValue)-stored.Total) > 0.01 {
			problems = append(problems, fmt.Sprintf(
				"快照内部恒等式不平: cash(%.2f)+mv(%.2f)=%.2f ≠ total=%.2f",
				stored.Cash, stored.MarketValue, stored.Cash+stored.MarketValue, stored.Total))
		}
	}

	// 2. 持仓闭合（逐股：成交净量 vs 持仓qty）
	fills, err := l.st.TradeRepo.FillsAll()
	if err != nil {
		return false, "", err
	}
	net := map[string]int{}
	for _, f := range fills {
		if f.Side == "buy" {
			net[f.Code] += f.Qty
		} else {
			net[f.Code] -= f.Qty
		}
	}
	seen := map[string]bool{}
	for _, p := range positions {
		seen[p.Code] = true
		if net[p.Code] != p.Qty {
			problems = append(problems, fmt.Sprintf(
				"持仓不闭合: %s 成交净量=%d ≠ 持仓qty=%d", p.Code, net[p.Code], p.Qty))
		}
	}
	for code, n := range net {
		if n != 0 && !seen[code] {
			problems = append(problems, fmt.Sprintf(
				"孤儿成交净持仓: %s 净量=%d 但无 open 持仓记录", code, n))
		}
	}

	// 3. 订单闭合：filled 订单必有 fill 引用
	var orphanFilled int
	err = l.st.DB().QueryRow(`SELECT COUNT(*) FROM plan_order o
		WHERE o.status='filled' AND NOT EXISTS (SELECT 1 FROM trade_fill f WHERE f.plan_order_id = o.id)`).
		Scan(&orphanFilled)
	if err != nil {
		return false, "", err
	}
	if orphanFilled > 0 {
		problems = append(problems, fmt.Sprintf("%d 个 filled 订单无对应成交记录", orphanFilled))
	}

	if len(problems) > 0 {
		return false, strings.Join(problems, "; "), nil
	}
	return true, fmt.Sprintf("恒等式全部通过（%d只持仓, 现金=%.2f, 市值=%.2f, 总权益=%.2f）",
		len(positions), cash, mv, cash+mv), nil
}
