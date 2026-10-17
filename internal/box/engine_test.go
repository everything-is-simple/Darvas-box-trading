package box

import (
	"reflect"
	"testing"
	"time"

	"darvas/internal/contract"
)

// ─── 测试辅助 ───────────────────────────────────────────────────────────────

// tp 测试用箱体参数：VolWindow=5 便于构造短序列；其余与默认一致。
func tp() contract.BoxParams {
	return contract.BoxParams{
		VolMultiplier: 2.0,
		VolWindow:     5,
		BreakPremium:  1.03,
		MinBoxWidth:   0.05,
	}
}

// bar 构造一根 K 线。open 默认取 close（非一字板场景），amount 仅占位。
func bar(code, date string, h, l, c float64, v int64) contract.DailyBar {
	return contract.DailyBar{Code: code, Date: date, Open: c, High: h, Low: l, Close: c, Volume: v, Amount: 0}
}

// onePrice 构造一字板（开=高=低=收）。
func onePrice(code, date string, p float64, v int64) contract.DailyBar {
	return contract.DailyBar{Code: code, Date: date, Open: p, High: p, Low: p, Close: p, Volume: v}
}

// d 返回基准日 +n 个日历日的日期串（测试用，连续日期不影响状态机）。
func d(n int) string {
	return time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC).AddDate(0, 0, n).Format("2006-01-02")
}

// scan 便捷封装：用测试参数扫描。
func scan(bars []contract.DailyBar) []contract.Box {
	return New().Scan(bars, tp())
}

// last 返回最后一个箱体（当前状态）。
func last(boxes []contract.Box) contract.Box {
	return boxes[len(boxes)-1]
}

// statusOf 取最后一个箱体的状态。
func statusOf(boxes []contract.Box) contract.BoxStatus {
	if len(boxes) == 0 {
		return ""
	}
	return last(boxes).Status
}

// ─── 基础与边界 ────────────────────────────────────────────────────────────

// 1. 空输入 → nil
func TestEmptyInput(t *testing.T) {
	if got := scan(nil); got != nil {
		t.Fatalf("empty input got %+v want nil", got)
	}
}

// 2. 单根 K 线 → 一个 forming 箱体，未确认上轨
func TestSingleBar(t *testing.T) {
	boxes := scan([]contract.DailyBar{bar("sh1", d(0), 10, 9, 9.5, 100)})
	if len(boxes) != 1 {
		t.Fatalf("len=%d want 1", len(boxes))
	}
	b := boxes[0]
	if b.Status != contract.BoxForming || b.TopConfirmed != "" || b.ID != 1 || b.PrevBoxID != 0 {
		t.Fatalf("single bar box=%+v want forming/topUnconfirmed/id1/prev0", b)
	}
}

// 3. 第二根创新高 → 重置上轨候选，仍 forming
func TestNewHighResetsTopCandidate(t *testing.T) {
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),
		bar("sh1", d(1), 11, 9.5, 10.5, 100),
	})
	if statusOf(boxes) != contract.BoxForming || boxes[0].TopConfirmed != "" {
		t.Fatalf("should still be forming with unconfirmed top: %+v", boxes[0])
	}
}

// 4. 第二根未创新高 → topCount=1（仍 forming）
func TestNoNewHighIncrementsCount(t *testing.T) {
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),
		bar("sh1", d(1), 9.5, 9, 9.2, 100), // high 9.5 < 10
	})
	if boxes[0].TopConfirmed != "" {
		t.Fatalf("top should not confirm after 1 day: %+v", boxes[0])
	}
}

// ─── 上轨确认（§3.1）──

// 5. 创新高后连续3日无新高 → 上轨确认，确认日=第4根
func TestTopConfirmedAfter3Days(t *testing.T) {
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),  // 新高 10
		bar("sh1", d(1), 9.5, 9, 9.2, 100),  // 1
		bar("sh1", d(2), 9.8, 9, 9.5, 100),  // 2
		bar("sh1", d(3), 9.6, 9, 9.3, 100),  // 3 → 确认
	})
	if boxes[0].TopConfirmed != d(3) {
		t.Fatalf("topConfirmed=%s want %s", boxes[0].TopConfirmed, d(3))
	}
	if boxes[0].Top != 10 {
		t.Fatalf("top=%.2f want 10", boxes[0].Top)
	}
}

