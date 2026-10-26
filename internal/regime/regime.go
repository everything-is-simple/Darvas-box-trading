// Package regime 实现市场体制标定与事后分体制统计。
//
// M8.8 标定规则（archive/13，宪法第 0 条「众人恐慌我更恐慌，众人贪婪我更贪婪」）：
// 不向外求（不看指数箱体/动量公式/人工标签），只用系统自身 box.Scan 扫出的全市场
// 个股 broken_up/broken_down 事件流：20 交易日滚动宽度比率 ratio=ΣU/(ΣU+ΣD)，
// 连续 3 日 ratio≥bull_thresh 判牛（启用主力仓），连续 3 日 <bear_thresh 细分熊
// （熊猴仓位规则相同，仅日志区分）。
//
// 零未来信息：ClassifyDate 只统计 broken_date ≤ 当日且落在滚动窗口内的完成事件。
//
// 历史：M8.3 曾用动量法；M8.7 改用上指箱体法（2026-10-20 证伪退役，archive/12）；
// M8.8 改为内生宽度法。LabelBoxes/Classify 保留供旧测试与报告函数引用，勿再用于实盘标定。
package regime

import (
	"context"
	"fmt"
	"html/template"
	"math"
	"os"
	"path/filepath"

	"darvas/internal/box"
	"darvas/internal/contract"
	"darvas/internal/store"
)

// Regime 市场体制。
type Regime string

const (
	Bull  Regime = "bull"  // 牛（上行趋势）
	Bear  Regime = "bear"  // 熊（下行趋势）
	Range Regime = "range" // 猴（震荡）
)

// cn 体制中文名（报告展示）。
func (r Regime) cn() string {
	switch r {
	case Bull:
		return "牛"
	case Bear:
		return "熊"
	default:
		return "猴"
	}
}

// Classify 保留但已废弃（动量法，2026-10-19 用户拍板改用箱体法 LabelBoxes）。
// 仅 LabelBoxes 用于体制标注，Classify 仅供向后兼容的测试引用，勿再使用。
func Classify(momentum, bullThresh, bearThresh float64) Regime {
	if momentum > bullThresh {
		return Bull
	}
	if momentum < -bearThresh {
		return Bear
	}
	return Range
}

// LabelBoxes 用达瓦斯箱体引擎给指数序列标注体制（单一理论，复用 box engine）：
//
//	指数向上突破箱体（broken_up）   → 牛（上涨市场）
//	指数向下破位（broken_down）     → 熊（下跌市场）
//	箱体内（forming/confirmed）     → 猴（震荡）
//
// 突破/破位是事件（BrokenDate 当日判定，只用当日及之前数据，禁摘樱桃）；
// 事件后延续该方向，直到反向事件。bars 为 sh000001 升序 K 线。
func LabelBoxes(bars []contract.DailyBar, params contract.BoxParams) map[string]Regime {
	boxes := box.New().Scan(bars, params)
	// 提取突破/破位事件（boxes 按时间顺序 emit，事件天然有序）
	type event struct {
		date   string
		regime Regime
	}
	var events []event
	for _, b := range boxes {
		if b.BrokenDate == "" {
			continue
		}
		switch b.Status {
		case contract.BoxBrokenUp:
			events = append(events, event{b.BrokenDate, Bull})
		case contract.BoxBrokenDown:
			events = append(events, event{b.BrokenDate, Bear})
		}
	}
	labels := make(map[string]Regime, len(bars))
	cur := Range
	ei := 0
	for _, b := range bars {
		for ei < len(events) && events[ei].date <= b.Date {
			cur = events[ei].regime
			ei++
		}
		labels[b.Date] = cur
	}
	return labels
}

// DefaultIndex 交易日历权威（ADR-2）。M8.8 不再用它标指数箱体（M8.7 已退役，archive/12），
// 只取 sh000001 的日期序列作为「交易日」定义；行情标定完全来自个股箱体事件流。
var DefaultIndex = "sh000001"

