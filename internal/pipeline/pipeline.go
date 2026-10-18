// Package pipeline 编排盘后流水线（docs/01 §4.1 串行：ingest→boxscan→screen→
// poolsync→signal→plan→report），每步幂等、留痕、可独立重跑。
package pipeline

import (
	"context"
	"log"

	"darvas/internal/box"
	"darvas/internal/contract"
	"darvas/internal/ingest"
	"darvas/internal/planner"
	"darvas/internal/pool"
	"darvas/internal/reporter"
	"darvas/internal/screener"
	"darvas/internal/signal"
	"darvas/internal/store"
)

// Pipeline 盘后流水线。
type Pipeline struct {
	st       *store.Store
	ingester *ingest.Ingester
	scanner  *box.Scanner
	report   *reporter.Reporter
	log      *log.Logger
}

// New 构造。ingester 由调用方注入（依赖 tdx reader + store）。
func New(st *store.Store, ing *ingest.Ingester) *Pipeline {
	return &Pipeline{
		st:       st,
		ingester: ing,
		scanner:  box.NewScanner(box.New(), st),
		report:   reporter.New(st, ""),
		log:      log.New(log.Writer(), "[pipeline] ", log.LstdFlags),
	}
}

// SetReportDir 重定向日报输出目录（M8.1 回放专用：scratch 日报绝不写生产 var/reports，
// docs/07-m8-issue-list.md #5）。生产路径（run/serve/rerun）不受影响。
func (p *Pipeline) SetReportDir(dir string) {
	p.report = reporter.New(p.st, dir)
}

// RunDaily 跑完整盘后流水线（严格串行，每步留痕 daily_run）。
// 失败策略：ingest 失败→整体中止；plan 失败→标红但已完成任务保留；其余失败不阻断后续（可续跑）。
func (p *Pipeline) RunDaily(ctx context.Context, date string) ([]contract.TaskResult, error) {
	var results []contract.TaskResult

	// 1. ingest（数据同步）——失败则整体中止（数据陈旧阻断后续）
	r1, err := p.runTask(ctx, date, "ingest", func() (contract.TaskResult, error) {
		return p.ingester.SyncIncremental(ctx, date)
	})
	results = append(results, r1)
	if err != nil {
		return results, err
	}

	// 2. boxscan（箱体重算，增量预过滤）
	r2, _ := p.runTask(ctx, date, "boxscan", func() (contract.TaskResult, error) {
		return p.scanner.RescanIncremental(ctx, date)
	})
	results = append(results, r2)

	// 3. screen（初选四条件）
	r3, _ := p.runTask(ctx, date, "screen", func() (contract.TaskResult, error) {
		return screener.New(p.st).Screen(ctx, date)
	})
	results = append(results, r3)

	// 4. poolsync（跟踪池状态机）
	r4, _ := p.runTask(ctx, date, "poolsync", func() (contract.TaskResult, error) {
		return pool.New(p.st).Sync(ctx, date)
	})
	results = append(results, r4)

	// 5. signal（四类信号判定）
	r5, _ := p.runTask(ctx, date, "signal", func() (contract.TaskResult, error) {
		return signal.New(p.st).Evaluate(ctx, date)
	})
	results = append(results, r5)

	// 6. plan（六问→交易计划）——失败标红但已完成任务保留
	r6, perr := p.runTask(ctx, date, "plan", func() (contract.TaskResult, error) {
		return planner.New(p.st).Build(ctx, date)
	})
	results = append(results, r6)
	if perr != nil {
		p.log.Printf("⚠ plan failed: %v（已完成任务保留，需人工介入）", perr)
	}

	// 7. report（HTML日报）——失败不影响主流程
	r7, _ := p.runTask(ctx, date, "report", func() (contract.TaskResult, error) {
		return p.report.Report(ctx, date)
	})
	results = append(results, r7)

	// 8. 备份（M7.3/NFR-3 流水线尾部运维动作，非任务：不占 daily_run 心跳位）
	p.BackupDaily(date)

	return results, nil
}

// runTask 执行单个任务并登记 daily_run 留痕（心跳）。
func (p *Pipeline) runTask(ctx context.Context, date, task string, fn func() (contract.TaskResult, error)) (contract.TaskResult, error) {
	id, _ := p.st.RunRepo.StartRun(date, task)
	res, err := fn()
	if err != nil && res.Status == "" {
		res.Status = "failed"
	}
	_ = p.st.RunRepo.FinishRun(id, res)
	return res, err
}

// RerunTask 幂等补跑单任务。
func (p *Pipeline) RerunTask(ctx context.Context, date, task string) (contract.TaskResult, error) {
	switch task {
	case "ingest":
		return p.runTask(ctx, date, "ingest", func() (contract.TaskResult, error) {
			return p.ingester.SyncIncremental(ctx, date)
		})
	case "boxscan":
		return p.runTask(ctx, date, "boxscan", func() (contract.TaskResult, error) {
			return p.scanner.RescanIncremental(ctx, date)
		})
	case "screen":
		return p.runTask(ctx, date, "screen", func() (contract.TaskResult, error) {
			return screener.New(p.st).Screen(ctx, date)
		})
	case "poolsync":
		return p.runTask(ctx, date, "poolsync", func() (contract.TaskResult, error) {
			return pool.New(p.st).Sync(ctx, date)
		})
	case "signal":
		return p.runTask(ctx, date, "signal", func() (contract.TaskResult, error) {
			return signal.New(p.st).Evaluate(ctx, date)
		})
	case "plan":
		return p.runTask(ctx, date, "plan", func() (contract.TaskResult, error) {
			return planner.New(p.st).Build(ctx, date)
		})
	case "report":
		return p.runTask(ctx, date, "report", func() (contract.TaskResult, error) {
			return p.report.Report(ctx, date)
		})
	case "weekly": // M7.2 周报：周六10:00调度或手动补跑，不进每日链（RunDaily 不含）
		return p.runTask(ctx, date, "weekly", func() (contract.TaskResult, error) {
			return p.report.Weekly(ctx, date)
		})
	default:
		return contract.TaskResult{Task: task, Date: date, Status: "failed", Error: "unknown task"}, nil
	}
}

// ReportPath 返回某日日报文件路径（CLI openreport 用）。
func (p *Pipeline) ReportPath(date string) string {
	return p.report.Path(date)
}

// WeeklyPath 返回某锚定日周报文件路径（CLI weekly 用）。
func (p *Pipeline) WeeklyPath(date string) string {
	return p.report.WeeklyPath(date)
}
