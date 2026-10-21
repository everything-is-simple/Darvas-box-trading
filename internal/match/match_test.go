package match

import (
	"context"
	"math"
	"testing"

	"darvas/internal/contract"
	"darvas/internal/store"
)

func bar(o, h, l, c float64) contract.DailyBar {
	return contract.DailyBar{Open: o, High: h, Low: l, Close: c}
}

func near(a, b float64) bool { return math.Abs(a-b) < 1e-6 }

func TestMatchOrder(t *testing.T) {
	cases := []struct {
		name          string
		side          string
		trigger, lim  float64
		b             contract.DailyBar
		wantPx        float64
		wantHit       bool
	}{
		{"buy 跳空高开", "buy", 10, 10, bar(11, 11.5, 10.8, 11.2), 11, true},
		{"buy 盘中上穿", "buy", 10, 10, bar(9, 10.5, 8.8, 10.2), 10, true},
		{"buy 未触发", "buy", 10, 10, bar(9, 9.8, 8.8, 9.5), 0, false},
		{"buy 触发但限价更低(limit!=trigger)", "buy", 10, 10.2, bar(9, 10.5, 8.8, 10.2), 10.2, true},
		{"sell 跳空低开", "sell", 10, 10, bar(9, 9.8, 8.8, 9.2), 9, true},
		{"sell 盘中下穿", "sell", 10, 10, bar(11, 11.2, 9.8, 10.5), 10, true},
		{"sell 未触发", "sell", 10, 10, bar(11, 11.2, 10.5, 10.8), 0, false},
		{"sell 触发但限价不同", "sell", 10, 9.8, bar(11, 11.2, 9.7, 10.5), 9.8, true},
	}
	for _, c := range cases {
		px, hit := matchOrder(c.side, c.trigger, c.lim, c.b)
		if hit != c.wantHit {
			t.Errorf("%s: hit=%v want %v", c.name, hit, c.wantHit)
		}
		if hit && !near(px, c.wantPx) {
			t.Errorf("%s: px=%.4f want %.4f", c.name, px, c.wantPx)
		}
	}
}

func TestIsLimitBoard(t *testing.T) {
	up, down := isLimitBoard(bar(11, 11, 11, 11), 10)
	if !up || down {
		t.Errorf("一字涨停判定失败: up=%v down=%v", up, down)
	}
	up, down = isLimitBoard(bar(9, 9, 9, 9), 10)
	if up || !down {
		t.Errorf("一字跌停判定失败: up=%v down=%v", up, down)
	}
	up, down = isLimitBoard(bar(9.5, 10.5, 9.3, 10.2), 10)
	if up || down {
		t.Errorf("非一字板误判: up=%v down=%v", up, down)
	}
}

func TestFeeOf(t *testing.T) {
	p := feeParams{commissionRate: 0.00025, commissionMin: 5, stampTax: 0.001, transferRate: 0.00001}
	// 买入 1万：佣金 max(2.5,5)=5 + 过户 0.1 = 5.1
	if f := feeOf("buy", 10, 1000, p); !near(f, 5.1) {
		t.Errorf("买入小单费=%.4f want 5.1", f)
	}
	// 卖出 1万：5 + 印花10 + 过户0.1 = 15.1
	if f := feeOf("sell", 10, 1000, p); !near(f, 15.1) {
		t.Errorf("卖出小单费=%.4f want 15.1", f)
	}
	// 买入 10万：佣金 max(25,5)=25 + 过户1 = 26
	if f := feeOf("buy", 100, 1000, p); !near(f, 26) {
		t.Errorf("买入大单费=%.4f want 26", f)
	}
}

func TestIsDividendGap(t *testing.T) {
	// 主板 15% 跌（超过 9% 阈值）→ 除权
	if !isDividendGap(bar(0, 0, 0, 8.5), bar(0, 0, 0, 10), 0.09) {
		t.Errorf("主板 15%% 跌应判除权")
	}
	// 主板 5% 跌 → 非除权
	if isDividendGap(bar(0, 0, 0, 9.5), bar(0, 0, 0, 10), 0.09) {
		t.Errorf("主板 5%% 跌不应判除权")
	}
	// 创业板 15% 跌（未超 19% 阈值）→ 非除权
	if isDividendGap(bar(0, 0, 0, 8.5), bar(0, 0, 0, 10), 0.19) {
		t.Errorf("创业板 15%% 跌不应判除权")
	}
	// 昨收为 0 → 不判
	if isDividendGap(bar(0, 0, 0, 8), bar(0, 0, 0, 0), 0.09) {
		t.Errorf("昨收为0不应判除权")
	}
}

