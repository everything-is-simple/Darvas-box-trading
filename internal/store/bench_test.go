package store

import (
	"testing"
	"time"

	"darvas/internal/contract"
)

// benchBars 生成 n 根合成 K线（8 列参数，与真实 daily_bar 一致）。
func benchBars(n int) []contract.DailyBar {
	bs := make([]contract.DailyBar, n)
	for i := range bs {
		bs[i] = contract.DailyBar{
			Code: "sh000001", Date: dayStr(i + 1),
			Open: 10, High: 11, Low: 9, Close: 10.5,
			Volume: int64(1000 + i), Amount: 1e5,
		}
	}
	return bs
}

// upsertPrepared 预编译 1 行复用实现（基准对比用）。
func (r *BarRepo) upsertPrepared(bars []contract.DailyBar) error {
	tx, err := r.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`INSERT OR REPLACE INTO daily_bar (code, date, open, high, low, close, volume, amount) VALUES (?,?,?,?,?,?,?,?)`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, b2 := range bars {
		if _, err := stmt.Exec(b2.Code, b2.Date, b2.Open, b2.High, b2.Low, b2.Close, b2.Volume, b2.Amount); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// upsertMultiRow 可变批次的多行 VALUES 实现（基准对比用）。
func (r *BarRepo) upsertMultiRow(bars []contract.DailyBar, size int) error {
	tx, err := r.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for start := 0; start < len(bars); start += size {
		end := start + size
		if end > len(bars) {
			end = len(bars)
		}
		n := end - start
		ph := buildPlaceholders(n, colCount)
		args := make([]any, 0, n*colCount)
		for i := start; i < end; i++ {
			b2 := bars[i]
			args = append(args, b2.Code, b2.Date, b2.Open, b2.High, b2.Low, b2.Close, b2.Volume, b2.Amount)
		}
		if _, err := tx.Exec(insertBarsSQL+ph, args...); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// TestBenchInsertStrategies 一次性对比四种策略的实测吞吐（直接报告 bars/sec）。
// 用于决定 ingest 批量写入采用哪种策略（docs/05 M1.1）。
// 较重（~15s），默认 `go test -short` 跳过；CI 全量跑时执行。
func TestBenchInsertStrategies(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping insert-strategy benchmark in short mode")
	}
	const n = 50000
	bars := benchBars(n)
	strategies := []struct {
		name string
		fn   func(*testing.T, []contract.DailyBar) error
	}{
		{"multiRow500", func(tt *testing.T, b2 []contract.DailyBar) error {
			st := newTestStore(tt)
			seedSecs(tt, st, "sh000001")
			_, err := st.BarRepo.UpsertBars(b2)
			return err
		}},
		{"prepared1row", func(tt *testing.T, b2 []contract.DailyBar) error {
			st := newTestStore(tt)
			seedSecs(tt, st, "sh000001")
			return st.BarRepo.upsertPrepared(b2)
		}},
		{"multiRow100", func(tt *testing.T, b2 []contract.DailyBar) error {
			st := newTestStore(tt)
			seedSecs(tt, st, "sh000001")
			return st.BarRepo.upsertMultiRow(b2, 100)
		}},
		{"multiRow1000", func(tt *testing.T, b2 []contract.DailyBar) error {
			st := newTestStore(tt)
			seedSecs(tt, st, "sh000001")
			return st.BarRepo.upsertMultiRow(b2, 1000)
		}},
	}
	for _, s := range strategies {
		start := time.Now()
		if err := s.fn(t, bars); err != nil {
			t.Fatalf("%s: %v", s.name, err)
		}
		dur := time.Since(start)
		rate := float64(n) / dur.Seconds()
		t.Logf("%-14s %d bars in %v → %.0f bars/sec", s.name, n, dur.Round(time.Millisecond), rate)
	}
}