// ClassifyDate 实时标注某日的市场体制（M8.8 宽度法）：
// 不读指数箱体、不用动量公式；改查系统自身 box 表的全市场个股 broken_up/broken_down
// 事件流，按 20 交易日滚动窗口算 ratio，末 confirm 日连续 ≥ bull_thresh 判牛，
// 连续 < bear_thresh 细分熊（熊猴仓位规则相同，仅日志区分）。
//
// 零未来信息：只统计 broken_date ≤ date 且落在滚动窗口内的完成事件；
// params/indices 参数保留仅为向后兼容调用方，内部忽略（M8.7 指数法退役，archive/12/13）。
func ClassifyDate(st *store.Store, date string, params contract.BoxParams, indices []string) Regime {
	_ = params
	_ = indices
	window := st.ParamRepo.GetInt("regime.breadth.window", 20)
	bullThresh := st.ParamRepo.GetFloat("regime.breadth.bull_thresh", 0.55)
	bearThresh := st.ParamRepo.GetFloat("regime.breadth.bear_thresh", 0.40)
	confirm := st.ParamRepo.GetInt("regime.breadth.confirm", 3)
	minHistory := st.ParamRepo.GetInt("regime.breadth.min_history", 60)
	if window <= 0 || confirm <= 0 {
		return Range
	}

	// 交易日历仍以 sh000001 为权威（ADR-2），但只取日期，不读指数行情/箱体。
	span := window + confirm - 1
	cal, err := st.BarRepo.BarsRange(DefaultIndex, "", date, span)
	if err != nil || len(cal) < span {
		return Range // 历史不足以形成 window+confirm-1 个交易日：中性
	}
	dates := make([]string, len(cal))
	for i, b := range cal {
		dates[i] = b.Date
	}
	up, down, err := st.BoxRepo.BreadthAgg(dates[0], date, minHistory)
	if err != nil {
		return Range
	}

	su, sd := 0, 0
	ratios := make([]float64, 0, confirm)
	for i, d := range dates {
		su += up[d]
		sd += down[d]
		if i >= window {
			old := dates[i-window]
			su -= up[old]
			sd -= down[old]
		}
		if i < window-1 {
			continue
		}
		ratio := 0.0 // 与 scripts/dbg_breadth 口径一致：窗口无事件 = 0，偏保守（不误触牛）
		if su+sd > 0 {
			ratio = float64(su) / float64(su+sd)
		}
		if len(dates)-i <= confirm {
			ratios = append(ratios, ratio)
		}
	}
	if len(ratios) < confirm {
		return Range
	}
	bull, bear := true, true
	for _, r := range ratios {
		if r < bullThresh {
			bull = false
		}
		if r >= bearThresh {
			bear = false
		}
	}
	if bull {
		return Bull
	}
	if bear {
		return Bear
	}
	return Range
}

// ClassifyDateA 标注某日的市场趋势（A 判据：60 日低点相邻日比，2026-10-23）。
//
// 算法（移植自 scripts/regime_sizing_backtest.py load_index_labels，达瓦斯箱体语言：
// 指数 N 日低点逐级抬升=升势，逐级下移=降势，否则震荡）：
//
//	cur  = min(截至当日含的最近 lookback 个 low)
//	prev = min(截至前一交易日的最近 lookback 个 low)
//	升势(Bull)  = cur > prev × up_bias
//	降势(Bear)  = cur < prev × down_bias
//	震荡(Range) = 其余
//
// 零未来信息：只读 date ≤ 当日的 K 线（ADR-2，sh000001 为交易日历权威）。
// 历史不足 lookback+1 个交易日返回 Range（保守中性，不放大仓位，与 Python i<lookback 同口径）。
//
// 用途：planner A M1 仓位规则——升势日首买 2 单位、其余 1 单位（不择时、不做熊市闸门）。
// 与 M8.8 宽度法 ClassifyDate 互不替代：本函数是「速度计」（量下轨移动速度），
// 专用于仓位缩放；宽度法仍供分体制报告使用，但 planner 不再据此择时。
//
// 参数（默认与 Python 一致，未在 migration 种子化时取默认值，保证回测可复现）：
//
//	regime.a.lookback   默认 60    回看窗口长度（含当日）
//	regime.a.up_bias    默认 1.002 升势阈值：cur > prev × 此值
//	regime.a.down_bias  默认 0.998 降势阈值：cur < prev × 此值
func ClassifyDateA(st *store.Store, date string) Regime {
	lookback := st.ParamRepo.GetInt("regime.a.lookback", 60)
	upBias := st.ParamRepo.GetFloat("regime.a.up_bias", 1.002)
	downBias := st.ParamRepo.GetFloat("regime.a.down_bias", 0.998)
	if lookback <= 0 {
		return Range
	}
	// 取 lookback+1 根 K 线（末根=当日）；BarsWindow 升序返回。
	bars, err := st.BarRepo.BarsWindow(DefaultIndex, date, lookback+1)
	if err != nil || len(bars) < lookback+1 {
		return Range
	}
	// bars[0..lookback]（共 lookback+1 根）：
	//   cur  = min(bars[1..lookback].Low)  ← 含当日，lookback 个
	//   prev = min(bars[0..lookback-1].Low) ← 不含当日，lookback 个
	cur, prev := math.MaxFloat64, math.MaxFloat64
	for i := 0; i < len(bars); i++ {
		lo := bars[i].Low
		if i >= 1 && lo < cur {
			cur = lo
		}
		if i <= lookback-1 && lo < prev {
			prev = lo
		}
	}
	if cur > prev*upBias {
		return Bull
	}
	if cur < prev*downBias {
		return Bear
	}
	return Range
}

