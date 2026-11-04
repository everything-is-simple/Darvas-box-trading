package store

import (
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"testing"

	"darvas/internal/contract"
)

// seedPriceFixture 造一个个股 + 一个指数、同一日两根K线的最小夹具。
// 原始价与后复权价刻意不同（close 10 vs 20），用来证明「读的是哪张表」。
func seedPriceFixture(t *testing.T, st *Store) {
	t.Helper()
	if _, err := st.DB().Exec(`INSERT INTO security(code, market, name, kind) VALUES
		('sh600000','sh','浦发银行','stock'), ('sh000001','sh','上证指数','index')`); err != nil {
		t.Fatalf("seed security: %v", err)
	}
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		{Code: "sh600000", Date: "2005-01-04", Open: 9, High: 11, Low: 8, Close: 10, Volume: 1000, Amount: 10000},
		{Code: "sh000001", Date: "2005-01-04", Open: 1200, High: 1250, Low: 1190, Close: 1230, Volume: 999, Amount: 1e8},
	}); err != nil {
		t.Fatalf("seed daily_bar: %v", err)
	}
	if _, err := st.BarRepo.UpsertAdjBars([]contract.AdjDailyBar{
		{Code: "sh600000", Date: "2005-01-04", Open: 18, High: 22, Low: 16, Close: 20,
			Volume: 500, Amount: 10000, FactorNorm: 2, Status: "ok"},
	}); err != nil {
		t.Fatalf("seed daily_bar_adj: %v", err)
	}
}

