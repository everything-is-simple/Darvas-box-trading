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

// TestPlannerBreakoutUnitQtyBear A M1：非升势（震荡/无历史）日首买 1 单位股数
// （默认权益100万、pos.unit=2%）。
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
	if basis, ok := o.Reason["sizing_basis"].(string); !ok || basis != "default" {
		t.Errorf("reason.sizing_basis=%v want default（非升势→1单位）", o.Reason["sizing_basis"])
	}
	if bucket, ok := o.Reason["budget_bucket"].(string); !ok || bucket != "normal" {
		t.Errorf("reason.budget_bucket=%v want normal", o.Reason["budget_bucket"])
	}
}

// TestPlannerBreakoutUnitQtyBull A M1（2026-10-23）：A 判据升势日首买 2 单位股数。
func TestPlannerBreakoutUnitQtyBull(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	seedARegimeUp(t, st, "2026-10-16") // 60 日低点逐级抬升 → A 判据升势
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
	// A M1：升势日首买 2 单位。trigger=24×1.03=24.72；1单位=800股 → 2单位=1600股。
	if o.Qty != 1600 {
		t.Errorf("qty=%d want 1600（A M1：升势日首买2单位）", o.Qty)
	}
	if units, ok := o.Reason["units"].(float64); !ok || units != 2 {
		t.Errorf("reason.units=%v want 2（升势日2单位）", o.Reason["units"])
	}
	if basis, ok := o.Reason["sizing_basis"].(string); !ok || basis != "A_regime_up" {
		t.Errorf("reason.sizing_basis=%v want A_regime_up", o.Reason["sizing_basis"])
	}
	if r, ok := o.Reason["regime"].(string); !ok || r != "bull" {
		t.Errorf("reason.regime=%v want bull（A 判据升势）", o.Reason["regime"])
	}
}

// TestPlannerNoPyramidOnHolding 0017（2026-10-22）：已持仓股再突破 → 一律不加码，无买单。
func TestPlannerNoPyramidOnHolding(t *testing.T) {
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
	// 已持仓（holding）
	posID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: code, OpenedDate: "2026-10-10", EntryBoxID: ids[0], Qty: 600, AvgCost: 25, StopPrice: 20,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := st.TradeRepo.AddLeg(posID, 1, "2026-10-10", 600, 25, 20, 0); err != nil {
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
			t.Fatalf("0017 已持仓一律不加码，不应有买单: %+v", o)
		}
	}
}

// TestPlannerNoWinnerBoost D1+A M1（2026-10-23）：winner 池不再 5x 加仓——
// 即使 A 判据升势日，winner 也只首买 2 单位（与普通池同档），budget_bucket 退为 normal。
// track_level 仍记录为 winner（审计字段保留），但不驱动 units。
func TestPlannerNoWinnerBoost(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	// 标记为赢家池（D1 前 winner 首买 5 单位；A M1 后 winner 不再放大）
	if err := st.SecRepo.SetTrackLevel(code, contract.TrackWinner, ""); err != nil {
		t.Fatal(err)
	}
	seedARegimeUp(t, st, "2026-10-16")
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
	// winner 池 A M1 升势日：首买 2 单位（不是 5）= 1600 股。
	if o.Qty != 1600 {
		t.Errorf("qty=%d want 1600（D1：winner 不再 5x，A M1 升势 2 单位）", o.Qty)
	}
	if units, ok := o.Reason["units"].(float64); !ok || units != 2 {
		t.Errorf("reason.units=%v want 2（winner 不再 5x）", o.Reason["units"])
	}
	if basis, ok := o.Reason["sizing_basis"].(string); !ok || basis != "A_regime_up" {
		t.Errorf("reason.sizing_basis=%v want A_regime_up", o.Reason["sizing_basis"])
	}
	if bucket, ok := o.Reason["budget_bucket"].(string); !ok || bucket != "normal" {
		t.Errorf("reason.budget_bucket=%v want normal（winner 档退役）", o.Reason["budget_bucket"])
	}
	if lv, ok := o.Reason["track_level"].(string); !ok || lv != "winner" {
		t.Errorf("reason.track_level=%v want winner（审计字段保留）", o.Reason["track_level"])
	}
}

