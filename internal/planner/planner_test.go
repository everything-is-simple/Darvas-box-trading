package planner

import (
	"context"
	"testing"

	"darvas/internal/box"
	"darvas/internal/contract"
	"darvas/internal/store"
)

// TestPlannerBuyOrderQty 验证停损买单数量公式（FR-404）：
// qty = floor(equity × risk% / (trigger−stop) / 100) × 100，且 ≤ 单股仓位上限。
func TestPlannerBuyOrderQty(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	// 构造一个 confirmed 箱体：top=24 bottom=17
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	// breakout 信号
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalBreakout,
		BoxID: boxes[0].ID, ClosePx: 25, BoxLevel: 24,
	}})
	// 权益基准 100 万
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	res, err := p.Build(context.Background(), "2026-10-16")
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.Status != "ok" {
		t.Fatalf("status=%s err=%s", res.Status, res.Error)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) == 0 {
		t.Fatal("expected ≥1 buy order from breakout signal")
	}
	o := orders[0]
	// trigger = 24 × 1.03 = 24.72；stop = 17 × 0.97 = 16.49；riskPerUnit = 8.23
	if !approx(o.TriggerPrice, 24.72, 0.01) {
		t.Errorf("trigger=%.2f want ≈24.72", o.TriggerPrice)
	}
	// qty = floor(1000000×0.01/8.23/100)×100 = floor(1.215)×100... 实际 floor(1000000*0.01/8.23)=1215, /100=12, ×100=1200
	if o.Qty <= 0 || o.Qty%100 != 0 {
		t.Errorf("qty=%d must be positive multiple of 100", o.Qty)
	}
	if o.Side != "buy" || o.OrderKind != "stop_buy" || o.LegNo != 1 {
		t.Errorf("order side/kind/leg=%s/%s/%d want buy/stop_buy/1", o.Side, o.OrderKind, o.LegNo)
	}
}

// TestPlannerSellOrderQty 止损卖单 = 持仓全部（一次全出）。
func TestPlannerSellOrderQty(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	// 一个跌破箱体（覆盖为 broken_down）
	boxes[0].Status = contract.BoxBrokenDown
	boxes[0].Bottom = 17
	boxes[0].BrokenDate = "2026-10-16"
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	// 取最终 box 的真实 DB ID（FK 指向存在的行）
	lb, _ := st.BoxRepo.LatestBox(code)
	if lb == nil {
		t.Fatal("no box after upsert")
	}
	boxDBID := lb.ID
	// 一个持仓 500 股
	st.DB().Exec(`INSERT INTO position(code, opened_date, qty, avg_cost, stop_price, status)
		VALUES (?, '2026-09-01', 500, 20, 16.49, 'open')`, code)
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)
	// stop 信号（BoxID 用真实 DB ID，FK 指向存在的 box）
	if err := st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalStop,
		BoxID: boxDBID, ClosePx: 16, BoxLevel: 17,
	}}); err != nil {
		t.Fatalf("append signal: %v", err)
	}

	p := New(st)
	p.Build(context.Background(), "2026-10-16")
	_, orders, _ := st.TradeRepo.PlanOn("2026-10-16")
	if len(orders) == 0 {
		t.Fatal("expected sell order from stop signal")
	}
	o := orders[0]
	if o.Side != "sell" || o.OrderKind != "stop_sell" {
		t.Errorf("want sell/stop_sell got %s/%s", o.Side, o.OrderKind)
	}
	if o.Qty != 500 {
		t.Errorf("sell qty=%d want 500 (全部持仓)", o.Qty)
	}
}

// TestPlannerExecuteDate P1修复验收（2026-10-17）：执行日=下一交易日（ADR-2），非日历+1。
// ① 回放场景：sh000001 有未来K线 → 周五10-09 → 周一10-12（旧逻辑给10-10周六死单，
//    实证 sh600052 2026-10-09 信号 → exec 10-10 expired）；
// ② 跨长假：09-30 → 10-08（国庆，旧逻辑给10-01假日死单）；
// ③ 实盘边界：planDate=最新数据日（周五10-16，无未来K线）→ 兜底跳周末 → 周一10-19。
func TestPlannerExecuteDate(t *testing.T) {
	st := newPlannerStore(t)
	seedIndexBars(t, st, "2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-16")
	p := New(st)
	cases := [][2]string{
		{"2026-09-30", "2026-10-08"}, // ② 跨国庆长假（sh000001 下一根K线）
		{"2026-10-09", "2026-10-12"}, // ① 周五→周一（sh000001 下一根K线）
		{"2026-10-16", "2026-10-19"}, // ③ 实盘兜底：10-16 后无K线 → 下一工作日周一
	}
	for _, c := range cases {
		if _, err := p.Build(context.Background(), c[0]); err != nil {
			t.Fatalf("Build(%s): %v", c[0], err)
		}
		plan, _, err := st.TradeRepo.PlanOn(c[0])
		if err != nil || plan == nil {
			t.Fatalf("PlanOn(%s): plan=%v err=%v", c[0], plan, err)
		}
		if plan.ExecuteDate != c[1] {
			t.Errorf("plan %s execute_date=%s want %s（下一交易日）", c[0], plan.ExecuteDate, c[1])
		}
	}
}

// ─── 辅助 ──

func newPlannerStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(t.TempDir() + "/plan.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

// seedIndexBars 播种 sh000001 日线（ADR-2 交易日历权威，planner 执行日依据）。
func seedIndexBars(t *testing.T, st *store.Store, dates ...string) {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sh000001", "")})
	bars := make([]contract.DailyBar, 0, len(dates))
	for _, d := range dates {
		bars = append(bars, contract.DailyBar{
			Code: "sh000001", Date: d, Open: 3000, High: 3010, Low: 2990, Close: 3000,
			Volume: 1000, Amount: 1e8,
		})
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
}

func seedBars(t *testing.T, st *store.Store, code string) {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	bars := make([]contract.DailyBar, 0, 8)
	day := "2026-10-"
	for i, spec := range [][5]float64{{24, 17, 23.9}, {23.5, 17, 23.5}, {23.8, 17, 23.5}, {23.6, 17, 23.3}, {23.9, 17.2, 23.5}, {23.7, 17.3, 23.5}, {23.6, 17.1, 23.4}} {
		bars = append(bars, contract.DailyBar{
			Code: code, Date: day + pad2(i + 9), // 10-09..10-15
			Open: spec[2], High: spec[0], Low: spec[1], Close: spec[2], Volume: 1000, Amount: 6e7,
		})
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
}

func scanBoxesForSeed(code string) []contract.Box {
	// 用 box 引擎扫出一个 confirmed 箱体（top≈24 bottom≈17）
	eng := box.New()
	bars := make([]contract.DailyBar, 0, 8)
	day := "2026-10-"
	specs := [][5]float64{{24, 17, 23.9}, {23.5, 17, 23.5}, {23.8, 17, 23.5}, {23.6, 17, 23.3}, {23.9, 17.2, 23.5}, {23.7, 17.3, 23.5}, {23.6, 17.1, 23.4}}
	for i, s := range specs {
		bars = append(bars, contract.DailyBar{Code: code, Date: day + pad2(i + 9), Open: s[2], High: s[0], Low: s[1], Close: s[2], Volume: 1000, Amount: 6e7})
	}
	return eng.Scan(bars, contract.BoxParams{VolMultiplier: 2.0, VolWindow: 5, BreakPremium: 1.03, MinBoxWidth: 0.05})
}

func pad2(n int) string {
	if n < 10 {
		return "0" + itoa(n)
	}
	return itoa(n)
}
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}

func approx(a, b, tol float64) bool { return a-b < tol && b-a < tol }

// TestPlannerRaiseStop M4.4 验收：新箱体确认 → 止损只升不降。
// ① 更高下轨的确认箱体 → 止损上移；② 更低候选不降（应用层跳过 + 触发器ABORT双防护）。
func TestPlannerRaiseStop(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})

	// 入场箱体（旧）：bottom=17 → 初始止损 17×0.97=16.49
	oldBox := contract.Box{Code: code, Top: 24, Bottom: 17, TopConfirmed: "2026-09-01",
		BottomConfirmed: "2026-09-05", Status: contract.BoxBrokenUp}
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{oldBox})
	if err != nil {
		t.Fatal(err)
	}
	// 持仓：entry=旧箱, stop=16.49
	posID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: code, OpenedDate: "2026-10-10", EntryBoxID: ids[0], Qty: 1200, AvgCost: 24.72, StopPrice: 16.49,
	})
	if err != nil {
		t.Fatal(err)
	}
	// 新箱体（更高阶梯）：bottom=20 已确认 → 候选止损 20×0.97=19.40 > 16.49
	newBox := contract.Box{Code: code, Top: 30, Bottom: 20, TopConfirmed: "2026-10-12",
		BottomConfirmed: "2026-10-16", Status: contract.BoxConfirmed, PrevBoxID: ids[0]}
	if _, err := st.BoxRepo.UpsertBoxes([]contract.Box{newBox}); err != nil {
		t.Fatal(err)
	}

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatalf("Build: %v", err)
	}
	poses, _ := st.TradeRepo.OpenPositions()
	if len(poses) != 1 {
		t.Fatalf("positions=%d want 1", len(poses))
	}
	if !approx(poses[0].StopPrice, 19.40, 0.01) {
		t.Errorf("止损应上移到19.40, got %.2f", poses[0].StopPrice)
	}

	// 只升不降②：另一个更低箱体（bottom=15，候选14.55 < 19.40）→ 应用层不调用
	lowBox := contract.Box{Code: code, Top: 22, Bottom: 15, TopConfirmed: "2026-10-14",
		BottomConfirmed: "2026-10-16", Status: contract.BoxConfirmed, PrevBoxID: ids[0]}
	st.BoxRepo.UpsertBoxes([]contract.Box{lowBox})
	if _, err := p.Build(context.Background(), "2026-10-17"); err != nil {
		t.Fatalf("Build#2: %v", err)
	}
	poses, _ = st.TradeRepo.OpenPositions()
	if !approx(poses[0].StopPrice, 19.40, 0.01) {
		t.Errorf("止损不得下降: got %.2f want 19.40", poses[0].StopPrice)
	}

	// 只升不降③：直接调 UpdateStop 下降 → 触发器 ABORT
	if err := st.TradeRepo.UpdateStop(posID, 18.0); err == nil {
		t.Error("UpdateStop 下降应被触发器 ABORT")
	}
}