// CloseVsMA 返回 date 当日 sh000001 的收盘与其 n 日简单均线（SMA，含当日）。
//
// 数据通路与零未来信息纪律与 ClassifyDateA 完全一致：BarsWindow(DefaultIndex, date, n)
// 取「≤ date 的最近 n 根」升序窗口，末根即当日。历史不足 n 根返回 ok=false（保守口径，
// 调用方应视为「闸门不触发」——与 ClassifyDateA 历史不足返 Range 的处理一致）。
//
// 用途（2026-10-26，P0-3 B 类）：planner 的 MA60 闸门实验（gate.ma60.mode=half/stop），
// 判断「上证收盘是否低于 MA60」。仅为归因后的 B 类规则验证服务，不引入新的择时信号。
func CloseVsMA(st *store.Store, date string, n int) (close, ma float64, ok bool) {
	if n <= 0 {
		return 0, 0, false
	}
	bars, err := st.BarRepo.BarsWindow(DefaultIndex, date, n)
	if err != nil || len(bars) < n {
		return 0, 0, false
	}
	sum := 0.0
	for _, b := range bars {
		sum += b.Close
	}
	return bars[len(bars)-1].Close, sum / float64(n), true
}

// Segment 连续同体制的一段。
type Segment struct {
	Regime     Regime
	Start      string
	End        string
	Days       int
	Return     float64 // 段内权益收益率（End/Start - 1）
	MaxDD      float64 // 段内最大回撤（最负 drawdown）
	Fills      int
	Wins       int
	Losses     int
	ExposedDay int // 段内有持仓（市值>0）的天数
}

// Stat 体制聚合汇总（同体制多段合并）。
type Stat struct {
	Regime     Regime
	Segments   int
	Days       int
	Return     float64 // 复利：Π(1+段收益率) - 1
	MaxDD      float64 // 各段最差回撤
	Fills      int
	Wins       int
	Losses     int
	ExposedDay int
}

// WinRate 胜率（完结交易中盈利占比；无交易返回 0）。
func (s Stat) WinRate() float64 {
	if s.Wins+s.Losses == 0 {
		return 0
	}
	return float64(s.Wins) / float64(s.Wins+s.Losses)
}

// Exposure 暴露时间（有持仓天数占比；无交易日返回 0）。
func (s Stat) Exposure() float64 {
	if s.Days == 0 {
		return 0
	}
	return float64(s.ExposedDay) / float64(s.Days)
}

// Summarize 把权益曲线按体制标签切分为连续段，并聚合出各体制 Stat。
// fills 按日期归因成交笔数；positions 取 closed 的 realized_pnl 判胜负（按 close_date 归因）。
func Summarize(equities []contract.EquitySnapshot, labels map[string]Regime,
	fills []contract.Fill, positions []contract.Position) ([]Segment, []Stat) {
	fillByDate := map[string]int{}
	for _, f := range fills {
		fillByDate[f.Date]++
	}
	winByDate := map[string]int{}
	lossByDate := map[string]int{}
	for _, p := range positions {
		if p.Status != "closed" || p.ClosedDate == "" {
			continue
		}
		if p.RealizedPnl > 0 {
			winByDate[p.ClosedDate]++
		} else if p.RealizedPnl < 0 {
			lossByDate[p.ClosedDate]++
		}
	}

	var segs []Segment
	for i := 0; i < len(equities); {
		r := labels[equities[i].Date]
		seg := Segment{Regime: r, Start: equities[i].Date, End: equities[i].Date}
		startTotal := equities[i].Total
		j := i
		for j < len(equities) && labels[equities[j].Date] == r {
			e := equities[j]
			seg.End = e.Date
			seg.Days++
			seg.Fills += fillByDate[e.Date]
			seg.Wins += winByDate[e.Date]
			seg.Losses += lossByDate[e.Date]
			if e.MarketValue > 0 {
				seg.ExposedDay++
			}
			if e.Drawdown < seg.MaxDD {
				seg.MaxDD = e.Drawdown
			}
			j++
		}
		seg.Return = equities[j-1].Total/startTotal - 1
		segs = append(segs, seg)
		i = j
	}

	order := []Regime{Bull, Bear, Range}
	agg := map[Regime]*Stat{}
	for _, r := range order {
		agg[r] = &Stat{Regime: r}
	}
	for _, s := range segs {
		st := agg[s.Regime]
		st.Segments++
		st.Days += s.Days
		st.Fills += s.Fills
		st.Wins += s.Wins
		st.Losses += s.Losses
		st.ExposedDay += s.ExposedDay
		if s.MaxDD < st.MaxDD {
			st.MaxDD = s.MaxDD
		}
		st.Return = (1+st.Return)*(1+s.Return) - 1
	}
	stats := make([]Stat, 0, 3)
	for _, r := range order {
		stats = append(stats, *agg[r])
	}
	return segs, stats
}

