package regimesweep

import (
	"testing"

	"darvas/internal/regime"
)

// ─── 标签统计纯函数 ─────────────────────────────────────────────────────────

var testDates = []string{
	"2014-01-02", "2014-01-03", "2014-07-21", "2014-07-22",
	"2014-08-15", "2014-11-11", "2014-12-31",
}

var testLabels = map[string]regime.Regime{
	"2014-01-02": regime.Bear,
	"2014-01-03": regime.Bear,
	"2014-07-21": regime.Range,
	"2014-07-22": regime.Range,
	"2014-08-15": regime.Bull,
	"2014-11-11": regime.Bull,
	"2014-12-31": regime.Bull,
}

func TestCountRegime(t *testing.T) {
	if n := countRegime(testDates, testLabels, "2014-01-01", "2014-12-31", regime.Bull); n != 3 {
		t.Fatalf("bull days=%d want 3", n)
	}
	if n := countRegime(testDates, testLabels, "2014-01-01", "2014-07-21", regime.Bull); n != 0 {
		t.Fatalf("H1 bull days=%d want 0", n)
	}
	if n := countRegime(testDates, testLabels, "2014-01-01", "2014-01-31", regime.Bear); n != 2 {
		t.Fatalf("jan bear days=%d want 2", n)
	}
}

func TestFirstRegime(t *testing.T) {
	if d := firstRegime(testDates, testLabels, "2014-07-22", "2014-12-31", regime.Bull); d != "2014-08-15" {
		t.Fatalf("first bull=%q want 2014-08-15", d)
	}
	// 窗口早于首个牛日 → 空
	if d := firstRegime(testDates, testLabels, "2014-01-01", "2014-07-21", regime.Bull); d != "" {
		t.Fatalf("H1 first bull=%q want empty", d)
	}
	// 无熊标签的目标 → 空
	if d := firstRegime(testDates, testLabels, "2014-07-22", "2014-12-31", regime.Bear); d != "" {
		t.Fatalf("first bear=%q want empty", d)
	}
}

// ─── 达标判定（七条先验 guard）──────────────────────────────────────────────

func TestQualification(t *testing.T) {
	mk := func(flip2014, bear2015 string, fb14, fe15, cb15, fb16, fb18 int) Result {
		r := Result{
			Flip2014: flip2014, BearFlip2015: bear2015,
			FalseBull2014H1: fb14, FalseBear2015H1: fe15,
			CrashBull2015: cb15, FalseBull2016: fb16, FalseBull2018: fb18,
		}
		r.computeQualification()
		return r
	}
	// 全过 + 提前 → 达标
	if r := mk("2014-08-15", "2015-06-16", 0, 0, 0, 0, 0); !r.Qualifies || !r.Significant {
		t.Fatalf("全过组合应达标且显著: %+v", r)
	}
	// 达标但不显著（10-01 > 08-31）
	if r := mk("2014-10-01", "2015-06-16", 0, 0, 0, 0, 0); !r.Qualifies || r.Significant {
		t.Fatalf("10-01 应达标但不显著: %+v", r)
	}
	// 翻牛等于基线（11-11）→ 不达标（须严格提前）
	if r := mk("2014-11-11", "2015-06-16", 0, 0, 0, 0, 0); r.Qualifies {
		t.Fatalf("翻牛=基线不应达标: %+v", r)
	}
	// 无翻牛 → 不达标
	if r := mk("", "2015-06-16", 0, 0, 0, 0, 0); r.Qualifies {
		t.Fatalf("无翻牛不应达标: %+v", r)
	}
	// 翻熊回退（晚于 06-29）→ 不达标
	if r := mk("2014-08-15", "2015-07-06", 0, 0, 0, 0, 0); r.Qualifies {
		t.Fatalf("翻熊回退不应达标: %+v", r)
	}
	// 股灾段无熊 → 不达标
	if r := mk("2014-08-15", "", 0, 0, 0, 0, 0); r.Qualifies {
		t.Fatalf("无翻熊不应达标: %+v", r)
	}
	// 各假牛/假熊 guard 单独失败
	for i, r := range []Result{
		mk("2014-08-15", "2015-06-16", 1, 0, 0, 0, 0),
		mk("2014-08-15", "2015-06-16", 0, 1, 0, 0, 0),
		mk("2014-08-15", "2015-06-16", 0, 0, 1, 0, 0),
		mk("2014-08-15", "2015-06-16", 0, 0, 0, 1, 0),
		mk("2014-08-15", "2015-06-16", 0, 0, 0, 0, 1),
	} {
		if r.Qualifies {
			t.Fatalf("guard %d 违反应不达标: %+v", i+1, r)
		}
	}
}

