package scheduler

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// fakeRunner 假流水线：记录调用顺序，并把结果真实落进 daily_run（模拟
// pipeline.runTask 的留痕语义，使 LastOKRun 判定与生产一致）。
type fakeRunner struct {
	mu     sync.Mutex
	calls  []string
	failOn map[string]error
	st     *store.Store
}

func (f *fakeRunner) RerunTask(ctx context.Context, date, task string) (contract.TaskResult, error) {
	f.mu.Lock()
	f.calls = append(f.calls, date+"|"+task)
	f.mu.Unlock()

	res := contract.TaskResult{Task: task, Date: date, Status: "ok", AffectedRows: 1}
	err := f.failOn[date+"|"+task]
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
	}
	id, _ := f.st.RunRepo.StartRun(date, task)
	_ = f.st.RunRepo.FinishRun(id, res)
	return res, err
}

func (f *fakeRunner) snapshot() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

// fakeReader 假 TDX：只喂 sh000001 的指定K线（交易日历直读路径）。
type fakeReader struct{ bars []contract.DailyBar }

func (f *fakeReader) ListCodes() ([]string, error) { return []string{"sh000001"}, nil }
func (f *fakeReader) ReadBars(code, since string) ([]contract.DailyBar, error) {
	var out []contract.DailyBar
	if code != "sh000001" {
		return out, nil
	}
	for _, b := range f.bars {
		if b.Date >= since {
			out = append(out, b)
		}
	}
	return out, nil
}
func (f *fakeReader) ModTime(code string) (time.Time, error) { return time.Time{}, nil }
func (f *fakeReader) ReadNames() (map[string]string, error)  { return nil, nil }

// newTestEnv 建临时库 + 假流水线 + 调度器；seedBars 写入 DB 日历。
func newTestEnv(t *testing.T, seedBars []contract.DailyBar, readerBars []contract.DailyBar) (*Scheduler, *fakeRunner) {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	if len(seedBars) > 0 {
		if err := st.SecRepo.UpsertSecurities(securitiesOf(seedBars)); err != nil {
			t.Fatal(err)
		}
		if _, err := st.BarRepo.UpsertBars(seedBars); err != nil {
			t.Fatal(err)
		}
	}
	if len(readerBars) > 0 {
		if err := st.SecRepo.UpsertSecurities(securitiesOf(readerBars)); err != nil {
			t.Fatal(err)
		}
	}
	runner := &fakeRunner{failOn: map[string]error{}, st: st}
	s := New(st, runner, &fakeReader{bars: readerBars})
	return s, runner
}

// securitiesOf 从K线推导证券登记（daily_bar 外键需要）。
func securitiesOf(bars []contract.DailyBar) []contract.Security {
	seen := map[string]bool{}
	var secs []contract.Security
	for _, b := range bars {
		if !seen[b.Code] {
			seen[b.Code] = true
			secs = append(secs, store.DeriveSecurity(b.Code, b.Date))
		}
	}
	return secs
}

func at(date string, h, m int) time.Time {
	t, err := time.ParseInLocation("2006-01-02", date, time.Local)
	if err != nil {
		panic(err)
	}
	return t.Add(time.Duration(h)*time.Hour + time.Duration(m)*time.Minute)
}

func eq(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("调用 %v，期望 %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("调用 %v，期望 %v", got, want)
		}
	}
}

// 2026-10-16 是周五（交易日），10-17 是周六。
var friBars = []contract.DailyBar{{Code: "sh000001", Date: "2026-10-16", Close: 3000, Volume: 100}}

func TestTickChainOrderAndIdempotent(t *testing.T) {
	s, runner := newTestEnv(t, friBars, nil)
	s.now = func() time.Time { return at("2026-10-16", 16, 25) }

	s.tick() // 16:25 → ingest/boxscan/screen/poolsync 四个触发点已过，链式补跑
	eq(t, runner.snapshot(), []string{
		"2026-10-16|ingest", "2026-10-16|boxscan", "2026-10-16|screen", "2026-10-16|poolsync",
	})

	s.tick() // 无新触发点 + 全部已 ok → 不再跑
	eq(t, runner.snapshot(), []string{
		"2026-10-16|ingest", "2026-10-16|boxscan", "2026-10-16|screen", "2026-10-16|poolsync",
	})
}

func TestIngestFailAbortsAndRetriesNextTrigger(t *testing.T) {
	s, runner := newTestEnv(t, friBars, nil)
	runner.failOn["2026-10-16|ingest"] = errors.New("tdx not ready")

	s.now = func() time.Time { return at("2026-10-16", 16, 25) }
	s.tick() // ingest 失败 → 中止本链，boxscan/screen/poolsync 不跑
	eq(t, runner.snapshot(), []string{"2026-10-16|ingest"})

	s.now = func() time.Time { return at("2026-10-16", 16, 30) } // signal 触发点
	s.tick()                                                     // 链头 ingest 仍未 ok → 重试（仍失败）→ 中止；signal 不跑
	eq(t, runner.snapshot(), []string{"2026-10-16|ingest", "2026-10-16|ingest"})

	// TDX 数据到位后，16:40 触发点一次链式补齐全部
	delete(runner.failOn, "2026-10-16|ingest")
	s.now = func() time.Time { return at("2026-10-16", 16, 40) }
	s.tick()
	eq(t, runner.snapshot(), []string{
		"2026-10-16|ingest", "2026-10-16|ingest",
		"2026-10-16|ingest", "2026-10-16|boxscan", "2026-10-16|screen",
		"2026-10-16|poolsync", "2026-10-16|signal", "2026-10-16|plan",
	})
}

