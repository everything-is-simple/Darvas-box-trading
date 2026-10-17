package ledger

import (
	"strings"
	"testing"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// newLedger 构造测试账本（临时DB + 证券种子）。
func newLedger(tb testing.TB, codes ...string) (*Ledger, *store.Store) {
	tb.Helper()
	st, err := store.Open(tb.TempDir() + "/test.db")
	if err != nil {
		tb.Fatalf("Open: %v", err)
	}
	tb.Cleanup(func() { st.Close() })
	secs := make([]contract.Security, len(codes))
	for i, c := range codes {
		secs[i] = store.DeriveSecurity(c, "")
	}
	if err := st.SecRepo.UpsertSecurities(secs); err != nil {
		tb.Fatalf("seedSecs: %v", err)
	}
	return New(st), st
}

// seedPlan 写入一个含买卖订单的计划，返回 planID。
func seedPlan(tb testing.TB, l *Ledger, st *store.Store, planDate, execDate string) int64 {
	tb.Helper()
	planID, err := st.TradeRepo.CreatePlan(contract.TradePlan{
		PlanDate: planDate, ExecuteDate: execDate, EquityBase: 1000000,
	})
	if err != nil {
		tb.Fatalf("CreatePlan: %v", err)
	}
	orders := []contract.PlanOrder{
		{PlanDate: planDate, ExecuteDate: execDate, Code: "sh600519", Side: "buy", OrderKind: "stop_buy",
			TriggerPrice: 1030, LimitPrice: 1030, Qty: 900, LegNo: 1,
			Reason: map[string]any{"stop": 970.0, "box_top": 1000.0, "box_bottom": 1000.0}},
		{PlanDate: planDate, ExecuteDate: execDate, Code: "sz000001", Side: "buy", OrderKind: "stop_buy",
			TriggerPrice: 10.30, LimitPrice: 10.30, Qty: 9000, LegNo: 1,
			Reason: map[string]any{"stop": 9.70}},
	}
	if err := st.TradeRepo.AddOrders(planID, orders); err != nil {
		tb.Fatalf("AddOrders: %v", err)
	}
	return planID
}

// ---------------------------------------------------------------------------
// M4.5 验收：样本文本解析
// ---------------------------------------------------------------------------

func TestParseFillFormats(t *testing.T) {
	l, _ := newLedger(t, "sh600519", "sz000001")
	cases := []struct {
		name string
		text string
		want contract.Fill
	}{
		{"同花顺类", "20261016 600519 买入 1700.50 100 170050.00",
			contract.Fill{Code: "sh600519", Date: "2026-10-16", Side: "buy", Price: 1700.50, Qty: 100}},
		{"通达信类", "2026-10-16 15:00:01  sh600519 贵州茅台  证券买入  1700.50  100",
			contract.Fill{Code: "sh600519", Date: "2026-10-16", Side: "buy", Price: 1700.50, Qty: 100}},
		{"CSV类", "2026/10/16,SZ000001,S,12.34,500,6170.00,5.00",
			contract.Fill{Code: "sz000001", Date: "2026-10-16", Side: "sell", Price: 12.34, Qty: 500, Fee: 5.00}},
	}
	for _, tc := range cases {
		fills, err := l.ParseFill(tc.text)
		if err != nil {
			t.Fatalf("%s: ParseFill: %v", tc.name, err)
		}
		if len(fills) != 1 {
			t.Fatalf("%s: 期望1笔, 得到%d笔", tc.name, len(fills))
		}
		got, exp := fills[0], tc.want
		if got.Code != exp.Code || got.Date != exp.Date || got.Side != exp.Side ||
			got.Price != exp.Price || got.Qty != exp.Qty || got.Fee != exp.Fee {
			t.Errorf("%s: got %+v, want %+v", tc.name, got, exp)
		}
	}
}

func TestParseFillMultiLineAndHeader(t *testing.T) {
	l, _ := newLedger(t, "sh600519", "sz000001")
	text := `成交日期        证券代码    操作      成交价格   成交数量
20261016      600519     买入      1700.50   100    170050.00
20261016      000001     卖出      12.34     500

`
	fills, err := l.ParseFill(text)
	if err != nil {
		t.Fatalf("ParseFill: %v", err)
	}
	if len(fills) != 2 {
		t.Fatalf("期望2笔（表头/空行跳过）, 得到%d笔", len(fills))
	}
	if fills[0].Side != "buy" || fills[1].Side != "sell" {
		t.Errorf("方向错误: %q / %q", fills[0].Side, fills[1].Side)
	}
}

func TestParseFillRejectsGarbage(t *testing.T) {
	l, _ := newLedger(t, "sh600519", "sz000001")
	for _, bad := range []string{
		"没有日期的一行 600519 买入 100",
		"20261016 600519 无方向词 1700.50 100",
		"20261016 600519 买入 1700.50", // 无数量
	} {
		if _, err := l.ParseFill(bad); err == nil {
			t.Errorf("应拒绝非法行: %q", bad)
		}
	}
}

// ---------------------------------------------------------------------------
// 订单状态机
// ---------------------------------------------------------------------------

func TestOrderStateMachine(t *testing.T) {
	valid := [][2]string{{"pending", "placed"}, {"pending", "filled"}, {"pending", "cancelled"},
		{"pending", "expired"}, {"placed", "filled"}, {"placed", "cancelled"}}
	for _, tr := range valid {
		if !CanTransition(tr[0], tr[1]) {
			t.Errorf("%s→%s 应合法", tr[0], tr[1])
		}
	}
	invalid := [][2]string{{"filled", "pending"}, {"filled", "cancelled"}, {"cancelled", "filled"},
		{"expired", "filled"}, {"pending", "pending"}}
	for _, tr := range invalid {
		if CanTransition(tr[0], tr[1]) {
			t.Errorf("%s→%s 应非法", tr[0], tr[1])
		}
	}
	// SetOrderStatus 拒绝非法迁移
	l, _ := newLedger(t, "sh600519", "sz000001")
	if err := l.SetOrderStatus(999, "filled", "pending"); err == nil || !strings.Contains(err.Error(), "非法") {
		t.Errorf("SetOrderStatus 应拒绝 filled→pending, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// ApplyFill：成交 → 订单 filled → 持仓状态机
// ---------------------------------------------------------------------------

func TestApplyBuyFillCreatesPosition(t *testing.T) {
	l, st := newLedger(t, "sh600519", "sz000001")
	seedPlan(t, l, st, "2026-10-15", "2026-10-16")

	res, err := l.ApplyFill(contract.Fill{
		Code: "sh600519", Date: "2026-10-16", Side: "buy", Price: 1030.5, Qty: 900,
	})
	if err != nil {
		t.Fatalf("ApplyFill: %v", err)
	}
	if !res.Matched {
		t.Fatal("应匹配到计划订单")
	}
	// 订单已 filled
	status, _ := st.TradeRepo.OrderStatus(res.OrderID)
	if status != "filled" {
		t.Errorf("订单状态应为 filled, got %s", status)
	}
	// 持仓已开
	pos, _ := st.TradeRepo.PositionByCode("sh600519")
	if pos == nil || pos.Qty != 900 || pos.AvgCost != 1030.5 {
		t.Fatalf("持仓错误: %+v", pos)
	}
	if pos.StopPrice != 970 { // 来自订单 reason.stop
		t.Errorf("止损应为970, got %v", pos.StopPrice)
	}
	// 幂等：重复粘贴跳过
	res2, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-16", Side: "buy", Price: 1030.5, Qty: 900})
	if err != nil {
		t.Fatalf("重复ApplyFill: %v", err)
	}
	if !res2.Duplicate {
		t.Error("重复成交应标记 Duplicate")
	}
}

func TestApplyPyramidAndSellClose(t *testing.T) {
	l, st := newLedger(t, "sh600519", "sz000001")
	seedPlan(t, l, st, "2026-10-15", "2026-10-16")

	// 首腿买入
	if _, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-16", Side: "buy", Price: 1030, Qty: 900}); err != nil {
		t.Fatalf("首腿: %v", err)
	}
	// 加码（计划外，更高价）：摊薄成本
	if _, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-20", Side: "buy", Price: 1100, Qty: 100}); err != nil {
		t.Fatalf("加码: %v", err)
	}
	pos, _ := st.TradeRepo.PositionByCode("sh600519")
	wantAvg := (1030*900 + 1100*100) / 1000.0
	if pos.Qty != 1000 || pos.AvgCost != wantAvg {
		t.Errorf("加码后 qty=%d avg=%.2f, want 1000/%.2f", pos.Qty, pos.AvgCost, wantAvg)
	}
	// 部分卖出
	if _, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-21", Side: "sell", Price: 1150, Qty: 400}); err != nil {
		t.Fatalf("部分卖出: %v", err)
	}
	pos, _ = st.TradeRepo.PositionByCode("sh600519")
	if pos == nil || pos.Qty != 600 {
		t.Fatalf("部分卖出后 qty 应为600, got %+v", pos)
	}
	// 清仓
	if _, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-22", Side: "sell", Price: 1100, Qty: 600}); err != nil {
		t.Fatalf("清仓: %v", err)
	}
	pos, _ = st.TradeRepo.PositionByCode("sh600519")
	if pos != nil {
		t.Errorf("清仓后不应有 open 持仓: %+v", pos)
	}
	all, _ := st.TradeRepo.AllPositions()
	if len(all) != 1 || all[0].Status != "closed" || all[0].CloseReason == "" {
		t.Errorf("closed 持仓记录错误: %+v", all)
	}
}

