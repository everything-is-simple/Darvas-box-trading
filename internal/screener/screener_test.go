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

// TestScreenBrokenUpBehindForming ST4 回归（2026-10-21）：真实 boxscan 会在 broken_up 后
// 再输出 forming(0,0) 箱体（id 更大）。修复前 LatestBox 取 forming → boxOK=false，
// 「刚 broken_up」分支死掉；修复后 LatestNonFormingBox 跳过 forming，刚突破股能进候选池。
func TestScreenBrokenUpBehindForming(t *testing.T) {
	st, err := store.Open(t.TempDir() + "/screen.db")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	code := "sh600519"
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	var bars []contract.DailyBar
	for d := 1; d <= 8; d++ {
		bars = append(bars, contract.DailyBar{
			Code: code, Date: fmt.Sprintf("2026-10-%02d", d),
			Open: 10, High: 11, Low: 9, Close: 10, Volume: 1000, Amount: 100000,
		})
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
	// broken_up（昨日突破 10-07）+ forming(0,0)（id 更大，模拟真实 boxscan 输出顺序）
	if _, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		{Code: code, Top: 11, Bottom: 9, Status: contract.BoxBrokenUp, BrokenDate: "2026-10-07"},
		{Code: code, Top: 0, Bottom: 0, Status: contract.BoxForming},
	}); err != nil {
		t.Fatal(err)
	}

	s := New(st)
	if _, err := s.Screen(context.Background(), "2026-10-08"); err != nil {
		t.Fatal(err)
	}
	cands, err := st.PoolRepo.CandidatesOn("2026-10-08")
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, c := range cands {
		if c.Code == code {
			found = true
		}
	}
	if !found {
		t.Fatal("ST4 回归：broken_up 后有 forming 箱体时，刚突破股应能进候选池（修复前被 forming 挡住）")
	}
}
