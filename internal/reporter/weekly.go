// weekly.go M7.2 周报（docs/01 §4.3 周六10:00 weekly 任务）。
//
// 覆盖区间：锚定日往前推 7 天（from = date-6）。内容四段：
// ① 持仓箱体阶梯全景 ② 周盈亏（权益增减+当周成交+当周平仓）
// ③ 权益曲线（区间快照+SVG sparkline） ④ 纪律审计（未止损/止损信号未执行/过期挂单/账本恒等式）。
// 输出 var/reports/weekly_YYYYMMDD.html。
package reporter

import (
	"context"
	"fmt"
	"html/template"
	"os"
	"path/filepath"
	"time"

	"darvas/internal/contract"
	"darvas/internal/ledger"
)

// WeeklyPath 返回某锚定日周报文件路径（不检查是否存在）。
func (r *Reporter) WeeklyPath(date string) string {
	return filepath.Join(r.outDir, fmt.Sprintf("weekly_%s.html", date))
}

// weekFrom 锚定日往前推 6 天（闭区间起点，覆盖完整一周）。
func weekFrom(date string) string {
	t, err := time.ParseInLocation("2006-01-02", date, time.Local)
	if err != nil {
		return date
	}
	return t.AddDate(0, 0, -6).Format("2006-01-02")
}

// Weekly 生成周报，返回文件路径。锚定日通常为周六。
func (r *Reporter) Weekly(ctx context.Context, date string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "weekly", Date: date}
	from := weekFrom(date)

	// ① 持仓阶梯全景
	type posLadder struct {
		Pos    contract.Position
		Ladder []contract.Box
		Last   float64 // 最新收盘（LastClose）
		HasBar bool
	}
	positions, _ := r.st.TradeRepo.OpenPositions()
	ladders := make([]posLadder, 0, len(positions))
	for _, p := range positions {
		l, _ := r.st.BoxRepo.Ladder(p.Code)
		last, ok, _ := r.st.BarRepo.LastClose(p.Code)
		ladders = append(ladders, posLadder{Pos: p, Ladder: l, Last: last, HasBar: ok})
	}

	// ② 周内成交 + 当周平仓
	fills, _ := r.st.TradeRepo.FillsRange(from, date)
	var closed []contract.Position
	if all, err := r.st.TradeRepo.AllPositions(); err == nil {
		for _, p := range all {
			if p.Status == "closed" && p.ClosedDate >= from && p.ClosedDate <= date {
				closed = append(closed, p)
			}
		}
	}

	// ③ 权益区间
	eqs, _ := r.st.TradeRepo.EquityRange(from, date)

	// ④ 纪律审计
	var violations []string
	for _, pl := range ladders {
		if pl.HasBar && pl.Last < pl.Pos.StopPrice {
			violations = append(violations, fmt.Sprintf(
				"未止损：%s 收盘 %.2f 已低于止损位 %.2f 仍持有（达瓦斯纪律：破位即离场）",
				pl.Pos.Code, pl.Last, pl.Pos.StopPrice))
		}
	}
	sigs, _ := r.st.SignalRepo.SignalsRange(from, date)
	sellFilled := map[string]bool{}
	for _, f := range fills {
		if f.Side == "sell" {
			sellFilled[f.Code] = true
		}
	}
	for _, s := range sigs {
		if (s.Type == contract.SignalStop || s.Type == contract.SignalFailed) && !sellFilled[s.Code] {
			violations = append(violations, fmt.Sprintf(
				"信号未执行：%s %s 收盘 %.2f（止损/假突破信号发出后当周无卖出成交）",
				s.Code, string(s.Type), s.ClosePx))
		}
	}
	if n, err := r.st.TradeRepo.StalePendingCount(date); err == nil && n > 0 {
		violations = append(violations, fmt.Sprintf(
			"过期挂单：%d 笔计划订单过期未成交未处理（次日应取消或按新计划重挂）", n))
	}
	auditOK, auditMsg := true, ""
	if ok, msg, err := ledger.New(r.st).AuditEquation(ctx, date); err == nil {
		auditOK, auditMsg = ok, msg
	} else if err != nil {
		auditOK, auditMsg = false, "账本审计失败: "+err.Error()
	}

	data := struct {
		Date       string
		From       string
		Ladders    []posLadder
		Fills      []contract.Fill
		Closed     []contract.Position
		Equities   []contract.EquitySnapshot
		EqFirst    float64
		EqLast     float64
		EqDelta    float64
		EqDeltaPct float64
		Sparkline  string
		Violations []string
		AuditOK    bool
		AuditMsg   string
	}{
		Date: date, From: from, Ladders: ladders, Fills: fills, Closed: closed,
		Equities: eqs, Violations: violations, AuditOK: auditOK, AuditMsg: auditMsg,
	}
	if len(eqs) > 1 {
		data.EqFirst = eqs[0].Total
		data.EqLast = eqs[len(eqs)-1].Total
		data.EqDelta = data.EqLast - data.EqFirst
		if data.EqFirst > 0 {
			data.EqDeltaPct = data.EqDelta / data.EqFirst * 100
		}
		data.Sparkline = sparkline(eqs)
	}

	if err := os.MkdirAll(r.outDir, 0o755); err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	path := r.WeeklyPath(date)
	f, err := os.Create(path)
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	defer f.Close()
	if err := weeklyTmpl.Execute(f, data); err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	res.Status = "ok"
	res.AffectedRows = 1
	res.DurationMS = time.Since(start).Milliseconds()
	r.log.Printf("weekly %s → %s (%d pos, %d fills, %d violations), %s",
		date, path, len(ladders), len(fills), len(violations), time.Since(start).Round(time.Millisecond))
	return res, nil
}