func TestSellWithoutPositionRejected(t *testing.T) {
	l, _ := newLedger(t, "sh600519", "sz000001")
	_, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-16", Side: "sell", Price: 100, Qty: 100})
	if err == nil || !strings.Contains(err.Error(), "无持仓") {
		t.Errorf("无持仓卖出应被拒绝, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 止损只升不降（DB 触发器 + 应用层双防护，M4.4 验收）
// ---------------------------------------------------------------------------

func TestStopOnlyGoesUp(t *testing.T) {
	l, st := newLedger(t, "sh600519", "sz000001")
	seedPlan(t, l, st, "2026-10-15", "2026-10-16")
	if _, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-16", Side: "buy", Price: 1030, Qty: 900}); err != nil {
		t.Fatalf("ApplyFill: %v", err)
	}
	pos, _ := st.TradeRepo.PositionByCode("sh600519")

	// 上移成功
	if err := st.TradeRepo.UpdateStop(pos.ID, 1000); err != nil {
		t.Fatalf("上移止损应成功: %v", err)
	}
	// 下移必须被触发器 ABORT
	if err := st.TradeRepo.UpdateStop(pos.ID, 980); err == nil {
		t.Fatal("下移止损应被触发器 ABORT")
	}
	// 降止损经 ApplyFill（订单 reason.stop 更低）也应保持不变
	if _, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-21", Side: "buy", Price: 1020, Qty: 100}); err != nil {
		t.Fatalf("低价加码: %v", err)
	}
	pos, _ = st.TradeRepo.PositionByCode("sh600519")
	if pos.StopPrice != 1000 {
		t.Errorf("止损应保持1000不降, got %v", pos.StopPrice)
	}
}

// ---------------------------------------------------------------------------
// M4.6 权益快照 + 恒等式审计
// ---------------------------------------------------------------------------

func TestSnapshotAndAudit(t *testing.T) {
	l, st := newLedger(t, "sh600519", "sz000001")
	seedPlan(t, l, st, "2026-10-15", "2026-10-16")
	// 当日收盘价（市值计算依赖）
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		{Code: "sh600519", Date: "2026-10-16", Close: 1050},
		{Code: "sh600519", Date: "2026-10-17", Close: 1080},
	}); err != nil {
		t.Fatalf("UpsertBars: %v", err)
	}

	// 无成交日：快照 = 全现金本金
	if err := l.Snapshot(t.Context(), "2026-10-15"); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	eq, _ := st.TradeRepo.LatestEquity()
	if eq.Cash != 1000000 || eq.Total != 1000000 {
		t.Errorf("初始快照错误: %+v", eq)
	}

	// 买入900股@1030 → 现金 1,000,000-927,000=73,000；市值 900×1050=945,000
	if _, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-16", Side: "buy", Price: 1030, Qty: 900}); err != nil {
		t.Fatalf("ApplyFill: %v", err)
	}
	if err := l.Snapshot(t.Context(), "2026-10-16"); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	eq, _ = st.TradeRepo.LatestEquity()
	if diff := eq.Cash - 73000; diff > 0.01 || diff < -0.01 {
		t.Errorf("现金应为73000, got %.2f", eq.Cash)
	}
	if diff := eq.MarketValue - 945000; diff > 0.01 || diff < -0.01 {
		t.Errorf("市值应为945000, got %.2f", eq.MarketValue)
	}

	// 审计通过
	ok, detail, err := l.AuditEquation(t.Context(), "")
	if err != nil {
		t.Fatalf("AuditEquation: %v", err)
	}
	if !ok {
		t.Errorf("审计应通过: %s", detail)
	}

	// 破坏恒等式（直接改快照）→ 审计必须报警
	st.DB().Exec(`UPDATE equity_snapshot SET total = total + 500 WHERE date='2026-10-16'`)
	ok, detail, _ = l.AuditEquation(t.Context(), "2026-10-16")
	if ok {
		t.Error("恒等式破坏后审计应失败")
	}
	if !strings.Contains(detail, "恒等式不平") {
		t.Errorf("审计详情应指明恒等式不平: %s", detail)
	}
}

func TestAuditPositionGap(t *testing.T) {
	l, st := newLedger(t, "sh600519", "sz000001")
	seedPlan(t, l, st, "2026-10-15", "2026-10-16")
	// 正常开仓 → 直接篡改持仓数量 → 持仓不闭合
	if _, err := l.ApplyFill(contract.Fill{Code: "sh600519", Date: "2026-10-16", Side: "buy", Price: 1030, Qty: 900}); err != nil {
		t.Fatalf("ApplyFill: %v", err)
	}
	st.DB().Exec(`UPDATE position SET qty = 800 WHERE code='sh600519'`)
	ok, detail, _ := l.AuditEquation(t.Context(), "2026-10-16")
	if ok || !strings.Contains(detail, "持仓不闭合") {
		t.Errorf("持仓数量篡改应被审计发现: ok=%v detail=%s", ok, detail)
	}
}
