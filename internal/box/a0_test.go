package box

import (
	"reflect"
	"testing"

	"darvas/internal/contract"
)

// A0 确定性契约测试（docs/experiments/e17_a0_determinism_framework.md §二）。
// 对应 docs/06 §3.1 生命周期不变量表前四行的引擎级验证；零策略变更。
// 序列混合三种终态（broken_up / invalid / broken_down），由 mixedTerminalSeries 构造。

// mixedTerminalSeries 构造覆盖全部三种终态的单一序列：
// box0(24/17) confirmed→broken_up(d7)；box1 invalid(d13，下轨确立前创新高)；
// box2(30/26) confirmed→broken_down(d20)；box3 尾部 forming。
func mixedTerminalSeries() []contract.DailyBar {
	return []contract.DailyBar{
		// box0: top=24 bottom=17（宽 29%）
		bar("sh1", d(0), 24, 17, 23.9, 200),
		bar("sh1", d(1), 23.5, 17, 23.5, 200),
		bar("sh1", d(2), 23.8, 17, 23.5, 200),
		bar("sh1", d(3), 23.6, 17, 23.3, 200), // 上轨确认 top=24
		bar("sh1", d(4), 23.9, 17.2, 23.5, 200),
		bar("sh1", d(5), 23.7, 17.3, 23.5, 200),
		bar("sh1", d(6), 23.6, 17.1, 23.4, 200), // 下轨确认 bottom=17 → confirmed
		bar("sh1", d(7), 25.4, 23.9, 25.0, 500), // 突破 close>24.72 放量 → broken_up
		// box1 起于 d7，topCandidate=25.4
		bar("sh1", d(8), 26.5, 24, 26.0, 200), // 新高 26.5 → 重置
		bar("sh1", d(9), 26.0, 24, 25.8, 200),
		bar("sh1", d(10), 26.2, 24, 26.0, 200),
		bar("sh1", d(11), 26.1, 24, 25.9, 200), // 上轨确认 top=26.5
		bar("sh1", d(12), 26.3, 24.2, 26.0, 200), // 下轨计数 1
		bar("sh1", d(13), 30, 24, 29.0, 200), // high 30 > top 26.5 → invalid，同根重启
		// box2 起于 d13，topCandidate=30
		bar("sh1", d(14), 29.5, 26, 29.0, 200),
		bar("sh1", d(15), 29.8, 26, 29.3, 200),
		bar("sh1", d(16), 29.6, 26, 29.2, 200), // 上轨确认 top=30
		bar("sh1", d(17), 29.7, 26.2, 29.3, 200),
		bar("sh1", d(18), 29.4, 26.3, 29.0, 200),
		bar("sh1", d(19), 29.5, 26.1, 29.2, 200), // 下轨确认 bottom=26（宽 13.3%）→ confirmed
		bar("sh1", d(20), 26.2, 25.0, 25.5, 100), // close 25.5 < 26 → broken_down
		// box3 起于 d20，尾部 forming
		bar("sh1", d(21), 26.0, 25.2, 25.8, 100),
	}
}

// A0① 终态不可逆 + 序列前缀稳定：任意前缀的扫描输出（除可变尾箱外）必须与全量扫描
// 的对应前缀逐字段相等。broken_up / invalid / broken_down 三终态同序列覆盖。
func TestA0TerminalIrreversibilityPrefixStable(t *testing.T) {
	seriesList := [][]contract.DailyBar{
		mixedTerminalSeries(),
		buildLadderSeries(3, 200), // 纯 broken_up 阶梯
	}
	seriesList = append(seriesList, func() []contract.DailyBar { // 纯 invalid 链
		s := mixedTerminalSeries()
		return s[:14] // 到 d13（第二次 invalid 前），含 broken_up + invalid
	}())
	for si, series := range seriesList {
		full := scan(series)
		for p := 1; p <= len(series); p++ {
			pre := scan(series[:p])
			if len(pre) == 0 {
				continue
			}
			n := len(pre) - 1 // 尾箱可变，不比较
			for j := 0; j < n; j++ {
				if j >= len(full) {
					t.Fatalf("series%d prefix p=%d: pre[%d] 超出全量长度 %d", si, p, j, len(full))
				}
				if !reflect.DeepEqual(pre[j], full[j]) {
					t.Fatalf("series%d prefix p=%d: 已收盘箱体 %d 发生变化（终态不可逆被破坏）\npre =%+v\nfull=%+v", si, p, j, pre[j], full[j])
				}
			}
		}
	}
}

