// Package gui 本地HTTP UI（M6，docs/02 §2 GUI风险预案生效版）。
//
// 方案：net/http + embed 静态单文件前端 + JSON API，零外部依赖
// （不用Wails前端工具链/Node，保持「单exe拷走即用」）。
// 四视图：仪表盘（任务心跳/计划订单/权益）/ 跟踪池 / 个股K线+箱体 / 交易记录。
// 只读：所有写操作仍走 CLI（fills/param/run），GUI 不越权（docs/04 §6 原则）。
package gui

import (
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"log"
	"net"
	"net/http"
	"os/exec"
	"runtime"
	"strconv"
	"time"

	"darvas/internal/contract"
	"darvas/internal/scheduler"
	"darvas/internal/store"
)

//go:embed static
var staticFS embed.FS

// Server 本地UI服务。
type Server struct {
	st  *store.Store
	log *log.Logger
}

// New 构造。
func New(st *store.Store) *Server {
	return &Server{st: st, log: log.New(log.Writer(), "[gui] ", log.LstdFlags)}
}

// Handler 返回HTTP路由（供 darvas ui 与 httptest 共用）。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/dashboard", s.handleDashboard)
	mux.HandleFunc("GET /api/dates", s.handleDates)
	mux.HandleFunc("GET /api/watch", s.handleWatch)
	mux.HandleFunc("GET /api/candidates", s.handleCandidates)
	mux.HandleFunc("GET /api/signals", s.handleSignals)
	mux.HandleFunc("GET /api/stock/{code}", s.handleStock)
	mux.HandleFunc("GET /api/trades", s.handleTrades)
	mux.HandleFunc("GET /api/schedule", s.handleSchedule) // 时刻集中一处管理（scheduler.Schedule 派生）
	sub, err := fs.Sub(staticFS, "static")
	if err != nil {
		panic(err) // embed 编译期保证，不可达
	}
	// 本地工具 UI 禁缓存：embed 文件无真实 mtime，FileServer 不发 Last-Modified，
	// 浏览器可能启发式缓存旧页面（升级 exe 后 UI 不更新）。noCache 强制每次拉取。
	mux.Handle("/", noCache(http.FileServer(http.FS(sub))))
	return mux
}

// noCache 给响应加禁止缓存头。
func noCache(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-cache, no-store, must-revalidate")
		h.ServeHTTP(w, r)
	})
}

// Run 监听、（可选）打开系统浏览器并阻塞服务。
func Run(st *store.Store, addr string, openBrowser bool) error {
	s := New(st)
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return fmt.Errorf("listen %s: %w", addr, err)
	}
	url := fmt.Sprintf("http://%s", displayAddr(ln.Addr().String()))
	fmt.Printf("darvas UI 已启动: %s （Ctrl+C 退出）\n", url)
	if openBrowser {
		openURL(url)
	}
	srv := &http.Server{Handler: s.Handler()}
	return srv.Serve(ln)
}

// displayAddr 把 *:8787 展示成 localhost:8787（浏览器可打开）。
func displayAddr(addr string) string {
	if len(addr) > 0 && addr[0] == ':' {
		return "localhost" + addr
	}
	return addr
}

