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

// TestScreenRSExcessBearMarket A1（2026-10-22，docs/21 F7）：RS 改超额收益后，
// 熊市里「抗跌但仍在跌」的股票能拿到 RS 分。场景：20 日窗口上证 -20%（10.0→8.0），
// 抗跌股 -5%（10.0→9.5，超额 +15pp）、跟跌股 -25%（10.0→7.5，超额 -5pp），
// 阈值设 0.10 → 仅抗跌股得分。旧比值法下抗跌股 0.95/0.80=1.19 < 1.2 被筛掉
// （docs/00 §6 注2 的失真描述，正是本修复的目标场景）。
func TestScreenRSExcessBearMarket(t *testing.T) {
	st, err := store.Open(t.TempDir() + "/screen.db")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	resilient, follower := "sh600519", "sz000001"
	// sz000001 这里当普通股票用（深主板）；指数 sh000001 也须落 security 行（daily_bar FK），
	// kind=index 会被 ActiveStockCodes 自然排除，不进候选。
	for _, code := range []string{resilient, follower, "sh000001"} {
		st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	}
	// 21 根 K 线（RSWindow=20 需要 21 根）：线性插值
	mkBars := func(code string, from, to float64) []contract.DailyBar {
		var out []contract.DailyBar
		for d := 1; d <= 21; d++ {
			c := from + (to-from)*float64(d-1)/20.0
			out = append(out, contract.DailyBar{
				Code: code, Date: fmt.Sprintf("2026-10-%02d", d),
				Open: c, High: c * 1.01, Low: c * 0.99, Close: c, Volume: 1000, Amount: 1000000,
			})
		}
		return out
	}
	bars := append(mkBars("sh000001", 10.0, 8.0), // 指数 -20%
		append(mkBars(resilient, 10.0, 9.5), // 抗跌 -5% → 超额 +15pp
			mkBars(follower, 10.0, 7.5)...)...) // 跟跌 -25% → 超额 -5pp
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
	// 两只都有 confirmed 箱体（box_pass 是候选硬门槛，与 RS 无关）
	for _, code := range []string{resilient, follower} {
		if _, err := st.BoxRepo.UpsertBoxes([]contract.Box{
			{Code: code, Top: 11, Bottom: 9, Status: contract.BoxConfirmed},
		}); err != nil {
			t.Fatal(err)
		}
	}
	if err := st.ParamRepo.Set("screen.rs_excess_min", "0.10"); err != nil {
		t.Fatal(err)
	}

	s := New(st)
	if _, err := s.Screen(context.Background(), "2026-10-21"); err != nil {
		t.Fatal(err)
	}
	cands, err := st.PoolRepo.CandidatesOn("2026-10-21")
	if err != nil {
		t.Fatal(err)
	}
	rsByCode := map[string]struct {
		pass bool
		val  float64
	}{}
	for _, c := range cands {
		if v, ok := c.Reasons["rs_pass"].(bool); ok {
			entry := rsByCode[c.Code]
			entry.pass = v
			if f, ok := c.Reasons["rs_value"].(float64); ok {
				entry.val = f
			}
			rsByCode[c.Code] = entry
		}
	}
	if len(rsByCode) != 2 {
		t.Fatalf("候选应有 2 支（均过箱体门槛），实际 %v", rsByCode)
	}
	if !rsByCode[resilient].pass {
		t.Fatalf("熊市抗跌股（-5%% vs 大盘 -20%%，超额 +15pp ≥ 0.10）应得 RS 分，rs_value=%.4f", rsByCode[resilient].val)
	}
	if rsByCode[resilient].val < 0.149 || rsByCode[resilient].val > 0.151 {
		t.Fatalf("抗跌股超额收益=%.4f，应 ≈ 0.15", rsByCode[resilient].val)
	}
	if rsByCode[follower].pass {
		t.Fatalf("跟跌股（-25%% vs 大盘 -20%%，超额 -5pp）不应得 RS 分，rs_value=%.4f", rsByCode[follower].val)
	}
}
