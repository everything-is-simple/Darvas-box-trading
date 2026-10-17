package store

import (
	"testing"
	"time"

	"darvas/internal/contract"
)

// TestUpsertBarsIdempotent 幂等：同一批重导不重复（FR-102/D4）。
func TestUpsertBarsIdempotent(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	bars := []contract.DailyBar{
		mkBar("sh600519", "2026-10-13", 1600, 1620, 1590, 1610, 50000, 8e7),
		mkBar("sh600519", "2026-10-14", 1610, 1640, 1605, 1635, 60000, 9.8e7),
		mkBar("sh600519", "2026-10-15", 1635, 1650, 1620, 1645, 45000, 7.4e7),
	}
	n1, err := st.BarRepo.UpsertBars(bars)
	if err != nil {
		t.Fatalf("first upsert: %v", err)
	}
	if n1 != 3 {
		t.Fatalf("first upsert count=%d want 3", n1)
	}
	// 重导同一批（含价格修正）→ INSERT OR REPLACE 更新，行数仍为 3
	bars[2].Close = 1648
	n2, err := st.BarRepo.UpsertBars(bars)
	if err != nil {
		t.Fatalf("second upsert: %v", err)
	}
	if n2 != 3 {
		t.Fatalf("second upsert count=%d want 3 (idempotent)", n2)
	}
	var cnt int
	if err := st.DB().QueryRow(`SELECT COUNT(*) FROM daily_bar WHERE code='sh600519'`).Scan(&cnt); err != nil {
		t.Fatal(err)
	}
	if cnt != 3 {
		t.Fatalf("row count after re-import=%d want 3 (no duplicates)", cnt)
	}
	// 验证更新生效
	var c float64
	st.DB().QueryRow(`SELECT close FROM daily_bar WHERE code='sh600519' AND date='2026-10-15'`).Scan(&c)
	if c != 1648 {
		t.Fatalf("updated close=%.2f want 1648", c)
	}
}

// TestUpsertBarsBatchSplit 批量超过 batchInsertSize(500) 时正确分批。
func TestUpsertBarsBatchSplit(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh000001")
	const n = 1200 // 跨 3 个批次（500+500+200）
	bars := make([]contract.DailyBar, n)
	for i := range bars {
		bars[i] = mkBar("sh000001", dayStr(i+1), 100, 101, 99, 100, 1000, 1e5)
	}
	got, err := st.BarRepo.UpsertBars(bars)
	if err != nil {
		t.Fatalf("upsert %d bars: %v", n, err)
	}
	if got != n {
		t.Fatalf("upsert count=%d want %d", got, n)
	}
	cnt, err := st.BarRepo.CountBars("sh000001")
	if err != nil {
		t.Fatal(err)
	}
	if cnt != n {
		t.Fatalf("stored count=%d want %d", cnt, n)
	}
}

// TestBarsWindow 最近 n 根、升序、截至 endDate。
func TestBarsWindow(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sz000001")
	bars := []contract.DailyBar{
		mkBar("sz000001", "2026-10-10", 10, 11, 9, 10, 100, 1e3),
		mkBar("sz000001", "2026-10-13", 10, 11, 9, 10.5, 200, 2e3),
		mkBar("sz000001", "2026-10-14", 10.5, 11.5, 10, 11, 300, 3e3),
		mkBar("sz000001", "2026-10-15", 11, 12, 10.5, 11.5, 400, 4e3),
	}
	st.BarRepo.UpsertBars(bars)
	// 截至含 endDate=2026-10-14 的最近 2 根 → 10-13, 10-14 升序
	got, err := st.BarRepo.BarsWindow("sz000001", "2026-10-14", 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].Date != "2026-10-13" || got[1].Date != "2026-10-14" {
		t.Fatalf("window=%+v want [10-13,10-14] asc", got)
	}
	// endDate 之后的不返回
	got2, _ := st.BarRepo.BarsWindow("sz000001", "2026-10-13", 5)
	if len(got2) != 2 { // 10-10, 10-13
		t.Fatalf("window up to 10-13 len=%d want 2", len(got2))
	}
}