// openURL 用系统默认浏览器打开（跨平台 best-effort）。
func openURL(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("cmd", "/c", "start", "", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	if err := cmd.Start(); err != nil {
		log.Printf("[gui] 打开浏览器失败: %v（请手动访问 %s）", err, url)
	}
}

// ---------------------------------------------------------------------------
// JSON API
// ---------------------------------------------------------------------------

// writeJSON 统一JSON输出。
func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	if err := json.NewEncoder(w).Encode(v); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

// handleSchedule 返回调度时刻表（scheduler.Schedule 派生；时刻集中一处管理，
// 前端心跳标题/格子/任务名全部动态渲染，不再硬编码——docs/01 §4.1 唯一事实源）。
func (s *Server) handleSchedule(w http.ResponseWriter, r *http.Request) {
	type item struct {
		Task  string `json:"task"`
		Clock string `json:"clock"`
	}
	items := make([]item, len(scheduler.Schedule))
	for i, tt := range scheduler.Schedule {
		items[i] = item{Task: tt.Task, Clock: tt.Clock}
	}
	writeJSON(w, map[string]any{
		"tasks":  items,
		"weekly": scheduler.WeeklyClock,
		"summary": scheduler.ScheduleSummary(),
	})
}

// writeErr 统一错误输出。
func writeErr(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

// DashboardResponse 仪表盘：任务心跳 + 今日计划订单 + 权益。
type DashboardResponse struct {
	Date    string                     `json:"date"`
	Runs    []contract.TaskResult      `json:"runs"`
	Plan    *contract.TradePlan        `json:"plan"`
	Orders  []contract.PlanOrder       `json:"orders"`
	Names   map[string]string          `json:"names"`
	Equity  *contract.EquitySnapshot   `json:"equity"`
}

// handleDashboard GET /api/dashboard?date=YYYY-MM-DD（缺省=最近心跳日）。
func (s *Server) handleDashboard(w http.ResponseWriter, r *http.Request) {
	date := r.URL.Query().Get("date")
	if date == "" {
		d, err := s.st.RunRepo.LatestRunDate()
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		date = d
	}
	if date == "" {
		date = time.Now().Format("2006-01-02")
	}
	runs, err := s.st.RunRepo.RunsOn(date)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if runs == nil { // 无心跳日：nil 序列化成 JSON null 会崩前端 .map（bug④）
		runs = []contract.TaskResult{}
	}
	plan, orders, err := s.st.TradeRepo.PlanOn(date)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if orders == nil {
		orders = []contract.PlanOrder{}
	}
	equity, err := s.st.TradeRepo.LatestEquity()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	names, err := s.namesFor(orders2codes(orders))
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, DashboardResponse{
		Date: date, Runs: runs, Plan: plan, Orders: orders, Names: names, Equity: equity,
	})
}

func orders2codes(orders []contract.PlanOrder) []string {
	out := make([]string, 0, len(orders))
	for _, o := range orders {
		out = append(out, o.Code)
	}
	return out
}

// handleDates GET /api/dates → 有数据的日期列表（GUI U1 全站日期维度）：
// UNION 自 daily_run/candidate/trade_plan/equity_snapshot，降序，每日附注。
func (s *Server) handleDates(w http.ResponseWriter, r *http.Request) {
	dates, err := s.st.RunRepo.DatesOverview()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if dates == nil {
		dates = []store.DateOverview{}
	}
	writeJSON(w, map[string]any{"dates": dates})
}

// handleCandidates GET /api/candidates?date= → 候选池按日快照（GUI U3）。
// date 缺省=最近一次候选快照日；无任何快照返回空列表。
func (s *Server) handleCandidates(w http.ResponseWriter, r *http.Request) {
	date := r.URL.Query().Get("date")
	if date == "" {
		d, err := s.st.PoolRepo.LatestCandidateDate()
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		date = d
	}
	items := []contract.Candidate{}
	if date != "" {
		var err error
		items, err = s.st.PoolRepo.CandidatesOn(date)
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
	}
	if items == nil {
		items = []contract.Candidate{}
	}
	codes := make([]string, 0, len(items))
	for _, c := range items {
		codes = append(codes, c.Code)
	}
	names, err := s.namesFor(codes)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]any{"date": date, "items": items, "names": names})
}

// handleSignals GET /api/signals?date= → 信号按日浏览（GUI U3）。
// date 缺省=最近一次信号日；无任何信号返回空列表。
func (s *Server) handleSignals(w http.ResponseWriter, r *http.Request) {
	date := r.URL.Query().Get("date")
	if date == "" {
		d, err := s.st.SignalRepo.LatestSignalDate()
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
		date = d
	}
	items := []contract.Signal{}
	if date != "" {
		var err error
		items, err = s.st.SignalRepo.SignalsOn(date)
		if err != nil {
			writeErr(w, 500, err.Error())
			return
		}
	}
	if items == nil {
		items = []contract.Signal{}
	}
	codes := make([]string, 0, len(items))
	for _, g := range items {
		codes = append(codes, g.Code)
	}
	names, err := s.namesFor(codes)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, map[string]any{"date": date, "items": items, "names": names})
}

// handleWatch GET /api/watch → 跟踪池看板（含名称/当前箱体/最新收盘）。
func (s *Server) handleWatch(w http.ResponseWriter, r *http.Request) {
	rows, err := s.st.PoolRepo.WatchOverview()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if rows == nil {
		rows = []store.WatchOverview{}
	}
	writeJSON(w, map[string]any{"items": rows})
}

// StockResponse 个股视图：K线窗口 + 箱体阶梯 + watch/持仓上下文。
type StockResponse struct {
	Code     string               `json:"code"`
	Name     string               `json:"name"`
	Days     int                  `json:"days"`
	From     string               `json:"from"`
	To       string               `json:"to"`
	Bars     []contract.DailyBar  `json:"bars"`
	Boxes    []contract.Box       `json:"boxes"`
	Watch    *contract.Watch      `json:"watch"`
	Position *contract.Position   `json:"position"`
}

