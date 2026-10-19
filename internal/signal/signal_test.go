package signal

import (
	"context"
	"testing"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// newTestStore 建临时库（signal 包测试不依赖 store 内部 helper，等价搭建）。
func newTestStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(t.TempDir() + "/test.db")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

// seed 搭建单股场景：证券→K线→箱体→跟踪池。
func seed(t *testing.T, st *store.Store, code string, box contract.Box,
	bars []contract.DailyBar, stage contract.WatchStage, boxCount int) {
	t.Helper()
	if err := st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")}); err != nil {
		t.Fatalf("seed sec: %v", err)
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatalf("seed bars: %v", err)
	}
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{box})
	if err != nil {
		t.Fatalf("seed box: %v", err)
	}
	w := contract.Watch{Code: code, AddedDate: "2026-10-12", Stage: stage, BoxCount: boxCount}
	if len(ids) > 0 {
		w.CurrentBoxID = ids[0]
	}
	if err := st.PoolRepo.UpsertWatch(w); err != nil {
		t.Fatalf("seed watch: %v", err)
	}
}

// mkBar 收盘价K线（信号判定只消费 close）。
func mkBar(code, date string, close float64) contract.DailyBar {
	return contract.DailyBar{Code: code, Date: date, Open: close, High: close, Low: close, Close: close}
}

// TestStopFiresOnlyOnFirstBreak stop 首次跌破语义：跌破当日发一次，次日仍破不重复。
func TestStopFiresOnlyOnFirstBreak(t *testing.T) {
	st := newTestStore(t)
	code := "sh600001"
	box := contract.Box{Code: code, Top: 10, Bottom: 8, TopConfirmed: "2026-10-10", Status: contract.BoxConfirmed}
	bars := []contract.DailyBar{
		mkBar(code, "2026-10-13", 9.6), // 昨：未破
		mkBar(code, "2026-10-14", 7.9), // 今：首次跌破
		mkBar(code, "2026-10-15", 7.8), // 次：仍破（不得重复发）
	}
	seed(t, st, code, box, bars, contract.StageWatching, 0)
	eng := New(st)

	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate 10-14: %v", err)
	}
	sigs, err := st.SignalRepo.SignalsOn("2026-10-14")
	if err != nil || len(sigs) != 1 || sigs[0].Type != contract.SignalStop {
		t.Fatalf("10-14 期望1条stop, got %v (err=%v)", sigs, err)
	}
	if _, err := eng.Evaluate(context.Background(), "2026-10-15"); err != nil {
		t.Fatalf("evaluate 10-15: %v", err)
	}
	if n := len(mustSigs(t, st, "2026-10-15")); n != 0 {
		t.Fatalf("10-15 期望0条（已破不重复）, got %d", n)
	}
}

// TestBreakoutFiresOnlyOnBrokenDate breakout 当日一次语义：BrokenDate==当日才发；
// 状态残留 broken_up 但突破日已过 → 不发（M7.1 回放 bug：sh600052 连发4天的场景）。
func TestBreakoutFiresOnlyOnBrokenDate(t *testing.T) {
	st := newTestStore(t)
	code := "sh600002"
	box := contract.Box{Code: code, Top: 10, Bottom: 8, TopConfirmed: "2026-10-10",
		Status: contract.BoxBrokenUp, BrokenDate: "2026-10-14"}
	bars := []contract.DailyBar{
		mkBar(code, "2026-10-13", 9.5),
		mkBar(code, "2026-10-14", 10.5), // 突破日
		mkBar(code, "2026-10-15", 10.6), // 状态仍 broken_up
	}
	seed(t, st, code, box, bars, contract.StageWatching, 0)
	eng := New(st)

	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate 10-14: %v", err)
	}
	sigs := mustSigs(t, st, "2026-10-14")
	if len(sigs) != 1 || sigs[0].Type != contract.SignalBreakout {
		t.Fatalf("10-14 期望1条breakout, got %v", sigs)
	}
	if _, err := eng.Evaluate(context.Background(), "2026-10-15"); err != nil {
		t.Fatalf("evaluate 10-15: %v", err)
	}
	if n := len(mustSigs(t, st, "2026-10-15")); n != 0 {
		t.Fatalf("10-15 期望0条（突破日已过）, got %d", n)
	}
}