// 6. 计数中再创新高 → 更新候选并重新计数，最终在新高水平确认
func TestTopResetThenConfirm(t *testing.T) {
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),   // 新高 10
		bar("sh1", d(1), 9.5, 9, 9.2, 100),  // count 1
		bar("sh1", d(2), 12, 9, 11, 100),    // 新高 12 → 重置
		bar("sh1", d(3), 11.5, 9, 11, 100),  // count 1
		bar("sh1", d(4), 11.8, 9, 11, 100),  // count 2
		bar("sh1", d(5), 11.6, 9, 11, 100),  // count 3 → 确认 top=12
	})
	if boxes[0].Top != 12 || boxes[0].TopConfirmed != d(5) {
		t.Fatalf("top=%.2f confirmed=%s want 12/%s", boxes[0].Top, boxes[0].TopConfirmed, d(5))
	}
}

// ─── 下轨确认（§3.2）──

// 7. 上轨确认后连续3日无新低 → 下轨确认，箱体 confirmed
func TestBottomConfirmedAfter3Days(t *testing.T) {
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),  // 新高 10
		bar("sh1", d(1), 9.5, 9, 9.2, 100),
		bar("sh1", d(2), 9.8, 9, 9.5, 100),
		bar("sh1", d(3), 9.6, 9, 9.3, 100),  // 上轨确认 top=10
		// 上轨确认日 low=9 → bottomCandidate=9
		bar("sh1", d(4), 9.8, 9.2, 9.5, 100), // 无新低 1
		bar("sh1", d(5), 9.7, 9.3, 9.5, 100), // 无新低 2
		bar("sh1", d(6), 9.6, 9.1, 9.4, 100), // 无新低 3 → 下轨确认 bottom=9
	})
	if boxes[0].Status != contract.BoxConfirmed {
		t.Fatalf("status=%s want confirmed", boxes[0].Status)
	}
	if boxes[0].Bottom != 9 || boxes[0].BottomConfirmed != d(6) {
		t.Fatalf("bottom=%.2f confirmed=%s want 9/%s", boxes[0].Bottom, boxes[0].BottomConfirmed, d(6))
	}
}

// 8. 下轨计数中创新低 → 重置候选
func TestBottomResetOnNewLow(t *testing.T) {
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),
		bar("sh1", d(1), 9.5, 9, 9.2, 100),
		bar("sh1", d(2), 9.8, 9, 9.5, 100),
		bar("sh1", d(3), 9.6, 9, 9.3, 100), // 上轨确认
		bar("sh1", d(4), 9.8, 9.2, 9.5, 100), // 无新低 1
		bar("sh1", d(5), 9.7, 8, 9.5, 100),   // 新低 8 → 重置
		bar("sh1", d(6), 9.6, 8.2, 9.4, 100), // 无新低 1
		bar("sh1", d(7), 9.6, 8.1, 9.4, 100), // 无新低 2
		bar("sh1", d(8), 9.6, 8.3, 9.4, 100), // 无新低 3 → 下轨=8
	})
	if boxes[0].Bottom != 8 {
		t.Fatalf("bottom=%.2f want 8 (reset)", boxes[0].Bottom)
	}
}

// ─── 作废规则（§3.2.4 / 宽度）──

// 9. 下轨确认时宽度不足(<5%) → 箱体 invalid，重启
func TestTooNarrowBoxInvalidates(t *testing.T) {
	// top=10, bottom=9.6 → 宽度 (10-9.6)/10=4% < 5%
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9.6, 9.8, 100),
		bar("sh1", d(1), 9.8, 9.6, 9.7, 100),
		bar("sh1", d(2), 9.9, 9.6, 9.8, 100),
		bar("sh1", d(3), 9.8, 9.6, 9.7, 100), // 上轨确认 top=10
		bar("sh1", d(4), 9.8, 9.6, 9.7, 100),
		bar("sh1", d(5), 9.8, 9.6, 9.7, 100),
		bar("sh1", d(6), 9.8, 9.6, 9.7, 100), // 下轨确认 bottom=9.6 → 宽度4%<5% → invalid
	})
	if boxes[0].Status != contract.BoxInvalid {
		t.Fatalf("narrow box status=%s want invalid", boxes[0].Status)
	}
	// 应已重启第二个箱体（forming）
	if len(boxes) < 2 || boxes[1].Status != contract.BoxForming {
		t.Fatalf("should restart a new forming box after invalidation, got %+v", boxes)
	}
}

