package regime

import (
	"math"
	"testing"

	"darvas/internal/contract"
)

func TestClassify(t *testing.T) {
	cases := []struct {
		mom  float64
		want Regime
	}{
		{0.30, Bull},
		{0.16, Bull},
		{0.15, Range}, // 边界：等于阈值不算牛
		{0.0, Range},
		{-0.15, Range},
		{-0.16, Bear},
		{-0.30, Bear},
	}
	for _, c := range cases {
		if got := Classify(c.mom, 0.15, 0.15); got != c.want {
			t.Errorf("Classify(%.2f)=%s want %s", c.mom, got, c.want)
		}
	}
}

func TestLabelBars(t *testing.T) {
	dates := []string{"d1", "d2", "d3", "d4", "d5", "d6"}
	closes := []float64{100, 110, 120, 130, 140, 150} // 一路涨，动量递增
	// n=2：d3 起有动量（d3/d1-1=20%牛），d1/d2 数据不足标 range
	labels := LabelBars(dates, closes, 2, 0.15, 0.15)
	if labels["d1"] != Range || labels["d2"] != Range {
		t.Errorf("前 n 日应标 range: %s %s", labels["d1"], labels["d2"])
	}
	// d3 动量 20% → bull；d4 30%→bull；d5 40%→bull
	for _, d := range []string{"d3", "d4", "d5", "d6"} {
		if labels[d] != Bull {
			t.Errorf("%s 应标 bull, got %s", d, labels[d])
		}
	}

	// 下跌序列
	closes2 := []float64{100, 90, 80, 70, 60, 50}
	labels2 := LabelBars(dates, closes2, 2, 0.15, 0.15)
	if labels2["d5"] != Bear || labels2["d6"] != Bear {
		t.Errorf("下跌序列应标 bear: d5=%s d6=%s", labels2["d5"], labels2["d6"])
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
	// 构造 6 天：牛牛猴猴熊熊；权益 100→110→120→115→110→100
	dates := []string{"d1", "d2", "d3", "d4", "d5", "d6"}
	totals := []float64{100, 110, 120, 115, 110, 100}
	labels := map[string]Regime{
		"d1": Bull, "d2": Bull, "d3": Range, "d4": Range, "d5": Bear, "d6": Bear,
	}
	var equities []contract.EquitySnapshot
	for i, d := range dates {
		e := eq(0, totals[i])
		e.Date = d
		if i > 0 {
			e = eq(totals[i-1], totals[i])
			e.Date = d
		}
		// d3 有持仓（市值>0）
		if d == "d3" || d == "d4" {
			e.MarketValue = 50
		}
		equities = append(equities, e)
	}
	// 两笔成交：d2 一笔、d4 一笔
	fills := []contract.Fill{{Date: "d2"}, {Date: "d4"}}
	// 一笔完结盈利交易 close 于 d5
	positions := []contract.Position{{Status: "closed", ClosedDate: "d5", RealizedPnl: 100}}

	segs, stats := Summarize(equities, labels, fills, positions)

	if len(segs) != 3 {
		t.Fatalf("段数 = %d, 期望 3", len(segs))
	}
	if segs[0].Regime != Bull || segs[0].Days != 2 || segs[0].Fills != 1 {
		t.Errorf("段0 = %+v, 期望 bull/2天/1成交", segs[0])
	}
	if !near(segs[0].Return, 0.10) { // 100→110 = +10%
		t.Errorf("段0 收益率 = %.4f, 期望 0.10", segs[0].Return)
	}
	if segs[1].Regime != Range || segs[1].Days != 2 || segs[1].ExposedDay != 2 {
		t.Errorf("段1 = %+v, 期望 range/2天/2暴露", segs[1])
	}
	if segs[2].Regime != Bear || segs[2].Wins != 1 {
		t.Errorf("段2 = %+v, 期望 bear/1胜", segs[2])
	}

	// 聚合：bull 1段2天、range 1段2天、bear 1段2天
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
