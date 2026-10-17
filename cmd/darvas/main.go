// darvas —— 达瓦斯箱体交易系统入口。
// 子命令与 docs/04-api-contract.md §7 CLI同构：ingest/doctor/ladder/param/run...
// M0里程碑：ingest（全量/增量）与 doctor 可用，其余引擎按 M2-M5 里程碑补齐。
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"darvas/internal/contract"
	"darvas/internal/datasource/tdx"
	"darvas/internal/ingest"
	"darvas/internal/store"
)

const version = "0.1.0-m0"

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
	fs.Parse(os.Args[2:])

	switch cmd {
	case "version":
		fmt.Println("darvas", version)
	case "ingest":
		runIngest(*db, *since, *date)
	case "doctor":
		runDoctor(*db)
	case "ladder":
		code := fs.Arg(0)
		if code == "" {
			fmt.Println("用法: darvas ladder <code>  例: sh600519")
			os.Exit(2)
		}
		fmt.Printf("[M2] 箱体阶梯引擎建设中，code=%s\n", code)
	case "param":
		runParam(*db, fs.Args())
	case "run":
		fmt.Printf("[M5] 完整流水线建设中（当前可用: ingest）—— date=%s\n", *date)
	default:
		usage()
		os.Exit(2)
	}
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

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "错误:", err)
		os.Exit(1)
	}
}