// TestPyramidOnlyOnBrokenDate holding 股新箱体突破 → pyramid（同样当日一次）。
// docs/07 #2 修复后：金字塔钳制改用 position_leg 真实腿数替代 watch.BoxCount。
func TestPyramidOnlyOnBrokenDate(t *testing.T) {
	st := newTestStore(t)
	code := "sh600003"
	box := contract.Box{Code: code, Top: 20, Bottom: 18, TopConfirmed: "2026-10-10",
		Status: contract.BoxBrokenUp, BrokenDate: "2026-10-14"}
	bars := []contract.DailyBar{
		mkBar(code, "2026-10-13", 19.5),
		mkBar(code, "2026-10-14", 20.8),
	}
	// 持仓已1腿（模拟leg1=leg_no=1）：先建仓 + 腿记录
	seed(t, st, code, box, bars, contract.StageHolding, 0)
	posID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: code, OpenedDate: "2026-10-13", EntryBoxID: 0, Qty: 100, AvgCost: 19.5, StopPrice: 17.5,
	})
	if err != nil {
		t.Fatalf("create position: %v", err)
	}
	if err := st.TradeRepo.AddLeg(posID, 1, "2026-10-13", 100, 19.5, 17.5, 0); err != nil {
		t.Fatalf("add leg: %v", err)
	}
	eng := New(st)

	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	sigs := mustSigs(t, st, "2026-10-14")
	if len(sigs) != 1 || sigs[0].Type != contract.SignalPyramid {
		t.Fatalf("期望1条pyramid, got %v", sigs)
	}
}

// TestPyramidMaxLegsReached 持仓已达最大腿数（maxlegs=3）→ 不再发 pyramid
// 信号（即 watch.BoxCount 与金字塔钳制解耦后的回归测试，docs/07 #2）。
func TestPyramidMaxLegsReached(t *testing.T) {
	st := newTestStore(t)
	code := "sh600004"
	box := contract.Box{Code: code, Top: 20, Bottom: 18, TopConfirmed: "2026-10-10",
		Status: contract.BoxBrokenUp, BrokenDate: "2026-10-14"}
	bars := []contract.DailyBar{
		mkBar(code, "2026-10-13", 19.5),
		mkBar(code, "2026-10-14", 20.8),
	}
	seed(t, st, code, box, bars, contract.StageHolding, 0)
	posID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: code, OpenedDate: "2026-10-03", EntryBoxID: 0, Qty: 300, AvgCost: 19.0, StopPrice: 17.0,
	})
	if err != nil {
		t.Fatalf("create position: %v", err)
	}
	// 已加码到第3腿，下次突破不应再发 pyramid
	legDates := []string{"2026-10-01", "2026-10-05", "2026-10-08"}
	for i, d := range legDates {
		if err := st.TradeRepo.AddLeg(posID, i+1, d, 100, 19.0, 17.0, 0); err != nil {
			t.Fatalf("add leg %d: %v", i+1, err)
		}
	}
	eng := New(st)

	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	sigs := mustSigs(t, st, "2026-10-14")
	if len(sigs) != 0 {
		t.Fatalf("已3腿应不再发pyramid, got %v", sigs)
	}
}