// ─── 网格与邻域 ─────────────────────────────────────────────────────────────

func TestAllCombosCount(t *testing.T) {
	if n := len(AllCombos()); n != len(gridPremium)*len(gridWidth)*len(gridVolMult)*len(gridVolWin)*len(gridConfirm) {
		t.Fatalf("组合数=%d 与网格乘积不符", n)
	}
	// 基线必在网格内
	found := false
	for _, c := range AllCombos() {
		if c == Baseline() {
			found = true
		}
	}
	if !found {
		t.Fatal("基线参数不在网格内")
	}
}

func TestNeighbors(t *testing.T) {
	// 中间组合：5 维各 ±1（溢价±1、宽度±1、放量±1、窗口在2值网格只能-1、确认±1）= 9 邻居
	c := Combo{1.02, 0.03, 1.5, 50, 3}
	ns := neighbors(c)
	if len(ns) != 9 {
		t.Fatalf("中间组合邻居数=%d want 9: %+v", len(ns), ns)
	}
	// 基线组合：溢价是 gridPremium[3]（-1/+1）、宽度 gridWidth[2]（-1/+1）、
	// 放量 gridVolMult[2]（-1/+1）、窗口 gridVolWin[1]（只有-1）、确认 gridConfirm[1]（±1）= 9
	ns = neighbors(Baseline())
	if len(ns) != 9 {
		t.Fatalf("基线邻居数=%d want 9", len(ns))
	}
	// 角落组合（每维取首值）：只有 +1 方向 → 5 邻居
	ns = neighbors(Combo{1.00, 0.02, 1.0, 20, 2})
	if len(ns) != 5 {
		t.Fatalf("角落组合邻居数=%d want 5", len(ns))
	}
	// 邻居互异性
	seen := map[string]bool{}
	for _, n := range ns {
		if seen[n.key()] {
			t.Fatalf("邻居重复: %s", n.key())
		}
		seen[n.key()] = true
	}
}

func TestComboKeyUnique(t *testing.T) {
	seen := map[string]bool{}
	for _, c := range AllCombos() {
		if seen[c.key()] {
			t.Fatalf("key 冲突: %s", c.key())
		}
		seen[c.key()] = true
	}
}

// ─── 邻域稳定性 ─────────────────────────────────────────────────────────────

func TestComputeNeighborhood(t *testing.T) {
	results := map[string]*Result{}
	for _, c := range AllCombos() {
		r := Result{Combo: c}
		results[c.key()] = &r
	}
	// 基线与基线的全部 9 个邻居都达标 → 基线邻域率 9/9 → 稳定
	results[Baseline().key()].Qualifies = true
	for _, n := range neighbors(Baseline()) {
		results[n.key()].Qualifies = true
	}
	computeNeighborhood(results)
	b := results[Baseline().key()]
	if !b.Stable || b.NeighborQual != 9 || b.NeighborTot != 9 {
		t.Fatalf("基线应稳定 9/9: qual=%d tot=%d stable=%v", b.NeighborQual, b.NeighborTot, b.Stable)
	}
	// 孤立点：达标但邻居全不达标 → 不稳定
	corner := Combo{1.00, 0.02, 1.0, 20, 2}
	results[corner.key()].Qualifies = true
	computeNeighborhood(results)
	iso := results[corner.key()]
	if iso.Stable || iso.NeighborQual != 0 {
		t.Fatalf("孤立点不应稳定: %+v", iso)
	}
}
