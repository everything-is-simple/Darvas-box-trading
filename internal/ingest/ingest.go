// Package ingest 负责 TDX vipdoc → SQLite 的全量/增量导入与质量校验。
// 对应需求 FR-101~104（docs/01-requirements.md §5.1）。
package ingest

import (
	"context"
	"log"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// flushEveryCodes 跨 code 合并事务的提交粒度：每读取该数量的代码后
// 把累积的 bars/securities 一次性写入（docs/05 M1.1）。
// 2000 只×约700根 ≈ 140万行/事务，全量仅 ~6 次 commit，
// 配合多行 VALUES(100行/语句) 把 DB 写入压到基准测得的 ~35k bars/sec。
const flushEveryCodes = 2000

// Ingester 导入器（docs/04-api-contract.md §3.2）。
type Ingester struct {
	reader    contract.DayFileReader
	st        *store.Store
	FullSince string // 全量导入起始日：箱体引擎+52周新高+RS窗口最多需要约2年历史
	log       *log.Logger
}

// New 构造。fullSince 为空或 "full"（CLI 标志）时默认 "2024-01-01"
// （箱体引擎+52周新高+RS窗口最多需要约2年历史）。
// fullSince="all" 表示全历史（1990 起，M8.3 全历史体制校准专用，数据量约 3000 万条）。
func New(reader contract.DayFileReader, st *store.Store, fullSince string) *Ingester {
	if fullSince == "" || fullSince == "full" || fullSince == "FULL" {
		fullSince = "2024-01-01"
	}
	if fullSince == "all" || fullSince == "ALL" {
		fullSince = "1990-01-01"
	}
	return &Ingester{
		reader:    reader,
		st:        st,
		FullSince: fullSince,
		log:       log.New(log.Writer(), "[ingest] ", log.LstdFlags),
	}
}

// accWrite 跨 code 累积缓冲：攒满 flushEveryCodes 个代码后一次性 flush。
type accWrite struct {
	bars []contract.DailyBar
	secs []contract.Security
}

func (a *accWrite) add(code string, bars []contract.DailyBar, firstDate string) {
	if len(bars) > 0 {
		a.bars = append(a.bars, bars...)
		a.secs = append(a.secs, store.DeriveSecurity(code, firstDate))
	} else {
		// 无行情也登记证券（保证 security 表覆盖全部代码）
		a.secs = append(a.secs, store.DeriveSecurity(code, ""))
	}
}

// flush 把缓冲写入 DB（bars 走多行 VALUES 单事务，securities 走批量 INSERT OR IGNORE）。
func (g *Ingester) flush(a *accWrite) (int64, error) {
	if len(a.bars) == 0 {
		if err := g.st.SecRepo.UpsertSecurities(a.secs); err != nil {
			return 0, err
		}
		a.secs = a.secs[:0]
		return 0, nil
	}
	if err := g.st.SecRepo.UpsertSecurities(a.secs); err != nil {
		return 0, err
	}
	n, err := g.st.BarRepo.UpsertBars(a.bars)
	a.bars = a.bars[:0]
	a.secs = a.secs[:0]
	return n, err
}

// FullImport 全量导入（幂等，INSERT OR REPLACE）。
// 性能策略（docs/05 M1.1）：
//   - 索引降级：导入期 DROP (date) 二级索引，结束后排序重建（散列 code 随机插入提速一个数量级）；
//   - 跨 code 缓冲：每 flushEveryCodes 只代码合并一次事务写入；
//   - 多行 VALUES：由 BarRepo.UpsertBars 每 100 行拼一条 INSERT（基准甜点）；
//   - PRAGMA 调优：synchronous=NORMAL/cache_size 由 store.Open 设置。
func (g *Ingester) FullImport(ctx context.Context) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "ingest", Date: today()}
	codes, err := g.reader.ListCodes()
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	// 导入期降级索引（失败不阻断，最坏只是慢）
	if ierr := g.st.BulkLoadToggle(true); ierr != nil {
		g.log.Printf("warn: bulk toggle on: %v", ierr)
	}
	var total int64
	acc := &accWrite{}
	pending := 0
	for i, code := range codes {
		select {
		case <-ctx.Done():
			g.st.BulkLoadToggle(false) // 尽力恢复索引
			res.Status, res.Error = "failed", ctx.Err().Error()
			return res, ctx.Err()
		default:
		}
		bars, rerr := g.reader.ReadBars(code, g.FullSince)
		if rerr != nil {
			// 单文件读取失败不阻断全量（质量报告统计）；仍登记证券
			acc.add(code, nil, "")
		} else {
			firstDate := ""
			if len(bars) > 0 {
				firstDate = bars[0].Date
			}
			acc.add(code, bars, firstDate)
		}
		pending++
		if pending >= flushEveryCodes {
			n, ferr := g.flush(acc)
			if ferr != nil {
				g.log.Printf("flush at code %d/%d: %v", i+1, len(codes), ferr)
			}
			total += n
			pending = 0
		}
		if (i+1)%2000 == 0 {
			g.log.Printf("progress %d/%d codes, %d bars", i+1, len(codes), total)
		}
	}
	// 尾部残余
	if pending > 0 {
		n, ferr := g.flush(acc)
		if ferr != nil {
			g.log.Printf("flush tail: %v", ferr)
		}
		total += n
	}
	// 重建索引（即使前面 toggle 失败也尝试，CREATE IF NOT EXISTS 幂等）
	if ierr := g.st.BulkLoadToggle(false); ierr != nil {
		g.log.Printf("warn: bulk toggle off (index rebuild): %v", ierr)
	}
	g.syncNames()
	res.Status = "ok"
	res.AffectedRows = total
	res.DurationMS = time.Since(start).Milliseconds()
	rate := 0.0
	sec := time.Since(start).Seconds()
	if sec > 0 {
		rate = float64(total) / sec
	}
	g.log.Printf("full import done: %d codes, %d bars, %s (%.0f bars/sec)",
		len(codes), total, time.Since(start).Round(time.Second), rate)
	return res, nil
}

