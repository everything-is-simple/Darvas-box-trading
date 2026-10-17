// Package scheduler 常驻调度器（M7.1，docs/01 §4 每日时间表 / docs/04 §5）。
//
// 语义：
//   - 时刻表触发：每日 15:30/16:00/16:10/16:20/16:30/16:40/16:45 各任务有自己的触发点。
//     每个触发点到达时，把「计划时刻已过且尚未 ok」的任务按依赖链顺序补跑一遍
//     （错过多个触发点的，一次链式补齐；ingest 失败则中止本链，等下个触发点重试
//     —— 天然满足 docs/01「失败重跑/ingest重试」而不会每分钟空转重试）。
//   - CatchUp 启动补跑：前一交易日（DB 日历内 < 今天的最大交易日）若完全没有任何
//     ok 心跳 → 整日补跑；今天已过触发点但未 ok 的 → 链式补跑。
//     更早的历史缺口不自动回放（数据多半未入库），用 `darvas run --date X` 手动补。
//   - 交易日历（ADR-2）：以 sh000001 的日线日期序列为权威——已入库的日期查
//     daily_bar（BarRepo.HasBar）；当日尚未入库的，直读 TDX sh000001.day 判断
//     用户是否已完成「下载数据」（15:05 下载后，15:30 触发 ingest 前即可判定；
//     未下载则跳过，待下个触发点重查）。
//
// 时区：A股时刻按北京时间；本工具单机本地运行，直接用系统本地时钟（time.Local）。
// 周六10:00 weekly 任务待 M7.2 周报交付后接入时刻表。
package scheduler

import (
	"context"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// TaskTime 时刻表条目（docs/01 §4.1）。
type TaskTime struct {
	Clock string // "15:30"
	Task  string // "ingest"
}

// Schedule 每日时刻表（顺序即依赖链 ingest→…→report）。
var Schedule = []TaskTime{
	{"15:30", "ingest"},
	{"16:00", "boxscan"},
	{"16:10", "screen"},
	{"16:20", "poolsync"},
	{"16:30", "signal"},
	{"16:40", "plan"},
	{"16:45", "report"},
}

// Runner 任务执行器（*pipeline.Pipeline 满足；测试可注入假实现）。
type Runner interface {
	RerunTask(ctx context.Context, date, task string) (contract.TaskResult, error)
}

// Scheduler 常驻调度器（contract.Scheduler 实现）。
type Scheduler struct {
	st     *store.Store
	runner Runner
	reader contract.DayFileReader // 可空：nil 时当日只能靠 DB 日历（未入库即视为非交易日）

	log      *log.Logger
	now      func() time.Time // 可注入时钟（测试）
	interval time.Duration    // 巡检周期

	ctx    context.Context
	cancel context.CancelFunc

	// OnDayDone 某日 report 刚转 ok 时的收尾钩子（每日期至多一次；
	// serve 接 VACUUM 备份轮转，NFR-3）。
	OnDayDone func(date string)

	mu        sync.Mutex
	fired     map[string]string // task → 已消费触发点的日期（每触发点每天只消费一次）
	doneFired map[string]bool   // date → OnDayDone 已触发
	running   bool              // 有链在跑时跳过新 tick（防重入）
	stopCh    chan struct{}
	stopped   chan struct{}
}

var _ contract.Scheduler = (*Scheduler)(nil)

// New 构造。reader 传 nil 则交易日判定只查 DB（当日数据未入库时视为非交易日）。
func New(st *store.Store, runner Runner, reader contract.DayFileReader) *Scheduler {
	ctx, cancel := context.WithCancel(context.Background())
	return &Scheduler{
		st:        st,
		runner:    runner,
		reader:    reader,
		log:       log.New(log.Writer(), "[scheduler] ", log.LstdFlags),
		now:       time.Now,
		interval:  30 * time.Second,
		fired:     map[string]string{},
		doneFired: map[string]bool{},
		ctx:       ctx,
		cancel:    cancel,
	}
}

// Start 启动调度循环（每 interval 巡检一次时刻表）。
func (s *Scheduler) Start() error {
	s.mu.Lock()
	if s.stopCh != nil {
		s.mu.Unlock()
		return fmt.Errorf("scheduler already started")
	}
	s.stopCh = make(chan struct{})
	stopped := make(chan struct{})
	s.stopped = stopped
	s.mu.Unlock()

	go func() {
		defer close(stopped)
		ticker := time.NewTicker(s.interval)
		defer ticker.Stop()
		for {
			select {
			case <-s.stopCh:
				return
			case <-ticker.C:
				s.tick()
			}
		}
	}()
	return nil
}

// Stop 停止：取消任务 ctx（长任务尽快退出），等待进行中的链收尾。
func (s *Scheduler) Stop() error {
	s.mu.Lock()
	stopCh, stopped := s.stopCh, s.stopped
	s.stopCh, s.stopped = nil, nil
	s.mu.Unlock()
	if stopCh == nil {
		return nil
	}
	s.cancel()
	close(stopCh)
	<-stopped
	return nil
}

// CatchUp 启动补跑（先于 Start 调用；见包注释）。
func (s *Scheduler) CatchUp() error {
	if !s.tryBegin() {
		return nil // 已有链在跑（如 Start 循环已在执行）
	}
	defer s.end()

	now := s.now()
	today := now.Format("2006-01-02")

	// ① 前一交易日整日补跑
	prev, err := s.st.BarRepo.PrevDate("sh000001", today)
	if err != nil {
		s.log.Printf("warn: 查询前一交易日: %v", err)
	} else if prev != "" && !s.anyOK(prev) {
		s.log.Printf("CatchUp: 前一交易日 %s 无任何 ok 心跳 → 整日补跑", prev)
		s.runOverdue(s.ctx, prev, endOfDay(prev))
	}

	// ② 今天：已过点的触发点立即消费
	s.fireDue(now)
	return nil
}

// tick 一次巡检（Start 循环内调用）。
func (s *Scheduler) tick() {
	if !s.tryBegin() {
		return
	}
	defer s.end()
	s.fireDue(s.now())
}

func (s *Scheduler) tryBegin() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.running {
		return false
	}
	s.running = true
	return true
}