// TestDataModeRaw 空 adj 表 = raw 模式：所有价格读取保持原始价（0032 前的行为）。
func TestDataModeRaw(t *testing.T) {
	st, err := Open(filepath.Join(t.TempDir(), "raw.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer st.Close()
	seedPriceFixture(t, st)

	if got := st.DataMode(); got != "raw" {
		t.Fatalf("空 adj 表应为 raw，得到 %s", got)
	}
	c, ok, err := st.BarRepo.CloseOn("sh600000", "2005-01-04")
	if err != nil || !ok || c != 10 {
		t.Fatalf("raw 模式应读原价 10，得到 %v ok=%v err=%v", c, ok, err)
	}
}

// TestDataModeRejectsHalfLoaded 半灌库必须拒绝打开：表有行但 adj_ready != 1。
// 这是 docs/53 §8.2 的核心防线——不允许「灌了一半的库」被当成完整后复权库。
func TestDataModeRejectsHalfLoaded(t *testing.T) {
	db := filepath.Join(t.TempDir(), "half.db")
	st, err := Open(db)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	seedPriceFixture(t, st)
	st.Close()

	if _, err := Open(db); err == nil {
		t.Fatal("adj 表有行但无 adj_ready=1，Open 应报错，得到 nil")
	} else if !strings.Contains(err.Error(), "adj_ready") {
		t.Fatalf("错误应指明 adj_ready 验收标志，得到 %v", err)
	}
}

// TestDataModeAdj adj_ready=1 后：个股读后复权价，指数仍读原始价。
func TestDataModeAdj(t *testing.T) {
	db := filepath.Join(t.TempDir(), "adj.db")
	st, err := Open(db)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	seedPriceFixture(t, st)
	if _, err := st.DB().Exec(`INSERT INTO data_meta(k,v) VALUES('adj_ready','1')`); err != nil {
		t.Fatalf("set adj_ready: %v", err)
	}
	st.Close()

	st, err = Open(db)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer st.Close()
	if got := st.DataMode(); got != "adj" {
		t.Fatalf("应为 adj 模式，得到 %s", got)
	}

	// 价格类：个股读 adj
	if c, _, _ := st.BarRepo.CloseOn("sh600000", "2005-01-04"); c != 20 {
		t.Fatalf("adj 模式个股应读后复权价 20，得到 %v", c)
	}
	if v, _ := st.BarRepo.AvgVolume("sh600000", "2005-01-04", 5); v != 500 {
		t.Fatalf("adj 模式均量应读 adj_volume=500，得到 %v", v)
	}
	if a, _ := st.BarRepo.AvgAmount("sh600000", "2005-01-04", 5); a != 10000 {
		t.Fatalf("amount 不调整，应为 10000，得到 %v", a)
	}
	bars, err := st.BarRepo.BarsWindow("sh600000", "2005-01-04", 5)
	if err != nil || len(bars) != 1 || bars[0].Close != 20 || bars[0].Open != 18 {
		t.Fatalf("BarsWindow 应读后复权（open18/close20），得到 %+v err=%v", bars, err)
	}
	if c, _, _ := st.BarRepo.LastClose("sh600000"); c != 20 {
		t.Fatalf("LastClose 应读后复权价 20，得到 %v", c)
	}

	// 指数：永远读原表（adj 表不含指数，读空会让 MA60 闸门静默失效）
	if c, _, _ := st.BarRepo.CloseOn("sh000001", "2005-01-04"); c != 1230 {
		t.Fatalf("指数应读原始价 1230，得到 %v", c)
	}
	if bars, _ := st.BarRepo.BarsWindow("sh000001", "2005-01-04", 5); len(bars) != 1 || bars[0].Close != 1230 {
		t.Fatalf("指数 BarsWindow 应读原表，得到 %+v", bars)
	}

	// 日期类：永远 daily_bar（日期轴单一事实源）
	if d, _ := st.BarRepo.LatestDate("sh600000"); d != "2005-01-04" {
		t.Fatalf("LatestDate 应读 daily_bar，得到 %s", d)
	}
	if n, _ := st.BarRepo.BarCountClosed("sh600000", "2005-01-04", "2005-01-04"); n != 1 {
		t.Fatalf("BarCountClosed 应读 daily_bar，得到 %d", n)
	}
	if n, _ := st.BarRepo.CountBars("sh000001"); n != 1 {
		t.Fatalf("指数 CountBars 应读 daily_bar，得到 %d", n)
	}
}

// TestNoDirectDailyBarPriceRead 护栏：白名单之外的代码不得直接读 daily_bar 的价格列。
//
// 存在理由（docs/53 §8.3）：切表只改了已知读取点，**以后新加一条读价 SQL 就会静默读回原价**，
// 而账务照常平、没人发现。本测试把这条纪律变成编译期之外的强制检查。
//
// 白名单分两类：
//   - 日期类读取（LatestDate/PrevDate/NextDate/HasBar/Count*）：只读日期轴，允许
//   - 显式需要原始价的地方：ingest 写入、回放预载、对账脚本
func TestNoDirectDailyBarPriceRead(t *testing.T) {
	_, thisFile, _, _ := runtime.Caller(0)
	root := filepath.Join(filepath.Dir(thisFile), "..", "..")

	// 允许出现「FROM daily_bar + 价格列」的文件（其余一律视为违规）
	whitelist := map[string]bool{
		// 日期轴单一事实源：daily_bar 的 date 列读取集中在这里，价格列已走 dv.price
		"internal/store/repo_bar.go": true,
		// WatchOverview 最新收盘子查询：已按 r.dv.adj 显式二分支切表（adj 读 adj_close），
		// raw 分支读 daily_bar 正是 raw 模式的设计行为（行内 if r.dv.adj 可核）
		"internal/store/repo_pool.go": true,
		// 回放预载：从**源库**读原始价灌入 scratch，本就该读原表
		"internal/backtest/backtest.go": true,
		// regime 只读指数 sh000001，adj 表不含指数
		"internal/regimesweep/sweep.go": true,
		// 诊断/校验工具：读原始价是职责本身
		"cmd/a0check/main.go": true,
	}

	priceCols := regexp.MustCompile(`(?i)\b(open|high|low|close|volume)\b`)
	fromDaily := regexp.MustCompile(`(?i)FROM\s+daily_bar\b`)
	var violations []string

	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		name := filepath.Base(path)
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			return nil
		}
		rel := filepath.ToSlash(strings.TrimPrefix(path, root+string(filepath.Separator)))
		if whitelist[rel] {
			return nil
		}
		body, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		src := string(body)
		if !fromDaily.MatchString(src) {
			return nil
		}
		// 粗粒度但有意识：同一文件里既 FROM daily_bar 又出现价格列名即需人工复核。
		// 误报时把文件加进白名单并写明理由（比静默读回原价便宜得多）。
		for i, line := range strings.Split(src, "\n") {
			if fromDaily.MatchString(line) && priceCols.MatchString(line) {
				violations = append(violations, rel+":"+strconv.Itoa(i+1)+" "+strings.TrimSpace(line))
			}
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk: %v", err)
	}
	if len(violations) > 0 {
		t.Fatalf("发现绕过数据口径的原始价读取（0032 后价格类必须走 dv.price）：\n  %s",
			strings.Join(violations, "\n  "))
	}
}
