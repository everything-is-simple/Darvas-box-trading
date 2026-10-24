// darvas —— 达瓦斯箱体交易系统入口。
// 子命令与 docs/04-api-contract.md §7 CLI同构：ingest/doctor/ladder/param/run...
// M0里程碑：ingest（全量/增量）与 doctor 可用，其余引擎按 M2-M5 里程碑补齐。
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"darvas/internal/backtest"
	"darvas/internal/box"
	"darvas/internal/contract"
	"darvas/internal/datasource/tdx"
	"darvas/internal/gui"
	"darvas/internal/ingest"
	"darvas/internal/ledger"
	"darvas/internal/pipeline"
	"darvas/internal/pool"
	"darvas/internal/scheduler"
	"darvas/internal/store"
)

const version = "0.5.0-m8"

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd := os.Args[1]
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	date := fs.String("date", time.Now().Format("2006-01-02"), "交易日 YYYY-MM-DD")
	db := fs.String("db", "", "数据库路径（默认 exe同目录/var/darvas.db）")
	since := fs.String("since", "2024-01-01", "全量导入起始日")
	yes := fs.Bool("yes", false, "fills: 解析后直接入库（跳过确认）")
	addr := fs.String("addr", "127.0.0.1:8787", "ui: HTTP监听地址")
	resume := fs.Float64("resume", 0, "pool suspend: 挂起激活价（收盘≥此价自动恢复跟踪）")
	reason := fs.String("reason", "", "pool remove: 人工移除原因")
	lesson := fs.String("lesson", "", "review add: 教训（人工复盘录入，原书核心）")
	errorType := fs.String("error-type", "", "review add: 错误类型（追高/不止损/过早止盈/情绪化…）")
	rule := fs.String("rule", "", "review add: 规则执行评价（followed/violated/自由文本）")
	from := fs.String("from", "", "backtest: 回放窗口起点 YYYY-MM-DD（空=源库最近20交易日）")
	to := fs.String("to", "", "backtest: 回放窗口终点 YYYY-MM-DD")
	src := fs.String("src", "", "backtest: 源库路径（只读）")
	btMode := fs.String("mode", "", "backtest: prefill|progressive|regime|verify")
	btSet := fs.String("set", "", "backtest: scratch 参数覆盖，格式 key=value,key=value")
	// Go flag 遇首个非 flag 即停止；这里把 flag 与位置参数分离，使
	// `darvas ladder <code> --db <path>` 这样的混合顺序也能正确解析。
	flagArgs, positional := splitFlags(os.Args[2:])
	fs.Parse(flagArgs)

	switch cmd {
	case "version":
		fmt.Println("darvas", version)
	case "ingest":
		runIngest(*db, *since, *date)
	case "doctor":
		runDoctor(*db)
	case "ladder":
		var code string
		if len(positional) > 0 {
			code = positional[0]
		}
		if code == "" {
			fmt.Println("用法: darvas ladder <code>  例: sh600519")
			os.Exit(2)
		}
		runLadder(*db, code)
	case "param":
		runParam(*db, positional)
	case "run":
		runPipeline(*db, *date)
	case "boxscan", "screen", "poolsync", "signal", "plan", "report":
		runRerun(*db, *date, cmd)
	case "pool":
		runPool(*db, positional, *resume, *reason)
	case "review":
		runReview(*db, positional, *lesson, *errorType, *rule)
	case "weekly":
		runWeekly(*db, *date)
	case "fills":
		runFills(*db, positional, *yes)
	case "ui":
		runUI(*db, *addr)
	case "serve":
		runServe(*db, *addr)
	case "backtest":
		runBacktest(*db, *from, *to, *src, *btMode, *btSet)
	default:
		usage()
		os.Exit(2)
	}
}

