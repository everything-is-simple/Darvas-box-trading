// Package box 实现达瓦斯箱体理论的三日法则状态机（docs/00-darvas-theory.md §3）。
//
// 设计依据 ADR-1（docs/02 §5）：箱体引擎=确定性纯函数，
// 「K线序列 → 箱体序列」同一输入必得同一输出，DB 仅是结果的持久化。
// 这使单元测试即规范（含书中案例断言）、重跑幂等天然成立。
package box

import (
	"math"

	"darvas/internal/contract"
)

// Engine 实现达瓦斯三日法则箱体状态机（contract.BoxEngine）。
// Scan 是零 IO 的纯函数：可被任意并发调用，结果只取决于输入。
type Engine struct{}

// New 构造一个无状态引擎。
func New() Engine { return Engine{} }

// Scan 把单只股票的升序 K 线序列扫描为箱体序列（阶梯）。
//
// 实现的状态机（docs/00-darvas-theory.md §3.3）：
//
//	forming(确认上轨中) ──3日无新高──→ forming(确认下轨中) ──3日无新低+宽度达标──→ confirmed
//	    │ 创新高(重置)            │ 创新低(重置)                           │
//	    │                        │ 下轨确立前创新高→invalid,从新高重启    │ 收盘>上轨×premium且放量且非一字板
//	    │                        ↓                                       ↓ → broken_up(新箱体,prev=本箱)
//	    │                                                              │ 收盘<下轨
//	    │                                                              ↓ → broken_down(止损信号)
//
// 输出按时间顺序的箱体切片，ID 为本切片内局部序号（1..N）；
// PrevBoxID 指向「最近一个达到 confirmed 的箱体」的局部 ID（0=无），
// 由此形成阶梯链（invalid 箱体不计入阶梯，被跳过）。
func (Engine) Scan(bars []contract.DailyBar, params contract.BoxParams) []contract.Box {
	if len(bars) == 0 {
		return nil
	}
	s := &scanner{params: params, code: bars[0].Code}
	for i := range bars {
		s.step(i, bars)
	}
	s.finish()
	return s.boxes
}

// scanner 单只股票的扫描状态（Scan 期间私有，不跨调用共享）。
type scanner struct {
	params contract.BoxParams
	code  string

	boxes          []contract.Box
	nextID         int64
	lastConfirmed  int64 // 最近一个达到 confirmed 的箱体局部 ID（阶梯锚点）
	cur            *boxState
}

// boxState 当前正在形成的箱体的可变状态。
type boxState struct {
	id                 int64
	code               string
	topCandidate       float64 // 上轨候选（forming 期间最高 high）
	topCount           int     // 距上次新高的交易日数
	topConfirmedDate   string  // 非空=上轨已确认
	top                float64 // 上轨确认值
	bottomCandidate    float64 // 下轨候选（上轨确认后最低 low）
	bottomCount        int     // 距上次新低的交易日数
	bottomConfirmedDate string // 非空=下轨已确认
	bottom             float64 // 下轨确认值
	status             contract.BoxStatus
	prevBoxID          int64
	brokenDate         string
}

// step 处理第 i 根 K 线，推进当前箱体状态机。
func (s *scanner) step(i int, bars []contract.DailyBar) {
	b := bars[i]
	if s.cur == nil {
		s.startBox(i, b)
		return
	}
	cur := s.cur

	// ── 阶段一：确认上轨中（docs/00 §3.1）──
	if cur.topConfirmedDate == "" {
		if b.High > cur.topCandidate {
			// 又创新高 → 更新候选，重新计数（§3.1.3）
			cur.topCandidate = b.High
			cur.topCount = 0
		} else {
			cur.topCount++
			if cur.topCount >= 3 {
				// 连续3日未再创新高 → 上轨确认（§3.1.2）
				cur.topConfirmedDate = b.Date
				cur.top = cur.topCandidate
				// 上轨确认日开始跟踪下轨
				cur.bottomCandidate = b.Low
				cur.bottomCount = 0
			}
		}
		return
	}

	// ── 阶段二：确认下轨中（docs/00 §3.2）──
	if cur.bottomConfirmedDate == "" {
		// 下轨确立前创出新高 → 箱体作废，从新高重新开始（§3.2.4）
		if b.High > cur.top {
			s.invalidateAndRestart(i, b)
			return
		}
		if b.Low < cur.bottomCandidate {
			// 又创新低 → 更新候选，重新计数（§3.2.3）
			cur.bottomCandidate = b.Low
			cur.bottomCount = 0
		} else {
			cur.bottomCount++
			if cur.bottomCount >= 3 {
				// 连续3日未再创新低 → 下轨确认（§3.2.2）
				cur.bottomConfirmedDate = b.Date
				cur.bottom = cur.bottomCandidate
				// 宽度校验：top>bottom 且 (top-bottom)/top >= MinBoxWidth（ERD §2.1）
				if cur.top > cur.bottom && (cur.top-cur.bottom)/cur.top >= s.params.MinBoxWidth {
					cur.status = contract.BoxConfirmed
					s.lastConfirmed = cur.id
				} else {
					// 宽度不足 → 箱体无效，从当前重新开始
					s.invalidateAndRestart(i, b)
				}
			}
		}
		return
	}

	// ── 阶段三：箱体已确认，等待突破或跌破（docs/00 §3.3 / §4.2 / §5.1）──
	// 先判跌破（更严格：收盘价<下轨即止损信号）
	if b.Close < cur.bottom {
		cur.status = contract.BoxBrokenDown
		cur.brokenDate = b.Date
		s.emit(cur)
		s.startBox(i, b)
		return
	}
	// 再判突破：收盘>上轨×premium 且 当日量≥VolWindow日均量×VolMultiplier 且 非一字板（§4.2 / FR-203）
	if s.isBreakout(i, bars, cur) {
		cur.status = contract.BoxBrokenUp
		cur.brokenDate = b.Date
		s.emit(cur)
		s.startBox(i, b)
		return
	}
	// 否则箱体保持 confirmed，等待
}