func (s *Scheduler) end() {
	s.mu.Lock()
	s.running = false
	s.mu.Unlock()
}

// fireDue 消费「新到达且未消费过」的触发点，执行当日补跑链。
func (s *Scheduler) fireDue(now time.Time) {
	today := now.Format("2006-01-02")
	s.mu.Lock()
	var due []TaskTime
	for _, tt := range Schedule {
		if !now.Before(clockOf(today, tt.Clock)) && s.fired[tt.Task] != today {
			s.fired[tt.Task] = today // 消费今天的触发点（无论链结果如何，不重复消费）
			due = append(due, tt)
		}
	}
	s.mu.Unlock()
	if len(due) == 0 {
		return
	}
	names := make([]string, len(due))
	for i, tt := range due {
		names[i] = tt.Task + "@" + tt.Clock
	}
	s.log.Printf("触发点到达: %s", strings.Join(names, ", "))
	s.runOverdue(s.ctx, today, now)
}

// runOverdue 按 Schedule 顺序补跑「计划时刻已过且未 ok」的任务。
// 失败策略与 pipeline.RunDaily 一致：ingest 失败→中止本链；其余失败→继续。
func (s *Scheduler) runOverdue(ctx context.Context, date string, now time.Time) {
	ok, err := s.isTradingDay(date)
	if err != nil {
		s.log.Printf("warn: %s 交易日判定失败: %v（跳过，待下个触发点）", date, err)
		return
	}
	if !ok {
		s.log.Printf("%s 非交易日（或 TDX 尚未下载当日数据），跳过", date)
		return
	}
	for _, tt := range Schedule {
		if now.Before(clockOf(date, tt.Clock)) {
			break // 后续任务未到点
		}
		if _, ok, err := s.st.RunRepo.LastOKRun(date, tt.Task); err == nil && ok {
			continue // 已成功，幂等跳过
		}
		res, err := s.runner.RerunTask(ctx, date, tt.Task)
		if res.Status == "ok" {
			s.log.Printf("%s %s ok rows=%d %dms", date, tt.Task, res.AffectedRows, res.DurationMS)
		} else {
			s.log.Printf("%s %s %s: %s", date, tt.Task, res.Status, res.Error)
		}
		if err != nil {
			if tt.Task == "ingest" {
				s.log.Printf("ingest 失败，中止本链（下个触发点重试）")
				return
			}
			// plan 失败标红继续 report；其余失败继续（同 RunDaily 策略）
		}
		if tt.Task == "report" && res.Status == "ok" {
			s.fireDayDone(date)
		}
	}
}

// fireDayDone 当日收尾钩子（每日期至多触发一次）。
func (s *Scheduler) fireDayDone(date string) {
	s.mu.Lock()
	if s.doneFired[date] {
		s.mu.Unlock()
		return
	}
	s.doneFired[date] = true
	s.mu.Unlock()
	if s.OnDayDone != nil {
		s.log.Printf("%s 流水线收尾（report ok）", date)
		s.OnDayDone(date)
	}
}

// isTradingDay 交易日判定（ADR-2：sh000001 日期序列为权威）。
// 当日数据可能尚未入库（ingest 之前），故补一手 TDX 文件直读。
func (s *Scheduler) isTradingDay(date string) (bool, error) {
	found, err := s.st.BarRepo.HasBar("sh000001", date)
	if err != nil {
		return false, err
	}
	if found {
		return true, nil
	}
	if s.reader != nil {
		bars, rerr := s.reader.ReadBars("sh000001", date)
		if rerr != nil {
			return false, fmt.Errorf("read tdx sh000001: %w", rerr)
		}
		if len(bars) > 0 {
			return true, nil // 用户已下载当日数据，等 ingest 入库
		}
	}
	return false, nil
}

// anyOK 某日是否存在任何 ok 心跳。
func (s *Scheduler) anyOK(date string) bool {
	runs, err := s.st.RunRepo.RunsOn(date)
	if err != nil {
		return false
	}
	for _, r := range runs {
		if r.Status == "ok" {
			return true
		}
	}
	return false
}

func clockOf(date, clock string) time.Time {
	t, err := time.ParseInLocation("2006-01-02 15:04", date+" "+clock, time.Local)
	if err != nil {
		panic("scheduler: bad clock constant " + clock) // 常量表，不可达
	}
	return t
}

func endOfDay(date string) time.Time { return clockOf(date, "23:59") }