// splitFlags 把参数序列拆成（flag 参数，位置参数）。
// 以 "-" 开头的视为 flag（含其后续值），其余为位置参数。
func splitFlags(args []string) (flags, positional []string) {
	i := 0
	for i < len(args) {
		a := args[i]
		if len(a) > 0 && a[0] == '-' {
			flags = append(flags, a)
			// -flag=value 单参；-flag value（非布尔）取下一个为值
			if !strings.Contains(a, "=") && i+1 < len(args) && !(len(args[i+1]) > 0 && args[i+1][0] == '-') {
				flags = append(flags, args[i+1])
				i += 2
				continue
			}
			i++
			continue
		}
		positional = append(positional, a)
		i++
	}
	return
}

func usage() {
	fmt.Print(`darvas —— 达瓦斯箱体交易系统（` + version + `）

用法:
  darvas ingest  [--date] [--since] [--db]   数据同步（默认增量；--since=full 2024起；--since=all 全历史1990起）
  darvas doctor [--db]                       数据库/schema/数据新鲜度体检
  darvas ladder  <code>                      个股箱体阶梯（M2）
  darvas param   get|set                     参数读写
  darvas pool    list [stage]                跟踪池查看（stage: watching/pending/holding/suspended/blacklisted）
  darvas pool    suspend <code> --resume <价> 条件挂起（收盘≥激活价自动恢复跟踪）
  darvas pool    remove <code> --reason <文本> 人工移除（留痕）
  darvas review list [code]                已交易股票历史库·战绩视图（B3，三记录制度）
  darvas review add <position_id> --lesson <文本> [--error-type <文本>] [--rule <评价>]
                                             复盘录入（亏损原因/教训，人工写）
  darvas run    [--date]                     完整盘后流水线（M5）
  darvas boxscan|screen|poolsync|signal|plan|report [--date]
                                             单任务幂等补跑（docs/04 §7）
  darvas fills  [file] [--yes]               成交录入（粘贴文本；.csv=炒股手导出→审核报告）
  darvas ui     [--addr]                     本地GUI（浏览器四视图，M6）
  darvas serve  [--addr]                     常驻模式（调度+GUI+备份，M7.1）
  darvas backtest --src <库> [--from <起>] [--to <止>] --mode prefill|progressive|regime|verify
                                             [--set key=value,key=value]
                                             内置回放器 M8：scratch库重放as-of六任务链；
                                             --set 仅覆盖 scratch 参数；verify=双模式差分审计

每日时间表（docs/01 §4 唯一事实源；此处由 scheduler.ScheduleSummary 派生，禁止他处硬编码）:
  ` + scheduler.ScheduleSummary() + ` → 备份轮转
`)
}

func dbPath(db string) string {
	if db != "" {
		return db
	}
	if env := os.Getenv("DARVAS_DB"); env != "" {
		return env
	}
	exe, err := os.Executable()
	if err == nil {
		return filepath.Join(filepath.Dir(exe), "var", "darvas.db")
	}
	return filepath.Join("var", "darvas.db")
}

func tdxRoot() string {
	if root := os.Getenv("DARVAS_TDX"); root != "" {
		return root
	}
	return "H:/new_tdx64/vipdoc" // 默认，可用 param tdx.root 覆盖
}

func runIngest(db, since, date string) {
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()
	root := st.ParamRepo.GetString("tdx.root", tdxRoot())
	reader := tdx.NewReader(root)

	g := ingest.New(reader, st, since)
	ctx := context.Background()

	var res contract.TaskResult
	if since == "full" || since == "FULL" || since == "all" || since == "ALL" {
		res, err = g.FullImport(ctx)
	} else {
		res, err = g.SyncIncremental(ctx, date)
	}
	must(err)
	printResult(res)

	q, err := g.Verify(ctx, date)
	must(err)
	fmt.Printf("[verify] 最新交易日=%s (期望 %s, fresh=%v) 当日截面股票数=%d\n",
		q.LatestTradeDate, date, q.Fresh, q.UpdatedCount)
	if !q.Fresh {
		fmt.Println("⚠ 数据不新鲜：请先在通达信客户端执行「下载数据」，再重跑 ingest")
	}
}

