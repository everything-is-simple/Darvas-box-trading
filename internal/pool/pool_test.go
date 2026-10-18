package pool

import (
	"context"
	"strings"
	"testing"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// ─── 辅助 ──

func newPoolStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(t.TempDir() + "/pool.db")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func seedPoolBar(t *testing.T, st *store.Store, code, date string, close float64) {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{{
		Code: code, Date: date, Open: close, High: close, Low: close, Close: close,
		Volume: 1000, Amount: 6e7,
	}}); err != nil {
		t.Fatal(err)
	}
}

func seedCandidate(t *testing.T, st *store.Store, date, code string, score float64) {
	t.Helper()
	if err := st.PoolRepo.AppendCandidates(date, []contract.Candidate{{
		ScreenDate: date, Code: code, Score: score,
		Reasons: map[string]any{"box_pass": true}, FundamentalFlag: "unknown",
	}}); err != nil {
		t.Fatal(err)
	}
}

// TestPoolSuspendResume B2⑤ 条件挂起与激活（"停传报价"：收盘≥激活价自动恢复）。
func TestPoolSuspendResume(t *testing.T) {
	st := newPoolStore(t)
	code := "sh600519"
	seedPoolBar(t, st, code, "2026-10-16", 25.0)
	if err := st.PoolRepo.UpsertWatch(contract.Watch{
		Code: code, AddedDate: "2026-10-10", Stage: contract.StageWatching,
	}); err != nil {
		t.Fatal(err)
	}

	m := New(st)
	// 挂起：激活价 24
	if err := m.Suspend(code, 24.0); err != nil {
		t.Fatalf("Suspend: %v", err)
	}
	w, _ := st.PoolRepo.WatchByCode(code)
	if w.Stage != contract.StageSuspended || w.ResumeTrigger != 24.0 {
		t.Fatalf("挂起后 stage=%s resume=%.2f want suspended/24.00", w.Stage, w.ResumeTrigger)
	}
	// 次日收盘 25 ≥ 24 → 激活回 watching
	seedPoolBar(t, st, code, "2026-10-17", 25.5)
	if _, err := m.Sync(context.Background(), "2026-10-17"); err != nil {
		t.Fatal(err)
	}
	w, _ = st.PoolRepo.WatchByCode(code)
	if w.Stage != contract.StageWatching {
		t.Fatalf("激活后 stage=%s want watching", w.Stage)
	}
	if w.ResumeTrigger != 0 {
		t.Fatalf("激活后 resume_trigger=%.2f 应清零", w.ResumeTrigger)
	}
}