// ---------------------------------------------------------------------------
// 报告
// ---------------------------------------------------------------------------

// SeriesPoint 权益曲线点（含预计算 SVG 坐标与当日体制）。
type SeriesPoint struct {
	Date   string
	Total  float64
	Regime Regime
	X, Y   float64
}

// ReportData 报告模板数据。
type ReportData struct {
	From, To    string
	TotalDays   int
	StartTotal  float64
	TotalReturn float64
	TotalMaxDD  float64
	Stats       []Stat
	Segments    []Segment
	Series      []SeriesPoint
}

// Run 生成分体制 HTML 报告，返回文件路径。
// 从 store 读 sh000001 日线、权益快照、成交、持仓；体制参数走 param。
func Run(ctx context.Context, st *store.Store, from, to, outDir string) (string, error) {
	// 读 sh000001 全历史（到 to）——箱体引擎需完整序列识别阶梯与突破事件。
	bars, err := st.BarRepo.BarsRange("sh000001", "", to, 100000)
	if err != nil {
		return "", fmt.Errorf("读 sh000001 日线: %w", err)
	}
	// 箱体法：复用 boxscan 的箱体参数（判断市场与判断个股用同一套工具，单一理论）
	params := contract.BoxParams{
		VolMultiplier: st.ParamRepo.GetFloat("box.volmult", 2.0),
		VolWindow:     st.ParamRepo.GetInt("box.volwindow", 50),
		BreakPremium:  st.ParamRepo.GetFloat("buy.premium", 1.03),
		MinBoxWidth:   0.05,
	}
	labels := LabelBoxes(bars, params)

	equities, err := st.TradeRepo.EquityRange(from, to)
	if err != nil {
		return "", fmt.Errorf("读权益: %w", err)
	}
	fills, err := st.TradeRepo.FillsRange(from, to)
	if err != nil {
		return "", fmt.Errorf("读成交: %w", err)
	}
	positions, err := st.TradeRepo.AllPositions()
	if err != nil {
		return "", fmt.Errorf("读持仓: %w", err)
	}

	segs, stats := Summarize(equities, labels, fills, positions)

	totalDays := len(equities)
	totalReturn := 0.0
	totalMaxDD := 0.0
	startTotal := 0.0
	if len(equities) > 0 {
		startTotal = equities[0].Total
		totalReturn = equities[len(equities)-1].Total/startTotal - 1
		for _, e := range equities {
			if e.Drawdown < totalMaxDD {
				totalMaxDD = e.Drawdown
			}
		}
	}

	series := buildSeries(equities, labels)

	data := ReportData{
		From: from, To: to, TotalDays: totalDays, StartTotal: startTotal,
		TotalReturn: totalReturn, TotalMaxDD: totalMaxDD,
		Stats: stats, Segments: segs, Series: series,
	}

	if err := os.MkdirAll(outDir, 0o755); err != nil {
		return "", err
	}
	path := filepath.Join(outDir, "regime_report.html")
	f, err := os.Create(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	if err := tmpl.Execute(f, data); err != nil {
		return "", err
	}
	return path, nil
}

// buildSeries 预计算权益曲线的 SVG 坐标（避免模板内算术）。
func buildSeries(equities []contract.EquitySnapshot, labels map[string]Regime) []SeriesPoint {
	if len(equities) == 0 {
		return nil
	}
	minY, maxY := math.MaxFloat64, -math.MaxFloat64
	for _, e := range equities {
		if e.Total < minY {
			minY = e.Total
		}
		if e.Total > maxY {
			maxY = e.Total
		}
	}
	if minY == maxY {
		maxY = minY + 1 // 避免除零
	}
	const w, h, pad = 900.0, 260.0, 20.0
	out := make([]SeriesPoint, 0, len(equities))
	for i, e := range equities {
		x := pad
		if len(equities) > 1 {
			x = pad + float64(i)/float64(len(equities)-1)*(w-2*pad)
		}
		y := h - pad - (e.Total-minY)/(maxY-minY)*(h-2*pad)
		out = append(out, SeriesPoint{Date: e.Date, Total: e.Total, Regime: labels[e.Date], X: x, Y: y})
	}
	return out
}

// pct 百分比格式化。
func pct(v float64) string {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return "0.0%"
	}
	return fmt.Sprintf("%.1f%%", v*100)
}