func runDoctor(db string) {
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()

	var ver int
	must(st.DB().QueryRow(`SELECT COALESCE(MAX(version),0) FROM schema_version`).Scan(&ver))
	fmt.Printf("[schema] version=%d\n", ver)

	var secCount, barCount int
	must(st.DB().QueryRow(`SELECT COUNT(*) FROM security`).Scan(&secCount))
	must(st.DB().QueryRow(`SELECT COUNT(*) FROM daily_bar`).Scan(&barCount))
	fmt.Printf("[data] securities=%d bars=%d\n", secCount, barCount)

	for _, idx := range []string{"sh000001", "sz399001"} {
		latest, err := st.BarRepo.LatestDate(idx)
		if err == nil && latest != "" {
			fmt.Printf("[calendar] %s 最新=%s\n", idx, latest)
		}
	}

	// 账本检查项（M4.6）：止损触发器在场 + 恒等式审计
	var trg int
	must(st.DB().QueryRow(`SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'
		AND name='trg_position_stop_no_down'`).Scan(&trg))
	if trg == 1 {
		fmt.Println("[ledger] 止损只升不降触发器: 在位")
	} else {
		fmt.Println("⚠ [ledger] 止损只升不降触发器缺失！请检查迁移")
	}
	led := ledger.New(st)
	ok, detail, err := led.AuditEquation(context.Background(), "")
	if err != nil {
		fmt.Printf("⚠ [ledger] 审计执行失败: %v\n", err)
	} else if ok {
		fmt.Printf("[ledger] 恒等式审计: %s\n", detail)
	} else {
		fmt.Printf("⚠ [ledger] 恒等式审计不通过: %s\n", detail)
	}

	// 订单一致性：执行日已过仍 pending 的订单
	var staleOrders int
	must(st.DB().QueryRow(`SELECT COUNT(*) FROM plan_order o JOIN trade_plan p ON p.id=o.trade_plan_id
		WHERE o.status='pending' AND p.execute_date < date('now','localtime')`).Scan(&staleOrders))
	if staleOrders > 0 {
		fmt.Printf("⚠ [ledger] %d 笔订单执行日已过仍 pending（跑 run 自动过期或人工核对）\n", staleOrders)
	}

	fmt.Println("[doctor] OK")
}

