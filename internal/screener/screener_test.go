package screener

import (
	"context"
	"fmt"
	"testing"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// TestScreenTooLateRule B2① 太晚规则（原书：突破后第四天还没买就放弃）：
// broken_up 突破后已过 ≥ screen.latedays 个交易日 → 不入候选；昨日刚突破的仍可入。
func TestScreenTooLateRule(t *testing.T) {
	st, err := store.Open(t.TempDir() + "/screen.db")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	late, fresh := "sh600519", "sz000001"
	for _, code := range []string{late, fresh} {
		st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	}

	// K线 10-01..10-16 连续16根（测试库无日历约束，BarCount 只数根数）
	var bars []contract.DailyBar
	for _, code := range []string{late, fresh} {
		for d := 1; d <= 16; d++ {
			bars = append(bars, contract.DailyBar{
				Code: code, Date: fmt.Sprintf("2026-10-%02d", d),
				Open: 10, High: 10, Low: 10, Close: 10, Volume: 100, Amount: 1000,
			})
		}
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}

	// late：突破日 10-10 → (10-10, 10-16] 共 6 根 ≥ 4 → 太晚不入池
	if _, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		{Code: late, Top: 11, Bottom: 9, Status: contract.BoxBrokenUp, BrokenDate: "2026-10-10"},
	}); err != nil {
		t.Fatal(err)
	}
	// fresh：突破日 10-15 → 1 根 < 4 → 正常入池
	if _, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		{Code: fresh, Top: 11, Bottom: 9, Status: contract.BoxBrokenUp, BrokenDate: "2026-10-15"},
	}); err != nil {
		t.Fatal(err)
	}

	s := New(st)
	if _, err := s.Screen(context.Background(), "2026-10-16"); err != nil {
		t.Fatal(err)
	}
	cands, err := st.PoolRepo.CandidatesOn("2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	found := map[string]bool{}
	for _, c := range cands {
		found[c.Code] = true
	}
	if found[late] {
		t.Fatal("突破后已 6 个交易日（≥ latedays=4）应判太晚，不入候选")
	}
	if !found[fresh] {
		t.Fatal("昨日刚突破（1 个交易日 < 4）应正常入候选")
	}
}
