// Package screener 实现达瓦斯初选四条件（docs/00-darvas-theory.md §6）。
//
// 四条件（FR-301 / docs/04 ScreenParams）：
//  1. 流动性：5 日均成交额 ≥ turnoverMin（默认 5000 万）——只买流动性好的主板股
//  2. 相对强度：20 日超额收益（个股涨幅−上证涨幅）≥ rs_excess_min（默认 0.20）——强于大盘/熊市抗跌
//     （A1 2026-10-22：旧「个股涨幅/上证涨幅≥1.2」比值法在市场极值处失真——深熊分母塌缩、
//      温和熊市筛掉「抗跌但仍在跌」的股票，已退役；机制描述以 docs/00 §6 注2 为准）
//  3. 放量比：5 日均量/20 日均量 ∈ [volRatioMin, volRatioMax]（默认 [2.0, 5.0]）——成交量放大
//  4. 箱体形态：box 状态 confirmed 或刚 broken_up——上升箱体形成中或刚突破
//
// 输出：candidate 快照（append-only），每条件 0-25 分，合计 score 降序。
// 条件 4（箱体形态）为达瓦斯核心，作为候选门槛；其余三条件加分排序。
package screener

import (
	"context"
	"encoding/json"
	"log"
	"sort"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// Screener 初选器（contract.Screener）。
type Screener struct {
	st  *store.Store
	log *log.Logger
}

// New 构造。
func New(st *store.Store) *Screener {
	return &Screener{st: st, log: log.New(log.Writer(), "[screen] ", log.LstdFlags)}
}

// params 从 param 表读初选参数（与 0001_init.sql 种子一致）。
func (s *Screener) params() contract.ScreenParams {
	return contract.ScreenParams{
		TurnoverMin: s.st.ParamRepo.GetFloat("screen.turnover", 50000000),
		RSWindow:    s.st.ParamRepo.GetInt("screen.rswindow", 20),
		RSExcessMin: s.st.ParamRepo.GetFloat("screen.rs_excess_min", 0.20),
		VolRatioMin: 2.0,
		LateDays:    s.st.ParamRepo.GetInt("screen.latedays", 4),
	}
}

// Screen 对全市场活跃股票执行四条件初选，输出打分排序的 candidate 快照。
func (s *Screener) Screen(ctx context.Context, date string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "screen", Date: date}
	p := s.params()

	codes, err := s.st.SecRepo.ActiveStockCodes()
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	// 上证指数 20 日涨幅（RS 超额收益基准，ADR-2 交易日历权威）
	idxRet, ok := retOver(s.st, "sh000001", date, p.RSWindow)
	if !ok {
		// 上证数据不足 → 视大盘走平，超额收益退化为个股自身涨幅
		idxRet = 1.0
	}

	var cands []contract.Candidate
	for _, code := range codes {
		select {
		case <-ctx.Done():
			res.Status, res.Error = "failed", ctx.Err().Error()
			return res, ctx.Err()
		default:
		}
		reasons, score := s.evaluate(code, date, p, idxRet)
		// 条件 4（箱体形态）为候选门槛：不满足则不计入候选池
		if _, boxOK := reasons["box_pass"]; !boxOK {
			continue
		}
		if score <= 0 {
			continue
		}
		cands = append(cands, contract.Candidate{
			ScreenDate:      date,
			Code:            code,
			Score:           score,
			Reasons:         reasons,
			FundamentalFlag: "unknown",
		})
	}
	// 按分数降序
	sort.Slice(cands, func(i, j int) bool { return cands[i].Score > cands[j].Score })

	// 持久化快照（append-only）
	if err := s.st.PoolRepo.AppendCandidates(date, cands); err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	res.Status = "ok"
	res.AffectedRows = int64(len(cands))
	res.DurationMS = time.Since(start).Milliseconds()
	s.log.Printf("screen %s: %d/%d stocks passed, %s", date, len(cands), len(codes), time.Since(start).Round(time.Millisecond))
	return res, nil
}

