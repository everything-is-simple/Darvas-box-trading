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