// TestPoolSuspendNotResumed 收盘 < 激活价 → 保持挂起。
func TestPoolSuspendNotResumed(t *testing.T) {
	st := newPoolStore(t)
	code := "sh600519"
	seedPoolBar(t, st, code, "2026-10-16", 23.0)
	st.PoolRepo.UpsertWatch(contract.Watch{Code: code, AddedDate: "2026-10-10", Stage: contract.StageWatching})

	m := New(st)
	m.Suspend(code, 24.0)
	if _, err := m.Sync(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	w, _ := st.PoolRepo.WatchByCode(code)
	if w.Stage != contract.StageSuspended {
		t.Fatalf("收盘23<24 应保持挂起, got %s", w.Stage)
	}
}

// TestPoolTwoLossBlacklist B2③ 两亏拉黑：历史亏损交易≥pool.maxloss → blacklisted，且不再入池。
func TestPoolTwoLossBlacklist(t *testing.T) {
	st := newPoolStore(t)
	code := "sh600519"
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	st.PoolRepo.UpsertWatch(contract.Watch{Code: code, AddedDate: "2026-08-01", Stage: contract.StageWatching})
	// 两次亏损交易（closed, realized_pnl<0）
	for _, d := range []string{"2026-08-15", "2026-09-10"} {
		if _, err := st.DB().Exec(`INSERT INTO position(code, opened_date, qty, avg_cost, stop_price, status,
			closed_date, close_reason, realized_pnl) VALUES (?, ?, 100, 24, 23, 'closed', ?, 'stop', -500)`,
			code, d, d); err != nil {
			t.Fatal(err)
		}
	}
	m := New(st)
	if _, err := m.Sync(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	w, _ := st.PoolRepo.WatchByCode(code)
	if w.Stage != contract.StageBlacklisted {
		t.Fatalf("两亏后 stage=%s want blacklisted（exit_reason=%s）", w.Stage, w.ExitReason)
	}
	// 黑名单不入池：再次成为候选也不进
	seedCandidate(t, st, "2026-10-16", code, 100)
	if _, err := m.Sync(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	w, _ = st.PoolRepo.WatchByCode(code)
	if w.Stage != contract.StageBlacklisted {
		t.Fatalf("黑名单股票不得重新入池, got %s", w.Stage)
	}
}

// TestPoolMaxWatch B2④ 跟踪上限：活跃池满（≥pool.maxwatch）不再入池。
func TestPoolMaxWatch(t *testing.T) {
	st := newPoolStore(t)
	st.ParamRepo.Set("pool.maxwatch", "2")
	for i, c := range []string{"sh600519", "sz000001"} {
		st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(c, "")})
		st.PoolRepo.UpsertWatch(contract.Watch{Code: c, AddedDate: "2026-10-10", Stage: contract.StageWatching})
		_ = i
	}
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sz300411", "")})
	seedCandidate(t, st, "2026-10-16", "sz300411", 100)
	m := New(st)
	if _, err := m.Sync(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	w, _ := st.PoolRepo.WatchByCode("sz300411")
	if w != nil && w.Stage == contract.StageWatching {
		t.Fatal("池满（2≥maxwatch=2）不应再入池 sz300411")
	}
	// 挂起股票不占额度：挂起一个后腾出名额，可再入池
	m.Suspend("sh600519", 999)
	if _, err := m.Sync(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	w, _ = st.PoolRepo.WatchByCode("sz300411")
	if w == nil || w.Stage != contract.StageWatching {
		t.Fatal("挂起腾出额度后 sz300411 应可入池")
	}
}

// TestClosePositionPnl B2③统计基础：清仓时 realized_pnl = 卖出所得−买入支出（含费）。
func TestClosePositionPnl(t *testing.T) {
	st := newPoolStore(t)
	code := "sh600519"
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	posID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: code, OpenedDate: "2026-10-01", Qty: 100, AvgCost: 10, StopPrice: 9.7,
	})
	if err != nil {
		t.Fatal(err)
	}
	// 买 100@10 费5（-1005）；卖 100@11 费5（+1095）→ pnl = +90
	st.DB().Exec(`INSERT INTO trade_fill(code, date, side, price, qty, fee) VALUES
		(?, '2026-10-01', 'buy', 10, 100, 5), (?, '2026-10-20', 'sell', 11, 100, 5)`, code, code)
	if err := st.TradeRepo.ClosePosition(posID, "2026-10-20", "stop"); err != nil {
		t.Fatal(err)
	}
	var pnl float64
	if err := st.DB().QueryRow(`SELECT realized_pnl FROM position WHERE id=?`, posID).Scan(&pnl); err != nil {
		t.Fatal(err)
	}
	if pnl < 89.99 || pnl > 90.01 {
		t.Fatalf("realized_pnl=%.2f want 90.00", pnl)
	}
	if n, _ := st.TradeRepo.LossCountByCode(code); n != 0 {
		t.Fatalf("盈利交易亏损计数=%d want 0", n)
	}
}

// TestPoolReentryNote B3 再纳入提示：一亏未拉黑者再入池 → watch.note 留痕战绩与"先读教训"。
func TestPoolReentryNote(t *testing.T) {
	st := newPoolStore(t)
	code := "sh600519"
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	// 首次入池 → 退出（模拟曾跟踪过）
	st.PoolRepo.UpsertWatch(contract.Watch{Code: code, AddedDate: "2026-08-01", Stage: contract.StageExited})
	// 一笔亏损交易（LossCount=1 < maxloss=2，未拉黑）
	if _, err := st.DB().Exec(`INSERT INTO position(code, opened_date, qty, avg_cost, stop_price, status,
		closed_date, close_reason, realized_pnl) VALUES (?, '2026-08-10', 100, 24, 23, 'closed', '2026-08-20', 'stop', -500)`,
		code); err != nil {
		t.Fatal(err)
	}
	// 再成为候选 → 重新入池，note 应带历史战绩提示
	seedCandidate(t, st, "2026-10-16", code, 100)
	m := New(st)
	if _, err := m.Sync(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	w, _ := st.PoolRepo.WatchByCode(code)
	if w == nil || w.Stage != contract.StageWatching {
		t.Fatalf("一亏未拉黑应可再入池, got %+v", w)
	}
	if !strings.Contains(w.Note, "历史战绩1笔(亏1)") || !strings.Contains(w.Note, "review list") {
		t.Fatalf("再入池 note 应带战绩提示, got %q", w.Note)
	}

	// 对照：从未交易的股票入池 → note 为空（不打扰）
	fresh := "sz000002"
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(fresh, "")})
	seedCandidate(t, st, "2026-10-16", fresh, 100)
	if _, err := m.Sync(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	w2, _ := st.PoolRepo.WatchByCode(fresh)
	if w2 == nil || w2.Note != "" {
		t.Fatalf("新股入池 note 应为空, got %q", w2.Note)
	}
}

// mkPoolBox 测试辅助：构造一个箱体。
func mkPoolBox(id int64, code string, top, bottom float64, status contract.BoxStatus) contract.Box {
	return contract.Box{
		ID: id, Code: code, Top: top, Bottom: bottom,
		TopConfirmed: "2026-10-01", BottomConfirmed: "2026-10-05",
		Status: status, BrokenDate: "",
	}
}

// TestBoxCountIncrementsOnBoxChange docs/07 #2 回归：
// watching 循环每次"换箱"（box.ID != w.CurrentBoxID）即累加 BoxCount，
// 累计到 flatMax(4) 后 → flat_too_long 出池。
func TestBoxCountIncrementsOnBoxChange(t *testing.T) {
	st := newPoolStore(t)
	code := "sh600519"
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})

	// 初始箱体 box1（confirmed）
	ids1, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		mkPoolBox(1, code, 24, 17, contract.BoxConfirmed),
	})
	if err != nil {
		t.Fatal(err)
	}

	// 入池 watching，CurrentBoxID 指向 box1，BoxCount=0
	if err := st.PoolRepo.UpsertWatch(contract.Watch{
		Code: code, AddedDate: "2026-10-10", Stage: contract.StageWatching,
		CurrentBoxID: ids1[0], BoxCount: 0,
	}); err != nil {
		t.Fatal(err)
	}

	mgr := New(st)

	// 换箱 #1（box1→box2）：Sync 后 BoxCount 应=1
	if _, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		mkPoolBox(2, code, 26, 19, contract.BoxConfirmed),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := mgr.Sync(context.Background(), "2026-10-11"); err != nil {
		t.Fatalf("sync 1: %v", err)
	}
	w, _ := st.PoolRepo.WatchByCode(code)
	if w.BoxCount != 1 {
		t.Fatalf("第一次换箱后 BoxCount=%d want 1", w.BoxCount)
	}
	if w.Stage != contract.StageWatching {
		t.Fatalf("第一次换箱后 Stage=%s want watching", w.Stage)
	}

	// 换箱 #2：BoxCount=2
	st.BoxRepo.UpsertBoxes([]contract.Box{mkPoolBox(3, code, 28, 21, contract.BoxConfirmed)})
	mgr.Sync(context.Background(), "2026-10-12")
	w, _ = st.PoolRepo.WatchByCode(code)
	if w.BoxCount != 2 {
		t.Fatalf("第二次换箱后 BoxCount=%d want 2", w.BoxCount)
	}

	// 换箱 #3：BoxCount=3
	st.BoxRepo.UpsertBoxes([]contract.Box{mkPoolBox(4, code, 30, 23, contract.BoxConfirmed)})
	mgr.Sync(context.Background(), "2026-10-13")
	w, _ = st.PoolRepo.WatchByCode(code)
	if w.BoxCount != 3 {
		t.Fatalf("第三次换箱后 BoxCount=%d want 3", w.BoxCount)
	}
	if w.Stage != contract.StageWatching {
		t.Fatalf("第三次换箱后 Stage=%s want watching (flatMax=4 未达)", w.Stage)
	}

	// 换箱 #4：BoxCount=4 → flat_too_long 出池
	st.BoxRepo.UpsertBoxes([]contract.Box{mkPoolBox(5, code, 32, 25, contract.BoxConfirmed)})
	mgr.Sync(context.Background(), "2026-10-14")
	w, _ = st.PoolRepo.WatchByCode(code)
	if w.BoxCount != 4 {
		t.Fatalf("第四次换箱后 BoxCount=%d want 4", w.BoxCount)
	}
	if w.Stage != contract.StageExited {
		t.Fatalf("BoxCount 达 flatMax 后 Stage=%s want exited", w.Stage)
	}
	if w.ExitReason != "flat_too_long" {
		t.Fatalf("ExitReason=%s want flat_too_long", w.ExitReason)
	}
}

