package regime

import (
	"math"
	"testing"
	"time"

	"darvas/internal/contract"
)

func d(n int) string {
	return time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC).AddDate(0, 0, n).Format("2006-01-02")
}

func mk(n int, h, l, c float64, v int64) contract.DailyBar {
	return contract.DailyBar{Code: "sh000001", Date: d(n), Open: c, High: h, Low: l, Close: c, Volume: v, Amount: 0}
}

// TestLabelBoxes 验证箱体法标注：confirmed 箱体（猴）→ 放量突破（牛）。
// 数据参考 box 引擎 confirmedBoxSeries(top=10, bottom=9)，末根 d(7) 为突破日。
func TestLabelBoxes(t *testing.T) {
	var series []contract.DailyBar
	series = append(series, mk(0, 10, 9, 9.9, 200))
	series = append(series, mk(1, 9.5, 9, 9.5, 200))
	series = append(series, mk(2, 9.8, 9, 9.8, 200))
	series = append(series, mk(3, 9.6, 9, 9.7, 200)) // 上轨确认 top=10
	series = append(series, mk(4, 9.9, 9.2, 9.8, 200))
	series = append(series, mk(5, 9.9, 9.3, 9.8, 200))
	series = append(series, mk(6, 9.9, 9.1, 9.8, 200)) // 下轨确认 bottom=9
	series = append(series, mk(7, 10.6, 9.9, 10.5, 500)) // 突破日 close 10.5 > 10×1.03 放量

	labels := LabelBoxes(series, contract.BoxParams{VolMultiplier: 2.0, VolWindow: 5, BreakPremium: 1.03, MinBoxWidth: 0.05})
	if labels[d(7)] != Bull {
		t.Errorf("突破日 d(7) 应标 bull, got %s", labels[d(7)])
	}
	for i := 0; i < 7; i++ {
		if labels[d(i)] != Range {
			t.Errorf("突破前 d(%d) 应标 range, got %s", i, labels[d(i)])
		}
	}
}

func eq(prev, next float64) contract.EquitySnapshot {
	dd := 0.0
	if prev > 0 {
		dd = (next - prev) / prev
	}
	return contract.EquitySnapshot{Date: "", Total: next, MarketValue: 0, Drawdown: dd}
}

func TestSummarize(t *testing.T) {
	dates := []string{"d1", "d2", "d3", "d4", "d5", "d6"}
	totals := []float64{100, 110, 120, 115, 110, 100}
	labels := map[string]Regime{
		"d1": Bull, "d2": Bull, "d3": Range, "d4": Range, "d5": Bear, "d6": Bear,
	}
	var equities []contract.EquitySnapshot
	for i, dd := range dates {
		e := eq(0, totals[i])
		e.Date = dd
		if i > 0 {
			e = eq(totals[i-1], totals[i])
			e.Date = dd
		}
		if dd == "d3" || dd == "d4" {
			e.MarketValue = 50
		}
		equities = append(equities, e)
	}
	fills := []contract.Fill{{Date: "d2"}, {Date: "d4"}}
	positions := []contract.Position{{Status: "closed", ClosedDate: "d5", RealizedPnl: 100}}

	segs, stats := Summarize(equities, labels, fills, positions)

	if len(segs) != 3 {
		t.Fatalf("段数 = %d, 期望 3", len(segs))
	}
	if segs[0].Regime != Bull || segs[0].Days != 2 || segs[0].Fills != 1 {
		t.Errorf("段0 = %+v, 期望 bull/2天/1成交", segs[0])
	}
	if !near(segs[0].Return, 0.10) {
		t.Errorf("段0 收益率 = %.4f, 期望 0.10", segs[0].Return)
	}
	if segs[1].Regime != Range || segs[1].Days != 2 || segs[1].ExposedDay != 2 {
		t.Errorf("段1 = %+v, 期望 range/2天/2暴露", segs[1])
	}
	if segs[2].Regime != Bear || segs[2].Wins != 1 {
		t.Errorf("段2 = %+v, 期望 bear/1胜", segs[2])
	}

	byRegime := map[Regime]*Stat{}
	for i := range stats {
		byRegime[stats[i].Regime] = &stats[i]
	}
	if byRegime[Bull].Days != 2 || byRegime[Bull].Fills != 1 {
		t.Errorf("bull 聚合 = %+v", byRegime[Bull])
	}
	if byRegime[Bear].Wins != 1 || byRegime[Bear].Losses != 0 {
		t.Errorf("bear 聚合 = %+v", byRegime[Bear])
	}
}

func near(a, b float64) bool { return math.Abs(a-b) < 1e-6 }