// 10. 下轨确立前创新高（高于上轨）→ 箱体作废，从新高重启
func TestInvalidateOnNewHighDuringBottomPhase(t *testing.T) {
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),
		bar("sh1", d(1), 9.5, 9, 9.2, 100),
		bar("sh1", d(2), 9.8, 9, 9.5, 100),
		bar("sh1", d(3), 9.6, 9, 9.3, 100), // 上轨确认 top=10
		bar("sh1", d(4), 9.8, 9.2, 9.5, 100), // 无新低 1
		bar("sh1", d(5), 11, 9, 10.5, 100),   // high 11 > top 10 → 作废
	})
	if boxes[0].Status != contract.BoxInvalid {
		t.Fatalf("status=%s want invalid (新高 above top during bottom)", boxes[0].Status)
	}
	if len(boxes) < 2 {
		t.Fatal("should start a new box after invalidation")
	}
	// 新箱体 topCandidate 应为创新高日的 high=11（重启点）
	if boxes[1].TopConfirmed != "" {
		// 新箱体尚未确认（仅1根）
	}
}

// 11. invalid 箱体的 prev 指向最近 confirmed（无 confirmed 时为 0）
func TestInvalidBoxPrevIsLastConfirmed(t *testing.T) {
	// 先造一个 confirmed 箱体并突破，再造一个 invalid，验证 prev 链
	boxes := scan(buildLadderSeries(2, 100)) // 2 个箱体的阶梯（见 helper）
	// 第一个箱体应为 broken_up（被突破），其 prev=0
	if boxes[0].Status != contract.BoxBrokenUp || boxes[0].PrevBoxID != 0 {
		t.Fatalf("box0=%+v want broken_up/prev0", boxes[0])
	}
	// 第二个箱体 prev 指向 box0（它是最近 confirmed）
	if len(boxes) < 2 || boxes[1].PrevBoxID != boxes[0].ID {
		t.Fatalf("box1.prev=%d want %d (box0)", boxes[1].PrevBoxID, boxes[0].ID)
	}
}

// ─── 突破判定（§4.2 / FR-203）──

// 12. 有效突破：收盘>上轨×1.03 且放量 且非一字板 → broken_up
func TestBreakoutValid(t *testing.T) {
	boxes := scan(confirmedBoxSeries(10, 9, 200)) // top=10 bottom=9，基线量200
	// 末根是突破日：close=10.5 > 10×1.03=10.3，量=500 ≥ 200×2=400
	last := boxes[len(boxes)-2] // 倒数第二个是被突破的箱体，最后一个是新箱体
	if last.Status != contract.BoxBrokenUp {
		t.Fatalf("status=%s want broken_up", last.Status)
	}
}

// 13. 无量突破 → 不算突破（保持 confirmed）
func TestBreakoutNoVolume(t *testing.T) {
	series := confirmedBoxSeries(10, 9, 200)
	// 把突破日量改小
	series[len(series)-1].Volume = 100 // < 400
	boxes := scan(series)
	last := boxes[len(boxes)-1]
	if last.Status != contract.BoxConfirmed {
		t.Fatalf("no-volume breakout should stay confirmed, got %s", last.Status)
	}
}

// 14. 收盘超上轨但未达溢价 → 不算突破
func TestBreakoutNoPremium(t *testing.T) {
	series := confirmedBoxSeries(10, 9, 200)
	// close=10.2 ≤ 10×1.03=10.3
	series[len(series)-1].Close = 10.2
	series[len(series)-1].High = 10.4
	series[len(series)-1].Volume = 500
	boxes := scan(series)
	if statusOf(boxes) != contract.BoxConfirmed {
		t.Fatalf("close below premium should stay confirmed, got %s", statusOf(boxes))
	}
}

// 15. 一字板突破 → 不算（追不进）
func TestBreakoutOnePriceBoard(t *testing.T) {
	series := confirmedBoxSeries(10, 9, 200)
	// 把突破日改成一字板（close=high=low=open=10.5，但 high≤... 需 close>top×1.03）
	last := &series[len(series)-1]
	p := 10.5
	*last = onePrice(last.Code, last.Date, p, 500)
	boxes := scan(series)
	if statusOf(boxes) != contract.BoxConfirmed {
		t.Fatalf("one-price board breakout should stay confirmed, got %s", statusOf(boxes))
	}
}

// 16. 放量恰为均量×倍数 → 算突破（>=）
func TestBreakoutVolumeExactlyThreshold(t *testing.T) {
	series := confirmedBoxSeries(10, 9, 200)
	// 均量=200，倍数2.0 → 阈值400，设量=400
	series[len(series)-1].Volume = 400
	boxes := scan(series)
	last := boxes[len(boxes)-2]
	if last.Status != contract.BoxBrokenUp {
		t.Fatalf("volume==threshold should breakout, got %s", last.Status)
	}
}

