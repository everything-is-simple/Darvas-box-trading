// Package ingest 负责 TDX vipdoc → SQLite 的全量/增量导入与质量校验。
// 对应需求 FR-101~104（docs/01-requirements.md §5.1）。
package ingest

import (
	"context"
	"log"
	"strings"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// Ingester 导入器（docs/04-api-contract.md §3.2）。
type Ingester struct {
	reader contract.DayFileReader
	st     *store.Store
	// FullSince 全量导入起始日：箱体引擎+52周新高+RS窗口最多需要约2年历史
	FullSince string
	// SinceDriver 增量读取时的兜底起点
	log *log.Logger
}

// New 构造。fullSince 为空时默认 "2024-01-01"。
func New(reader contract.DayFileReader, st *store.Store, fullSince string) *Ingester {
	if fullSince == "" {
		fullSince = "2024-01-01"
	}
	return &Ingester{
		reader:     reader,
		st:         st,
		FullSince:  fullSince,
		log:        log.New(log.Writer(), "[ingest] ", log.LstdFlags),
	}
}

// FullImport 全量导入（幂等，INSERT OR REPLACE）。
func (g *Ingester) FullImport(ctx context.Context) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "ingest", Date: today()}
	codes, err := g.reader.ListCodes()
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	var total int64
	for i, code := range codes {
		select {
		case <-ctx.Done():
			res.Status, res.Error = "failed", ctx.Err().Error()
			return res, ctx.Err()
		default:
		}
		bars, err := g.reader.ReadBars(code, g.FullSince)
		if err != nil {
			continue // 单文件失败不阻断全量（质量报告统计）
		}
		if len(bars) == 0 {
			continue
		}
		if err := g.upsertSecurity(code, bars[0].Date); err != nil {
			g.log.Printf("security %s: %v", code, err)
		}
		n, err := g.st.BarRepo.UpsertBars(bars)
		if err != nil {
			g.log.Printf("bars %s: %v", code, err)
			continue
		}
		total += n
		if (i+1)%500 == 0 {
			g.log.Printf("progress %d/%d codes, %d bars", i+1, len(codes), total)
		}
	}
	res.Status = "ok"
	res.AffectedRows = total
	res.DurationMS = time.Since(start).Milliseconds()
	g.log.Printf("full import done: %d codes, %d bars, %s", len(codes), total, time.Since(start).Round(time.Second))
	return res, nil
}

// SyncIncremental 增量导入：只读「文件修改时间在当日零点之后」的文件，自库内最新日期续读。
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
	for _, code := range codes {
		select {
		case <-ctx.Done():
			res.Status, res.Error = "failed", ctx.Err().Error()
			return res, ctx.Err()
		default:
		}
		mt, err := g.reader.ModTime(code)
		if err != nil || mt.Before(midnight) {
			continue // 今日未更新的文件跳过
		}
		files++
		latest, err := g.st.BarRepo.LatestDate(code)
		if err != nil || latest == "" {
			latest = g.FullSince
		}
		bars, err := g.reader.ReadBars(code, latest)
		if err != nil || len(bars) == 0 {
			continue
		}
		if err := g.upsertSecurity(code, ""); err != nil {
			g.log.Printf("security %s: %v", code, err)
		}
		n, err := g.st.BarRepo.UpsertBars(bars)
		if err != nil {
			continue
		}
		total += n
	}
	res.Status = "ok"
	res.AffectedRows = total
	res.DurationMS = time.Since(start).Milliseconds()
	g.log.Printf("incremental %s: %d files updated, %d bars, %s", date, files, total, time.Since(start).Round(time.Second))
	return res, nil
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

// upsertSecurity 从代码推导证券基础属性（FR-103：板块/类型识别）。
func (g *Ingester) upsertSecurity(code, firstDate string) error {
	market := "sh"
	if strings.HasPrefix(code, "sz") {
		market = "sz"
	}
	num := strings.TrimPrefix(strings.TrimPrefix(code, "sh"), "sz")
	kind, board := classify(market, num)
	_, err := g.st.DB().Exec(`INSERT INTO security(code, market, kind, board, list_date)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(code) DO NOTHING`,
		code, market, kind, board, nullEmpty(firstDate))
	return err
}

// classify 按市场前缀+数字代码分类（docs/00-darvas-theory.md §7 A股适配）。
func classify(market, num string) (kind, board string) {
	if len(num) != 6 {
		return "other", "other"
	}
	// 指数：sh000xxx / sz399xxx
	if (market == "sh" && strings.HasPrefix(num, "000")) ||
		(market == "sz" && strings.HasPrefix(num, "399")) {
		return "index", "index"
	}
	switch {
	case strings.HasPrefix(num, "60"): // 沪主板
		return "stock", "main"
	case strings.HasPrefix(num, "68"): // 科创板
		return "stock", "star"
	case strings.HasPrefix(num, "30"): // 创业板
		return "stock", "gem"
	case strings.HasPrefix(num, "00"): // 深主板（000/001/002/003）
		return "stock", "main"
	case strings.HasPrefix(num, "20"): // B股
		return "stock", "b"
	default:
		return "other", "other"
	}
}

func nullEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func today() string { return time.Now().Format("2006-01-02") }