// TestFailedBreakoutFiresOnce failed 首次跌回语义：3日内首次收盘跌回上轨下发一次；
// 次日仍在上轨下 → 不发。
func TestFailedBreakoutFiresOnce(t *testing.T) {
	st := newTestStore(t)
	code := "sh600004"
	box := contract.Box{Code: code, Top: 10, Bottom: 8, TopConfirmed: "2026-10-10",
		Status: contract.BoxBrokenUp, BrokenDate: "2026-10-13"}
	bars := []contract.DailyBar{
		mkBar(code, "2026-10-13", 10.5), // 突破日
		mkBar(code, "2026-10-14", 9.9),  // 首次跌回
		mkBar(code, "2026-10-15", 9.8),  // 仍在上轨下（不得重复发）
	}
	seed(t, st, code, box, bars, contract.StageWatching, 0)
	eng := New(st)

	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate 10-14: %v", err)
	}
	sigs := mustSigs(t, st, "2026-10-14")
	if len(sigs) != 1 || sigs[0].Type != contract.SignalFailed {
		t.Fatalf("10-14 期望1条failed_breakout, got %v", sigs)
	}
	if _, err := eng.Evaluate(context.Background(), "2026-10-15"); err != nil {
		t.Fatalf("evaluate 10-15: %v", err)
	}
	if n := len(mustSigs(t, st, "2026-10-15")); n != 0 {
		t.Fatalf("10-15 期望0条, got %d", n)
	}
}

// TestSameDayRerunIdempotent 同日重跑幂等：同一天 Evaluate 两次（补跑/调度重试场景）
// signal 表只留一行。
func TestSameDayRerunIdempotent(t *testing.T) {
	st := newTestStore(t)
	code := "sh600005"
	box := contract.Box{Code: code, Top: 10, Bottom: 8, TopConfirmed: "2026-10-10",
		Status: contract.BoxBrokenUp, BrokenDate: "2026-10-14"}
	bars := []contract.DailyBar{
		mkBar(code, "2026-10-13", 9.5),
		mkBar(code, "2026-10-14", 10.5),
	}
	seed(t, st, code, box, bars, contract.StageWatching, 0)
	eng := New(st)

	for i := 0; i < 2; i++ {
		if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
			t.Fatalf("evaluate #%d: %v", i, err)
		}
	}
	if n := len(mustSigs(t, st, "2026-10-14")); n != 1 {
		t.Fatalf("同日跑两次期望仅1条, got %d", n)
	}
}

// mustSigs 查某日信号（失败即 Fatal）。
func mustSigs(t *testing.T, st *store.Store, date string) []contract.Signal {
	t.Helper()
	sigs, err := st.SignalRepo.SignalsOn(date)
	if err != nil {
		t.Fatalf("signals on %s: %v", date, err)
	}
	return sigs
}

// ─── M8.6 第②通道（达瓦斯「未触损补仓」，布鲁斯案例）──────────────────────

// addonSeed 搭建第②通道场景：Confirmed 箱体（无当日 broken_up）+ 持仓首腿。
// box.Bottom 必须低于 today.Close，避免 stop 信号抢先触发。
func addonSeed(t *testing.T, st *store.Store, code string, legPrice, stop float64) int64 {
	t.Helper()
	box := contract.Box{Code: code, Top: 21, Bottom: 18, TopConfirmed: "2026-10-10",
		Status: contract.BoxConfirmed}
	bars := []contract.DailyBar{
		mkBar(code, "2026-10-13", 19.0),
	}
	seed(t, st, code, box, bars, contract.StageHolding, 1)
	posID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: code, OpenedDate: "2026-10-13", Qty: 100, AvgCost: legPrice, StopPrice: stop,
	})
	if err != nil {
		t.Fatalf("create position: %v", err)
	}
	if err := st.TradeRepo.AddLeg(posID, 1, "2026-10-13", 100, legPrice, stop, 0); err != nil {
		t.Fatalf("add leg: %v", err)
	}
	return posID
}