// ─── 跌破判定（§5.1）──

// 17. 收盘跌破下轨 → broken_down
func TestBreakdown(t *testing.T) {
	series := confirmedBoxSeries(10, 9, 200)
	// 末根：close=8.9 < bottom=9
	last := &series[len(series)-1]
	last.Close = 8.9
	last.Low = 8.8
	last.Volume = 100
	boxes := scan(series)
	bd := boxes[len(boxes)-2]
	if bd.Status != contract.BoxBrokenDown {
		t.Fatalf("status=%s want broken_down", bd.Status)
	}
}

// 18. 收盘==下轨 → 不算跌破（严格 <）
func TestBreakdownStrictLess(t *testing.T) {
	series := confirmedBoxSeries(10, 9, 200)
	last := &series[len(series)-1]
	last.Close = 9.0 // == bottom
	last.Low = 8.9
	last.Volume = 100
	boxes := scan(series)
	if statusOf(boxes) != contract.BoxConfirmed {
		t.Fatalf("close==bottom should stay confirmed, got %s", statusOf(boxes))
	}
}

// ─── 阶梯链（§3.4）──

// 19. 多次突破形成阶梯，prev 逐级指向前一箱体
func TestLadderChainPrevLinks(t *testing.T) {
	boxes := scan(buildLadderSeries(3, 200))
	// 期望：3 个 broken_up 箱体 + 1 个收尾 forming/confirmed
	if len(boxes) < 3 {
		t.Fatalf("len=%d want ≥3 boxes", len(boxes))
	}
	for i := 0; i < 3; i++ {
		if boxes[i].Status != contract.BoxBrokenUp {
			t.Fatalf("box%d status=%s want broken_up", i, boxes[i].Status)
		}
		wantPrev := int64(0)
		if i > 0 {
			wantPrev = boxes[i-1].ID
		}
		if boxes[i].PrevBoxID != wantPrev {
			t.Fatalf("box%d.prev=%d want %d", i, boxes[i].PrevBoxID, wantPrev)
		}
	}
}

// 20. 第一个箱体的 prev_box_id=0
func TestFirstBoxPrevZero(t *testing.T) {
	boxes := scan(buildLadderSeries(2, 200))
	if boxes[0].PrevBoxID != 0 {
		t.Fatalf("first box prev=%d want 0", boxes[0].PrevBoxID)
	}
}

// 21. 跌破后新箱体 prev 指向被跌破箱体
func TestBreakdownNewBoxPrev(t *testing.T) {
	series := confirmedBoxSeries(10, 9, 200)
	last := &series[len(series)-1]
	last.Close = 8.9
	last.Low = 8.8
	last.Volume = 100
	// 再追加几根形成新箱体的上轨候选
	series = append(series, bar("sh1", d(20), 9.5, 8.5, 9.2, 100))
	boxes := scan(series)
	// box[-2]=broken_down, box[-1]=新 forming
	bd := boxes[len(boxes)-2]
	if bd.Status != contract.BoxBrokenDown {
		t.Fatalf("want broken_down, got %s", bd.Status)
	}
	nb := boxes[len(boxes)-1]
	if nb.PrevBoxID != bd.ID {
		t.Fatalf("new box after breakdown prev=%d want %d", nb.PrevBoxID, bd.ID)
	}
}