// evaluate 对单只股票逐条评估，返回 reasons 明细与 score。
func (s *Screener) evaluate(code, date string, p contract.ScreenParams, idxRet float64) (map[string]any, float64) {
	r := map[string]any{}
	score := 0.0

	// 条件1 流动性：5 日均成交额 ≥ turnoverMin
	avgAmt, err := s.st.BarRepo.AvgAmount(code, date, 5)
	liquidityOK := err == nil && avgAmt >= p.TurnoverMin
	r["liquidity_avg_amt"] = avgAmt
	r["liquidity_pass"] = liquidityOK
	if liquidityOK {
		score += 25
	}

	// 条件2 RS：20 日超额收益 = 个股涨幅 − 上证涨幅 ≥ rs_excess_min（A1，2026-10-22，docs/21 F7）。
	// 旧算法 rs=个股涨幅/上证涨幅（比值）在市场极值处失真：深熊分母塌缩（指数 -50% 时跌 40% 的
	// 股票也能过 1.2 阈值）、温和熊市把「抗跌但仍在跌」的股票筛掉（大盘 -20%、个股 -5% →
	// 1.19 < 1.2）——与达瓦斯「熊市找抗跌股」（L1273「股市下跌时它们能逆市而动，风向变化时
	// 迅速上涨的也将是这些股票」）冲突（docs/00 §6 注2）。超额收益语义：大盘跌 20%、它只跌
	// 5% → 超额 +15pp，正是「跌得很不情愿」的量化，且阈值以百分点直接可解释。
	// retOver 返回比值（如 1.15），差值即超额幅度（0.30=+30pp）。
	if stockRet, ok := retOver(s.st, code, date, p.RSWindow); ok {
		rs := stockRet - idxRet
		rsOK := rs >= p.RSExcessMin
		r["rs_value"] = rs
		r["rs_pass"] = rsOK
		if rsOK {
			score += 25
		}
	} else {
		r["rs_pass"] = false
	}

	// 条件3 放量比：5 日均量/20 日均量 ≥ min（A1 校订 2026-10-22：删上限，原书只要求「显著放大」）
	avg5, e1 := s.st.BarRepo.AvgVolume(code, date, 5)
	avg20, e2 := s.st.BarRepo.AvgVolume(code, date, 20)
	volOK := false
	var ratio float64
	if e1 == nil && e2 == nil && avg20 > 0 {
		ratio = avg5 / avg20
		volOK = ratio >= p.VolRatioMin
	}
	r["vol_ratio"] = ratio
	r["vol_pass"] = volOK
	if volOK {
		score += 25
	}

	// 条件4 箱体形态：confirmed 或刚 broken_up（达瓦斯核心，候选门槛）
	// B2① 太晚规则（原书：股价四天都收在突破点之上=看到太晚，放弃）：
	//   broken_up 箱体突破后已走过 ≥ LateDays 个交易日 → 不入候选池。
	// 修复 ST4（2026-10-21）：LatestBox → LatestNonFormingBox。archive/07 #1 修复 signal
	// 时漏改此处——LatestBox 取 id DESC 第一条 forming(0,0)，boxOK 恒 false，
	// 导致「刚 broken_up」分支长期死掉（实测 10-19 候选池 166 支全 confirmed、0 broken_up）。
	if box, err := s.st.BoxRepo.LatestNonFormingBox(code); err == nil && box != nil {
		boxOK := box.Status == contract.BoxConfirmed || box.Status == contract.BoxBrokenUp
		if boxOK && box.Status == contract.BoxBrokenUp && box.BrokenDate != "" && p.LateDays > 0 {
			if n, err := s.st.BarRepo.BarCount(code, box.BrokenDate, date); err == nil && n >= p.LateDays {
				r["too_late_days"] = n
				boxOK = false
			}
		}
		r["box_status"] = string(box.Status)
		r["box_top"] = box.Top
		r["box_bottom"] = box.Bottom
		if boxOK {
			r["box_pass"] = true
			score += 25
		}
	}
	return r, score
}

// retOver 计算 code 截至 date 的 n 日涨幅（close[n日前]→close[date]）。
// 返回 (涨幅比 close_today/close_n_ago, ok)；数据不足时 ok=false。
func retOver(st *store.Store, code, date string, n int) (float64, bool) {
	bars, err := st.BarRepo.BarsWindow(code, date, n+1)
	if err != nil || len(bars) < 2 {
		return 0, false
	}
	first := bars[0].Close
	last := bars[len(bars)-1].Close
	if first <= 0 {
		return 0, false
	}
	return last / first, true
}

// reasonsJSON 把 reasons map 序列化为 JSON（candidate.reasons_json 存储用）。
func reasonsJSON(r map[string]any) string {
	b, err := json.Marshal(r)
	if err != nil {
		return "{}"
	}
	return string(b)
}