// handleStock GET /api/stock/{code}?days=250 或 ?from=&to=（GUI U2 时间范围查询）。
// days 缺省250上限1000；from/to 给定则按区间取（上限1000根，超出截尾保最近）。
func (s *Server) handleStock(w http.ResponseWriter, r *http.Request) {
	code := r.PathValue("code")
	if code == "" {
		writeErr(w, 400, "missing code")
		return
	}
	q := r.URL.Query()
	from, to := q.Get("from"), q.Get("to")
	days, _ := strconv.Atoi(q.Get("days"))
	if days <= 0 {
		days = 250
	}
	if days > 1000 {
		days = 1000
	}
	var bars []contract.DailyBar
	var err error
	if from != "" || to != "" {
		bars, err = s.st.BarRepo.BarsRange(code, from, to, 1000)
	} else {
		bars, err = s.st.BarRepo.BarsWindow(code, "2999-12-31", days)
	}
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if len(bars) == 0 {
		writeErr(w, 404, "无 "+code+" 的行情数据（请先 ingest）")
		return
	}
	boxes, err := s.st.BoxRepo.Ladder(code)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if boxes == nil {
		boxes = []contract.Box{}
	}
	watch, err := s.st.PoolRepo.WatchByCode(code)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	pos, err := s.st.TradeRepo.PositionByCode(code)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	name, _ := s.namesFor([]string{code}) // 单码查询
	writeJSON(w, StockResponse{
		Code: code, Name: name[code], Days: days, From: from, To: to,
		Bars: bars, Boxes: boxes, Watch: watch, Position: pos,
	})
}

// PositionView 持仓 + 浮盈亏（GUI 交易记录视图）。
type PositionView struct {
	contract.Position
	Name      string  `json:"name"`
	LastClose float64 `json:"last_close"`
	PnL       float64 `json:"pnl"`    // 浮动盈亏额（closed=最终盈亏按0计）
	PnLPct    float64 `json:"pnl_pct"`
}

// TradesResponse 交易记录：计划/成交/持仓/权益曲线。
type TradesResponse struct {
	From      string                    `json:"from"`
	To        string                    `json:"to"`
	Plans     []store.PlanView          `json:"plans"`
	Fills     []contract.Fill           `json:"fills"`
	Positions []PositionView            `json:"positions"`
	Equity    []contract.EquitySnapshot `json:"equity"`
	Names     map[string]string         `json:"names"`
}

// handleTrades GET /api/trades?from=&to= → 交易记录四合一（GUI U2 时间范围）。
// fills/plans/equity 按范围过滤（缺省=全部）；positions 为当前态不按日过滤。
func (s *Server) handleTrades(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	from, to := q.Get("from"), q.Get("to")
	plans, err := s.st.TradeRepo.PlansRange(from, to, 0)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if plans == nil {
		plans = []store.PlanView{}
	}
	fills, err := s.st.TradeRepo.FillsRange(from, to)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if fills == nil {
		fills = []contract.Fill{}
	}
	positions, err := s.st.TradeRepo.AllPositions()
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	posViews := make([]PositionView, 0, len(positions))
	codes := make([]string, 0, len(fills)+len(positions))
	for _, f := range fills {
		codes = append(codes, f.Code)
	}
	for _, p := range positions {
		codes = append(codes, p.Code)
		v := PositionView{Position: p}
		if p.Status == "open" {
			if c, ok, err := s.st.BarRepo.LastClose(p.Code); err == nil && ok {
				v.LastClose = c
				v.PnL = (c - p.AvgCost) * float64(p.Qty)
				if p.AvgCost > 0 {
					v.PnLPct = (c/p.AvgCost - 1) * 100
				}
			}
		}
		posViews = append(posViews, v)
	}
	equity, err := s.st.TradeRepo.EquityRange(from, to)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	if equity == nil {
		equity = []contract.EquitySnapshot{}
	}
	names, err := s.namesFor(codes)
	if err != nil {
		writeErr(w, 500, err.Error())
		return
	}
	writeJSON(w, TradesResponse{
		From: from, To: to,
		Plans: plans, Fills: fills, Positions: posViews, Equity: equity, Names: names,
	})
}

// namesFor 取「只含指定代码」的名称映射（避免把1.2万全量名称塞进响应）。
func (s *Server) namesFor(codes []string) (map[string]string, error) {
	out := map[string]string{}
	if len(codes) == 0 {
		return out, nil
	}
	all, err := s.st.SecRepo.Names()
	if err != nil {
		return nil, err
	}
	for _, c := range codes {
		if n, ok := all[c]; ok {
			out[c] = n
		}
	}
	return out, nil
}
