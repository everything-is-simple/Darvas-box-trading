package regime

import (
	"testing"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// seedIndex 向测试库写入某指数的「箱体+突破」序列，使其在 target 日标为指定体制。
// 用与 TestLabelBoxes 完全一致的箱体结构（top=10, bottom=9，8 根，末根 01-08 突破 close>10×1.03 放量=牛）。
// 若 breakDown=true 则构造向下破位（熊）。
func seedIndex(t *testing.T, st *store.Store, code string, breakDown bool) {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")})
	var bars []contract.DailyBar
	if breakDown {
		// 向下破位序列（镜像 TestLabelBoxes）：confirmed 箱体 top=12/bottom=11，
		// 末根 01-08 收盘 10.5 < 下轨 11 → 熊。
		bars = []contract.DailyBar{
			{Code: code, Date: "2026-01-01", Open: 12, High: 12, Low: 11, Close: 12, Volume: 200},
			{Code: code, Date: "2026-01-02", Open: 11.5, High: 11.5, Low: 11, Close: 11.5, Volume: 200},
			{Code: code, Date: "2026-01-03", Open: 11.8, High: 11.8, Low: 11, Close: 11.8, Volume: 200},
			{Code: code, Date: "2026-01-04", Open: 11.7, High: 11.6, Low: 11, Close: 11.7, Volume: 200},
			{Code: code, Date: "2026-01-05", Open: 11.8, High: 11.9, Low: 11.2, Close: 11.8, Volume: 200},
			{Code: code, Date: "2026-01-06", Open: 11.8, High: 11.9, Low: 11.3, Close: 11.8, Volume: 200},
			{Code: code, Date: "2026-01-07", Open: 11.8, High: 11.9, Low: 11.1, Close: 11.8, Volume: 200},
			{Code: code, Date: "2026-01-08", Open: 10.5, High: 11.2, Low: 10.2, Close: 10.5, Volume: 500},
		}
	} else {
		// 向上突破（牛）：与 TestLabelBoxes 完全一致，末根 01-08 突破日。
		bars = []contract.DailyBar{
			{Code: code, Date: "2026-01-01", Open: 9.9, High: 10, Low: 9, Close: 9.9, Volume: 200},
			{Code: code, Date: "2026-01-02", Open: 9.5, High: 9.5, Low: 9, Close: 9.5, Volume: 200},
			{Code: code, Date: "2026-01-03", Open: 9.8, High: 9.8, Low: 9, Close: 9.8, Volume: 200},
			{Code: code, Date: "2026-01-04", Open: 9.7, High: 9.6, Low: 9, Close: 9.7, Volume: 200},
			{Code: code, Date: "2026-01-05", Open: 9.8, High: 9.9, Low: 9.2, Close: 9.8, Volume: 200},
			{Code: code, Date: "2026-01-06", Open: 9.8, High: 9.9, Low: 9.3, Close: 9.8, Volume: 200},
			{Code: code, Date: "2026-01-07", Open: 9.8, High: 9.9, Low: 9.1, Close: 9.8, Volume: 200},
			{Code: code, Date: "2026-01-08", Open: 10.5, High: 10.6, Low: 9.9, Close: 10.5, Volume: 500},
		}
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatalf("seed %s: %v", code, err)
	}
}

func defaultParams() contract.BoxParams {
	return contract.BoxParams{VolMultiplier: 2.0, VolWindow: 5, BreakPremium: 1.03, MinBoxWidth: 0.05}
}

// TestClassifyDateBull 上证指数牛 → 判牛（单一指数晴雨表）。
func TestClassifyDateBull(t *testing.T) {
	st := newTestStore(t)
	seedIndex(t, st, "sh000001", false)
	got := ClassifyDate(st, "2026-01-08", defaultParams(), []string{"sh000001"})
	if got != Bull {
		t.Fatalf("上证指数牛应判 bull, got %s", got)
	}
}

// TestClassifyDateBear 上证指数熊 → 判熊（单一指数晴雨表）。
func TestClassifyDateBear(t *testing.T) {
	st := newTestStore(t)
	seedIndex(t, st, "sh000001", true)
	got := ClassifyDate(st, "2026-01-08", defaultParams(), []string{"sh000001"})
	if got != Bear {
		t.Fatalf("上证指数熊应判 bear, got %s", got)
	}
}

// TestClassifyDateDefaultIndex indices 为空 → 用缺省上证指数（DefaultIndex）。
func TestClassifyDateDefaultIndex(t *testing.T) {
	st := newTestStore(t)
	seedIndex(t, st, "sh000001", false)
	got := ClassifyDate(st, "2026-01-08", defaultParams(), nil)
	if got != Bull {
		t.Fatalf("缺省指数应判 bull, got %s", got)
	}
}

// TestClassifyDateNoData 上证无数据 → 猴（中性）。
func TestClassifyDateNoData(t *testing.T) {
	st := newTestStore(t)
	got := ClassifyDate(st, "2026-01-08", defaultParams(), []string{"sh000001"})
	if got != Range {
		t.Fatalf("上证无数据应判 range, got %s", got)
	}
}

// newTestStore 本地构造临时 store（避免与 store 包 helper 冲突）。
func newTestStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(t.TempDir() + "/regime_test.db")
	if err != nil {
		t.Fatalf("open test store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}