// A0②(a) 单日至多一个终止事件：逐前缀推进时箱体序列长度日增量 ∈ {0,1}。
// 即「终止/作废当根 K 线只开一个新 forming」的直接推论。
func TestA0AtMostOneTerminalEventPerBar(t *testing.T) {
	series := mixedTerminalSeries()
	prevLen := 0
	for p := 1; p <= len(series); p++ {
		n := len(scan(series[:p]))
		if n-prevLen > 1 {
			t.Fatalf("p=%d: 单日箱体数增量 %d > 1（同根 K 线开了多个箱体）", p, n-prevLen)
		}
		if n < prevLen {
			t.Fatalf("p=%d: 箱体数回缩 %d→%d（已收盘箱体被删除）", p, prevLen, n)
		}
		prevLen = n
	}
}

// A0②(b) 输出 ID 严格连续 1..N（无跳号、无重号）。
func TestA0IDsStrictlyConsecutive(t *testing.T) {
	for si, series := range [][]contract.DailyBar{mixedTerminalSeries(), buildLadderSeries(2, 200)} {
		boxes := scan(series)
		for i, b := range boxes {
			if b.ID != int64(i+1) {
				t.Fatalf("series%d: boxes[%d].ID=%d want %d", si, i, b.ID, i+1)
			}
		}
	}
}

// A0②(c) 终止当根即开新箱：broken 箱体的 brokenDate 当日，前缀扫描已含 ID+1 的
// 新 tail（forming，身份 (0,0)）。
func TestA0NewBoxStartsOnBrokenDay(t *testing.T) {
	series := mixedTerminalSeries()
	boxes := scan(series)
	dateIdx := map[string]int{}
	for i, b := range series {
		dateIdx[b.Date] = i
	}
	for k, b := range boxes {
		if b.Status != contract.BoxBrokenUp && b.Status != contract.BoxBrokenDown {
			continue
		}
		p := dateIdx[b.BrokenDate] + 1 // 前缀含至 brokenDate 当根
		pre := scan(series[:p])
		if len(pre) != k+2 {
			t.Fatalf("box%d(%s) brokenDate=%s: 前缀长度 %d want %d（终止当根必须且只能开一个新箱）",
				k+1, b.Status, b.BrokenDate, len(pre), k+2)
		}
		tail := pre[len(pre)-1]
		if tail.ID != b.ID+1 || tail.Status != contract.BoxForming || tail.Top != 0 || tail.Bottom != 0 {
			t.Fatalf("box%d 终止日新箱异常: %+v", k+1, tail)
		}
	}
}

