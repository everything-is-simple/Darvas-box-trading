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
