// Package box 的 scanner.go 实现面向 DB 的每日箱体批处理（contract.BoxScanner）。
//
// 设计依据 docs/02-architecture.md §6 boxscan 增量策略：
//   - 全量（RescanAll）：首次/参数变更时，全市场逐 code 读 K 线窗口 → Engine.Scan → 持久化；
//   - 增量（RescanIncremental）：跟踪池+候选池全量重放（正确性优先，量小）；
//     其余全市场只对「今日创 60 日新高 或 今日量≥均量×倍数 或 最新confirmed箱体下轨被收盘跌破」
//     的股票重放——前两条覆盖突破/箱体形成，第三条是 M8.8 宽度法补的破位兜底：
//     无量阴跌破位不伴随新高/放量，若漏记会让 broken_down 事件延迟入库、市场宽度分母慢性欠账。
package box

import (
	"context"
	"log"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// rescanWindow 重算读取的 K 线窗口长度（交易日）。
// 取 800 覆盖约 3 年历史，足以让引擎从当前箱体 regime 起点完整识别阶梯。
const rescanWindow = 800

// Scanner 面向 DB 的箱体批处理器（contract.BoxScanner）。
type Scanner struct {
	eng contract.BoxEngine
	st  *store.Store
	log *log.Logger
}

// NewScanner 构造。eng 通常为 box.New()（确定性纯函数）。
func NewScanner(eng contract.BoxEngine, st *store.Store) *Scanner {
	return &Scanner{
		eng: eng,
		st:  st,
		log: log.New(log.Writer(), "[boxscan] ", log.LstdFlags),
	}
}

// rescanOne 读单只股票的 K 线窗口 → 引擎扫描 → 持久化箱体。
func (s *Scanner) rescanOne(ctx context.Context, code, date string) (int, error) {
	select {
	case <-ctx.Done():
		return 0, ctx.Err()
	default:
	}
	bars, err := s.st.BarRepo.BarsWindow(code, date, rescanWindow)
	if err != nil {
		return 0, err
	}
	if len(bars) == 0 {
		return 0, nil
	}
	params := s.boxParams()
	boxes := s.eng.Scan(bars, params)
	if len(boxes) == 0 {
		return 0, nil
	}
	if _, err := s.st.BoxRepo.UpsertBoxes(boxes); err != nil {
		return 0, err
	}
	return len(boxes), nil
}

// boxParams 从 param 表读箱体参数（与 0001_init.sql 种子一致）。
func (s *Scanner) boxParams() contract.BoxParams {
	return contract.BoxParams{
		VolMultiplier: s.st.ParamRepo.GetFloat("box.volmult", 2.0),
		VolWindow:     s.st.ParamRepo.GetInt("box.volwindow", 50),
		BreakPremium:  s.st.ParamRepo.GetFloat("buy.premium", 1.03),
		MinBoxWidth:   0.05,
	}
}

// RescanAll 全市场箱体重算（docs/02 §6 全量路径，首次/参数变更时）。
func (s *Scanner) RescanAll(ctx context.Context, date string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "boxscan", Date: date}
	codes, err := s.st.SecRepo.ActiveStockCodes()
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	var total int64
	for i, code := range codes {
		n, err := s.rescanOne(ctx, code, date)
		if err != nil {
			s.log.Printf("rescan %s: %v", code, err)
			continue
		}
		total += int64(n)
		if (i+1)%2000 == 0 {
			s.log.Printf("progress %d/%d codes, %d boxes", i+1, len(codes), total)
		}
	}
	res.Status = "ok"
	res.AffectedRows = total
	res.DurationMS = time.Since(start).Milliseconds()
	s.log.Printf("rescan-all %s: %d codes, %d boxes, %s", date, len(codes), total, time.Since(start).Round(time.Second))
	return res, nil
}

// RescanIncremental 增量重算（docs/02 §6 增量路径，每日盘后）。
// 跟踪池全量重放 + 其余全市场预过滤（新高/放量/破位兜底）。
func (s *Scanner) RescanIncremental(ctx context.Context, date string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "boxscan", Date: date}

	// 1) 跟踪池+候选池关联股票：全量重放（正确性优先）
	poolCodes, err := s.st.SecRepo.ActiveWatchCodes()
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	rescan := map[string]bool{}
	for _, c := range poolCodes {
		rescan[c] = true
	}

	// 2) 其余全市场：预过滤
	allCodes, err := s.st.SecRepo.ActiveStockCodes()
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	volMult := s.st.ParamRepo.GetFloat("box.volmult", 2.0)
	for _, code := range allCodes {
		if rescan[code] {
			continue
		}
		if s.prefilter(ctx, code, date, volMult) {
			rescan[code] = true
		}
	}

	// 3) 重算命中集合
	var total int64
	hit := 0
	for _, code := range allCodes {
		if !rescan[code] {
			continue
		}
		n, err := s.rescanOne(ctx, code, date)
		if err != nil {
			s.log.Printf("incr rescan %s: %v", code, err)
			continue
		}
		total += int64(n)
		hit++
	}
	res.Status = "ok"
	res.AffectedRows = total
	res.DurationMS = time.Since(start).Milliseconds()
	s.log.Printf("rescan-incr %s: %d/%d codes hit, %d boxes, %s", date, hit, len(allCodes), total, time.Since(start).Round(time.Second))
	return res, nil
}

// prefilter 增量预过滤（docs/02 §6 + M8.8 破位兜底）：
// 今日创 60 日新高、今日量≥均量×倍数、或最新 confirmed 箱体下轨被今日收盘跌破。
// 任一成立即需重算；否则跳过。
func (s *Scanner) prefilter(ctx context.Context, code, date string, volMult float64) bool {
	select {
	case <-ctx.Done():
		return false
	default:
	}
	bars, err := s.st.BarRepo.BarsWindow(code, date, 60)
	if err != nil || len(bars) == 0 {
		return false
	}
	today := bars[len(bars)-1]
	// 今日创 60 日新高：今日 high 严格高于窗口前 59 日的最高 high
	maxPrev := 0.0
	for i := 0; i < len(bars)-1; i++ {
		if bars[i].High > maxPrev {
			maxPrev = bars[i].High
		}
	}
	if today.High > maxPrev && maxPrev > 0 {
		return true
	}
	// 今日量≥均量×倍数（放量）
	if avg, err := s.st.BarRepo.AvgVolume(code, date, 50); err == nil && avg > 0 &&
		float64(today.Volume) >= avg*volMult {
		return true
	}
	// M8.8 破位兜底：最新 confirmed 箱体下轨被今日收盘跌破。
	// 只兜 confirmed → broken_down 这一次迁移；broken_down 后引擎通常转为 forming，
	// 不会因价格持续低于旧下轨而每日重复重算。
	lb, err := s.st.BoxRepo.LatestNonFormingBox(code)
	if err != nil || lb == nil {
		return false
	}
	return lb.Status == contract.BoxConfirmed && lb.Bottom > 0 && today.Close < lb.Bottom
}