// runFills 成交粘贴录入（M4.5，ADR-4）：file 参数或 stdin 读入 → 解析预览 → 入库。
// 默认只预览；--yes 直接入库；交互终端（无重定向）时询问确认。
// C 扩展：file 为 .csv（炒股手导出，GBK）→ ParseChaoGushouCsv 第二数据源，
// 导入后打印执行质量审核报告（指令 vs 实际执行对照）。
func runFills(db string, args []string, yes bool) {
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()
	led := ledger.New(st)

	var fills []contract.Fill
	isCsv := len(args) > 0 && strings.EqualFold(filepath.Ext(args[0]), ".csv")
	if isCsv {
		b, err := os.ReadFile(args[0])
		must(err)
		fills, err = led.ParseChaoGushouCsv(b)
		must(err)
	} else {
		var raw string
		if len(args) > 0 {
			b, err := os.ReadFile(args[0])
			must(err)
			raw = string(b)
		} else {
			b, err := io.ReadAll(os.Stdin)
			must(err)
			raw = string(b)
		}
		if strings.TrimSpace(raw) == "" {
			fmt.Println("用法: darvas fills [file|--yes]   （或粘贴文本后 Ctrl+Z/Ctrl+D 结束；.csv=炒股手导出）")
			os.Exit(2)
		}
		fills, err = led.ParseFill(raw)
		must(err)
	}
	if isCsv {
		fmt.Println("[数据源] 炒股手训练软件 CSV（GBK，第二数据源）")
	}

	fmt.Printf("解析出 %d 笔成交:\n", len(fills))
	fmt.Println("日期        代码      方向  价格      数量    手续费")
	for _, f := range fills {
		fmt.Printf("%s  %-9s %-4s  %8.2f  %5d  %.2f\n",
			f.Date, f.Code, f.Side, f.Price, f.Qty, f.Fee)
	}
	if !yes {
		if isTerminal(os.Stdin) {
			fmt.Print("\n确认入库? [y/N] ")
			var ans string
			_, _ = fmt.Scanln(&ans)
			ans = strings.TrimSpace(strings.ToLower(ans))
			if ans != "y" && ans != "yes" {
				fmt.Println("已取消（未入库）。加 --yes 可跳过确认。")
				return
			}
		} else {
			fmt.Println("\n（预览模式：未入库。加 --yes 直接入库）")
			return
		}
	}

	results, err := led.ApplyFills(fills)
	must(err)
	applied, dup, matched, stopUp := 0, 0, 0, 0
	for _, r := range results {
		if r.Duplicate {
			dup++
			continue
		}
		applied++
		if r.Matched {
			matched++
		}
		if r.StopUpdated {
			stopUp++
		}
		fmt.Printf("✓ %s %s %.2f×%d", r.Fill.Code, r.Fill.Date, r.Fill.Price, r.Fill.Qty)
		if r.Matched {
			fmt.Printf(" → 订单#%d已成交", r.OrderID)
		} else {
			fmt.Print(" → 计划外成交")
		}
		if r.StopUpdated {
			fmt.Print("，止损已上移")
		}
		fmt.Println()
	}
	fmt.Printf("\n入库 %d 笔（匹配订单 %d，止损上移 %d，重复跳过 %d）\n", applied, matched, stopUp, dup)

	// C：执行质量审核报告（指令 vs 实际执行对照）
	if isCsv {
		fmt.Println()
		fmt.Print(led.FillsAuditReport(fills, results))
	}

	// 重算当日权益快照并审计
	dates := map[string]bool{}
	for _, f := range fills {
		dates[f.Date] = true
	}
	for d := range dates {
		must(led.Snapshot(context.Background(), d))
	}
	if ok, detail, err := led.AuditEquation(context.Background(), ""); err == nil {
		if ok {
			fmt.Printf("[审计] %s\n", detail)
		} else {
			fmt.Printf("⚠ [审计] %s\n", detail)
		}
	}
}

// isTerminal 判断 stdin 是否交互终端（非重定向）。
func isTerminal(f *os.File) bool {
	fi, err := f.Stat()
	if err != nil {
		return false
	}
	return fi.Mode()&os.ModeCharDevice != 0
}

func runParam(db string, args []string) {
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()
	if len(args) == 0 {
		all, err := st.ParamRepo.All()
		must(err)
		for k, v := range all {
			fmt.Printf("%s = %s\n", k, v)
		}
		return
	}
	switch args[0] {
	case "get":
		if len(args) < 2 {
			fmt.Println("用法: darvas param get <key>")
			os.Exit(2)
		}
		fmt.Println(st.ParamRepo.GetString(args[1], ""))
	case "set":
		if len(args) < 3 {
			fmt.Println("用法: darvas param set <key> <value>")
			os.Exit(2)
		}
		must(st.ParamRepo.Set(args[1], args[2]))
		fmt.Println("ok")
	default:
		fmt.Println("用法: darvas param get|set")
		os.Exit(2)
	}
}

func printResult(res contract.TaskResult) {
	fmt.Printf("[%s] status=%s rows=%d 耗时=%dms\n", res.Task, res.Status, res.AffectedRows, res.DurationMS)
	if res.Error != "" {
		fmt.Println("error:", res.Error)
	}
}