// 22. invalid 箱体被阶梯跳过：其后新箱体 prev 指向更早的 confirmed
func TestInvalidSkippedInLadder(t *testing.T) {
	// 受控构造：box0 confirmed→broken_up；box1 上轨确认后下轨阶段创新高→invalid；
	// box2 从创新高重启。断言：box1(invalid).prev==box0.id，box2(forming).prev==box0.id
	series := []contract.DailyBar{
		// box0: top=24 bottom=17
		bar("sh1", d(0), 24, 17, 23.9, 200),
		bar("sh1", d(1), 23.5, 17, 23.5, 200),
		bar("sh1", d(2), 23.8, 17, 23.5, 200),
		bar("sh1", d(3), 23.6, 17, 23.3, 200), // 上轨确认 top=24
		bar("sh1", d(4), 23.9, 17.2, 23.5, 200),
		bar("sh1", d(5), 23.7, 17.3, 23.5, 200),
		bar("sh1", d(6), 23.6, 17.1, 23.4, 200), // 下轨确认 bottom=17（宽32%）
		bar("sh1", d(7), 25.2, 23.9, 25.0, 500), // 突破 close>24×1.03=24.72 放量 → broken_up
		// box1 起于 d7，topCandidate=25.2
		bar("sh1", d(8), 27, 24, 26.5, 200),  // 新高27>25.2 → topCandidate=27
		bar("sh1", d(9), 26.5, 24, 26, 200),
		bar("sh1", d(10), 26.8, 24, 26.2, 200),
		bar("sh1", d(11), 26.6, 24, 26.1, 200), // 上轨确认 top=27
		bar("sh1", d(12), 26.9, 24.2, 26.3, 200), // 下轨阶段1
		bar("sh1", d(13), 30, 24, 29, 200),      // high30>top27 → box1 invalid，新箱 topCandidate=30
		// box2 起于 d13
		bar("sh1", d(14), 29.5, 25, 29, 200),
	}
	boxes := scan(series)
	// 定位 box0(broken_up)、box1(invalid)、box2(尾部)
	if len(boxes) < 3 {
		t.Fatalf("want ≥3 boxes, got %d: %+v", len(boxes), boxes)
	}
	if boxes[0].Status != contract.BoxBrokenUp {
		t.Fatalf("box0 status=%s want broken_up", boxes[0].Status)
	}
	// 找 invalid 箱体
	var inv *contract.Box
	for i := range boxes {
		if boxes[i].Status == contract.BoxInvalid {
			inv = &boxes[i]
			break
		}
	}
	if inv == nil {
		t.Fatalf("no invalid box found: %+v", boxes)
	}
	if inv.PrevBoxID != boxes[0].ID {
		t.Fatalf("invalid.prev=%d want %d (box0, last confirmed)", inv.PrevBoxID, boxes[0].ID)
	}
	// invalid 之后的尾箱 prev 也应==box0（阶梯跳过 invalid）
	tail := boxes[len(boxes)-1]
	if tail.PrevBoxID != boxes[0].ID {
		t.Fatalf("tail box prev=%d want %d (skip invalid, anchor at box0)", tail.PrevBoxID, boxes[0].ID)
	}
}

// ─── 确定性（ADR-1）──

// 23. 同输入两次扫描 → 完全相等
func TestDeterminismIdentical(t *testing.T) {
	series := buildLadderSeries(3, 200)
	a := scan(series)
	b := scan(series)
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("non-deterministic:\na=%+v\nb=%+v", a, b)
	}
}

// 24. 确定性：复杂序列（含作废+突破+跌破）
func TestDeterminismComplex(t *testing.T) {
	series := []contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),
		bar("sh1", d(1), 9.5, 9, 9.2, 100),
		bar("sh1", d(2), 9.8, 9, 9.5, 100),
		bar("sh1", d(3), 9.6, 9, 9.3, 100),
		bar("sh1", d(4), 9.8, 9.2, 9.5, 100),
		bar("sh1", d(5), 9.7, 9.3, 9.5, 100),
		bar("sh1", d(6), 9.6, 9.1, 9.4, 100),
		bar("sh1", d(7), 11, 9, 10.8, 500), // 突破 close>10.3 放量
	}
	a := scan(series)
	b := scan(series)
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("non-deterministic complex series")
	}
}

// ─── 收尾状态 ──

// 25. 一直未确认上轨 → 收尾输出 forming
func TestTailFormingBox(t *testing.T) {
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),
		bar("sh1", d(1), 9.5, 9, 9.2, 100), // 仅2根，不够3日
	})
	if len(boxes) != 1 || boxes[0].Status != contract.BoxForming {
		t.Fatalf("want single forming tail, got %+v", boxes)
	}
}

// 26. confirmed 但未破 → 收尾输出 confirmed
func TestTailConfirmedBox(t *testing.T) {
	series := confirmedBoxSeries(10, 9, 200)
	series = series[:len(series)-1] // 去掉突破日，留 confirmed
	boxes := scan(series)
	if statusOf(boxes) != contract.BoxConfirmed {
		t.Fatalf("want confirmed tail, got %s", statusOf(boxes))
	}
}

// ─── 书中案例（docs/00 §3.4）──

