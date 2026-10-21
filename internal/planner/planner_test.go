package planner

import (
	"context"
	"fmt"
	"testing"
	"time"

	"darvas/internal/box"
	"darvas/internal/contract"
	"darvas/internal/store"
)

// TestPlannerBreakoutUnitQtyBear 熊猴市腿1 = 1 单位股数（默认权益100万、pos.unit=2%）。
func TestPlannerBreakoutUnitQtyBear(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalBreakout,
		BoxID: boxes[0].ID, ClosePx: 25, BoxLevel: 24,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	res, err := p.Build(context.Background(), "2026-10-16")
	if err != nil || res.Status != "ok" {
		t.Fatalf("Build: res=%+v err=%v", res, err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 1 {
		t.Fatalf("orders=%d want 1", len(orders))
	}
	o := orders[0]
	// trigger=24×1.03=24.72；unitValue=20000；1单位股数=floor(20000/24.72/100)*100=800。
	if o.Qty != 800 {
		t.Errorf("qty=%d want 800（1单位股）", o.Qty)
	}
	if o.LegNo != 1 || o.Side != "buy" || o.OrderKind != "stop_buy" {
		t.Errorf("order=%+v want leg1 stop_buy", o)
	}
	if stop, ok := o.Reason["stop"].(float64); !ok || !approx(stop, 23.28, 0.01) {
		t.Errorf("reason.stop=%v want ≈23.28（首腿紧止损=上轨×0.97）", o.Reason["stop"])
	}
	if units, ok := o.Reason["units"].(float64); !ok || units != 1 {
		t.Errorf("reason.units=%v want 1", o.Reason["units"])
	}
	if bucket, ok := o.Reason["budget_bucket"].(string); !ok || bucket != "probe" {
		t.Errorf("reason.budget_bucket=%v want probe", o.Reason["budget_bucket"])
	}
}

// TestPlannerBreakoutUnitQtyBull 牛市腿1 = 2 单位股数，占用试探预算。
func TestPlannerBreakoutUnitQtyBull(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	seedBullRegime(t, st, "2026-10-16")
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalBreakout,
		BoxID: boxes[0].ID, ClosePx: 25, BoxLevel: 24,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatalf("Build: %v", err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 1 {
		t.Fatalf("orders=%d want 1", len(orders))
	}
	o := orders[0]
	if o.Qty != 1600 {
		t.Errorf("qty=%d want 1600（牛市腿1=2单位×800股）", o.Qty)
	}
	if units, ok := o.Reason["units"].(float64); !ok || units != 2 {
		t.Errorf("reason.units=%v want 2", o.Reason["units"])
	}
	if r, ok := o.Reason["regime"].(string); !ok || r != "bull" {
		t.Errorf("reason.regime=%v want bull", o.Reason["regime"])
	}
}

// TestPlannerPyramidSuppressedOutsideBull M8.8 硬规则：熊猴 pyramid 不生成订单。
func TestPlannerPyramidSuppressedOutsideBull(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{{
		Code: code, Top: 30, Bottom: 20, TopConfirmed: "2026-10-10",
		BottomConfirmed: "2026-10-14", Status: contract.BoxConfirmed,
	}})
	if err != nil {
		t.Fatal(err)
	}
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalPyramid,
		BoxID: ids[0], ClosePx: 31, BoxLevel: 30,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatalf("Build: %v", err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	for _, o := range orders {
		if o.Side == "buy" {
			t.Fatalf("熊猴不应生成 pyramid 买单: %+v", o)
		}
	}
}

// TestPlannerBullPyramidLeg2Qty 牛市腿2=3单位；止损仍=新箱体下轨×0.97（B1不改此腿）。
func TestPlannerBullPyramidLeg2Qty(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	seedBullRegime(t, st, "2026-10-16")
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{{
		Code: code, Top: 30, Bottom: 20, TopConfirmed: "2026-10-10",
		BottomConfirmed: "2026-10-14", Status: contract.BoxConfirmed,
	}})
	if err != nil {
		t.Fatal(err)
	}
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalPyramid,
		BoxID: ids[0], ClosePx: 31, BoxLevel: 30,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatalf("Build: %v", err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 1 {
		t.Fatalf("orders=%d want 1", len(orders))
	}
	o := orders[0]
	// trigger=30×1.03=30.90；1单位股数=floor(20000/30.90/100)*100=600；腿2=3单位。
	if o.Qty != 1800 || o.LegNo != 2 {
		t.Errorf("order qty/leg=%d/%d want 1800/2", o.Qty, o.LegNo)
	}
	if stop, ok := o.Reason["stop"].(float64); !ok || !approx(stop, 19.40, 0.01) {
		t.Errorf("reason.stop=%v want ≈19.40（腿2=新箱体下轨×0.97）", o.Reason["stop"])
	}
	if bucket, ok := o.Reason["budget_bucket"].(string); !ok || bucket != "main" {
		t.Errorf("reason.budget_bucket=%v want main", o.Reason["budget_bucket"])
	}
}

// TestPlannerBullPyramidLeg3Qty 正常路径腿1=2单位、腿2=3单位，腿3补到单票10单位=5单位。
func TestPlannerBullPyramidLeg3Qty(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	seedBullRegime(t, st, "2026-10-16")
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		{ID: 1, Code: code, Top: 24, Bottom: 17, TopConfirmed: "2026-10-09",
			BottomConfirmed: "2026-10-13", Status: contract.BoxBrokenUp},
		{ID: 2, Code: code, Top: 26, Bottom: 21, TopConfirmed: "2026-10-14",
			BottomConfirmed: "2026-10-15", Status: contract.BoxConfirmed, PrevBoxID: 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	posID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: code, OpenedDate: "2026-10-10", EntryBoxID: ids[0],
		Qty: 1400, AvgCost: 24, StopPrice: 20,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := st.TradeRepo.AddLeg(posID, 1, "2026-10-10", 1400, 24, 20, 0); err != nil {
		t.Fatal(err)
	}
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalPyramid,
		BoxID: ids[1], ClosePx: 27, BoxLevel: 26,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatalf("Build: %v", err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	buys := 0
	for _, o := range orders {
		if o.Side != "buy" {
			continue
		}
		buys++
		// trigger=26×1.03=26.78；1单位股数=700；current=1400（2单位）→ 补5单位=3500。
		if o.Qty != 3500 || o.LegNo != 3 {
			t.Errorf("buy qty/leg=%d/%d want 3500/3", o.Qty, o.LegNo)
		}
		if units, ok := o.Reason["units"].(float64); !ok || units != 5 {
			t.Errorf("reason.units=%v want 5", o.Reason["units"])
		}
	}
	if buys != 1 {
		t.Fatalf("buy orders=%d want 1", buys)
	}
}

// TestPlannerProbeCapBlocksNewProbe 试探 5 单位用满后，熊猴/普通试探不再开新票。
func TestPlannerProbeCapBlocksNewProbe(t *testing.T) {
	st := newPlannerStore(t)
	// 5 只已有试探仓：各 1 单位（close=25，qty=800，unitValue=20000）。
	for i := 1; i <= 5; i++ {
		code := fmt.Sprintf("sh60000%d", i)
		seedPosition(t, st, code, 800, 25, 1)
	}
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalBreakout,
		BoxID: boxes[0].ID, ClosePx: 25, BoxLevel: 24,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatalf("Build: %v", err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	for _, o := range orders {
		if o.Side == "buy" {
			t.Fatalf("试探预算已满，不应再开新试探单: %+v", o)
		}
	}
}

// TestPlannerMainCapBlocksPyramid 主力 30 单位用满后，牛市也不再加码。
func TestPlannerMainCapBlocksPyramid(t *testing.T) {
	st := newPlannerStore(t)
	seedBullRegime(t, st, "2026-10-16")
	// 已有一只 30 单位主力仓：close=25，qty=24000，leg1+leg2。
	seedPosition(t, st, "sh600000", 24000, 25, 2)

	code := "sh600519"
	seedBars(t, st, code)
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{{
		Code: code, Top: 30, Bottom: 20, TopConfirmed: "2026-10-10",
		BottomConfirmed: "2026-10-14", Status: contract.BoxConfirmed,
	}})
	if err != nil {
		t.Fatal(err)
	}
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalPyramid,
		BoxID: ids[0], ClosePx: 31, BoxLevel: 30,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatalf("Build: %v", err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	for _, o := range orders {
		if o.Side == "buy" {
			t.Fatalf("主力预算已满，不应再加码: %+v", o)
		}
	}
}

// TestPlannerUnitBelowOneLot 高价股 1 单位不足 100 股 → 不硬凑下单。
func TestPlannerUnitBelowOneLot(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{{
		Code: code, Top: 900, Bottom: 800, TopConfirmed: "2026-10-10",
		BottomConfirmed: "2026-10-14", Status: contract.BoxConfirmed,
	}})
	if err != nil {
		t.Fatal(err)
	}
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalBreakout,
		BoxID: ids[0], ClosePx: 950, BoxLevel: 900,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatalf("Build: %v", err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 0 {
		t.Fatalf("orders=%d want 0（1单位不足1手，不硬凑）", len(orders))
	}
}

// TestPlannerSellOrderQty 止损卖单 = 持仓全部（一次全出），与市况无关。
func TestPlannerSellOrderQty(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	boxes[0].Status = contract.BoxBrokenDown
	boxes[0].Bottom = 17
	boxes[0].BrokenDate = "2026-10-16"
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	lb, _ := st.BoxRepo.LatestBox(code)
	if lb == nil {
		t.Fatal("no box after upsert")
	}
	st.DB().Exec(`INSERT INTO position(code, opened_date, qty, avg_cost, stop_price, status)
		VALUES (?, '2026-09-01', 500, 20, 16.49, 'open')`, code)
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalStop,
		BoxID: lb.ID, ClosePx: 16, BoxLevel: 17,
	}})

	p := New(st)
	p.Build(context.Background(), "2026-10-16")
	_, orders, _ := st.TradeRepo.PlanOn("2026-10-16")
	if len(orders) == 0 {
		t.Fatal("expected sell order from stop signal")
	}
	o := orders[0]
	if o.Side != "sell" || o.OrderKind != "stop_sell" || o.Qty != 500 {
		t.Errorf("sell order=%+v want stop_sell qty=500", o)
	}
}

// TestPlannerNoDuplicateSell P0-3（2026-10-21）：同仓位同批次只产一张卖单。
// 持仓既有 stop 信号（跌破箱下轨）又满足 time_stop（持有≥staledays 且收盘≤成本），
// 修复前 pendingSell 未回写会生成两张卖单；修复后仅一张。
func TestPlannerNoDuplicateSell(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	boxes[0].Status = contract.BoxBrokenDown
	boxes[0].Bottom = 17
	boxes[0].BrokenDate = "2026-10-16"
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	lb, _ := st.BoxRepo.LatestNonFormingBox(code)
	if lb == nil {
		t.Fatal("no box after upsert")
	}
	// 持仓：开仓于 2026-09-01（距 10-16 远超 staledays=15），成本 20 > 现价 16 → 同时满足 time_stop
	st.DB().Exec(`INSERT INTO position(code, opened_date, qty, avg_cost, stop_price, status)
		VALUES (?, '2026-09-01', 500, 20, 16.49, 'open')`, code)
	// 收盘价 16（≤成本 20）触发 time_stop；seedBars 已有 10-16 收盘约 23.5，需覆盖为 16
	st.DB().Exec(`UPDATE daily_bar SET close=16 WHERE code=? AND date='2026-10-16'`, code)
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalStop,
		BoxID: lb.ID, ClosePx: 16, BoxLevel: 17,
	}})

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	_, orders, _ := st.TradeRepo.PlanOn("2026-10-16")
	sellCount := 0
	for _, o := range orders {
		if o.Side == "sell" {
			sellCount++
		}
	}
	if sellCount != 1 {
		t.Fatalf("同仓位同批次卖单数=%d，期望 1（stop 与 time_stop 去重）", sellCount)
	}
}