// runLadder 读 DB 中该 code 的全部 K 线 → 跑箱体引擎 → 持久化 + 打印阶梯。
// 这是 boxscan 的单股缩影（docs/04-api-contract.md §7 CLI），亦供 M2.6 真实数据人工核对。
func runLadder(db, code string) {
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()

	// 从 param 表读箱体参数（与 0001_init.sql 种子一致；MinBoxWidth 走 box.min_width，A2 校订）
	params := contract.BoxParams{
		VolMultiplier: st.ParamRepo.GetFloat("box.volmult", 2.0),
		VolWindow:     st.ParamRepo.GetInt("box.volwindow", 50),
		BreakPremium:  st.ParamRepo.GetFloat("buy.premium", 1.03),
		MinBoxWidth:   st.ParamRepo.GetFloat("box.min_width", 0.05),
	}

	// 读全部 K 线（endDate 取远期，n 取大值以取尽）
	bars, err := st.BarRepo.BarsWindow(code, "2999-12-31", 100000)
	must(err)
	if len(bars) == 0 {
		fmt.Printf("无 %s 的行情数据：请先 `darvas ingest --since full` 导入\n", code)
		return
	}

	eng := box.New()
	boxes := eng.Scan(bars, params)

	// 持久化（boxscan 重算替换语义，docs/02 §6）
	ids, err := st.BoxRepo.UpsertBoxes(boxes)
	must(err)
	_ = ids

	fmt.Printf("%s  K线 %d 根（%s ~ %s）  箱体 %d 个\n",
		code, len(bars), bars[0].Date, bars[len(bars)-1].Date, len(boxes))
	fmt.Println("ID   状态         上轨      下轨      上轨确认    下轨确认    前驱  突破日")
	fmt.Println("───  ──────────  ────────  ────────  ──────────  ──────────  ────  ──────────")
	for _, b := range boxes {
		fmt.Printf("%-4d %-12s %8.2f  %8.2f  %-10s  %-10s  %4d  %s\n",
			b.ID, b.Status, b.Top, b.Bottom,
			dashIfEmpty(b.TopConfirmed), dashIfEmpty(b.BottomConfirmed),
			b.PrevBoxID, dashIfEmpty(b.BrokenDate))
	}
	// 阶梯摘要（仅 confirmed/broken，跳过 invalid/forming）
	var ladder []contract.Box
	for _, b := range boxes {
		if b.Status == contract.BoxConfirmed || b.Status == contract.BoxBrokenUp || b.Status == contract.BoxBrokenDown {
			ladder = append(ladder, b)
		}
	}
	fmt.Printf("\n阶梯节点 %d 个: ", len(ladder))
	for _, b := range ladder {
		fmt.Printf("[%.0f/%.0f %s] ", b.Bottom, b.Top, ladderShort(b.Status))
	}
	fmt.Println()
}

func dashIfEmpty(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

func ladderShort(s contract.BoxStatus) string {
	switch s {
	case contract.BoxConfirmed:
		return "✓"
	case contract.BoxBrokenUp:
		return "↑"
	case contract.BoxBrokenDown:
		return "↓"
	case contract.BoxInvalid:
		return "✗"
	default:
		return string(s)
	}
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "错误:", err)
		os.Exit(1)
	}
}

// runPipeline 跑完整盘后流水线（docs/01 §4.1）并打开日报。
func runPipeline(db, date string) {
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()
	root := st.ParamRepo.GetString("tdx.root", tdxRoot())
	reader := tdx.NewReader(root)
	ing := ingest.New(reader, st, "")
	pipe := pipeline.New(st, ing)
	ctx := context.Background()
	results, err := pipe.RunDaily(ctx, date)
	must(err)
	fmt.Printf("=== 盘后流水线 %s 完成 ===\n", date)
	for _, r := range results {
		status := r.Status
		if r.Error != "" {
			status = r.Status + " (" + r.Error + ")"
		}
		fmt.Printf("  %-10s %-7s rows=%-8d %dms\n", r.Task, status, r.AffectedRows, r.DurationMS)
	}
	// 打开日报
	rp := pipe.ReportPath(date)
	if _, err := os.Stat(rp); err == nil {
		fmt.Printf("日报: %s\n", rp)
		_ = openReport(rp)
	} else {
		fmt.Println("日报未生成（report 任务可能失败）")
	}
}

