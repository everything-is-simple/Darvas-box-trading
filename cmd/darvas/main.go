// darvas —— 达瓦斯箱体交易系统入口。
// 子命令与 docs/04-api-contract.md §7 CLI同构：ingest/doctor/ladder/param/run...
// M0里程碑：ingest（全量/增量）与 doctor 可用，其余引擎按 M2-M5 里程碑补齐。
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"darvas/internal/box"
	"darvas/internal/contract"
	"darvas/internal/datasource/tdx"
	"darvas/internal/ingest"
	"darvas/internal/pipeline"
	"darvas/internal/store"
)

const version = "0.2.0-m2"

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
  darvas ingest  [--date] [--since] [--db]   数据同步（默认增量；--since=full 全量）
  darvas doctor [--db]                       数据库/schema/数据新鲜度体检
  darvas ladder  <code>                      个股箱体阶梯（M2）
  darvas param   get|set                     参数读写
  darvas run    [--date]                     完整盘后流水线（M5）

每日时间表（docs/01-requirements.md §4）:
  15:30 ingest → 16:00 boxscan → 16:10 screen → 16:20 poolsync
  → 16:30 signal → 16:40 plan → 16:45 report
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
	if since == "full" || since == "FULL" {
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
	fmt.Println("[doctor] OK")
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

	// 从 param 表读箱体参数（与 0001_init.sql 种子一致）
	params := contract.BoxParams{
		VolMultiplier: st.ParamRepo.GetFloat("box.volmult", 2.0),
		VolWindow:     st.ParamRepo.GetInt("box.volwindow", 50),
		BreakPremium:  st.ParamRepo.GetFloat("buy.premium", 1.03),
		MinBoxWidth:   0.05,
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