func TestNonTradingDaySkips(t *testing.T) {
	// 周六 2026-10-17：DB 无当日K线、TDX 也无 → 全部触发点跳过
	s, runner := newTestEnv(t, friBars, nil)
	s.now = func() time.Time { return at("2026-10-17", 16, 25) }
	s.tick()
	eq(t, runner.snapshot(), nil)

	// 周五 16:25 但 TDX 未下载且当日未入库（DB 只有 10-15 及更早）→ 跳过
	s2, runner2 := newTestEnv(t,
		[]contract.DailyBar{{Code: "sh000001", Date: "2026-10-15", Close: 3000, Volume: 100}}, nil)
	s2.now = func() time.Time { return at("2026-10-16", 16, 25) }
	s2.tick()
	eq(t, runner2.snapshot(), nil)
}

func TestTradingDayViaTDX(t *testing.T) {
	// DB 尚无 10-16 K线（ingest 未跑），但 TDX sh000001.day 已有当日数据 → 是交易日
	s, runner := newTestEnv(t,
		[]contract.DailyBar{{Code: "sh000001", Date: "2026-10-15", Close: 3000, Volume: 100}}, friBars)
	s.now = func() time.Time { return at("2026-10-16", 15, 31) }
	s.tick()
	eq(t, runner.snapshot(), []string{"2026-10-16|ingest"})
}

func TestCatchUpPrevTradingDay(t *testing.T) {
	// 周六早上启动：周五（10-16，在库日历中）完全没跑 → 整日补跑；今天周六不跑
	s, runner := newTestEnv(t, friBars, nil)
	var doneDates []string
	s.OnDayDone = func(d string) { doneDates = append(doneDates, d) }
	s.now = func() time.Time { return at("2026-10-17", 10, 0) }

	if err := s.CatchUp(); err != nil {
		t.Fatal(err)
	}
	eq(t, runner.snapshot(), []string{
		"2026-10-16|ingest", "2026-10-16|boxscan", "2026-10-16|screen",
		"2026-10-16|poolsync", "2026-10-16|signal", "2026-10-16|plan", "2026-10-16|report",
	})
	eq(t, doneDates, []string{"2026-10-16"}) // 收尾钩子（备份）恰好一次

	// 前一日已有 ok 心跳（CatchUp 补过）→ 再次 CatchUp 不重跑
	if err := s.CatchUp(); err != nil {
		t.Fatal(err)
	}
	eq(t, runner.snapshot(), []string{
		"2026-10-16|ingest", "2026-10-16|boxscan", "2026-10-16|screen",
		"2026-10-16|poolsync", "2026-10-16|signal", "2026-10-16|plan", "2026-10-16|report",
	})
	eq(t, doneDates, []string{"2026-10-16"})
}

func TestCatchUpTodayMidDay(t *testing.T) {
	s, runner := newTestEnv(t, friBars, nil)
	s.now = func() time.Time { return at("2026-10-16", 16, 25) }
	if err := s.CatchUp(); err != nil {
		t.Fatal(err)
	}
	eq(t, runner.snapshot(), []string{
		"2026-10-16|ingest", "2026-10-16|boxscan", "2026-10-16|screen", "2026-10-16|poolsync",
	})
}

func TestAlreadyOkSkips(t *testing.T) {
	s, runner := newTestEnv(t, friBars, nil)
	// 预先标记 ingest 已 ok（如手动 darvas run 跑过）
	id, _ := s.st.RunRepo.StartRun("2026-10-16", "ingest")
	_ = s.st.RunRepo.FinishRun(id, contract.TaskResult{Task: "ingest", Date: "2026-10-16", Status: "ok"})

	s.now = func() time.Time { return at("2026-10-16", 15, 31) }
	s.tick() // ingest 触发点，但已 ok → 链空转
	eq(t, runner.snapshot(), nil)

	s.now = func() time.Time { return at("2026-10-16", 16, 0) }
	s.tick() // boxscan 触发点 → 只补 boxscan
	eq(t, runner.snapshot(), []string{"2026-10-16|boxscan"})
}

func TestStartStopSmoke(t *testing.T) {
	s, runner := newTestEnv(t, friBars, nil)
	s.interval = 5 * time.Millisecond
	s.now = func() time.Time { return at("2026-10-16", 16, 25) }
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(30 * time.Millisecond) // 循环至少巡检一次
	if err := s.Stop(); err != nil {
		t.Fatal(err)
	}
	if err := s.Stop(); err != nil { // 幂等
		t.Fatal(err)
	}
	if got := runner.snapshot(); len(got) > 4 {
		t.Fatalf("重复执行: %v", got)
	}
}