// openReport 用系统默认浏览器打开 HTML。
func openReport(path string) error {
	abs, _ := filepath.Abs(path)
	switch runtime.GOOS {
	case "windows":
		return exec.Command("cmd", "/c", "start", "", abs).Start()
	case "darwin":
		return exec.Command("open", abs).Start()
	default:
		return exec.Command("xdg-open", abs).Start()
	}
}

// runRerun 单任务幂等补跑（docs/04 §7：boxscan/screen/poolsync/signal/plan/report）。
// 与 scheduler.runOverdue 共用 pipeline.RerunTask 引擎；留痕 daily_run。
func runRerun(db, date, task string) {
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()
	root := st.ParamRepo.GetString("tdx.root", tdxRoot())
	reader := tdx.NewReader(root)
	pipe := pipeline.New(st, ingest.New(reader, st, ""))
	res, err := pipe.RerunTask(context.Background(), date, task)
	must(err)
	printResult(res)
}

// runPool 跟踪池人工操作（B2；GUI 只读原则，状态修改走 CLI）：
//
//	darvas pool list [stage]                  池列表（默认全部活跃+挂起+黑名单）
//	darvas pool suspend <code> --resume <价>   条件挂起（"停传报价"：收盘≥激活价自动恢复）
//	darvas pool remove <code> --reason <文本> 人工移除（出池）
func runPool(db string, positional []string, resume float64, reason string) {
	if len(positional) == 0 {
		fmt.Println("用法: darvas pool list [stage] | suspend <code> --resume <价> | remove <code> --reason <文本>")
		os.Exit(2)
	}
	sub, args := positional[0], positional[1:]
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()
	m := pool.New(st)
	switch sub {
	case "list":
		stages := []contract.WatchStage{contract.StageWatching, contract.StagePending, contract.StageHolding,
			contract.StageSuspended, contract.StageBlacklisted, contract.StageExited}
		if len(args) > 0 {
			stages = []contract.WatchStage{contract.WatchStage(args[0])}
		}
		for _, stage := range stages {
			ws, err := st.PoolRepo.WatchByStage(stage)
			if err != nil {
				must(err)
			}
			if len(ws) == 0 {
				continue
			}
			fmt.Printf("== %s (%d) ==\n", stage, len(ws))
			for _, w := range ws {
				extra := ""
				if w.Stage == contract.StageSuspended {
					extra = fmt.Sprintf("  激活价=%.2f", w.ResumeTrigger)
				}
				if w.ExitReason != "" {
					extra += "  " + w.ExitReason
				}
				fmt.Printf("  %s  入池%s  箱数%d%s\n", w.Code, w.AddedDate, w.BoxCount, extra)
			}
		}
	case "suspend":
		if len(args) < 1 || resume <= 0 {
			fmt.Println("用法: darvas pool suspend <code> --resume <激活价>")
			os.Exit(2)
		}
		must(m.Suspend(args[0], resume))
		fmt.Printf("已挂起 %s（收盘≥%.2f 自动恢复跟踪）\n", args[0], resume)
	case "remove":
		if len(args) < 1 {
			fmt.Println("用法: darvas pool remove <code> --reason <文本>")
			os.Exit(2)
		}
		if reason == "" {
			reason = "manual"
		}
		must(m.Remove(args[0], reason))
		fmt.Printf("已移除 %s（%s）\n", args[0], reason)
	default:
		fmt.Println("未知子命令:", sub)
		os.Exit(2)
	}
}