// TestPlannerExecuteDate P1修复验收（2026-10-17）：执行日=下一交易日（ADR-2），非日历+1。
func TestPlannerExecuteDate(t *testing.T) {
	st := newPlannerStore(t)
	seedIndexBars(t, st, "2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-16")
	p := New(st)
	cases := [][2]string{
		{"2026-09-30", "2026-10-08"},
		{"2026-10-09", "2026-10-12"},
		{"2026-10-16", "2026-10-19"},
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

// TestPlannerMinWatchDays G1：入池后 min_watch_days 个交易日内不产生首笔买单。
func TestPlannerMinWatchDays(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	// 入池日期=2026-10-15（距 planDate 10-16 仅 1 个交易日 < min_watch_days=3）
	st.PoolRepo.UpsertWatch(contract.Watch{Code: code, AddedDate: "2026-10-15", Stage: contract.StageWatching})
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalBreakout,
		BoxID: boxes[0].ID, ClosePx: 25, BoxLevel: 24,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 0 {
		t.Fatalf("观察期内应无买单，实际 %d 笔", len(orders))
	}
}

// TestPlannerMinWatchDaysExpired G1：入池已满 min_watch_days 个交易日 → 正常出首买单。
func TestPlannerMinWatchDaysExpired(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	// 入池日期=2026-10-08（距 planDate 10-16 有 10-09/10-12/10-15 ≥3 个交易日）
	st.PoolRepo.UpsertWatch(contract.Watch{Code: code, AddedDate: "2026-10-08", Stage: contract.StageWatching})
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalBreakout,
		BoxID: boxes[0].ID, ClosePx: 25, BoxLevel: 24,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 1 {
		t.Fatalf("观察期已满应出 1 笔买单，实际 %d 笔", len(orders))
	}
}

// TestIsOldFriend G2：IsOldFriend 判定连续 2 笔盈利。
func TestIsOldFriend(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})

	// 造两笔已平仓盈利记录（直接 SQL 插入 position）
	if _, err := st.DB().Exec(`INSERT INTO position(code, opened_date, closed_date, qty, avg_cost, stop_price, status, close_reason, realized_pnl)
		VALUES (?, '2026-01-05','2026-02-05', 100, 10, 9.7, 'closed','stop', 500),
		       (?, '2026-03-05','2026-04-05', 100, 12, 11.6, 'closed','stop', 800)`, code, code); err != nil {
		t.Fatal(err)
	}
	ok, err := st.TradeRepo.IsOldFriend(code)
	if err != nil || !ok {
		t.Fatalf("IsOldFriend=%v err=%v，连续2笔盈利应为 true", ok, err)
	}

	// 追加一笔亏损 → 最近一笔亏损，应为 false
	if _, err := st.DB().Exec(`INSERT INTO position(code, opened_date, closed_date, qty, avg_cost, stop_price, status, close_reason, realized_pnl)
		VALUES (?, '2026-05-05','2026-06-05', 100, 15, 14.5, 'closed','stop', -300)`, code); err != nil {
		t.Fatal(err)
	}
	ok, _ = st.TradeRepo.IsOldFriend(code)
	if ok {
		t.Fatalf("最近一笔亏损后 IsOldFriend 应为 false")
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
	// 单测里事件股只播 1 根 K 线；把次新股过滤降到 1（仍走 BreadthAgg 过滤路径）。
	if _, err := st.DB().Exec(`INSERT INTO param(key,value) VALUES('regime.breadth.min_history','1')
		ON CONFLICT(key) DO UPDATE SET value='1'`); err != nil {
		t.Fatal(err)
	}
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

// seedBullRegime 造 M8.8 牛市：最近 3 个交易日全为 broken_up 事件，ratio=1 连续3日。
func seedBullRegime(t *testing.T, st *store.Store, planDate string) []string {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sh000001", "")})
	d, err := time.Parse("2006-01-02", planDate)
	if err != nil {
		t.Fatal(err)
	}
	dates := make([]string, 0, 25)
	for d = d.AddDate(0, 0, 1); len(dates) < 25; d = d.AddDate(0, 0, -1) {
		if d.Weekday() != time.Saturday && d.Weekday() != time.Sunday {
			dates = append([]string{d.Format("2006-01-02")}, dates...)
		}
	}
	bars := make([]contract.DailyBar, 0, len(dates))
	for _, ds := range dates {
		bars = append(bars, contract.DailyBar{Code: "sh000001", Date: ds, Open: 3000, High: 3010, Low: 2990, Close: 3000, Volume: 1000, Amount: 1e8})
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
	for i, ds := range dates[len(dates)-3:] {
		for j := 0; j < 2; j++ {
			code := fmt.Sprintf("sh60%04d", i*10+j)
			if err := st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")}); err != nil {
				t.Fatal(err)
			}
			if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{{Code: code, Date: "2026-01-01", Open: 10, High: 10, Low: 10, Close: 10, Volume: 100, Amount: 1e5}}); err != nil {
				t.Fatal(err)
			}
			if _, err := st.DB().Exec(`INSERT INTO box(code, top, bottom, status, broken_date)
				VALUES (?, 12, 10, 'broken_up', ?)`, code, ds); err != nil {
				t.Fatal(err)
			}
		}
	}
	return dates
}

// seedPosition 造一个带最新收盘与 nLegs 条腿的持仓，供预算测试。
func seedPosition(t *testing.T, st *store.Store, code string, qty int, close float64, nLegs int) {
	t.Helper()
	if err := st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{{
		Code: code, Date: "2026-10-16", Open: close, High: close, Low: close, Close: close,
		Volume: 100, Amount: 1e5,
	}}); err != nil {
		t.Fatal(err)
	}
	posID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: code, OpenedDate: "2026-10-16", Qty: qty, AvgCost: close, StopPrice: close * 0.9, Status: "open",
	})
	if err != nil {
		t.Fatal(err)
	}
	for leg := 1; leg <= nLegs; leg++ {
		if err := st.TradeRepo.AddLeg(posID, leg, "2026-10-16", qty, close, close*0.9, 0); err != nil {
			t.Fatal(err)
		}
	}
}

func seedBars(t *testing.T, st *store.Store, code string) {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	bars := make([]contract.DailyBar, 0, 8)
	day := "2026-10-"
	for i, spec := range [][5]float64{{24, 17, 23.9}, {23.5, 17, 23.5}, {23.8, 17, 23.5}, {23.6, 17, 23.3}, {23.9, 17.2, 23.5}, {23.7, 17.3, 23.5}, {23.6, 17.1, 23.4}} {
		bars = append(bars, contract.DailyBar{
			Code: code, Date: day + pad2(i+9), // 10-09..10-15
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
		bars = append(bars, contract.DailyBar{Code: code, Date: day + pad2(i+9), Open: s[2], High: s[0], Low: s[1], Close: s[2], Volume: 1000, Amount: 6e7})
	}
	return eng.Scan(bars, contract.BoxParams{VolMultiplier: 2.0, VolWindow: 5, BreakPremium: 1.03, MinBoxWidth: 0.05})
}

func approx(a, b, tol float64) bool {
	d := a - b
	if d < 0 {
		d = -d
	}
	return d <= tol
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