var tmpl = template.Must(template.New("regime").Funcs(template.FuncMap{
	"pct": pct,
	"cn":  func(r Regime) string { return r.cn() },
	"regimeColor": func(r Regime) string {
		switch r {
		case Bull:
			return "#ff6b6b"
		case Bear:
			return "#5fd96f"
		default:
			return "#ffd060"
		}
	},
}).Parse(`<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>达瓦斯 · 分体制报告</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:2em;background:#1a1a1a;color:#e0e0e0}
h1{color:#f0c040} h2{color:#80c0ff;border-bottom:1px solid #444;padding-bottom:4px}
table{border-collapse:collapse;margin:8px 0}
th,td{border:1px solid #555;padding:4px 10px;text-align:right}
th{background:#2a2a2a;color:#ffd060}
td.l{text-align:left}
.bull{color:#ff6b6b} .bear{color:#5fd96f} .range{color:#ffd060}
.pos{color:#ff6b6b} .neg{color:#5fd96f}
.note{color:#888;font-size:0.9em}
</style></head><body>
<h1>达瓦斯 · 分体制报告（{{.From}} ~ {{.To}}）</h1>
<p class="note">客观标注：以 sh000001 为代理，复用达瓦斯箱体引擎标体制——指数向上突破箱体=牛，
向下破位=熊，箱体内=猴（单一理论，零未来信息，禁摘樱桃）。趋势跟随固有滞后属「不用未来信息」的诚实代价。</p>

<h2>① 总览</h2>
<table>
<tr><th class="l">交易日</th><th>起点权益</th><th>总收益率</th><th>最大回撤</th></tr>
<tr><td class="l">{{.TotalDays}}</td>
<td>{{printf "%.0f" .StartTotal}}</td>
<td class="{{if ge .TotalReturn 0.0}}pos{{else}}neg{{end}}">{{pct .TotalReturn}}</td>
<td class="neg">{{pct .TotalMaxDD}}</td></tr>
</table>

<h2>② 分体制汇总（牛/熊/猴）</h2>
<table>
<tr><th class="l">体制</th><th>段数</th><th>天数</th><th>收益率</th><th>最大回撤</th><th>成交</th><th>胜率</th><th>暴露时间</th></tr>
{{range .Stats}}
<tr><td class="l {{.Regime}}">{{.Regime | cn}}</td><td>{{.Segments}}</td><td>{{.Days}}</td>
<td class="{{if ge .Return 0.0}}pos{{else}}neg{{end}}">{{pct .Return}}</td>
<td class="neg">{{pct .MaxDD}}</td><td>{{.Fills}}</td>
<td>{{if .Wins}}{{pct .WinRate}}{{else}}—{{end}}</td>
<td>{{pct .Exposure}}</td></tr>
{{end}}
</table>

<h2>③ 分段明细（连续同体制段）</h2>
<table>
<tr><th class="l">体制</th><th class="l">起</th><th class="l">止</th><th>天数</th><th>收益率</th><th>最大回撤</th><th>成交</th><th>胜</th><th>负</th><th>暴露天数</th></tr>
{{range .Segments}}
<tr><td class="l {{.Regime}}">{{.Regime | cn}}</td><td class="l">{{.Start}}</td><td class="l">{{.End}}</td>
<td>{{.Days}}</td>
<td class="{{if ge .Return 0.0}}pos{{else}}neg{{end}}">{{pct .Return}}</td>
<td class="neg">{{pct .MaxDD}}</td><td>{{.Fills}}</td><td>{{.Wins}}</td><td>{{.Losses}}</td><td>{{.ExposedDay}}</td></tr>
{{end}}
</table>

<h2>④ 权益曲线（点色=当日体制，红牛绿熊黄猴）</h2>
<svg width="100%" viewBox="0 0 900 260" preserveAspectRatio="none" style="background:#0d0d0d;border:1px solid #444;display:block">
{{range .Series}}<circle cx="{{printf "%.1f" .X}}" cy="{{printf "%.1f" .Y}}" r="1.5" fill="{{.Regime | regimeColor}}"/>
{{end}}</svg>

<p class="note">本报告仅提供分体制证据，供「大盘闸门」拍板参考，不做主。达瓦斯纪律：单一理论。</p>
</body></html>`))
