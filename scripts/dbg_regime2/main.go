// 临时诊断2：对比不同箱体参数下 sh000001 的体制标注，定位「指数上箱体法退化」根因。
// 跑法：go run ./scripts/dbg_regime2
package main

import (
	"database/sql"
	"fmt"
	"path/filepath"

	_ "modernc.org/sqlite"

	"darvas/internal/box"
	"darvas/internal/contract"
	"darvas/internal/regime"
)

func loadBars() []contract.DailyBar {
	path := "var/bt/full_hist_10pct.db"
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro")
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

func main() {
	bars := loadBars()
	fmt.Printf("sh000001 %d 根（%s ~ %s）\n\n", len(bars), bars[0].Date, bars[len(bars)-1].Date)

	type cfg struct {
		name    string
		minW    float64
		volMult float64
		prem    float64
	}
	cfgs := []cfg{
		{"基线 volmult=2.0 width=5% prem=1.03", 0.05, 2.0, 1.03},
		{"仅放宽宽度 width=2%", 0.02, 2.0, 1.03},
		{"仅放宽放量 volmult=1.2", 0.05, 1.2, 1.03},
		{"仅放宽溢价 prem=1.01", 0.05, 2.0, 1.01},
		{"全放宽 w=2% vm=1.2 prem=1.01", 0.02, 1.2, 1.01},
		{"全放宽 w=1.5% vm=1.1 prem=1.005", 0.015, 1.1, 1.005},
	}

	for _, c := range cfgs {
		p := contract.BoxParams{VolMultiplier: c.volMult, VolWindow: 50, BreakPremium: c.prem, MinBoxWidth: c.minW, ConfirmDays: 3}
		boxes := box.New().Scan(bars, p)
		up, down, conf := 0, 0, 0
		byYearUp := map[string]int{}
		for _, b := range boxes {
			switch b.Status {
			case contract.BoxBrokenUp:
				up++
				if len(b.BrokenDate) >= 4 {
					byYearUp[b.BrokenDate[:4]]++
				}
			case contract.BoxBrokenDown:
				down++
			case contract.BoxConfirmed:
				conf++
			}
		}
		labels := regime.LabelBoxes(bars, p)
		cnt := map[string]map[string]int{}
		for _, b := range bars {
			y := b.Date[:4]
			if cnt[y] == nil {
				cnt[y] = map[string]int{}
			}
			cnt[y][string(labels[b.Date])]++
		}
		fmt.Printf("【%s】\n  boxes: confirmed=%d broken_up=%d broken_down=%d\n", c.name, conf, up, down)
		fmt.Printf("  broken_up 年份分布: %v\n", byYearUp)
		for _, y := range []string{"2014", "2015", "2016", "2017", "2018"} {
			if cnt[y] == nil {
				continue
			}
			bull := cnt[y]["bull"]
			rng := cnt[y]["range"]
			bear := cnt[y]["bear"]
			tot := bull + rng + bear
			if tot == 0 {
				continue
			}
			avg := (float64(bull)*2.0 + float64(rng)*0.5 + float64(bear)*0.2) / float64(tot)
			fmt.Printf("  %s: 牛%3d 猴%3d 熊%3d  混合系数%.2f\n", y, bull, rng, bear, avg)
		}
		fmt.Println()
	}
}