// runReview 已交易股票历史库（B3，原书自称最重要的内容——三记录制度）。
// ①买入理由（reason_json）②卖出理由（close_reason+realized_pnl）自动落库；
// ③亏损原因/教训（trade_review）人工复盘录入。再纳入时 poolsync 留痕提示。
func runReview(db string, args []string, lesson, errorType, rule string) {
	if len(args) == 0 {
		fmt.Println("用法: darvas review list [code]                    战绩视图（无code=全部汇总；有code=单股明细）")
		fmt.Println("      darvas review add <position_id> --lesson <文本> [--error-type <文本>] [--rule <评价>]")
		os.Exit(2)
	}
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()
	switch args[0] {
	case "list":
		code := ""
		if len(args) > 1 {
			code = args[1]
		}
		if code == "" {
			rows, err := st.TradeRepo.ReviewSummary("")
			must(err)
			if len(rows) == 0 {
				fmt.Println("（暂无已完成的交易）")
				return
			}
			fmt.Printf("%-10s %-10s 笔数 胜/负  %-12s %s\n", "代码", "名称", "累计盈亏", "最新教训")
			var totTrades, totWins, totLosses int
			var totPnl float64
			for _, r := range rows {
				fmt.Printf("%-10s %-10s %3d  %d胜/%d负  %-12.2f %s\n",
					r.Code, r.Name, r.Trades, r.Wins, r.Losses, r.TotalPnl, ellipsis(r.Lesson, 40))
				totTrades += r.Trades
				totWins += r.Wins
				totLosses += r.Losses
				totPnl += r.TotalPnl
			}
			fmt.Printf("合计 %d 只股票：%d笔 %d胜/%d负 累计盈亏 %.2f\n",
				len(rows), totTrades, totWins, totLosses, totPnl)
			fmt.Println("单股明细: darvas review list <code>")
			return
		}
		// 单股明细
		sum, err := st.TradeRepo.ReviewSummary(code)
		must(err)
		if len(sum) == 0 {
			fmt.Printf("%s 暂无已完成的交易\n", code)
			return
		}
		s := sum[0]
		fmt.Printf("%s %s：%d笔 %d胜/%d负 累计盈亏 %.2f\n\n", s.Code, s.Name, s.Trades, s.Wins, s.Losses, s.TotalPnl)
		detail, err := st.TradeRepo.ReviewDetail(code)
		must(err)
		unreviewed := 0
		for _, d := range detail {
			fmt.Printf("position#%d  %s ~ %s  %d股  盈亏 %.2f  卖出原因:%s\n",
				d.PositionID, d.OpenedDate, d.ClosedDate, d.Qty, d.RealizedPnl, d.CloseReason)
			if d.Lesson == "" && d.ErrorType == "" {
				fmt.Println("    （未写复盘）darvas review add", d.PositionID, "--lesson <教训>")
				unreviewed++
				continue
			}
			fmt.Printf("    规则:%s  错误类型:%s\n    教训:%s\n", d.RuleFollowed, d.ErrorType, d.Lesson)
		}
		if unreviewed > 0 {
			fmt.Printf("\n⚠ %d 笔未写复盘（原书：每笔交易完成都必须记录教训）\n", unreviewed)
		}
	case "add":
		if len(args) < 2 {
			fmt.Println("用法: darvas review add <position_id> --lesson <文本> [--error-type <文本>] [--rule <评价>]")
			fmt.Println("position_id 见: darvas review list <code>")
			os.Exit(2)
		}
		id, err := strconv.ParseInt(args[1], 10, 64)
		if err != nil {
			fmt.Println("position_id 必须是数字（见 darvas review list <code>）")
			os.Exit(2)
		}
		if lesson == "" && errorType == "" && rule == "" {
			fmt.Println("至少提供 --lesson / --error-type / --rule 之一")
			os.Exit(2)
		}
		must(st.TradeRepo.AddReview(id, rule, errorType, lesson))
		fmt.Printf("已记录复盘：position#%d 教训=%q 错误类型=%q 规则=%q（重录覆盖）\n", id, lesson, errorType, rule)
	default:
		fmt.Println("未知子命令:", args[0])
		os.Exit(2)
	}
}

