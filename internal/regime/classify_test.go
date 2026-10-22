package regime

import (
	"fmt"
	"math"
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

// ─── A 判据（60 日低点相邻日比，ClassifyDateA）── A M1 仓位规则依据 ───

// seedIndexLowsA 播种 sh000001 日线，按 gen(i) 生成 low（i=0..n-1，升序日期）。
// 用于 ClassifyDateA 测试：控制 60 日低点序列的走向。返回末根日期。
func seedIndexLowsA(t *testing.T, st *store.Store, end string, n int, gen func(i int) float64) string {
	t.Helper()
	st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity(DefaultIndex, "")})
	d, err := time.Parse("2006-01-02", end)
	if err != nil {
		t.Fatal(err)
	}
	dates := make([]string, 0, n)
	for cur := d; len(dates) < n; cur = cur.AddDate(0, 0, -1) {
		if cur.Weekday() != time.Saturday && cur.Weekday() != time.Sunday {
			dates = append([]string{cur.Format("2006-01-02")}, dates...)
		}
	}
	bars := make([]contract.DailyBar, 0, n)
	for i, ds := range dates {
		lo := gen(i)
		bars = append(bars, contract.DailyBar{
			Code: DefaultIndex, Date: ds, Open: lo + 10, High: lo + 15, Low: lo, Close: lo + 10,
			Volume: 1000, Amount: 1e8,
		})
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
	return dates[len(dates)-1]
}

// TestClassifyDateAUp A 判据升势：61 根 low 逐日抬升 0.3%（>0.2% 阈值）→ Bull。
// cur=min(末60)=bars[1].Low，prev=min(前60)=bars[0].Low；cur>prev×1.002。
func TestClassifyDateAUp(t *testing.T) {
	st := newTestStore(t)
	end := seedIndexLowsA(t, st, "2026-03-06", 61, func(i int) float64 {
		return 1000.0 * math.Pow(1.003, float64(i))
	})
	if got := ClassifyDateA(st, end); got != Bull {
		t.Fatalf("low 逐级抬升应判升势(bull), got %s", got)
	}
}

// TestClassifyDateADown A 判据降势：61 根 low 逐日下移 0.3%（>0.2% 阈值）→ Bear。
// cur=min(末60)=bars[60].Low（最小），prev=min(前60)=bars[59].Low；cur<prev×0.998。
func TestClassifyDateADown(t *testing.T) {
	st := newTestStore(t)
	end := seedIndexLowsA(t, st, "2026-03-06", 61, func(i int) float64 {
		return 1000.0 * math.Pow(0.997, float64(i))
	})
	if got := ClassifyDateA(st, end); got != Bear {
		t.Fatalf("low 逐级下移应判降势(bear), got %s", got)
	}
}

// TestClassifyDateAFlat A 判据震荡：61 根 low 持平 → cur==prev → Range。
func TestClassifyDateAFlat(t *testing.T) {
	st := newTestStore(t)
	end := seedIndexLowsA(t, st, "2026-03-06", 61, func(i int) float64 { return 1000.0 })
	if got := ClassifyDateA(st, end); got != Range {
		t.Fatalf("low 持平应判震荡(range), got %s", got)
	}
}

// TestClassifyDateAInsufficientHistory 历史不足 61 根 → Range（保守不放大仓位）。
func TestClassifyDateAInsufficientHistory(t *testing.T) {
	st := newTestStore(t)
	end := seedIndexLowsA(t, st, "2026-03-06", 60, func(i int) float64 {
		return 1000.0 * math.Pow(1.003, float64(i)) // 即使逐级抬升，历史不足也判震荡
	})
	if got := ClassifyDateA(st, end); got != Range {
		t.Fatalf("历史不足 61 根应判 range（保守不放大），got %s", got)
	}
}