// sparkline 权益快照 → SVG polyline points（600x120 视口）。
func sparkline(eqs []contract.EquitySnapshot) string {
	if len(eqs) < 2 {
		return ""
	}
	minV, maxV := eqs[0].Total, eqs[0].Total
	for _, e := range eqs {
		if e.Total < minV {
			minV = e.Total
		}
		if e.Total > maxV {
			maxV = e.Total
		}
	}
	span := maxV - minV
	if span == 0 {
		span = 1
	}
	w, h := 600.0, 120.0
	pts := ""
	for i, e := range eqs {
		x := float64(i) / float64(len(eqs)-1) * w
		y := h - (e.Total-minV)/span*(h-20) - 10
		pts += fmt.Sprintf("%.1f,%.1f ", x, y)
	}
	return pts
}

var weeklyTmpl = template.Must(template.New("weekly").Parse(`<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>达瓦斯周报 {{.Date}}</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:2em;background:#1a1a1a;color:#e0e0e0}
h1{color:#f0c040} h2{color:#80c0ff;border-bottom:1px solid #444;padding-bottom:4px}
table{border-collapse:collapse;margin:8px 0}
th,td{border:1px solid #555;padding:4px 10px;text-align:right}
th{background:#2a2a2a;color:#ffd060}
td.l{text-align:left}
.sell{color:#ff6b6b} .buy{color:#5fd96f} .warn{color:#ff6b6b;font-weight:bold}
.note{color:#888;font-size:0.9em}
.bad{color:#ff6b6b} .good{color:#5fd96f}
</style></head><body>
<h1>达瓦斯周度复盘 · {{.From}} ~ {{.Date}}</h1>

<h2>① 持仓箱体阶梯全景（{{.Ladders | len}} 个持仓）</h2>
{{if .Ladders}}
{{range .Ladders}}
<p><b>{{.Pos.Code}}</b> · 数量 {{.Pos.Qty}} · 成本 {{printf "%.2f" .Pos.AvgCost}} · 止损 {{printf "%.2f" .Pos.StopPrice}}{{if .HasBar}} · 最新收盘 {{printf "%.2f" .Last}}{{end}} · 阶梯 {{.Ladder | len}} 箱</p>
<table>
<tr><th>#</th><th>上轨</th><th>下轨</th><th>状态</th><th class="l">上轨确认</th><th class="l">下轨确认</th></tr>
{{range .Ladder}}<tr><td>{{printf "%d" .ID}}</td>
<td>{{printf "%.2f" .Top}}</td><td>{{printf "%.2f" .Bottom}}</td>
<td class="l">{{.Status}}</td>
<td class="l">{{.TopConfirmed}}</td><td class="l">{{.BottomConfirmed}}</td></tr>{{end}}
</table>
{{end}}
{{else}}<p class="note">当前无持仓。跟踪池中的 watching 股票等待突破信号，勿提前入场（达瓦斯纪律：只在箱体突破时买入）。</p>{{end}}

<h2>② 周盈亏</h2>
{{if .Equities}}<p>权益 {{printf "%.0f" .EqFirst}} → {{printf "%.0f" .EqLast}}
（{{if ge .EqDelta 0.0}}<span class="good">+{{printf "%.0f" .EqDelta}}（{{printf "%.2f" .EqDeltaPct}}%）</span>{{else}}<span class="bad">{{printf "%.0f" .EqDelta}}（{{printf "%.2f" .EqDeltaPct}}%）</span>{{end}}）</p>
{{else}}<p class="note">本周无权益快照（Snapshot 随成交录入生成；无成交则权益不变）。</p>{{end}}
<h3>当周成交（{{.Fills | len}} 笔）</h3>
{{if .Fills}}
<table>
<tr><th class="l">日期</th><th class="l">代码</th><th class="l">方向</th><th>价格</th><th>数量</th></tr>
{{range .Fills}}<tr><td class="l">{{.Date}}</td><td class="l">{{.Code}}</td>
<td class="{{.Side}} {{.Side}}">{{if eq .Side "buy"}}买入{{else}}卖出{{end}}</td>
<td>{{printf "%.2f" .Price}}</td><td>{{.Qty}}</td></tr>{{end}}
</table>
{{else}}<p class="note">本周无成交（成交后用 darvas fills [file] --yes 录入，周报自动核算）。</p>{{end}}
<h3>当周平仓（{{.Closed | len}} 笔）</h3>
{{if .Closed}}
<table>
<tr><th class="l">代码</th><th>数量</th><th>成本</th><th class="l">平仓日</th><th class="l">原因</th></tr>
{{range .Closed}}<tr><td class="l">{{.Code}}</td><td>{{.Qty}}</td>
<td>{{printf "%.2f" .AvgCost}}</td><td class="l">{{.ClosedDate}}</td><td class="l">{{.CloseReason}}</td></tr>{{end}}
</table>
{{else}}<p class="note">本周无平仓。</p>{{end}}

<h2>③ 权益曲线（{{.Equities | len}} 个快照）</h2>
{{if .Sparkline}}
<svg viewBox="0 0 600 120" width="600" height="120" style="background:#222;border:1px solid #444">
<polyline points="{{.Sparkline}}" fill="none" stroke="#f0c040" stroke-width="2"/>
</svg>
<table>
<tr><th class="l">日期</th><th>现金</th><th>市值</th><th>总权益</th><th>回撤</th></tr>
{{range .Equities}}<tr><td class="l">{{.Date}}</td><td>{{printf "%.0f" .Cash}}</td>
<td>{{printf "%.0f" .MarketValue}}</td><td>{{printf "%.0f" .Total}}</td>
<td>{{printf "%.1f" .Drawdown}}%</td></tr>{{end}}
</table>
{{else}}<p class="note">本周权益快照不足 2 个，无法绘图（快照随每日流水线生成）。</p>{{end}}

<h2>④ 纪律审计</h2>
{{if .Violations}}
{{range .Violations}}<p class="warn">✗ {{.}}</p>{{end}}
{{else}}<p class="good">✓ 本周无违规：无未止损持仓、止损信号均已执行、无过期挂单。</p>{{end}}
{{if .AuditOK}}<p class="good">✓ 账本恒等式审计通过。</p>
{{else}}<p class="warn">✗ 账本恒等式审计未通过：{{.AuditMsg}}</p>{{end}}
<p class="note">达瓦斯纪律复盘要点：所有卖出是否只因止损/目标位触发？有无盘中冲动交易？突破买入是否等到了确认？</p>
</body></html>`))
