// 临时诊断3：换粒度（周线/月线）标指数，检验能否恢复牛熊识别能力（仍是箱体法，单一理论）。
// 跑法：go run ./scripts/dbg_regime3
package main

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"sort"
	"time"

	_ "modernc.org/sqlite"

	"darvas/internal/box"
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

// resample 按周/月聚合（日期取区间最后一个交易日，便于回查）。
func resample(bars []contract.DailyBar, span string) []contract.DailyBar {
	var out []contract.DailyBar
	var cur *contract.DailyBar
	var key string
	flush := func() {
		if cur != nil {
			out = append(out, *cur)
			cur = nil
		}
	}
	for _, b := range bars {
		t, err := time.Parse("2006-01-02", b.Date)
		if err != nil {
			continue
		}
		var k string
		if span == "month" {
			k = t.Format("2006-01")
		} else { // week
			y, w := t.ISOWeek()
			k = fmt.Sprintf("%04d-W%02d", y, w)
		}
		if k != key {
			flush()
			key = k
			nb := b
			cur = &nb
			continue
		}
		cur.Date = b.Date // 区间末交易日
		if b.High > cur.High {
			cur.High = b.High
		}
		if b.Low < cur.Low || cur.Low == 0 {
			cur.Low = b.Low
		}
		cur.Close = b.Close
		cur.Volume += b.Volume
		cur.Amount += b.Amount
	}
	flush()
	return out
}

func report(name string, daily []contract.DailyBar, coarse []contract.DailyBar, p contract.BoxParams) {
	labels := regime.LabelBoxes(coarse, p)
	// 粗粒度标注 → 映射回日线（用区间末日期向后延续）
	cnt := map[string]map[string]int{}
	for _, b := range coarse {
		y := b.Date[:4]
		if cnt[y] == nil {
			cnt[y] = map[string]int{}
		}
		cnt[y][string(labels[b.Date])]++
	}
	boxes := box.New().Scan(coarse, p)
	up, down, conf := 0, 0, 0
	upYears := map[string]int{}
	for _, b := range boxes {
		switch b.Status {
		case contract.BoxBrokenUp:
			up++
			if len(b.BrokenDate) >= 4 {
				upYears[b.BrokenDate[:4]]++
			}
		case contract.BoxBrokenDown:
			down++
		case contract.BoxConfirmed:
			conf++
		}
	}
	fmt.Printf("\n【%s】bars=%d confirmed=%d broken_up=%d broken_down=%d\n", name, len(coarse), conf, up, down)
	fmt.Printf("  broken_up 年份: ")
	var kys []string
	for k := range upYears {
		kys = append(kys, k)
	}
	sort.Strings(kys)
	for _, k := range kys {
		fmt.Printf("%s:%d ", k, upYears[k])
	}
	fmt.Println()
	fmt.Printf("  (粗粒度根数口径) %-6s %5s %5s %5s\n", "年", "牛", "猴", "熊")
	var yrs []string
	for y := range cnt {
		yrs = append(yrs, y)
	}
	sort.Strings(yrs)
	for _, y := range yrs {
		if y < "2004" {
			continue
		}
		m := cnt[y]
		fmt.Printf("  %-16s %5d %5d %5d\n", y, m["bull"], m["range"], m["bear"])
	}
}

func main() {
	daily := loadBars()
	fmt.Printf("日线 %d 根（%s ~ %s）\n", len(daily), daily[0].Date, daily[len(daily)-1].Date)

	baseline := contract.BoxParams{VolMultiplier: 2.0, VolWindow: 50, BreakPremium: 1.03, MinBoxWidth: 0.05, ConfirmDays: 3}
	relaxed := contract.BoxParams{VolMultiplier: 1.5, VolWindow: 20, BreakPremium: 1.02, MinBoxWidth: 0.03, ConfirmDays: 2}

	week := resample(daily, "week")
	month := resample(daily, "month")
	fmt.Printf("周线 %d 根、月线 %d 根\n", len(week), len(month))

	report("周线 + 基线参数", daily, week, baseline)
	report("周线 + 放宽参数(vm1.5 win20 prem1.02 w3% cd2)", daily, week, relaxed)
	report("月线 + 基线参数", daily, month, baseline)
	report("月线 + 放宽参数(vm1.5 win20 prem1.02 w3% cd2)", daily, month, relaxed)
}