// TestPlannerMaxHoldCap 0017：同时持仓达上限 pool.max_hold=7 → 不再开新仓。
func TestPlannerMaxHoldCap(t *testing.T) {
	st := newPlannerStore(t)
	// 已持有 7 只（达上限）
	for i := 1; i <= 7; i++ {
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
		if o.Side == "buy" && o.Code == code {
			t.Fatalf("持仓已达上限 7 只，不应再开新仓: %+v", o)
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

// TestPlannerInitialStopOrder ST1（2026-10-21）：持仓且当日无卖出信号 → 挂常驻初始止损单。
// 修复前 pos.StopPrice 只写不执行（sellOrder 只看箱体下轨），首腿紧止损形同虚设。
// 修复后：持仓期每日生成 trigger=pos.StopPrice 的 initial_stop 卖单。
func TestPlannerInitialStopOrder(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	boxes := scanBoxesForSeed(code)
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	// 持仓：stop_price=16.49（初始止损位），当日无任何卖出信号
	st.DB().Exec(`INSERT INTO position(code, opened_date, qty, avg_cost, stop_price, status)
		VALUES (?, '2026-10-10', 500, 20, 16.49, 'open')`, code)
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	_, orders, _ := st.TradeRepo.PlanOn("2026-10-16")
	// 应有一张 initial_stop 卖单
	found := false
	for _, o := range orders {
		if o.Side == "sell" && o.Reason != nil {
			if sig, _ := o.Reason["signal"].(string); sig == "initial_stop" {
				found = true
				if o.TriggerPrice != 16.49 {
					t.Errorf("initial_stop trigger=%v want 16.49（=pos.StopPrice）", o.TriggerPrice)
				}
				if o.Qty != 500 {
					t.Errorf("initial_stop qty=%d want 500", o.Qty)
				}
			}
		}
	}
	if !found {
		t.Fatalf("ST1：持仓且无卖出信号时应生成 initial_stop 卖单，实际订单 %d 笔", len(orders))
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

// TestPlannerRejectStaleBoxOrder P0-2（2026-10-21）：陈旧箱体致 trigger 严重偏离现价 → 拒单。
// 场景（sh600825 型）：箱顶 5.10（久远确认），现价已 15.58，trigger=5.253 偏离 +196%，
// 远超 max_entry_gap=1.05，必须拦截（否则以 5.253 挂单追高买入）。
func TestPlannerRejectStaleBoxOrder(t *testing.T) {
	st := newPlannerStore(t)
	code := "sh600519"
	seedBars(t, st, code)
	// 陈旧箱体：箱顶 5.10，但信号现价 15.58（远高于箱顶）
	boxes := []contract.Box{{Code: code, Top: 5.10, Bottom: 4.58, Status: contract.BoxBrokenUp, BrokenDate: "2026-10-16"}}
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	st.PoolRepo.UpsertWatch(contract.Watch{Code: code, AddedDate: "2026-10-08", Stage: contract.StageWatching})
	st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-16", Code: code, Type: contract.SignalBreakout,
		BoxID: boxes[0].ID, ClosePx: 15.58, BoxLevel: 5.10,
	}})
	st.TradeRepo.SetEquityDefault("2026-10-16", 1000000)

	p := New(st)
	if _, err := p.Build(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	_, orders, _ := st.TradeRepo.PlanOn("2026-10-16")
	if len(orders) != 0 {
		t.Fatalf("陈旧箱体 trigger 偏离现价 +196%% 应拒单，实际生成 %d 笔订单", len(orders))
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

// TestNewHighLadderTierFor E17 分档边界：0-5/5-10/10-20/20-40，>40 拒绝。
func TestNewHighLadderTierFor(t *testing.T) {
	ladder := newHighLadder{cutoff: 40, units: [4]float64{3, 2, 1, 0.5}}
	cases := []struct {
		distance float64
		tier     int
		units    float64
		ok       bool
	}{
		{0, 1, 3, true},
		{4.99, 1, 3, true},
		{5, 2, 2, true},
		{9.99, 2, 2, true},
		{10, 3, 1, true},
		{19.99, 3, 1, true},
		{20, 4, 0.5, true},
		{40, 4, 0.5, true},
		{40.01, 0, 0, false},
	}
	for _, tc := range cases {
		tier, units, ok := ladder.tierFor(tc.distance)
		if tier != tc.tier || units != tc.units || ok != tc.ok {
			t.Errorf("distance=%.2f: tier=%d units=%v ok=%v, want %d/%v/%v",
				tc.distance, tier, units, ok, tc.tier, tc.units, tc.ok)
		}
	}
}

// TestLotQty E17 半单位仍按整手向下取整；整数单位保持原行为。
func TestLotQty(t *testing.T) {
	cases := []struct {
		shares int
		units  float64
		want   int
	}{
		{800, 1, 800},
		{800, 2, 1600},
		{800, 0.5, 400},
		{100, 0.5, 0},
	}
	for _, tc := range cases {
		if got := lotQty(tc.shares, tc.units); got != tc.want {
			t.Errorf("lotQty(%d, %v)=%d want %d", tc.shares, tc.units, got, tc.want)
		}
	}
}

// TestPlannerNewHighLadderNearTier E17：距新高 <5% → 默认参数 3 单位。
func TestPlannerNewHighLadderNearTier(t *testing.T) {
	st := newPlannerStore(t)
	enableNewHighLadder(t, st)
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
		t.Fatal(err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 1 {
		t.Fatalf("orders=%d want 1", len(orders))
	}
	o := orders[0]
	if o.Qty != 2400 {
		t.Errorf("qty=%d want 2400（3 单位 × 800 股）", o.Qty)
	}
	if units, ok := o.Reason["units"].(float64); !ok || units != 3 {
		t.Errorf("reason.units=%v want 3", o.Reason["units"])
	}
	if basis, ok := o.Reason["sizing_basis"].(string); !ok || basis != "newhigh_t1" {
		t.Errorf("reason.sizing_basis=%v want newhigh_t1", o.Reason["sizing_basis"])
	}
}

// TestPlannerNewHighLadderRejectFar E17：距52周新高 >40% → 不生成买单。
func TestPlannerNewHighLadderRejectFar(t *testing.T) {
	st := newPlannerStore(t)
	enableNewHighLadder(t, st)
	code := "sh600519"
	seedBars(t, st, code)
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{{
		Code: code, Date: "2026-10-01", Open: 50, High: 50, Low: 50, Close: 50,
		Volume: 1000, Amount: 6e7,
	}}); err != nil {
		t.Fatal(err)
	}
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
		t.Fatal(err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 0 {
		t.Fatalf("距52周新高50%% > 40%% 应拒买，实际订单 %d 笔", len(orders))
	}
}

// ─── 辅助 ──

func enableNewHighLadder(t *testing.T, st *store.Store) {
	t.Helper()
	if _, err := st.DB().Exec(`INSERT INTO param(key,value) VALUES('pos.newhigh_ladder','on')
		ON CONFLICT(key) DO UPDATE SET value='on'`); err != nil {
		t.Fatal(err)
	}
}

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

// seedARegimeUp 造 A 判据升势：播 61 根 sh000001 日线，low 逐日抬升 0.3%（>0.2% 升势阈值），
// 使 cur=min(末60根) > prev=min(前60根) × 1.002 → 升势(Bull)。末根日期 = planDate。
// 对应 regime.ClassifyDateA（60 日低点相邻日比），移植自 scripts/regime_sizing_backtest.py。
func seedARegimeUp(t *testing.T, st *store.Store, planDate string) {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sh000001", "")})
	d, err := time.Parse("2006-01-02", planDate)
	if err != nil {
		t.Fatal(err)
	}
	dates := make([]string, 0, 61)
	for cur := d; len(dates) < 61; cur = cur.AddDate(0, 0, -1) {
		if cur.Weekday() != time.Saturday && cur.Weekday() != time.Sunday {
			dates = append([]string{cur.Format("2006-01-02")}, dates...)
		}
	}
	bars := make([]contract.DailyBar, 0, 61)
	lo := 1000.0
	for _, ds := range dates {
		bars = append(bars, contract.DailyBar{
			Code: "sh000001", Date: ds, Open: lo + 10, High: lo + 15, Low: lo, Close: lo + 10,
			Volume: 1000, Amount: 1e8,
		})
		lo *= 1.003 // 严格递增 0.3%/日 > 0.2% 升势阈值
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
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

// --- P0-3 B 类 MA60 闸门（gate.ma60.mode = off|half|stop）---

// seedIndexDecline 播种 61 根逐日 −0.3% 的 sh000001 日线 → planDate 收盘 < MA60（A 判据降势）。
func seedIndexDecline(t *testing.T, st *store.Store, planDate string) {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sh000001", "")})
	d, err := time.Parse("2006-01-02", planDate)
	if err != nil {
		t.Fatal(err)
	}
	dates := make([]string, 0, 61)
	for cur := d; len(dates) < 61; cur = cur.AddDate(0, 0, -1) {
		if cur.Weekday() != time.Saturday && cur.Weekday() != time.Sunday {
			dates = append([]string{cur.Format("2006-01-02")}, dates...)
		}
	}
	bars := make([]contract.DailyBar, 0, 61)
	lo := 1000.0
	for _, ds := range dates {
		bars = append(bars, contract.DailyBar{
			Code: "sh000001", Date: ds, Open: lo + 10, High: lo + 15, Low: lo, Close: lo + 10,
			Volume: 1000, Amount: 1e8,
		})
		lo *= 0.997 // 严格递减 0.3%/日 → 收盘 < MA60
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
}

func setGateMode(t *testing.T, st *store.Store, mode string) {
	t.Helper()
	if _, err := st.DB().Exec(
		`INSERT INTO param(key,value) VALUES('gate.ma60.mode',?) ON CONFLICT(key) DO UPDATE SET value=?`,
		mode, mode); err != nil {
		t.Fatal(err)
	}
}

// gateTestSetup 造标准首买场景（与 TestPlannerBreakoutUnitQtyBear 同构），返回 store。
func gateTestSetup(t *testing.T) *store.Store {
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
	return st
}

// TestGateMA60OffNoReasonField：默认 off 不写入 gate_ma60（保与历史基线逐单等价）。
func TestGateMA60OffNoReasonField(t *testing.T) {
	st := gateTestSetup(t)
	seedIndexDecline(t, st, "2026-10-16") // 即使指数在跌，off 也不得生效
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
	if _, ok := orders[0].Reason["gate_ma60"]; ok {
		t.Errorf("off 模式不应写 gate_ma60，got %v", orders[0].Reason["gate_ma60"])
	}
	if units, _ := orders[0].Reason["units"].(float64); units != 1 {
		t.Errorf("off 模式 units=%v want 1（A判据降势→1单位，闸门不生效）", units)
	}
}

// TestGateMA60Half：指数在 MA60 下方 → 首买降为 0.5 单位并留审计字段。
func TestGateMA60Half(t *testing.T) {
	st := gateTestSetup(t)
	seedIndexDecline(t, st, "2026-10-16")
	setGateMode(t, st, "half")
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
	if units, _ := o.Reason["units"].(float64); units != 0.5 {
		t.Errorf("units=%v want 0.5（half 闸门）", units)
	}
	if basis, _ := o.Reason["sizing_basis"].(string); basis != "default|ma60_half" {
		t.Errorf("sizing_basis=%v want default|ma60_half", basis)
	}
	if o.Qty != 400 { // 800 股/单位 × 0.5 = 400
		t.Errorf("qty=%d want 400", o.Qty)
	}
	g, ok := o.Reason["gate_ma60"].(map[string]any)
	if !ok {
		t.Fatalf("reason.gate_ma60 缺失: %v", o.Reason)
	}
	if below, _ := g["below"].(bool); !below {
		t.Errorf("gate_ma60.below=%v want true（下跌指数）", g["below"])
	}
}

// TestGateMA60Stop：指数在 MA60 下方 → 不生成买单。
func TestGateMA60Stop(t *testing.T) {
	st := gateTestSetup(t)
	seedIndexDecline(t, st, "2026-10-16")
	setGateMode(t, st, "stop")
	p := New(st)
	res, err := p.Build(context.Background(), "2026-10-16")
	if err != nil || res.Status != "ok" {
		t.Fatalf("Build: res=%+v err=%v", res, err)
	}
	_, orders, err := st.TradeRepo.PlanOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 0 {
		t.Fatalf("orders=%d want 0（stop 闸门拦截下方首买）", len(orders))
	}
}

// TestGateMA60StopRisingNotTriggered：指数在 MA60 上方 → 闸门不触发，正常生成买单。
func TestGateMA60StopRisingNotTriggered(t *testing.T) {
	st := gateTestSetup(t)
	seedARegimeUp(t, st, "2026-10-16") // 上涨指数 → 收盘 > MA60
	setGateMode(t, st, "stop")
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
		t.Fatalf("orders=%d want 1（上方不拦截）", len(orders))
	}
	g, ok := orders[0].Reason["gate_ma60"].(map[string]any)
	if !ok {
		t.Fatalf("reason.gate_ma60 缺失")
	}
	if below, _ := g["below"].(bool); below {
		t.Errorf("gate_ma60.below=%v want false（上涨指数）", g["below"])
	}
	if units, _ := orders[0].Reason["units"].(float64); units != 2 {
		t.Errorf("units=%v want 2（升势日 A M1 2 单位，闸门不触发）", units)
	}
}
