package store

import (
	"testing"

	"darvas/internal/contract"
)

// mkBox 构造一个箱体（测试用）。
func mkBox(id int64, code string, top, bottom float64, status contract.BoxStatus, prev int64) contract.Box {
	return contract.Box{
		ID: id, Code: code, Top: top, Bottom: bottom,
		TopConfirmed: "2026-10-01", BottomConfirmed: "2026-10-05",
		Status: status, PrevBoxID: prev, BrokenDate: "",
	}
}

// TestUpsertBoxesLadderChain 写入阶梯链，验证 DB ID 修复与 prev 链。
func TestUpsertBoxesLadderChain(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	boxes := []contract.Box{
		mkBox(1, "sh600519", 24, 17, contract.BoxBrokenUp, 0),
		mkBox(2, "sh600519", 27, 24, contract.BoxBrokenUp, 1),
		mkBox(3, "sh600519", 35, 31, contract.BoxConfirmed, 2),
	}
	ids, err := st.BoxRepo.UpsertBoxes(boxes)
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 3 {
		t.Fatalf("ids len=%d want 3", len(ids))
	}
	// 验证 prev 链：box2.prev=DB.id of box1，box3.prev=DB.id of box2
	ladder, _ := st.BoxRepo.Ladder("sh600519")
	if len(ladder) != 3 {
		t.Fatalf("ladder len=%d want 3", len(ladder))
	}
	if ladder[1].PrevBoxID != ladder[0].ID {
		t.Errorf("box1.prev=%d want %d (box0)", ladder[1].PrevBoxID, ladder[0].ID)
	}
	if ladder[2].PrevBoxID != ladder[1].ID {
		t.Errorf("box2.prev=%d want %d (box1)", ladder[2].PrevBoxID, ladder[1].ID)
	}
	if ladder[0].PrevBoxID != 0 {
		t.Errorf("box0.prev=%d want 0", ladder[0].PrevBoxID)
	}
}

// TestUpsertBoxesIdempotent 重跑替换旧箱体（无重复）。
func TestUpsertBoxesIdempotent(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	boxes := []contract.Box{
		mkBox(1, "sh600519", 24, 17, contract.BoxConfirmed, 0),
		mkBox(2, "sh600519", 27, 24, contract.BoxConfirmed, 1),
	}
	st.BoxRepo.UpsertBoxes(boxes)
	// 再跑一次（boxscan 重算）
	boxes[1].Top = 28 // 模拟重算结果微变
	st.BoxRepo.UpsertBoxes(boxes)
	ladder, _ := st.BoxRepo.Ladder("sh600519")
	if len(ladder) != 2 {
		t.Fatalf("after rerun ladder len=%d want 2 (replaced, no dup)", len(ladder))
	}
	if ladder[1].Top != 28 {
		t.Fatalf("rerun top=%.1f want 28 (updated)", ladder[1].Top)
	}
}

// TestLatestBox 取最新箱体。
func TestLatestBox(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	st.BoxRepo.UpsertBoxes([]contract.Box{
		mkBox(1, "sh600519", 24, 17, contract.BoxBrokenUp, 0),
		mkBox(2, "sh600519", 27, 24, contract.BoxConfirmed, 1),
	})
	b, err := st.BoxRepo.LatestBox("sh600519")
	if err != nil || b == nil {
		t.Fatalf("LatestBox err=%v b=%v", err, b)
	}
	if b.Top != 27 {
		t.Fatalf("latest top=%.1f want 27", b.Top)
	}
	// 无箱体的 code
	none, err := st.BoxRepo.LatestBox("sh999999")
	if err != nil || none != nil {
		t.Fatalf("missing code should return nil, got err=%v b=%v", err, none)
	}
}

// TestUpsertBoxesEmpty 空切片。
func TestUpsertBoxesEmpty(t *testing.T) {
	st := newTestStore(t)
	ids, err := st.BoxRepo.UpsertBoxes(nil)
	if err != nil || ids != nil {
		t.Fatalf("empty ids=%v err=%v want nil/nil", ids, err)
	}
}

// TestLadderIncludesInvalid 阶梯含 invalid（调用方负责过滤）。
func TestLadderIncludesInvalid(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	st.BoxRepo.UpsertBoxes([]contract.Box{
		mkBox(1, "sh600519", 24, 17, contract.BoxBrokenUp, 0),
		mkBox(2, "sh600519", 0, 0, contract.BoxInvalid, 1),
		mkBox(3, "sh600519", 30, 24, contract.BoxConfirmed, 1),
	})
	ladder, _ := st.BoxRepo.Ladder("sh600519")
	if len(ladder) != 3 {
		t.Fatalf("ladder len=%d want 3 (incl invalid)", len(ladder))
	}
	hasInvalid := false
	for _, b := range ladder {
		if b.Status == contract.BoxInvalid {
			hasInvalid = true
		}
	}
	if !hasInvalid {
		t.Fatal("ladder should include invalid boxes (caller filters)")
	}
}

