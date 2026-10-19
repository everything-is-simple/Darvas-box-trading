// Package regime 实现 M8.3 体制运行器（PROMPT.md P4 M8.3）：
// 用客观规则「事先」标注牛/熊/猴窗口（禁摘樱桃），分体制聚合权益曲线/回撤/胜率/
// 暴露时间，输出 HTML 报告——为「大盘闸门要不要加」提供证据，不做主。
//
// 体制标注规则（客观、只用过去数据、零未来信息、单一理论、不引入均线等其他理论）：
// 以 sh000001 上证指数为市场代理，复用达瓦斯箱体引擎（box.New().Scan）给指数标箱体：
//
//	指数向上突破箱体（broken_up）   → 牛（bull）
//	指数向下破位（broken_down）     → 熊（bear）
//	箱体内（forming/confirmed）     → 猴（range，震荡）
//
// 突破/破位是事件（BrokenDate 当日判定，只用当日及之前数据）；事件后延续该方向直到反向事件。
// 复用判断个股的同一套箱体参数（box.volmult / box.volwindow / buy.premium）——单一理论。
// 趋势跟随固有滞后：牛熊转换初期箱体未突破，标为 range 属「不用未来信息」的诚实代价。
//
// 历史：初版用动量法（过去 N 日涨跌幅超 ±阈值），2026-10-19 用户拍板改用箱体法（commit 994cc16），
// Classify 函数保留仅供旧测试引用，勿再使用。
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
		st.Return = (1 + st.Return) * (1 + s.Return) - 1
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
	From, To      string
	TotalDays     int
	StartTotal    float64
	TotalReturn   float64
	TotalMaxDD    float64
	Stats         []Stat
	Segments      []Segment
	Series        []SeriesPoint
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
