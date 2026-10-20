package regime

import (
	"fmt"
	"testing"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// newTestStore 本地构造临时 store（避免与 store 包 helper 冲突）。
func newTestStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(t.TempDir() + "/regime_test.db")
	if err != nil {
		t.Fatalf("open test store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	// 测试事件不需要真实 60 根历史；保留默认 20日窗口+3日确认语义即可。
	if _, err := st.DB().Exec(`INSERT INTO param(key,value) VALUES('regime.breadth.min_history','1')
		ON CONFLICT(key) DO UPDATE SET value='1'`); err != nil {
		t.Fatal(err)
	}
	return st
}

// seedIndexCalendar 播种 sh000001 交易日历（ADR-2），返回升序日期。
func seedIndexCalendar(t *testing.T, st *store.Store, end string, n int) []string {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(DefaultIndex, "")})
	d, err := time.Parse("2006-01-02", end)
	if err != nil {
		t.Fatal(err)
	}
	dates := make([]string, 0, n)
	for d = d.AddDate(0, 0, 1); len(dates) < n; d = d.AddDate(0, 0, -1) {
		if d.Weekday() != time.Saturday && d.Weekday() != time.Sunday {
			dates = append([]string{d.Format("2006-01-02")}, dates...)
		}
	}
	bars := make([]contract.DailyBar, 0, n)
	for _, ds := range dates {
		bars = append(bars, contract.DailyBar{
			Code: DefaultIndex, Date: ds, Open: 3000, High: 3010, Low: 2990,
			Close: 3000, Volume: 1000, Amount: 1e8,
		})
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
	return dates
}

// seedEventStock 造一只最小股票：有 first_date（供 BreadthAgg 次新股过滤）。
func seedEventStock(t *testing.T, st *store.Store, code, firstDate string) {
	t.Helper()
	if err := st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(code, "")}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{{
		Code: code, Date: firstDate, Open: 10, High: 10, Low: 10, Close: 10, Volume: 100, Amount: 1e5,
	}}); err != nil {
		t.Fatal(err)
	}
}

// addBreadthEvent 直接向 box 表写入一个 broken_up/broken_down 事件。
func addBreadthEvent(t *testing.T, st *store.Store, code, date string, up bool) {
	t.Helper()
	status := string(contract.BoxBrokenDown)
	if up {
		status = string(contract.BoxBrokenUp)
	}
	_, err := st.DB().Exec(`INSERT INTO box(code, top, bottom, status, broken_date)
		VALUES (?, 12, 10, ?, ?)`, code, status, date)
	if err != nil {
		t.Fatal(err)
	}
}

// seedEventsOnDays 在指定日期各放 2 个 up/down 事件（ratio 足够清晰）。
func seedEventsOnDays(t *testing.T, st *store.Store, dates []string, up bool) {
	t.Helper()
	for i, d := range dates {
		for j := 0; j < 2; j++ {
			var code string
			if up {
				code = fmt.Sprintf("sh60%04d", i*10+j)
			} else {
				code = fmt.Sprintf("sz00%04d", i*10+j)
			}
			seedEventStock(t, st, code, "2026-01-01")
			addBreadthEvent(t, st, code, d, up)
		}
	}
}

func defaultParams() contract.BoxParams {
	return contract.BoxParams{VolMultiplier: 2.0, VolWindow: 5, BreakPremium: 1.03, MinBoxWidth: 0.05}
}

// TestClassifyDateBull 最后 3 个交易日全为 up 事件 → ratio=1 连续3日 → 牛。
func TestClassifyDateBull(t *testing.T) {
	st := newTestStore(t)
	dates := seedIndexCalendar(t, st, "2026-03-06", 25) // 跨月：2月→3月
	seedEventsOnDays(t, st, dates[len(dates)-3:], true)
	got := ClassifyDate(st, dates[len(dates)-1], defaultParams(), []string{"sh000001"})
	if got != Bull {
		t.Fatalf("末3日全 up 应判 bull, got %s", got)
	}
}

// TestClassifyDateBear 最后 3 个交易日全为 down 事件 → ratio=0 连续3日 → 熊（仅细分日志）。
func TestClassifyDateBear(t *testing.T) {
	st := newTestStore(t)
	dates := seedIndexCalendar(t, st, "2026-03-06", 25)
	seedEventsOnDays(t, st, dates[len(dates)-3:], false)
	got := ClassifyDate(st, dates[len(dates)-1], defaultParams(), nil)
	if got != Bear {
		t.Fatalf("末3日全 down 应判 bear, got %s", got)
	}
}

// TestClassifyDateThreeDayConfirm 只有最后 2 日 up，第 3 日不满足 → 不判牛（3日确认过滤脉冲）。
func TestClassifyDateThreeDayConfirm(t *testing.T) {
	st := newTestStore(t)
	dates := seedIndexCalendar(t, st, "2026-03-06", 25)
	seedEventsOnDays(t, st, dates[len(dates)-2:], true)
	got := ClassifyDate(st, dates[len(dates)-1], defaultParams(), nil)
	if got == Bull {
		t.Fatalf("仅2日 up 不应判 bull, got %s", got)
	}
}

// TestClassifyDateIgnoresLegacyParams 签名向后兼容：params/indices 不参与宽度法。
func TestClassifyDateIgnoresLegacyParams(t *testing.T) {
	st := newTestStore(t)
	dates := seedIndexCalendar(t, st, "2026-03-06", 25)
	seedEventsOnDays(t, st, dates[len(dates)-3:], true)
	got := ClassifyDate(st, dates[len(dates)-1], contract.BoxParams{}, []string{"sz399006"})
	if got != Bull {
		t.Fatalf("M8.8 应忽略旧指数参数仍判 bull, got %s", got)
	}
}

// TestClassifyDateNoData 无日历/事件 → 猴（中性）。
func TestClassifyDateNoData(t *testing.T) {
	st := newTestStore(t)
	got := ClassifyDate(st, "2026-03-06", defaultParams(), nil)
	if got != Range {
		t.Fatalf("无数据应判 range, got %s", got)
	}
}