// TestBoxCountDoesNotIncrementOnFirstSync docs/07 #2 边界：
// 首次入池（CurrentBoxID=0）→ Sync 时拿到首个 box 不应累加 BoxCount。
func TestBoxCountDoesNotIncrementOnFirstSync(t *testing.T) {
	st := newPoolStore(t)
	code := "sh600520"
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		mkPoolBox(1, code, 24, 17, contract.BoxConfirmed),
	})
	if err != nil {
		t.Fatal(err)
	}

	// 入池但 CurrentBoxID=0（首次入池场景）
	if err := st.PoolRepo.UpsertWatch(contract.Watch{
		Code: code, AddedDate: "2026-10-10", Stage: contract.StageWatching,
		CurrentBoxID: 0, BoxCount: 0,
	}); err != nil {
		t.Fatal(err)
	}

	mgr := New(st)
	if _, err := mgr.Sync(context.Background(), "2026-10-11"); err != nil {
		t.Fatalf("sync: %v", err)
	}
	w, _ := st.PoolRepo.WatchByCode(code)
	if w.BoxCount != 0 {
		t.Fatalf("首次入池 Sync 后 BoxCount=%d want 0（不应累加）", w.BoxCount)
	}
	if w.CurrentBoxID != ids[0] {
		t.Fatalf("首次 Sync 后 CurrentBoxID=%d want %d", w.CurrentBoxID, ids[0])
	}
}
