// 诊断：全市场个股箱体事件宽度（M8.8 市场标定地基）。
// 思路：市场牛熊 = 系统自己的箱体引擎在全市场个股上的突破/破位事件流（不向外求）。
//   U(d) = d 日个股箱体 broken_up 事件数；D(d) = d 日 broken_down 事件数
//   ratio(D) = Σ20日U / (Σ20日U + Σ20日D)
// 输出 2004-2018 逐年 ratio 分布与候选阈值的年度判定，供拍板 regime.breadth.* 阈值。
// 只读 full_hist 源库；box.Scan 是纯函数折叠，事件序列与回测逐日扫描完全一致（零未来信息）。
// 跑法：go run ./scripts/dbg_breadth [db路径]
package main

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"

	_ "modernc.org/sqlite"

	"darvas/internal/box"
	"darvas/internal/contract"
)

type ev struct {
	date  string
	up    bool
	has60 bool // 事件发生时该股已有≥60根日线（剔除次新股噪声版）
}

func main() {
	path := "var/bt/full_hist_10pct.db"
	if len(os.Args) > 1 {
		path = os.Args[1]
	}
	dsn := "file:" + filepath.ToSlash(path) + "?mode=ro"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		fmt.Println("open:", err)
		return
	}
	defer db.Close()

	// 交易日历（sh000001）
	var cal []string
	{
		rows, err := db.Query(`SELECT CAST(date AS TEXT) FROM daily_bar WHERE code='sh000001' ORDER BY date`)
		if err != nil {
			fmt.Println("cal:", err)
			return
		}
		defer rows.Close()
		for rows.Next() {
			var d string
			rows.Scan(&d)
			cal = append(cal, d)
		}
	}
	fmt.Printf("交易日历 %d 天（%s ~ %s）\n", len(cal), cal[0], cal[len(cal)-1])

	// 全市场个股流式扫描（排除指数 sh000*/sz39*；一次查询按 code,date 排序，逐股分组处理）
	params := contract.BoxParams{VolMultiplier: 2.0, VolWindow: 50, BreakPremium: 1.03, MinBoxWidth: 0.05, ConfirmDays: 3}
	var events []ev
	nStock, nBars := 0, 0
	{
		rows, err := db.Query(`SELECT code, CAST(date AS TEXT), open, high, low, close, volume, amount
			FROM daily_bar
			WHERE (code LIKE 'sh6%' OR code LIKE 'sz0%' OR code LIKE 'sz3%')
			ORDER BY code, date`)
		if err != nil {
			fmt.Println("query:", err)
			return
		}
		defer rows.Close()
		var cur []contract.DailyBar
		flush := func() {
			if len(cur) == 0 {
				return
			}
			nStock++
			nBars += len(cur)
			boxes := box.New().Scan(cur, params)
			for _, b := range boxes {
				if b.Status != contract.BoxBrokenUp && b.Status != contract.BoxBrokenDown {
					continue
				}
				if b.BrokenDate == "" {
					continue
				}
				// 事件日的barsBefore：用箱体破位日在K线里的位置近似（够用：只判≥60）
				has60 := barsIdx(cur, b.BrokenDate) >= 60
				events = append(events, ev{date: b.BrokenDate, up: b.Status == contract.BoxBrokenUp, has60: has60})
			}
			cur = nil
		}
		for rows.Next() {
			var b contract.DailyBar
			if err := rows.Scan(&b.Code, &b.Date, &b.Open, &b.High, &b.Low, &b.Close, &b.Volume, &b.Amount); err != nil {
				fmt.Println("scan:", err)
				return
			}
			if len(cur) > 0 && cur[0].Code != b.Code {
				flush()
			}
			cur = append(cur, b)
		}
		flush()
	}
	fmt.Printf("个股 %d 只，K线 %d 根，箱体破位事件 %d 个\n\n", nStock, nBars, len(events))

	// 按日聚合
	upByDate := map[string]int{}
	downByDate := map[string]int{}
	up60, down60 := map[string]int{}, map[string]int{}
	for _, e := range events {
		if e.up {
			upByDate[e.date]++
			if e.has60 {
				up60[e.date]++
			}
		} else {
			downByDate[e.date]++
			if e.has60 {
				down60[e.date]++
			}
		}
	}

	// 滚动 20 交易日窗口 ratio
	const W = 20
	type dayStat struct {
		date                          string
		u, d                          int
		ratio, ratio60                float64
	}
	var stats []dayStat
	su, sd, su60, sd60 := 0, 0, 0, 0
	for i, d := range cal {
		su += upByDate[d]
		sd += downByDate[d]
		su60 += up60[d]
		sd60 += down60[d]
		if i >= W {
			old := cal[i-W]
			su -= upByDate[old]
			sd -= downByDate[old]
			su60 -= up60[old]
			sd60 -= down60[old]
		}
		if i < W {
			continue
		}
		ds := dayStat{date: d, u: su, d: sd}
		if su+sd > 0 {
			ds.ratio = float64(su) / float64(su+sd)
		}
		if su60+sd60 > 0 {
			ds.ratio60 = float64(su60) / float64(su60+sd60)
		}
		stats = append(stats, ds)
	}

	// 逐年统计
	fmt.Printf("%-6s %4s %7s %7s %8s | %s\n", "年", "天数", "日均U", "日均D", "avg ratio", "ratio≥0.50/0.55/0.60 的天数（全量 | 剔次新）")
	yearAgg := map[string]*struct {
		n                                        int
		su, sd                                   float64
		sr, sr60                                 float64
		b50, b55, b60, b50x, b55x, b60x          int
		bear50, bear40                           int // ratio<0.50 / <0.40（熊判定的候选）
		bear50x, bear40x                         int
	}{
	}
	for _, s := range stats {
		y := s.date[:4]
		if y < "2004" {
			continue
		}
		a := yearAgg[y]
		if a == nil {
			a = &struct {
				n                               int
				su, sd                          float64
				sr, sr60                        float64
				b50, b55, b60, b50x, b55x, b60x int
				bear50, bear40                  int
				bear50x, bear40x                int
			}{}
			yearAgg[y] = a
		}
		a.n++
		a.su += float64(s.u)
		a.sd += float64(s.d)
		a.sr += s.ratio
		a.sr60 += s.ratio60
		if s.ratio >= 0.50 {
			a.b50++
		}
		if s.ratio >= 0.55 {
			a.b55++
		}
		if s.ratio >= 0.60 {
			a.b60++
		}
		if s.ratio60 >= 0.50 {
			a.b50x++
		}
		if s.ratio60 >= 0.55 {
			a.b55x++
		}
		if s.ratio60 >= 0.60 {
			a.b60x++
		}
		if s.ratio < 0.50 {
			a.bear50++
		}
		if s.ratio < 0.40 {
			a.bear40++
		}
		if s.ratio60 < 0.50 {
			a.bear50x++
		}
		if s.ratio60 < 0.40 {
			a.bear40x++
		}
	}
	for y := "2004"; ; y = nextYear(y) {
		a := yearAgg[y]
		if a == nil {
			if y >= "2026" {
				break
			}
			continue
		}
		fmt.Printf("%-6s %4d %7.0f %7.0f %8.3f | %3d/%3d/%3d（%3d/%3d/%3d） 熊<0.5:%3d <0.4:%3d（剔新%3d/%3d）\n",
			y, a.n, a.su/float64(a.n), a.sd/float64(a.n), a.sr/float64(a.n),
			a.b50, a.b55, a.b60, a.b50x, a.b55x, a.b60x, a.bear50, a.bear40, a.bear50x, a.bear40x)
		if y == "2025" {
			break
		}
	}

	// 2014-2018 逐月 avg ratio（看切换节奏）
	fmt.Printf("\n== 2014-2018 逐月 avg ratio（全量 | 剔次新）==\n")
	monthAgg := map[string][3]float64{} // [n, sumRatio, sumRatio60]
	for _, s := range stats {
		if s.date < "2014-01-01" || s.date > "2018-12-31" {
			continue
		}
		m := monthAgg[s.date[:7]]
		m[0]++
		m[1] += s.ratio
		m[2] += s.ratio60
		monthAgg[s.date[:7]] = m
	}
	for m := "2014-01"; m <= "2018-12"; m = nextMonth(m) {
		a := monthAgg[m]
		if a[0] == 0 {
			continue
		}
		fmt.Printf("%s  %.3f | %.3f\n", m, a[1]/a[0], a[2]/a[0])
	}

	// 候选阈值切换事件（2014-2018，阈值0.55/0.40，剔次新版）
	fmt.Printf("\n== 候选规则切换事件（牛≥0.55 熊<0.40，剔次新，2014-2018）==\n")
	prev := ""
	for _, s := range stats {
		if s.date < "2014-01-01" {
			continue
		}
		cur := classify(s.ratio60)
		if cur != prev {
			fmt.Printf("  %s -> %s (U=%d D=%d ratio=%.3f)\n", s.date, cur, s.u, s.d, s.ratio60)
			prev = cur
		}
	}

	// 带 3 日确认的切换（过滤 1-2 日假脉冲；与箱体三日法则同构）
	fmt.Printf("\n== 3日确认版切换事件（牛≥0.55 熊<0.40 连续3日，剔次新）==\n")
	const CONF = 3
	state, runCand, runN := "range", "range", 0
	bullDaysByYear := map[string]int{}
	totDaysByYear := map[string]int{}
	for _, s := range stats {
		if s.date < "2004-01-01" {
			continue
		}
		totDaysByYear[s.date[:4]]++
		want := classify(s.ratio60)
		if want == runCand {
			runN++
		} else {
			runCand, runN = want, 1
		}
		if runN >= CONF && runCand != state {
			state = runCand
			fmt.Printf("  %s -> %s (U=%d D=%d ratio=%.3f)\n", s.date, state, s.u, s.d, s.ratio60)
		}
		if state == "bull" {
			bullDaysByYear[s.date[:4]]++
		}
	}
	fmt.Printf("\n== 3日确认版 逐年牛天数（主力仓启用天数）==\n")
	for y := "2004"; y <= "2026"; y = nextYear(y) {
		if totDaysByYear[y] == 0 {
			continue
		}
		fmt.Printf("  %s  %3d/%3d (%.0f%%)\n", y, bullDaysByYear[y], totDaysByYear[y], float64(bullDaysByYear[y])/float64(totDaysByYear[y])*100)
	}
}

func classify(r float64) string {
	if r >= 0.55 {
		return "bull"
	}
	if r < 0.40 {
		return "bear"
	}
	return "range"
}

func barsIdx(bars []contract.DailyBar, date string) int {
	for i := range bars {
		if bars[i].Date == date {
			return i
		}
	}
	// 破位日一般是最后一根：找不到（边界）返回 len-1
	return len(bars) - 1
}

func nextYear(y string) string {
	n := 0
	fmt.Sscanf(y, "%d", &n)
	return fmt.Sprintf("%04d", n+1)
}

func nextMonth(m string) string {
	var y, mo int
	fmt.Sscanf(m, "%d-%d", &y, &mo)
	mo++
	if mo > 12 {
		mo = 1
		y++
	}
	return fmt.Sprintf("%04d-%02d", y, mo)
}
