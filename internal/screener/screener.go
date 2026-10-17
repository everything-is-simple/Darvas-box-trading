// Package screener 实现达瓦斯初选四条件（docs/00-darvas-theory.md §6）。
//
// 四条件（FR-301 / docs/04 ScreenParams）：
//  1. 流动性：5 日均成交额 ≥ turnoverMin（默认 5000 万）——只买流动性好的主板股
//  2. 相对强度：20 日 RS（个股涨幅/上证涨幅）≥ rsthreshold（默认 1.2）——强于大盘
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
		RSThreshold: s.st.ParamRepo.GetFloat("screen.rsthreshold", 1.2),
		VolRatioMin: 2.0,
		VolRatioMax: 5.0,
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
	// 上证指数 20 日涨幅（RS 分母，ADR-2 交易日历权威）
	idxRet, ok := retOver(s.st, "sh000001", date, p.RSWindow)
	if !ok {
		// 上证数据不足，RS 条件无法判定 → 全部股票 RS 记 0
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

	// 条件2 RS：20 日个股涨幅 / 上证涨幅 ≥ rsthreshold
	if stockRet, ok := retOver(s.st, code, date, p.RSWindow); ok {
		var rs float64
		if idxRet > 0 {
			rs = stockRet / idxRet
		}
		rsOK := rs >= p.RSThreshold
		r["rs_value"] = rs
		r["rs_pass"] = rsOK
		if rsOK {
			score += 25
		}
	} else {
		r["rs_pass"] = false
	}

	// 条件3 放量比：5 日均量/20 日均量 ∈ [min, max]
	avg5, e1 := s.st.BarRepo.AvgVolume(code, date, 5)
	avg20, e2 := s.st.BarRepo.AvgVolume(code, date, 20)
	volOK := false
	var ratio float64
	if e1 == nil && e2 == nil && avg20 > 0 {
		ratio = avg5 / avg20
		volOK = ratio >= p.VolRatioMin && ratio <= p.VolRatioMax
	}
	r["vol_ratio"] = ratio
	r["vol_pass"] = volOK
	if volOK {
		score += 25
	}

	// 条件4 箱体形态：confirmed 或刚 broken_up（达瓦斯核心，候选门槛）
	// B2① 太晚规则（原书：股价四天都收在突破点之上=看到太晚，放弃）：
	//   broken_up 箱体突破后已走过 ≥ LateDays 个交易日 → 不入候选池。
	if box, err := s.st.BoxRepo.LatestBox(code); err == nil && box != nil {
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