// TestCloseOn 查收盘价。
func TestCloseOn(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	st.BarRepo.UpsertBars([]contract.DailyBar{mkBar("sh600519", "2026-10-16", 1645, 1660, 1640, 1655, 50000, 8e7)})
	c, ok, err := st.BarRepo.CloseOn("sh600519", "2026-10-16")
	if err != nil || !ok || c != 1655 {
		t.Fatalf("CloseOn c=%.2f ok=%v err=%v want 1655", c, ok, err)
	}
	_, ok2, _ := st.BarRepo.CloseOn("sh600519", "2099-01-01")
	if ok2 {
		t.Fatal("missing date should return ok=false")
	}
}

// TestAvgVolume n 日均量。
func TestAvgVolume(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	bars := []contract.DailyBar{
		mkBar("sh600519", "2026-10-10", 0, 0, 0, 0, 100, 0),
		mkBar("sh600519", "2026-10-13", 0, 0, 0, 0, 200, 0),
		mkBar("sh600519", "2026-10-14", 0, 0, 0, 0, 300, 0),
	}
	st.BarRepo.UpsertBars(bars)
	// 截至 10-14 最近 3 日均量 = (100+200+300)/3 = 200
	avg, err := st.BarRepo.AvgVolume("sh600519", "2026-10-14", 3)
	if err != nil {
		t.Fatal(err)
	}
	if avg != 200 {
		t.Fatalf("avg=%.1f want 200", avg)
	}
}

// TestLatestDate 交易日历与新鲜度（ADR-2）。
func TestLatestDate(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh000001")
	st.BarRepo.UpsertBars([]contract.DailyBar{
		mkBar("sh000001", "2026-10-14", 0, 0, 0, 0, 0, 0),
		mkBar("sh000001", "2026-10-16", 0, 0, 0, 0, 0, 0),
		mkBar("sh000001", "2026-10-15", 0, 0, 0, 0, 0, 0),
	})
	d, err := st.BarRepo.LatestDate("sh000001")
	if err != nil {
		t.Fatal(err)
	}
	if d != "2026-10-16" {
		t.Fatalf("latest=%s want 2026-10-16", d)
	}
}

// TestHasBar 存在性。
func TestHasBar(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	st.BarRepo.UpsertBars([]contract.DailyBar{mkBar("sh600519", "2026-10-16", 0, 0, 0, 0, 0, 0)})
	ok, _ := st.BarRepo.HasBar("sh600519", "2026-10-16")
	if !ok {
		t.Fatal("HasBar should be true")
	}
	ok2, _ := st.BarRepo.HasBar("sh600519", "2099-01-01")
	if ok2 {
		t.Fatal("HasBar missing should be false")
	}
}

// TestUpsertBarsEmpty 空切片不报错。
func TestUpsertBarsEmpty(t *testing.T) {
	st := newTestStore(t)
	n, err := st.BarRepo.UpsertBars(nil)
	if err != nil || n != 0 {
		t.Fatalf("empty upsert n=%d err=%v want 0/nil", n, err)
	}
}

// TestUpsertSecurities 证券批量写入与分类。
func TestUpsertSecurities(t *testing.T) {
	st := newTestStore(t)
	secs := []contract.Security{
		DeriveSecurity("sh600519", "2001-08-27"), // 沪主板 main
		DeriveSecurity("sz300750", ""),           // 创业板 gem
		DeriveSecurity("sh688981", ""),            // 科创板 star
		DeriveSecurity("sh000001", ""),            // 指数 index
	}
	if err := st.SecRepo.UpsertSecurities(secs); err != nil {
		t.Fatal(err)
	}
	checks := map[string]string{ // code -> board
		"sh600519": "main", "sz300750": "gem", "sh688981": "star", "sh000001": "index",
	}
	for code, want := range checks {
		var board string
		st.DB().QueryRow(`SELECT board FROM security WHERE code=?`, code).Scan(&board)
		if board != want {
			t.Errorf("security %s board=%s want %s", code, board, want)
		}
	}
	// 幂等：重写不报错
	if err := st.SecRepo.UpsertSecurities(secs); err != nil {
		t.Fatalf("idempotent rewrite: %v", err)
	}
}

// dayStr 把序号转成唯一日期串（以 2024-01-01 为基准，逐日递增，保证 PK 唯一）。
func dayStr(n int) string {
	return time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC).AddDate(0, 0, n-1).Format("2006-01-02")
}