// A0②(d) invalid 无原生日期：作废日 = 下一实例首现日；作废前一晚该箱为 tail 且
// 身份为 (top,0)；作废当根 tail 已是新箱 (0,0)。
func TestA0InvalidRestartSameBar(t *testing.T) {
	series := mixedTerminalSeries()
	boxes := scan(series)
	var inv contract.Box
	invIdx := -1
	for i, b := range boxes {
		if b.Status == contract.BoxInvalid {
			inv, invIdx = b, i
			break
		}
	}
	if invIdx < 0 {
		t.Fatal("序列中未找到 invalid 箱体")
	}
	// 找作废日：下一实例（ID=inv.ID+1）首现的最短前缀
	restartDay := -1
	for p := 1; p <= len(series); p++ {
		if len(scan(series[:p])) >= int(inv.ID)+1 {
			restartDay = p
			break
		}
	}
	if restartDay < 0 {
		t.Fatal("未找到重启日")
	}
	// 作废当根：tail 是新箱 (0,0)，且 invalid 箱体已在输出中
	pre := scan(series[:restartDay])
	if pre[invIdx].Status != contract.BoxInvalid {
		t.Fatalf("重启日 invalid 箱体状态=%s want invalid", pre[invIdx].Status)
	}
	tail := pre[len(pre)-1]
	if tail.ID != inv.ID+1 || tail.Top != 0 || tail.Bottom != 0 {
		t.Fatalf("作废当根 tail 应为新 forming (0,0): %+v", tail)
	}
	// 作废前一晚：该箱是 tail，身份 (top,0)
	prePrev := scan(series[:restartDay-1])
	tailPrev := prePrev[len(prePrev)-1]
	if tailPrev.ID != inv.ID || tailPrev.Top != inv.Top || tailPrev.Bottom != 0 || tailPrev.Status != contract.BoxForming {
		t.Fatalf("作废前夜 tail 应为 (top,0) forming: %+v (invalid.Top=%v)", tailPrev, inv.Top)
	}
}

// A0②(e) 身份相位演化：(0,0) → (top,0) → (top,bottom)。
// DB 身份键随子阶段演化（UpsertBoxes 按 (top,bottom) 复用的直接推论），此处锚定语义。
func TestA0IdentityPhaseEvolution(t *testing.T) {
	series := mixedTerminalSeries()
	// d0..d2: box0 上轨未确认 → tail (0,0)
	pre := scan(series[:3])
	tail := pre[len(pre)-1]
	if tail.Top != 0 || tail.Bottom != 0 || tail.Status != contract.BoxForming {
		t.Fatalf("相位(0,0) 断言失败: %+v", tail)
	}
	// d4: 上轨已确认（d3）、下轨未确认 → tail (24,0)
	pre = scan(series[:5])
	tail = pre[len(pre)-1]
	if tail.Top != 24 || tail.Bottom != 0 || tail.Status != contract.BoxForming {
		t.Fatalf("相位(top,0) 断言失败: %+v", tail)
	}
	// d7: 下轨已确认（d6）→ tail (24,17) confirmed（d7 为突破日，tail 为新箱）
	pre = scan(series[:7])
	tail = pre[len(pre)-1]
	if tail.Top != 24 || tail.Bottom != 17 || tail.Status != contract.BoxConfirmed {
		t.Fatalf("相位(top,bottom) 断言失败: %+v", tail)
	}
}

// A0③ 重复 Scan 幂等：5 次重复 + 双引擎交替 + 输入不变异。
func TestA0ScanIdempotentAndPure(t *testing.T) {
	series := mixedTerminalSeries()
	before := append([]contract.DailyBar(nil), series...)

	first := scan(series)
	for rep := 0; rep < 5; rep++ {
		again := scan(series)
		if !reflect.DeepEqual(first, again) {
			t.Fatalf("第 %d 次重复扫描结果不同（幂等被破坏）", rep+1)
		}
	}
	// 双引擎交替扫描（排除跨调用状态残留）
	e1, e2 := New(), New()
	var interleave []contract.Box
	for i := 0; i < 3; i++ {
		interleave = append(interleave, e1.Scan(series, tp())...)
		interleave = append(interleave, e2.Scan(series, tp())...)
	}
	// 交替结果按每次扫描分组比对 first（每次 Scan 输出 len 相同）
	step := len(first)
	for i := 0; i+step <= len(interleave); i += step {
		if !reflect.DeepEqual(first, interleave[i:i+step]) {
			t.Fatalf("引擎交替扫描第 %d 组输出不同（存在跨调用状态）", i/step)
		}
	}
	// 输入不得被变异（纯函数）
	if !reflect.DeepEqual(before, series) {
		t.Fatal("Scan 变异了输入 K 线切片")
	}
}