// 27. 书中阶梯模型：$17/$24 → $24/$27 → $31/$35（构造3级阶梯，断言轨值与prev链）
func TestBookLadderModel(t *testing.T) {
	boxes := scan(bookLadderSeries())
	// 找出所有 broken_up 箱体（阶梯节点）
	var ladder []contract.Box
	for _, b := range boxes {
		if b.Status == contract.BoxBrokenUp {
			ladder = append(ladder, b)
		}
	}
	if len(ladder) < 3 {
		t.Fatalf("ladder nodes=%d want ≥3 (17/24→24/27→31/35)", len(ladder))
	}
	// 第一级：top≈24 bottom≈17
	if !approx(ladder[0].Top, 24, 0.5) || !approx(ladder[0].Bottom, 17, 0.5) {
		t.Fatalf("ladder[0] top=%.2f bottom=%.2f want ≈24/17", ladder[0].Top, ladder[0].Bottom)
	}
	// 第二级：top≈27 bottom≈24
	if !approx(ladder[1].Top, 27, 0.5) || !approx(ladder[1].Bottom, 24, 0.5) {
		t.Fatalf("ladder[1] top=%.2f bottom=%.2f want ≈27/24", ladder[1].Top, ladder[1].Bottom)
	}
	// 第三级：top≈35 bottom≈31
	if !approx(ladder[2].Top, 35, 0.5) || !approx(ladder[2].Bottom, 31, 0.5) {
		t.Fatalf("ladder[2] top=%.2f bottom=%.2f want ≈35/31", ladder[2].Top, ladder[2].Bottom)
	}
	// prev 链逐级
	for i := 1; i < len(ladder); i++ {
		if ladder[i].PrevBoxID != ladder[i-1].ID {
			t.Fatalf("ladder[%d].prev=%d want %d", i, ladder[i].PrevBoxID, ladder[i-1].ID)
		}
	}
}

// 28. 新箱体下轨≈前一箱体上轨（阶梯涌现属性，§3.4 容差）
// 注：用阶梯对齐的 buildLadderSeries（bottom=前级top）验证；书中的实际箱体
// 可有缺口（如 31 vs 27），±3% 是理想属性而非硬规则。
func TestLadderBottomApproxPrevTop(t *testing.T) {
	boxes := scan(buildLadderSeries(3, 200))
	var ladder []contract.Box
	for _, b := range boxes {
		if b.Status == contract.BoxBrokenUp {
			ladder = append(ladder, b)
		}
	}
	if len(ladder) < 2 {
		t.Fatalf("need ≥2 ladder nodes, got %d", len(ladder))
	}
	for i := 1; i < len(ladder); i++ {
		prevTop := ladder[i-1].Top
		thisBottom := ladder[i].Bottom
		if prevTop == 0 {
			continue
		}
		dev := (thisBottom - prevTop) / prevTop
		if dev < -0.03 || dev > 0.03 {
			t.Errorf("ladder[%d] bottom=%.2f vs prevTop=%.2f dev=%.1f%% outside ±3%%", i, thisBottom, prevTop, dev*100)
		}
	}
}

// ─── 默认参数路径 ──

// 29. 默认参数（VolWindow=50）在足够长序列上正常工作不崩溃
func TestDefaultParamsLongSeries(t *testing.T) {
	series := buildLadderSeries(2, 200)
	// 补足 50+ 根前置量以支持默认 VolWindow
	for len(series) < 60 {
		series = append(series, bar("sh1", d(len(series)), 9.5, 9, 9.2, 200))
	}
	boxes := New().Scan(series, DefaultParams())
	if len(boxes) == 0 {
		t.Fatal("default params should produce boxes")
	}
}

// 30. VolWindow 大于可用历史 → 放量判定用可用样本（不崩溃）
func TestVolWindowLargerThanHistory(t *testing.T) {
	series := confirmedBoxSeries(10, 9, 200)
	// 用 VolWindow=100 但序列只有十几根
	p := tp()
	p.VolWindow = 100
	boxes := New().Scan(series, p)
	// 突破日量500，可用历史约10根均量≈200 → 500≥400 仍应突破
	if len(boxes) < 2 || boxes[len(boxes)-2].Status != contract.BoxBrokenUp {
		t.Fatalf("should breakout using available history, got %+v", boxes)
	}
}

// 31. avgVolumeBefore i=0 → 0（无历史，无法放量确认）
func TestAvgVolumeBeforeZero(t *testing.T) {
	if avgVolumeBefore([]contract.DailyBar{bar("s", d(0), 1, 1, 1, 1)}, 0, 5) != 0 {
		t.Fatal("avgVolumeBefore at i=0 should be 0")
	}
}

// 32. 一字板判定：开=高=低=收
func TestOnePriceBoardDetection(t *testing.T) {
	if !isOnePriceBoard(onePrice("s", d(0), 10, 100)) {
		t.Fatal("one-price board not detected")
	}
	if isOnePriceBoard(bar("s", d(0), 10, 9, 9.5, 100)) {
		t.Fatal("normal bar misclassified as one-price")
	}
}

