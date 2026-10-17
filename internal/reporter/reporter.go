// Package reporter 生成每日决策日报 HTML（docs/01 §4.1 report 任务 / FR-605）。
//
// 日报内容：任务心跳 + 候选池 + 信号 + 明日交易计划（六问答案）+ 持仓。
// 输出 var/reports/daily_YYYYMMDD.html。
package reporter

import (
	"context"
	"database/sql"
	"fmt"
	"html/template"
	"log"
	"os"
	"path/filepath"
	"sort"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// Reporter 日报生成器。
type Reporter struct {
	st     *store.Store
	log    *log.Logger
	outDir string
}

// New 构造。outDir 为报告输出目录（默认 var/reports）。
func New(st *store.Store, outDir string) *Reporter {
	if outDir == "" {
		outDir = "var/reports"
	}
	return &Reporter{st: st, log: log.New(log.Writer(), "[report] ", log.LstdFlags), outDir: outDir}
}

// Path 返回某日日报文件路径（不检查是否存在）。
func (r *Reporter) Path(date string) string {
	return filepath.Join(r.outDir, fmt.Sprintf("daily_%s.html", date))
}

// Report 生成日报，返回文件路径。
func (r *Reporter) Report(ctx context.Context, date string) (contract.TaskResult, error) {
	start := time.Now()
	res := contract.TaskResult{Task: "report", Date: date}

	// 汇总数据
	runs := r.taskHeartbeats(date)
	cands, _ := r.st.PoolRepo.CandidatesOn(date)
	sigs, _ := r.st.SignalRepo.SignalsOn(date)
	plan, orders := r.planView(date)
	positions, _ := r.st.TradeRepo.OpenPositions()

	// 排序订单：卖单在前（止损优先），再买单
	sort.Slice(orders, func(i, j int) bool {
		if orders[i].Side != orders[j].Side {
			return orders[i].Side == "sell"
		}
		return orders[i].Code < orders[j].Code
	})

	data := struct {
		Date      string
		Runs      []runRow
		Cands     []contract.Candidate
		Sigs      []contract.Signal
		Plan      *contract.TradePlan
		Orders    []contract.PlanOrder
		Positions []contract.Position
		CountDown int
		CountUp   int
	}{date, runs, cands, sigs, plan, orders, positions, countBy(cands), 0}

	if err := os.MkdirAll(r.outDir, 0o755); err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	path := filepath.Join(r.outDir, fmt.Sprintf("daily_%s.html", date))
	f, err := os.Create(path)
	if err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	defer f.Close()
	if err := reportTmpl.Execute(f, data); err != nil {
		res.Status, res.Error = "failed", err.Error()
		return res, err
	}
	res.Status = "ok"
	res.AffectedRows = 1
	res.DurationMS = time.Since(start).Milliseconds()
	r.log.Printf("report %s → %s (%d cands, %d sigs, %d orders), %s",
		date, path, len(cands), len(sigs), len(orders), time.Since(start).Round(time.Millisecond))
	return res, nil
}

// runRow 任务心跳行。
type runRow struct {
	Task, Status, Error string
	Finished            string
	Affected            int64
}

func (r *Reporter) taskHeartbeats(date string) []runRow {
	tasks := []string{"ingest", "boxscan", "screen", "poolsync", "signal", "plan", "report"}
	var out []runRow
	for _, t := range tasks {
		var status, errStr, finished string
		var aff sql.NullInt64
		_ = r.st.DB().QueryRow(`SELECT status, COALESCE(error,''), COALESCE(affected_rows,0),
			COALESCE(finished_at,'') FROM daily_run WHERE date = ? AND task = ?`, date, t).
			Scan(&status, &errStr, &aff, &finished)
		out = append(out, runRow{t, status, errStr, finished, aff.Int64})
	}
	return out
}

func countBy(cands []contract.Candidate) int { return len(cands) }

func (r *Reporter) planView(date string) (*contract.TradePlan, []contract.PlanOrder) {
	plan, orders, err := r.st.TradeRepo.PlanOn(date)
	if err != nil || plan == nil {
		return nil, nil
	}
	return plan, orders
}

var reportTmpl = template.Must(template.New("daily").Parse(`<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>达瓦斯日报 {{.Date}}</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:2em;background:#1a1a1a;color:#e0e0e0}
h1{color:#f0c040} h2{color:#80c0ff;border-bottom:1px solid #444;padding-bottom:4px}
table{border-collapse:collapse;margin:8px 0}
th,td{border:1px solid #555;padding:4px 10px;text-align:right}
th{background:#2a2a2a;color:#ffd060}
td.l{text-align:left}
.ok{color:#5fd96f} .failed{color:#ff6b6b} .running{color:#ffd060} .none{color:#888}
.sell{color:#ff6b6b} .buy{color:#5fd96f}
.note{color:#888;font-size:0.9em}
</style></head><body>
<h1>达瓦斯决策日报 · {{.Date}}</h1>

<h2>① 今日流程心跳</h2>
<table>
<tr><th class="l">任务</th><th>状态</th><th>影响行数</th><th class="l">完成时间</th><th class="l">错误</th></tr>
{{range .Runs}}
<tr><td class="l">{{.Task}}</td>
<td class="{{if eq .Status "ok"}}ok{{else if eq .Status "failed"}}failed{{else if eq .Status "running"}}running{{else}}none{{end}}">{{.Status}}</td>
<td>{{.Affected}}</td><td class="l">{{.Finished}}</td><td class="l">{{.Error}}</td></tr>
{{end}}
</table>

<h2>② 候选池（{{.Cands | len}} 只，按分数降序）</h2>
<table>
<tr><th class="l">代码</th><th>分数</th><th class="l">流动性</th><th class="l">RS</th><th class="l">放量</th><th class="l">箱体</th></tr>
{{range .Cands}}
<tr><td class="l">{{.Code}}</td><td>{{printf "%.0f" .Score}}</td>
<td class="l">{{if index .Reasons "liquidity_pass"}}✓{{else}}✗{{end}}</td>
<td class="l">{{if index .Reasons "rs_pass"}}✓{{else}}✗{{end}}</td>
<td class="l">{{if index .Reasons "vol_pass"}}✓{{else}}✗{{end}}</td>
<td class="l">{{if index .Reasons "box_pass"}}✓{{else}}✗{{end}}</td>
</tr>{{end}}
</table>

<h2>③ 今日信号（{{.Sigs | len}} 条）</h2>
<table>
<tr><th class="l">代码</th><th class="l">类型</th><th>收盘价</th><th>箱体轨</th></tr>
{{range .Sigs}}<tr><td class="l">{{.Code}}</td>
<td class="l">{{.Type}}</td><td>{{printf "%.2f" .ClosePx}}</td><td>{{printf "%.2f" .BoxLevel}}</td></tr>{{end}}
</table>

<h2>④ 明日交易计划（六问答案，{{.Orders | len}} 笔订单）</h2>
<table>
<tr><th class="l">代码</th><th class="l">方向</th><th class="l">类型</th><th>触发价</th><th>委托价</th><th>数量</th><th>腿</th></tr>
{{range .Orders}}<tr>
<td class="l">{{.Code}}</td>
<td class="{{.Side}} {{.Side}}">{{if eq .Side "buy"}}买入{{else}}卖出{{end}}</td>
<td class="l">{{.OrderKind}}</td>
<td>{{printf "%.2f" .TriggerPrice}}</td><td>{{printf "%.2f" .LimitPrice}}</td><td>{{.Qty}}</td><td>{{.LegNo}}</td>
</tr>{{end}}
</table>
<p class="note">{{if .Plan}}计划日 {{.Plan.PlanDate}} → 执行日 {{.Plan.ExecuteDate}}  权益基准 {{printf "%.0f" .Plan.EquityBase}}  开仓数 {{.Plan.OpenPositionCount}}{{else}}（当日无计划生成）{{end}}</p>

<h2>⑤ 持仓（{{.Positions | len}} 个）</h2>
<table>
<tr><th class="l">代码</th><th>数量</th><th>成本</th><th>止损位</th></tr>
{{range .Positions}}<tr>
<td class="l">{{.Code}}</td><td>{{.Qty}}</td><td>{{printf "%.2f" .AvgCost}}</td><td>{{printf "%.2f" .StopPrice}}</td>
</tr>{{end}}
</table>

<p class="note">达瓦斯纪律：次日09:25前按④挂券商条件单；盘中不盯盘；止损位只升不降。</p>
</body></html>`))
