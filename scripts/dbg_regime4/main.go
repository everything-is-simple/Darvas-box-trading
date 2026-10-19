// 临时诊断4：动量法（第一版，60日±10%）在 2004-2018 的体制分布，与箱体法对比。
// 跑法：go run ./scripts/dbg_regime4
package main

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"sort"

	_ "modernc.org/sqlite"

	"darvas/internal/contract"
	"darvas/internal/regime"
)

func loadBars() []contract.DailyBar {
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash("var/bt/full_hist_10pct.db")+"?mode=ro")
	if err != nil {
		panic(err)
	}
	defer db.Close()
	rows, err := db.Query(`SELECT CAST(date AS TEXT), open, high, low, close, volume, amount
		FROM daily_bar WHERE code = 'sh000001' AND date <= '2018-12-31' ORDER BY date`)
	if err != nil {
		panic(err)
	}
	defer rows.Close()
	var bars []contract.DailyBar
	for rows.Next() {
		var b contract.DailyBar
		if err := rows.Scan(&b.Date, &b.Open, &b.High, &b.Low, &b.Close, &b.Volume, &b.Amount); err != nil {
			panic(err)
		}
		b.Code = "sh000001"
		bars = append(bars, b)
	}
	return bars
}

// momentumLabel 第一版动量法：过去 n 日涨跌幅 vs ±thresh。
func momentumLabel(bars []contract.DailyBar, n int, thresh float64) map[string]regime.Regime {
	out := make(map[string]regime.Regime, len(bars))
	for i, b := range bars {
		if i < n {
			out[b.Date] = regime.Range
			continue
		}
		m := b.Close/bars[i-n].Close - 1
		out[b.Date] = regime.Classify(m, thresh, thresh)
	}
	return out
}

func stats(name string, bars []contract.DailyBar, labels map[string]regime.Regime) {
	cnt := map[string][3]int{}
	for _, b := range bars {
		y := b.Date[:4]
		m := cnt[y]
		switch labels[b.Date] {
		case regime.Bull:
			m[0]++
		case regime.Range:
			m[1]++
		default:
			m[2]++
		}
		cnt[y] = m
	}
	fmt.Printf("\n【%s】\n%-6s %5s %5s %5s  %s\n", name, "年", "牛", "猴", "熊", "混合系数")
	var ys []string
	for y := range cnt {
		ys = append(ys, y)
	}
	sort.Strings(ys)
	for _, y := range ys {
		if y < "2004" {
			continue
		}
		m := cnt[y]
		tot := m[0] + m[1] + m[2]
		avg := (float64(m[0])*2.0 + float64(m[1])*0.5 + float64(m[2])*0.2) / float64(tot)
		fmt.Printf("%-6s %5d %5d %5d  %.2f\n", y, m[0], m[1], m[2], avg)
	}
}

func main() {
	bars := loadBars()
	fmt.Printf("sh000001 %d 根（%s ~ %s）\n", len(bars), bars[0].Date, bars[len(bars)-1].Date)

	stats("动量法 60日 ±10%（第一版原参数）", bars, momentumLabel(bars, 60, 0.10))
	stats("动量法 60日 ±8%", bars, momentumLabel(bars, 60, 0.08))
	stats("动量法 40日 ±8%", bars, momentumLabel(bars, 40, 0.08))
	stats("动量法 20日 ±5%", bars, momentumLabel(bars, 20, 0.05))
}