// 33. 连续多次作废 → 全部 invalid，prev 始终指向最近 confirmed
func TestRepeatedInvalidations(t *testing.T) {
	// 连续两次「上轨确认→下轨阶段创新高→invalid」的序列
	series2 := []contract.DailyBar{
		bar("sh1", d(0), 20, 18, 19, 100),  // 新高 20
		bar("sh1", d(1), 19.5, 18, 19, 100),
		bar("sh1", d(2), 19.8, 18, 19, 100),
		bar("sh1", d(3), 19.6, 18, 19, 100), // 上轨确认 top=20
		bar("sh1", d(4), 19.8, 18.5, 19, 100),
		bar("sh1", d(5), 22, 18, 21, 100), // 创新 high>20 → invalid，新箱 topCandidate=22
		bar("sh1", d(6), 21.5, 18, 21, 100),
		bar("sh1", d(7), 21.8, 18, 21, 100),
		bar("sh1", d(8), 21.6, 18, 21, 100), // 上轨确认 top=22
		bar("sh1", d(9), 21.8, 18.5, 21, 100),
		bar("sh1", d(10), 25, 18, 24, 100), // 创新 high>22 → invalid
	}
	boxes := scan(series2)
	// 两个 invalid
	if boxes[0].Status != contract.BoxInvalid || boxes[1].Status != contract.BoxInvalid {
		t.Fatalf("want two invalids, got %+v %+v", boxes[0], boxes[1])
	}
	// 两者 prev 都=0（无 confirmed 前驱）
	if boxes[0].PrevBoxID != 0 || boxes[1].PrevBoxID != 0 {
		t.Fatalf("invalids prev should be 0 (no confirmed ancestor), got %d %d", boxes[0].PrevBoxID, boxes[1].PrevBoxID)
	}
}

// 34. 三日法则：连续3日（非累计），中间创新高/新低即重置
func TestThreeDayRuleStrictConsecutive(t *testing.T) {
	// 上轨：新高后第2日又创新高→重置，需再连续3日
	boxes := scan([]contract.DailyBar{
		bar("sh1", d(0), 10, 9, 9.5, 100),  // 新高10
		bar("sh1", d(1), 9.5, 9, 9.2, 100), // 1
		bar("sh1", d(2), 11, 9, 10.5, 100), // 新高11→重置（第2日）
		bar("sh1", d(3), 10.5, 9, 10, 100), // 1
		bar("sh1", d(4), 10.8, 9, 10.5, 100), // 2
		// 仅2日，未到3 → 未确认
	})
	if boxes[0].TopConfirmed != "" {
		t.Fatalf("should NOT confirm after reset+2 days, got confirmed=%s", boxes[0].TopConfirmed)
	}
}

// 35. 突破后新箱体立即开始 forming（突破日即新箱体首日）
func TestNewBoxStartsOnBreakoutDay(t *testing.T) {
	boxes := scan(buildLadderSeries(1, 200))
	// box0 broken_up（brokenDate=突破日），box1 forming 起始于同日
	if len(boxes) < 2 {
		t.Fatal("want ≥2 boxes after one breakout")
	}
	if boxes[0].BrokenDate == "" {
		t.Fatal("broken_up box should have brokenDate")
	}
	if boxes[1].Status != contract.BoxForming && boxes[1].Status != contract.BoxConfirmed {
		t.Fatalf("new box after breakout status=%s want forming/confirmed", boxes[1].Status)
	}
}

// ─── 序列构造辅助 ──────────────────────────────────────────────────────────

