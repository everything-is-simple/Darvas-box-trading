// 临时诊断：输出 sh000001 的箱体法体制标注分布（只读，不干扰回测）。
// 用途：回测跑完前预判 M8.7 仓位系数在各年/各月的分布，验证「慢牛年滞后」是否重现。
// 跑法：go run ./scripts/dbg_regime
package main

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	_ "modernc.org/sqlite"

	"darvas/internal/box"
	"darvas/internal/contract"
	"darvas/internal/regime"
)

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

	rows, err := db.Query(`SELECT CAST(date AS TEXT), open, high, low, close, volume, amount
		FROM daily_bar WHERE code = 'sh000001' AND date <= '2018-12-31'
		ORDER BY date`)
	if err != nil {
		fmt.Println("query:", err)
		return
	}
	defer rows.Close()
	var bars []contract.DailyBar
	for rows.Next() {
		var b contract.DailyBar
		if err := rows.Scan(&b.Date, &b.Open, &b.High, &b.Low, &b.Close, &b.Volume, &b.Amount); err != nil {
			fmt.Println("scan:", err)
			return
		}
		b.Code = "sh000001"
		bars = append(bars, b)
	}
	fmt.Printf("sh000001 bars %d（%s ~ %s）\n\n", len(bars), bars[0].Date, bars[len(bars)-1].Date)

	params := contract.BoxParams{VolMultiplier: 2.0, VolWindow: 50, BreakPremium: 1.03, MinBoxWidth: 0.05}
	labels := regime.LabelBoxes(bars, params)

	// 每年 × 每体制 天数
	type key struct{ year, r string }
	cnt := map[key]int{}
	for _, b := range bars {
		cnt[key{b.Date[:4], string(labels[b.Date])}]++
	}
	years := []string{"2015", "2016", "2017", "2018"}
	fmt.Printf("%-6s %6s %6s %6s  %s\n", "年", "牛", "猴", "熊", "仓位系数(牛2.0/猴0.5/熊0.2)混合")
	for _, y := range years {
		bull, rng, bear := cnt[key{y, "bull"}], cnt[key{y, "range"}], cnt[key{y, "bear"}]
		tot := bull + rng + bear
		if tot == 0 {
			continue
		}
		avg := (float64(bull)*2.0 + float64(rng)*0.5 + float64(bear)*0.2) / float64(tot)
		fmt.Printf("%-6s %6d %6d %6d  %.2f\n", y, bull, rng, bear, avg)
	}

	fmt.Printf("\n== 2017 逐月 ==\n%-8s %5s %5s %5s\n", "月", "牛", "猴", "熊")
	monthCnt := map[string][3]int{}
	for _, b := range bars {
		if b.Date[:4] != "2017" {
			continue
		}
		m := monthCnt[b.Date[:7]]
		switch labels[b.Date] {
		case regime.Bull:
			m[0]++
		case regime.Range:
			m[1]++
		default:
			m[2]++
		}
		monthCnt[b.Date[:7]] = m
	}
	var months []string
	for k := range monthCnt {
		months = append(months, k)
	}
	sort.Strings(months)
	for _, k := range months {
		m := monthCnt[k]
		fmt.Printf("%-8s %5d %5d %5d\n", k, m[0], m[1], m[2])
	}

	// 关键事件：突破/破位日
	fmt.Printf("\n== 2016-2018 体制切换事件 ==\n")
	prev := ""
	for _, b := range bars {
		r := string(labels[b.Date])
		if r != prev {
			if b.Date >= "2016-01-01" {
				fmt.Printf("  %s -> %s\n", b.Date, r)
			}
			prev = r
		}
	}

	// 箱体事件诊断：为什么 2016-2018 无突破？
	fmt.Printf("\n== box.Scan 箱体状态分布（全历史）==\n")
	boxes := box.New().Scan(bars, params)
	byYearStatus := map[string]map[string]int{}
	for _, b := range boxes {
		y := b.TopConfirmed
		if y == "" {
			y = b.BottomConfirmed
		}
		if len(y) >= 4 {
			y = y[:4]
		} else {
			y = "?"
		}
		if byYearStatus[y] == nil {
			byYearStatus[y] = map[string]int{}
		}
		byYearStatus[y][string(b.Status)]++
	}
	var ys []string
	for y := range byYearStatus {
		ys = append(ys, y)
	}
	sort.Strings(ys)
	fmt.Printf("%-6s %10s %12s %12s %10s\n", "年", "confirmed", "broken_up", "broken_down", "forming")
	for _, y := range ys {
		if y < "2014" {
			continue
		}
		m := byYearStatus[y]
		fmt.Printf("%-6s %10d %12d %12d %10d\n", y, m["confirmed"], m["broken_up"], m["broken_down"], m["forming"])
	}

	// 2017 年：是否有 confirmed 箱体？最高价 vs 上轨×1.03 差多少？
	fmt.Printf("\n== 2017 年 confirmed 箱体明细（前10个）==\n")
	n := 0
	for _, b := range boxes {
		if b.Status != contract.BoxConfirmed || b.TopConfirmed[:4] != "2017" {
			continue
		}
		fmt.Printf("  top=%.1f bottom=%.1f 宽=%.2f%% 上轨确认=%s 下轨确认=%s\n",
			b.Top, b.Bottom, (b.Top-b.Bottom)/b.Top*100, b.TopConfirmed, b.BottomConfirmed)
		if n++; n >= 10 {
			break
		}
	}
	if n == 0 {
		fmt.Println("  （2017 年无任何 confirmed 箱体）")
	}
}