// TestUpsertBoxesStaleDeleteKeepsPrevRef docs/07 #6 回归：
// 幸存箱体（此处靠 signal FK 保护，不在重算批次内、prev 不被重写）
// 的 prev_box_id 指向的 stale 箱体被 DELETE 时，不得触发 box 自引用
// FK 787 回滚整个事务（sz300411 曾每日复现：该股当日箱体更新全丢）。
func TestUpsertBoxesStaleDeleteKeepsPrevRef(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	// 第一批：阶梯 box1(24,17) → box2(27,24, prev=box1)
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		mkBox(1, "sh600519", 24, 17, contract.BoxBrokenUp, 0),
		mkBox(2, "sh600519", 27, 24, contract.BoxBrokenUp, 1),
	})
	if err != nil {
		t.Fatal(err)
	}
	// 突破信号引用 box2 → box2 靠 signal 保护幸存，其 prev 仍指 box1
	if err := st.SignalRepo.AppendSignals([]contract.Signal{{
		Date: "2026-10-12", Code: "sh600519", Type: contract.SignalBreakout,
		BoxID: ids[1], ClosePx: 27.5, BoxLevel: 27,
	}}); err != nil {
		t.Fatal(err)
	}
	// 第二批重算：旧阶梯不再输出，只有新箱体（50,40）。
	// box1 成 stale 且无 position/watch/signal 引用；
	// box2 成 stale 但被 signal 保护幸存、prev 未被重写仍指 box1。
	// 修复前：DELETE box1 触发 FK 787 → 整个事务回滚、UpsertBoxes 报错。
	newIDs, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		mkBox(1, "sh600519", 50, 40, contract.BoxConfirmed, 0),
	})
	if err != nil {
		t.Fatalf("UpsertBoxes should tolerate prev_box_id self-reference (docs/07 #6): %v", err)
	}
	if len(newIDs) != 1 {
		t.Fatalf("newIDs len=%d want 1", len(newIDs))
	}
	// 修复后语义：box1 保留（被幸存 box2 的 prev 引用）、box2 保留（signal 引用）、
	// 新箱体入库 → 阶梯 3 节
	ladder, err := st.BoxRepo.Ladder("sh600519")
	if err != nil {
		t.Fatal(err)
	}
	if len(ladder) != 3 {
		t.Fatalf("ladder len=%d want 3 (prev-referenced stale box kept, docs/07 #6)", len(ladder))
	}
}

// TestBreadthAgg 宽度法聚合：只统计个股、按 min_history 剔次新、按 broken_date 分桶。
func TestBreadthAgg(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519", "sh600000", "sh000001")
	// sh600519：老股（2025-12-01 已有 K 线，满足 60×1.5=90 个日历日），事件应计入。
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		mkBar("sh600519", "2025-12-01", 10, 10, 10, 10, 100, 1e5),
	}); err != nil {
		t.Fatal(err)
	}
	st.BoxRepo.UpsertBoxes([]contract.Box{{
		Code: "sh600519", Top: 12, Bottom: 10, Status: contract.BoxBrokenUp,
		BrokenDate: "2026-03-02", TopConfirmed: "2026-02-20", BottomConfirmed: "2026-02-25",
	}})
	// sh600000：次新股（2026-03-01 才有 K 线），min_history=60（90 个日历日）应剔除。
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		mkBar("sh600000", "2026-03-01", 10, 10, 10, 10, 100, 1e5),
	}); err != nil {
		t.Fatal(err)
	}
	st.BoxRepo.UpsertBoxes([]contract.Box{{
		Code: "sh600000", Top: 12, Bottom: 10, Status: contract.BoxBrokenUp,
		BrokenDate: "2026-03-02", TopConfirmed: "2026-03-01", BottomConfirmed: "2026-03-01",
	}})
	// 指数事件不计入市场宽度（只统计 kind='stock'）。
	st.BoxRepo.UpsertBoxes([]contract.Box{{
		Code: "sh000001", Top: 3300, Bottom: 3000, Status: contract.BoxBrokenDown,
		BrokenDate: "2026-03-02", TopConfirmed: "2026-02-20", BottomConfirmed: "2026-02-25",
	}})

	up, down, err := st.BoxRepo.BreadthAgg("2026-03-01", "2026-03-03", 60)
	if err != nil {
		t.Fatal(err)
	}
	if up["2026-03-02"] != 1 || len(up) != 1 {
		t.Fatalf("up=%v want only sh600519 2026-03-02=1（次新 sh600000 与指数剔除）", up)
	}
	if len(down) != 0 {
		t.Fatalf("down=%v want empty", down)
	}
}