// startBox 从第 i 根 K 线开始一个新箱体（forming）。
// 每根 K 线必属且仅属一个箱体：突破/跌破/作废当日即开启下一个箱体。
func (s *scanner) startBox(i int, b contract.DailyBar) {
	s.nextID++
	s.cur = &boxState{
		id:           s.nextID,
		code:         s.code,
		topCandidate: b.High,
		topCount:     0,
		status:       contract.BoxForming,
		prevBoxID:    s.lastConfirmed, // 阶梯锚点=最近 confirmed 箱体（invalid 被跳过）
	}
}

// invalidateAndRestart 把当前箱体标记为 invalid 并立即从当前 K 线重启新箱体。
func (s *scanner) invalidateAndRestart(i int, b contract.DailyBar) {
	s.cur.status = contract.BoxInvalid
	s.emit(s.cur)
	s.startBox(i, b)
}

// emit 把当前箱体的快照追加到输出（按值拷贝，之后 cur 可被新箱体复用）。
func (s *scanner) emit(cur *boxState) {
	s.boxes = append(s.boxes, contract.Box{
		ID:              cur.id,
		Code:            cur.code,
		Top:             cur.top,
		Bottom:          cur.bottom,
		TopConfirmed:    cur.topConfirmedDate,
		BottomConfirmed: cur.bottomConfirmedDate,
		Status:          cur.status,
		PrevBoxID:       cur.prevBoxID,
		BrokenDate:      cur.brokenDate,
	})
}

// finish 把仍在形成中的当前箱体（forming/confirmed 未破）作为「当前状态」输出。
func (s *scanner) finish() {
	if s.cur != nil {
		s.emit(s.cur)
	}
}

// isBreakout 判定第 i 日是否构成有效向上突破（docs/00 §4.2 / FR-203）：
//   - 收盘价 > 上轨 × BreakPremium（默认 1.03，突破确认溢价）
//   - 当日成交量 ≥ VolWindow 日均量 × VolMultiplier（默认 50 日×2.0，放量确认）
//   - 非一字板（一字涨停追不进，§7 涨停板细分）
//
// 均量基准取突破日「之前」VolWindow 个交易日的均值（不含突破日本身）。
func (s *scanner) isBreakout(i int, bars []contract.DailyBar, cur *boxState) bool {
	b := bars[i]
	if b.Close <= cur.top*s.params.BreakPremium {
		return false
	}
	avg := avgVolumeBefore(bars, i, s.params.VolWindow)
	if avg <= 0 {
		return false // 历史样本不足，无法确认放量
	}
	if float64(b.Volume) < avg*s.params.VolMultiplier {
		return false // 无量突破=假信号，忽略（§4.2）
	}
	if isOnePriceBoard(b) {
		return false // 一字板追不进，放弃（§7）
	}
	return true
}

// avgVolumeBefore 第 i 根 K 线之前 min(n, i) 根 K 线的成交量均值。
func avgVolumeBefore(bars []contract.DailyBar, i, n int) float64 {
	if i <= 0 || n <= 0 {
		return 0
	}
	start := i - n
	if start < 0 {
		start = 0
	}
	var sum float64
	cnt := 0
	for k := start; k < i; k++ {
		sum += float64(bars[k].Volume)
		cnt++
	}
	if cnt == 0 {
		return 0
	}
	return sum / float64(cnt)
}

// isOnePriceBoard 一字板判定：开=高=低=收（盘中只有单一价格，涨停/跌停锁定追不进）。
// 价格来自 TDX uint32/100，用 1e-9 容差规避浮点尾差。
func isOnePriceBoard(b contract.DailyBar) bool {
	const eps = 1e-9
	return math.Abs(b.Open-b.High) < eps &&
		math.Abs(b.High-b.Low) < eps &&
		math.Abs(b.Low-b.Close) < eps
}

// DefaultParams 返回与 docs/01-requirements §6 / 0001_init.sql param 种子一致的默认箱体参数。
func DefaultParams() contract.BoxParams {
	return contract.BoxParams{
		VolMultiplier: 2.0,
		VolWindow:     50,
		BreakPremium:  1.03,
		MinBoxWidth:   0.05,
	}
}