// SyncIncremental 增量导入：只读「文件修改时间在当日零点之后」的文件，
// 自库内最新日期续读。同样走跨 code 缓冲批量写入（docs/01 D2）。
func (g *Ingester) SyncIncremental(ctx context.Context, date string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "ingest", Date: date}
	midnight, _ := time.ParseInLocation("2006-01-02", date, time.Local)

	codes, err := g.reader.ListCodes()
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	var total, files int64
	acc := &accWrite{}
	pending := 0
	for _, code := range codes {
		select {
		case <-ctx.Done():
			res.Status, res.Error = "failed", ctx.Err().Error()
			return res, ctx.Err()
		default:
		}
		mt, merr := g.reader.ModTime(code)
		if merr != nil || mt.Before(midnight) {
			continue // 今日未更新的文件跳过
		}
		files++
		latest, lerr := g.st.BarRepo.LatestDate(code)
		if lerr != nil || latest == "" {
			latest = g.FullSince
		}
		bars, rerr := g.reader.ReadBars(code, latest)
		if rerr != nil {
			acc.add(code, nil, "")
		} else {
			acc.add(code, bars, "")
		}
		pending++
		if pending >= flushEveryCodes {
			n, ferr := g.flush(acc)
			if ferr != nil {
				g.log.Printf("incr flush: %v", ferr)
			}
			total += n
			pending = 0
		}
	}
	if pending > 0 {
		n, ferr := g.flush(acc)
		if ferr != nil {
			g.log.Printf("incr flush tail: %v", ferr)
		}
		total += n
	}
	g.syncNames()
	res.Status = "ok"
	res.AffectedRows = total
	res.DurationMS = time.Since(start).Milliseconds()
	g.log.Printf("incremental %s: %d files updated, %d bars, %s",
		date, files, total, time.Since(start).Round(time.Second))
	return res, nil
}

// syncNames 从 TDX tnf 代码→名称表回填 security.name（GUI 展示用）。
// 名称是可选增强数据：失败仅告警，不影响 ingest 结果状态。
func (g *Ingester) syncNames() {
	names, err := g.reader.ReadNames()
	if err != nil {
		g.log.Printf("warn: read tnf names: %v（名称留空，不影响行情）", err)
		return
	}
	n, err := g.st.SecRepo.UpdateNames(names)
	if err != nil {
		g.log.Printf("warn: update security names: %v", err)
		return
	}
	if n > 0 {
		g.log.Printf("names synced: %d updated (%d known)", n, len(names))
	}
}

// Verify 数据质量校验（FR-104 / 需求D3）：
// 以 sh000001（上证指数）为交易日历权威（ADR-2），判断数据是否新鲜、更新量是否异常。
func (g *Ingester) Verify(ctx context.Context, date string) (contract.DataQuality, error) {
	var q contract.DataQuality
	latest, err := g.st.BarRepo.LatestDate("sh000001")
	if err != nil {
		return q, err
	}
	q.LatestTradeDate = latest
	q.Fresh = latest == date
	// 统计「拥有date当日K线」的股票数
	var one int
	err = g.st.DB().QueryRow(`SELECT COUNT(*) FROM daily_bar WHERE date = ?`, date).Scan(&one)
	if err != nil {
		return q, err
	}
	q.UpdatedCount = one
	return q, nil
}

func today() string { return time.Now().Format("2006-01-02") }