// TestPyramidAddonFires 五条件全过：未触损 + 收盘在首腿价上方 7% 内 → 加码。
func TestPyramidAddonFires(t *testing.T) {
	st := newTestStore(t)
	code := "sh600101"
	addonSeed(t, st, code, 19.0, 17.5)
	// 今日：low=19.3 > 17.5（未触损），close=19.5 ∈ [19.0, 19×1.07=20.33]
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		{Code: code, Date: "2026-10-14", Open: 19.2, High: 19.6, Low: 19.3, Close: 19.5},
	}); err != nil {
		t.Fatalf("seed today bar: %v", err)
	}
	eng := New(st)
	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	sigs := mustSigs(t, st, "2026-10-14")
	if len(sigs) != 1 || sigs[0].Type != contract.SignalPyramid {
		t.Fatalf("期望1条pyramid(addon), got %v", sigs)
	}
	if got, _ := sigs[0].Payload["trigger"].(string); got != "addon" {
		t.Fatalf("trigger=%q want addon", got)
	}
}

// TestPyramidAddonStoppedOut C1：盘中最低触及止损 → 不加码。
func TestPyramidAddonStoppedOut(t *testing.T) {
	st := newTestStore(t)
	code := "sh600102"
	addonSeed(t, st, code, 19.0, 17.5)
	// low=17.0 <= 17.5 → 触损；close 仍高于 box.Bottom(18) 不触发 stop 信号
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		{Code: code, Date: "2026-10-14", Open: 19.2, High: 19.6, Low: 17.0, Close: 19.5},
	}); err != nil {
		t.Fatalf("seed today bar: %v", err)
	}
	eng := New(st)
	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if n := len(mustSigs(t, st, "2026-10-14")); n != 0 {
		t.Fatalf("触及止损不应加码, got %d", n)
	}
}

// TestPyramidAddonTooHigh C3：收盘超过首腿价×1.07 → 不追高。
func TestPyramidAddonTooHigh(t *testing.T) {
	st := newTestStore(t)
	code := "sh600103"
	addonSeed(t, st, code, 19.0, 17.5)
	// close=20.5 > 19×1.07=20.33
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		{Code: code, Date: "2026-10-14", Open: 20.0, High: 20.6, Low: 19.9, Close: 20.5},
	}); err != nil {
		t.Fatalf("seed today bar: %v", err)
	}
	eng := New(st)
	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if n := len(mustSigs(t, st, "2026-10-14")); n != 0 {
		t.Fatalf("超7%%不应加码, got %d", n)
	}
}

// TestPyramidAddonBelowLast C2：收盘低于首腿成交价（摊低成本）→ 不加码。
func TestPyramidAddonBelowLast(t *testing.T) {
	st := newTestStore(t)
	code := "sh600104"
	addonSeed(t, st, code, 19.0, 17.5)
	// close=18.8 < 19.0（仍在 box 内、未触损）
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		{Code: code, Date: "2026-10-14", Open: 19.0, High: 19.1, Low: 18.5, Close: 18.8},
	}); err != nil {
		t.Fatalf("seed today bar: %v", err)
	}
	eng := New(st)
	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if n := len(mustSigs(t, st, "2026-10-14")); n != 0 {
		t.Fatalf("低于首腿价不应加码（不摊低成本）, got %d", n)
	}
}

// TestPyramidAddonOnlyOnce C4：已有 2 条腿（leg1+leg3）→ 第一版不再发第②通道。
func TestPyramidAddonOnlyOnce(t *testing.T) {
	st := newTestStore(t)
	code := "sh600105"
	posID := addonSeed(t, st, code, 19.0, 17.5)
	if err := st.TradeRepo.AddLeg(posID, 3, "2026-10-13", 500, 19.4, 17.5, 0); err != nil {
		t.Fatalf("add leg3: %v", err)
	}
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		{Code: code, Date: "2026-10-14", Open: 19.2, High: 19.6, Low: 19.3, Close: 19.5},
	}); err != nil {
		t.Fatalf("seed today bar: %v", err)
	}
	eng := New(st)
	if _, err := eng.Evaluate(context.Background(), "2026-10-14"); err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if n := len(mustSigs(t, st, "2026-10-14")); n != 0 {
		t.Fatalf("已2腿不应再加码（第一版至多加一次）, got %d", n)
	}
}