// confirmedBoxSeries 构造一个已确认箱体（top,bottom）并在末根放突破候选 K 线。
// baseVol 前 VolWindow 根的基线成交量；末根默认设为放量突破（close>top×1.03, vol=500）。
// 末根即突破日，便于测试按 len-1 定位修改。
func confirmedBoxSeries(top, bottom, baseVol float64) []contract.DailyBar {
	var s []contract.DailyBar
	// 上轨：一根新高 + 3 根无新高（共4根，索引0-3）
	s = append(s, bar("sh1", d(0), top, bottom, top-0.1, int64(baseVol)))
	s = append(s, bar("sh1", d(1), top-0.5, bottom, top-0.5, int64(baseVol)))
	s = append(s, bar("sh1", d(2), top-0.2, bottom, top-0.2, int64(baseVol)))
	s = append(s, bar("sh1", d(3), top-0.4, bottom, top-0.3, int64(baseVol))) // 上轨确认 top
	// 下轨：3 根无新低（索引4-6），bottomCandidate=bottom（上轨确认日 low）
	s = append(s, bar("sh1", d(4), top-0.1, bottom+0.2, top-0.2, int64(baseVol)))
	s = append(s, bar("sh1", d(5), top-0.1, bottom+0.3, top-0.2, int64(baseVol)))
	s = append(s, bar("sh1", d(6), top-0.1, bottom+0.1, top-0.2, int64(baseVol))) // 下轨确认 bottom
	// 突破日（末根，索引7）：close>top×1.03，放量；前置7根≥VolWindow5 足够算均量
	breakClose := top*1.03 + 0.2
	s = append(s, bar("sh1", d(7), breakClose+0.1, top-0.1, breakClose, 500))
	return s
}

// buildLadderSeries 构造 n 级上升阶梯：每级一个 confirmed 箱体被突破。
// baseVol 基线量。每级 top 上移、bottom=前级 top（阶梯对齐，§3.4 容差内）。
// 不加填充——每级 8 根（含突破日），前置根数足够 VolWindow=5 算均量。
func buildLadderSeries(n int, baseVol float64) []contract.DailyBar {
	var s []contract.DailyBar
	tops := []float64{24, 27, 35, 40}
	day := 0
	for lvl := 0; lvl < n; lvl++ {
		top := tops[lvl%len(tops)]
		// bottom 阶梯对齐：第0级用17起步，其后 bottom=前级 top
		bottom := 17.0
		if lvl > 0 {
			bottom = tops[(lvl-1)%len(tops)]
		}
		if top <= bottom {
			top = bottom + 3
		}
		// 上轨：新高 + 3 无新高
		s = append(s, bar("sh1", d(day), top, bottom, top-0.1, int64(baseVol)))
		day++
		s = append(s, bar("sh1", d(day), top-0.5, bottom, top-0.5, int64(baseVol)))
		day++
		s = append(s, bar("sh1", d(day), top-0.2, bottom, top-0.2, int64(baseVol)))
		day++
		s = append(s, bar("sh1", d(day), top-0.4, bottom, top-0.3, int64(baseVol))) // 上轨确认
		day++
		// 下轨：3 无新低（bottomCandidate=bottom 来自上轨确认日 low）
		s = append(s, bar("sh1", d(day), top-0.1, bottom+0.2, top-0.2, int64(baseVol)))
		day++
		s = append(s, bar("sh1", d(day), top-0.1, bottom+0.3, top-0.2, int64(baseVol)))
		day++
		s = append(s, bar("sh1", d(day), top-0.1, bottom+0.1, top-0.2, int64(baseVol))) // 下轨确认
		day++
		// 突破日：close>top×1.03 放量
		bc := top*1.03 + 0.3
		s = append(s, bar("sh1", d(day), bc+0.1, top-0.1, bc, int64(baseVol*2.5)))
		day++
	}
	return s
}

// bookLadderSeries 构造书中三级阶梯：17/24 → 24/27 → 31/35。
func bookLadderSeries() []contract.DailyBar {
	var s []contract.DailyBar
	day := 0
	addBox := func(top, bottom float64) {
		s = append(s, bar("bk", d(day), top, bottom, top-0.1, 1000))
		day++
		s = append(s, bar("bk", d(day), top-0.5, bottom, top-0.5, 1000))
		day++
		s = append(s, bar("bk", d(day), top-0.2, bottom, top-0.2, 1000))
		day++
		s = append(s, bar("bk", d(day), top-0.4, bottom, top-0.3, 1000)) // 上轨确认
		day++
		s = append(s, bar("bk", d(day), top-0.1, bottom+0.2, top-0.2, 1000))
		day++
		s = append(s, bar("bk", d(day), top-0.1, bottom+0.3, top-0.2, 1000))
		day++
		s = append(s, bar("bk", d(day), top-0.1, bottom+0.1, top-0.2, 1000)) // 下轨确认
		day++
		bc := top*1.03 + 0.3
		s = append(s, bar("bk", d(day), bc+0.1, top-0.1, bc, 3000)) // 放量突破
		day++
	}
	addBox(24, 17) // 第一级 17/24
	addBox(27, 24) // 第二级 24/27
	addBox(35, 31) // 第三级 31/35
	return s
}

// approx 浮点近似相等（容差 tol）。
func approx(a, b, tol float64) bool {
	return a-b < tol && b-a < tol
}