// ellipsis 超长文本截断（终端单行显示）。
func ellipsis(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

// runWeekly M7.2 周报：默认锚定最近周六（docs/01 §4.3 周六10:00的命令行入口）。
// 未显式指定 --date 时把缺省「今天」翻译为「最近周六」，覆盖往前7天区间。
func runWeekly(db, date string) {
	if date == time.Now().Format("2006-01-02") {
		now := time.Now()
		off := (int(now.Weekday()) - int(time.Saturday) + 7) % 7
		date = now.AddDate(0, 0, -off).Format("2006-01-02")
	}
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()
	root := st.ParamRepo.GetString("tdx.root", tdxRoot())
	reader := tdx.NewReader(root)
	pipe := pipeline.New(st, ingest.New(reader, st, ""))
	res, err := pipe.RerunTask(context.Background(), date, "weekly")
	must(err)
	printResult(res)
	if wp := pipe.WeeklyPath(date); res.Status == "ok" {
		fmt.Printf("周报: %s\n", wp)
		_ = openReport(wp)
	}
}

// runBacktest M8 内置回放器入口（PROMPT.md P4/M8.1）。scratch 独立库、源库只读、
// 观察期内规则冻结（回放只产出问题清单 archive/07-m8-issue-list.md）。
func runBacktest(db, from, to, src, mode, paramSets string) {
	o := backtest.Options{
		From:           from,
		To:             to,
		SrcDB:          src,
		ScratchDB:      "", // --db 默认值是生产 var/darvas.db，不可用作 scratch；显式传 --db 时才用
		Mode:           backtest.Mode(mode),
		ParamOverrides: parseParamOverrides(paramSets),
	}
	if db != "" {
		o.ScratchDB = db // Bootstrap 内置护栏：拒用 darvas.db / perf_test.db / 与源库同路径
	}
	if err := backtest.Run(o); err != nil {
		fmt.Fprintln(os.Stderr, "错误:", err)
		os.Exit(1)
	}
}

func parseParamOverrides(s string) map[string]string {
	out := map[string]string{}
	for _, pair := range strings.Split(s, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		key, value, ok := strings.Cut(pair, "=")
		if ok && key != "" {
			out[strings.TrimSpace(key)] = strings.TrimSpace(value)
		}
	}
	return out
}

// runUI 启动本地HTTP GUI（M6，docs/02 §2 GUI降级预案：embed静态资产+JSON API）。
// 起服务后自动打开系统浏览器；Ctrl+C 退出。
func runUI(db, addr string) {
	st, err := store.Open(dbPath(db))
	must(err)
	defer st.Close()
	must(gui.Run(st, addr, true))
}

// runServe 常驻模式（M7.1 无人值守）：启动即 CatchUp 补跑 + 按时刻表调度七任务链
// + 流水线收尾自动备份（NFR-3）+ 本地GUI。读写分离双连接（WAL：调度写+GUI读
// 互不阻塞，ADR-5）。用法：darvas serve [--addr 127.0.0.1:8787]。
func runServe(db, addr string) {
	dbp := dbPath(db)
	stW, err := store.Open(dbp) // 调度/写连接
	must(err)
	defer stW.Close()
	stR, err := store.Open(dbp) // GUI只读连接
	must(err)
	defer stR.Close()

	root := stW.ParamRepo.GetString("tdx.root", tdxRoot())
	reader := tdx.NewReader(root)
	pipe := pipeline.New(stW, ingest.New(reader, stW, ""))

	sched := scheduler.New(stW, pipe, reader)
	sched.OnDayDone = pipe.BackupDaily // report ok 后：VACUUM备份+30份轮转
	must(sched.CatchUp())
	must(sched.Start())
	defer sched.Stop()

	fmt.Printf("=== darvas 常驻模式（%s）===\n", version)
	fmt.Println("时刻表:", scheduler.ScheduleSummary(),
		"→ 备份 | 周六", scheduler.WeeklyClock, "weekly 周报")
	fmt.Printf("GUI: http://%s （Ctrl+C 退出）\n", addr)
	must(gui.Run(stR, addr, false)) // 常驻模式不自动弹浏览器，需要时手动打开
}
