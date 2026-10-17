package box

import (
	"context"
	"testing"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// tpScan 测试用扫描器参数环境。
func newTestScanner(t *testing.T) (*Scanner, *store.Store) {
	t.Helper()
	st, err := store.Open(t.TempDir() + "/scan.db")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	// store.Open 已跑迁移
	return NewScanner(New(), st), st
}

func seedStock(t *testing.T, st *store.Store, code string, bars []contract.DailyBar) {
	t.Helper()
	if err := st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
}

// flatSeries 构造 n 根平稳 K 线（high 不创新高、量平稳）。
func flatSeries(code string, n int) []contract.DailyBar {
	bars := make([]contract.DailyBar, n)
	for i := range bars {
		bars[i] = contract.DailyBar{Code: code, Date: d(i + 1), Open: 10, High: 10.5, Low: 9.5, Close: 10, Volume: 1000, Amount: 1e4}
	}
	return bars
}

// TestPrefilterNewHigh 今日创 60 日新高 → 需重算。
func TestPrefilterNewHigh(t *testing.T) {
	s, st := newTestScanner(t)
	bars := flatSeries("sh600001", 60)
	bars[59].High = 12.0 // 今日新高
	seedStock(t, st, "sh600001", bars)
	if !s.prefilter(context.Background(), "sh600001", d(60), 2.0) {
		t.Fatal("today new 60-day high should trigger prefilter")
	}
}

// TestPrefilterVolSpike 今日放量 → 需重算。
func TestPrefilterVolSpike(t *testing.T) {
	s, st := newTestScanner(t)
	bars := flatSeries("sh600002", 60)
	bars[59].Volume = 5000 // 5× 均量 1000 ≥ 2×
	seedStock(t, st, "sh600002", bars)
	if !s.prefilter(context.Background(), "sh600002", d(60), 2.0) {
		t.Fatal("volume spike should trigger prefilter")
	}
}

// TestPrefilterNeither 平稳无新高无量 → 跳过。
func TestPrefilterNeither(t *testing.T) {
	s, st := newTestScanner(t)
	bars := flatSeries("sh600003", 60)
	seedStock(t, st, "sh600003", bars)
	if s.prefilter(context.Background(), "sh600003", d(60), 2.0) {
		t.Fatal("flat series should NOT trigger prefilter")
	}
}

// TestPrefilterShortHistory 不足 60 根 → 不创新高路径 false（maxPrev 守卫）。
func TestPrefilterShortHistory(t *testing.T) {
	s, st := newTestScanner(t)
	bars := flatSeries("sh600004", 10)
	seedStock(t, st, "sh600004", bars)
	// 仅 10 根、平稳 → 不触发
	if s.prefilter(context.Background(), "sh600004", d(10), 2.0) {
		t.Fatal("short flat history should not trigger")
	}
}

// TestRescanAllSmoke 全量重算冒烟：3 只股票 → 箱体持久化、可读回。
func TestRescanAllSmoke(t *testing.T) {
	s, st := newTestScanner(t)
	// 股票 A：构造一个 confirmed 箱体（top=24 bottom=17）+ 突破
	seriesA := []contract.DailyBar{
		{Code: "sh600519", Date: d(1), Open: 24, High: 24, Low: 17, Close: 23.9, Volume: 1000, Amount: 1e4},
		{Code: "sh600519", Date: d(2), High: 23.5, Low: 17, Close: 23.5, Volume: 1000, Amount: 1e4},
		{Code: "sh600519", Date: d(3), High: 23.8, Low: 17, Close: 23.5, Volume: 1000, Amount: 1e4},
		{Code: "sh600519", Date: d(4), High: 23.6, Low: 17, Close: 23.3, Volume: 1000, Amount: 1e4},
		{Code: "sh600519", Date: d(5), High: 23.9, Low: 17.2, Close: 23.5, Volume: 1000, Amount: 1e4},
		{Code: "sh600519", Date: d(6), High: 23.7, Low: 17.3, Close: 23.5, Volume: 1000, Amount: 1e4},
		{Code: "sh600519", Date: d(7), High: 23.6, Low: 17.1, Close: 23.4, Volume: 1000, Amount: 1e4},
		{Code: "sh600519", Date: d(8), High: 25.2, Low: 23.9, Close: 25.0, Volume: 5000, Amount: 5e4},
	}
	seedStock(t, st, "sh600519", seriesA)
	seedStock(t, st, "sh600001", flatSeries("sh600001", 5))
	seedStock(t, st, "sh600002", flatSeries("sh600002", 5))

	res, err := s.RescanAll(context.Background(), d(8))
	if err != nil {
		t.Fatalf("RescanAll: %v", err)
	}
	if res.Status != "ok" || res.AffectedRows == 0 {
		t.Fatalf("rescan result %+v want ok/boxes>0", res)
	}
	// 茅台应能读回箱体阶梯
	ladder, err := st.BoxRepo.Ladder("sh600519")
	if err != nil {
		t.Fatal(err)
	}
	if len(ladder) == 0 {
		t.Fatal("maotai ladder empty after rescan")
	}
	// 平稳股不应有 confirmed 箱体（5 根不够 3 日确认+突破）
	ladderFlat, _ := st.BoxRepo.Ladder("sh600001")
	if len(ladderFlat) == 0 {
		t.Fatal("flat stock should still have a forming box")
	}
	if ladderFlat[0].Status != contract.BoxForming {
		t.Fatalf("flat stock box status=%s want forming", ladderFlat[0].Status)
	}
}