func TestGapThreshold(t *testing.T) {
	cases := map[string]float64{
		"sh600519": 0.09, // 沪主板
		"sz000001": 0.09, // 深主板
		"sz300750": 0.19, // 创业板
		"sh688981": 0.19, // 科创板
	}
	for code, want := range cases {
		if got := gapThreshold(code); !near(got, want) {
			t.Errorf("%s 阈值=%.4f want %.4f", code, got, want)
		}
	}
}

// TestSlip C4（2026-10-22）：滑点买贵卖贱；0/负值关闭；价格非法原样返回。
func TestSlip(t *testing.T) {
	if px := slip("buy", 10, 0.002); !near(px, 10.02) {
		t.Errorf("买入滑点价=%.4f want 10.02", px)
	}
	if px := slip("sell", 10, 0.002); !near(px, 9.98) {
		t.Errorf("卖出滑点价=%.4f want 9.98", px)
	}
	if px := slip("buy", 10, 0); !near(px, 10) {
		t.Errorf("滑点关闭应原价=%.4f want 10", px)
	}
	if px := slip("buy", 0, 0.002); !near(px, 0) {
		t.Errorf("非法价格应原样返回=%.4f want 0", px)
	}
}

// TestMatchSlippage C4（2026-10-22）：模拟成交按 match.slippage（默认 0.2%）买贵卖贱，
// 滑点先于 max_entry_gap 闸门（闸门校验实际要付的价）。含两个边界：
//   ① 滑点后越过闸门边界（raw 1.05 恰好不拒、slipped 1.0521 > 1.05 拒）→ 弃单；
//   ② match.slippage=0 → 关闭，按原始成交价。
func TestMatchSlippage(t *testing.T) {
	st, err := store.Open(t.TempDir() + "/test.db")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer st.Close()
	if err := st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sz000001", "")}); err != nil {
		t.Fatalf("seed secs: %v", err)
	}
	mkBar := func(date string, o, h, l, c float64) contract.DailyBar {
		return contract.DailyBar{Code: "sz000001", Date: date, Open: o, High: h, Low: l, Close: c}
	}
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		mkBar("2014-07-29", 10, 10.2, 9.8, 10),
		mkBar("2014-07-30", 10.4, 10.6, 10.2, 10.5), // 跳空高开，raw 比值 1.04 < 1.05 → 成交
		mkBar("2014-07-31", 10.5, 10.7, 10.3, 10.6),  // raw 比值恰 1.05（不拒）但滑点后 1.0521 > 1.05 → 弃单
		mkBar("2014-08-01", 10.4, 10.5, 10.2, 10.45), // slippage=0 后重测；raw 1.04 < 1.05 → 成交
	}); err != nil {
		t.Fatalf("seed bars: %v", err)
	}

	m := New(st)
	ctx := context.Background()
	addBuy := func(planDate, execDate string, trigger float64) {
		t.Helper()
		planID, err := st.TradeRepo.CreatePlan(contract.TradePlan{PlanDate: planDate, ExecuteDate: execDate, EquityBase: 1e6})
		if err != nil {
			t.Fatalf("CreatePlan: %v", err)
		}
		if err := st.TradeRepo.AddOrders(planID, []contract.PlanOrder{{
			PlanDate: planDate, ExecuteDate: execDate, Code: "sz000001", Side: "buy",
			OrderKind: "stop_buy", TriggerPrice: trigger, LimitPrice: trigger, Qty: 1000,
		}}); err != nil {
			t.Fatalf("AddOrders: %v", err)
		}
	}

	// ① 默认滑点 0.2%：跳空 10.4 成交价应为 10.4×1.002=10.4208
	addBuy("2014-07-29", "2014-07-30", 10.0)
	if n, err := m.Match(ctx, "2014-07-30"); err != nil || n != 1 {
		t.Fatalf("07-30 撮合 n=%d err=%v（raw 比值 1.04 应成交）", n, err)
	}
	var px float64
	if err := st.DB().QueryRow(`SELECT price FROM trade_fill WHERE date='2014-07-30'`).Scan(&px); err != nil {
		t.Fatalf("查成交价: %v", err)
	}
	if !near(px, 10.4*1.002) {
		t.Fatalf("滑点成交价=%.4f want %.4f", px, 10.4*1.002)
	}

	// ② 滑点后越过闸门：raw 10.5/10=1.05 不拒（严格大于才拒），滑点后 1.0521 > 1.05 → 弃单
	addBuy("2014-07-30", "2014-07-31", 10.0)
	if n, err := m.Match(ctx, "2014-07-31"); err != nil || n != 0 {
		t.Fatalf("07-31 撮合 n=%d err=%v（滑点后越闸门应弃单）", n, err)
	}
	var status string
	if err := st.DB().QueryRow(`SELECT po.status FROM plan_order po
		JOIN trade_plan tp ON po.trade_plan_id = tp.id
		WHERE tp.execute_date='2014-07-31'`).Scan(&status); err != nil {
		t.Fatalf("查订单状态: %v", err)
	}
	if status != "cancelled" {
		t.Fatalf("滑点越闸门订单 status=%s，期望 cancelled", status)
	}

	// ③ match.slippage=0 关闭：按原始成交价 10.4
	if err := st.ParamRepo.Set("match.slippage", "0"); err != nil {
		t.Fatalf("关闭滑点: %v", err)
	}
	addBuy("2014-07-31", "2014-08-01", 10.0)
	if n, err := m.Match(ctx, "2014-08-01"); err != nil || n != 1 {
		t.Fatalf("08-01 撮合 n=%d err=%v（滑点关闭应成交）", n, err)
	}
	if err := st.DB().QueryRow(`SELECT price FROM trade_fill WHERE date='2014-08-01'`).Scan(&px); err != nil {
		t.Fatalf("查成交价: %v", err)
	}
	if !near(px, 10.4) {
		t.Fatalf("滑点关闭成交价=%.4f want 10.4", px)
	}
}

// TestMatchDividendClearCancelsPendingOrders 回归：除权日强制清仓后，在途订单必须取消。
// 场景复刻 2018-05-23 sz300630：05-21 持仓 + 挂 05-23 止损卖单 → 05-22 除权跳空清仓
// → 修复前 05-23 陈旧卖单触发「卖出成交但无持仓」报错，回放中断无汇总报告。
func TestMatchDividendClearCancelsPendingOrders(t *testing.T) {
	st, err := store.Open(t.TempDir() + "/test.db")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer st.Close()
	if err := st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sz300630", "")}); err != nil {
		t.Fatalf("seed secs: %v", err)
	}
	mkBar := func(date string, o, h, l, c float64) contract.DailyBar {
		return contract.DailyBar{Code: "sz300630", Date: date, Open: o, High: h, Low: l, Close: c}
	}
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		mkBar("2018-05-21", 104, 105, 103, 104),
		mkBar("2018-05-22", 70, 71, 69, 69.28), // 除权跳空 -33%（创业板阈值 19%）
		mkBar("2018-05-23", 69.5, 70, 68.5, 69.0),
	}); err != nil {
		t.Fatalf("seed bars: %v", err)
	}

	m := New(st)
	ctx := context.Background()

	// 05-21 买入开仓 500 股
	if _, err := m.l.ApplyFill(contract.Fill{Code: "sz300630", Date: "2018-05-21",
		Side: "buy", Price: 104, Qty: 500, RawText: "sim:order"}); err != nil {
		t.Fatalf("buy fill: %v", err)
	}

	// 05-22 盘后计划：05-23 执行的止损卖单（trigger=92.344，复权前价格）
	planID, err := st.TradeRepo.CreatePlan(contract.TradePlan{PlanDate: "2018-05-22", ExecuteDate: "2018-05-23", EquityBase: 1e6})
	if err != nil {
		t.Fatalf("CreatePlan: %v", err)
	}
	if err := st.TradeRepo.AddOrders(planID, []contract.PlanOrder{{
		PlanDate: "2018-05-22", ExecuteDate: "2018-05-23", Code: "sz300630", Side: "sell",
		OrderKind: "stop_sell", TriggerPrice: 92.344, LimitPrice: 92.344, Qty: 500,
	}}); err != nil {
		t.Fatalf("AddOrders: %v", err)
	}

	// 05-22 撮合：除权跳空 → 强制清仓 + 取消在途订单
	n, err := m.Match(ctx, "2018-05-22")
	if err != nil {
		t.Fatalf("除权日撮合报错: %v", err)
	}
	if n != 1 {
		t.Fatalf("除权日成交笔数=%d want 1（sim:dividend 清仓）", n)
	}

	// 05-23 撮合：修复前这里报「卖出成交但无持仓」；修复后应零成交、无报错
	n2, err := m.Match(ctx, "2018-05-23")
	if err != nil {
		t.Fatalf("次日撮合报错（陈旧卖单未取消）: %v", err)
	}
	if n2 != 0 {
		t.Fatalf("次日成交笔数=%d want 0", n2)
	}
	// 在途卖出单应为空（已 cancelled）
	remaining, err := st.TradeRepo.PendingOrdersFor("sz300630", "sell", "2018-05-23")
	if err != nil {
		t.Fatalf("PendingOrdersFor: %v", err)
	}
	if len(remaining) != 0 {
		t.Fatalf("在途卖出单残留 %d 笔，应已取消", len(remaining))
	}
}

// TestMatchRejectGapChase P0-2（2026-10-21）：买入单跳空高开成交价超触发价 > max_entry_gap → 弃单。
// 场景（sz002640 2014-07-30）：trigger=17.10，当日跳空高开 27.80（+62.6%），
// 修复前按开盘价成交（追高假利润）；修复后弃单置 cancelled。
func TestMatchRejectGapChase(t *testing.T) {
	st, err := store.Open(t.TempDir() + "/test.db")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer st.Close()
	if err := st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sz002640", "")}); err != nil {
		t.Fatalf("seed secs: %v", err)
	}
	mkBar := func(date string, o, h, l, c float64) contract.DailyBar {
		return contract.DailyBar{Code: "sz002640", Date: date, Open: o, High: h, Low: l, Close: c}
	}
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		mkBar("2014-07-29", 17.0, 17.2, 16.8, 17.1),
		mkBar("2014-07-30", 27.8, 28.0, 27.5, 27.8), // 跳空高开 +62.6%
	}); err != nil {
		t.Fatalf("seed bars: %v", err)
	}

	m := New(st)
	ctx := context.Background()

	// 07-29 计划：07-30 执行的买入单（trigger=17.10）
	planID, err := st.TradeRepo.CreatePlan(contract.TradePlan{PlanDate: "2014-07-29", ExecuteDate: "2014-07-30", EquityBase: 1e6})
	if err != nil {
		t.Fatalf("CreatePlan: %v", err)
	}
	if err := st.TradeRepo.AddOrders(planID, []contract.PlanOrder{{
		PlanDate: "2014-07-29", ExecuteDate: "2014-07-30", Code: "sz002640", Side: "buy",
		OrderKind: "stop_buy", TriggerPrice: 17.10, LimitPrice: 17.10, Qty: 1000,
	}}); err != nil {
		t.Fatalf("AddOrders: %v", err)
	}

	// 07-30 撮合：跳空高开 +62.6% > max_entry_gap=5% → 弃单
	n, err := m.Match(ctx, "2014-07-30")
	if err != nil {
		t.Fatalf("撮合报错: %v", err)
	}
	if n != 0 {
		t.Fatalf("跳空追高应弃单（成交 0 笔），实际 %d 笔", n)
	}
	// 订单应置 cancelled（直接查 plan_order.status）
	var status string
	if err := st.DB().QueryRow(`SELECT status FROM plan_order WHERE code='sz002640'`).Scan(&status); err != nil {
		t.Fatalf("查订单状态: %v", err)
	}
	if status != "cancelled" {
		t.Fatalf("跳空追高订单 status=%s，期望 cancelled", status)
	}
}
