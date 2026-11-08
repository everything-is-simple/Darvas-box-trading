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

// TestUpsertBoxesDeadDate dead_date（死亡日）读写往返：broken_down/invalid 写值，
// broken_up/confirmed 留空，读回一致（docs/49 §五之二 特征一，migration 0028/0029）。
// 0029 起 broken_date 只表「向上突破」：broken_down 箱体的 broken_date 必须为空。
func TestUpsertBoxesDeadDate(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	boxes := []contract.Box{
		{ID: 1, Code: "sh600519", Top: 24, Bottom: 17, Status: contract.BoxBrokenUp,
			BrokenDate: "2026-10-10", DeadDate: ""},
		{ID: 2, Code: "sh600519", Top: 27, Bottom: 24, Status: contract.BoxBrokenDown,
			BrokenDate: "", DeadDate: "2026-10-20"},
		{ID: 3, Code: "sh600519", Top: 30, Bottom: 28, Status: contract.BoxInvalid,
			BrokenDate: "", DeadDate: "2026-10-25"},
	}
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	ladder, err := st.BoxRepo.Ladder("sh600519")
	if err != nil {
		t.Fatal(err)
	}
	if len(ladder) != 3 {
		t.Fatalf("ladder len=%d want 3", len(ladder))
	}
	// broken_up：突破日有值、死亡日为空（生命周期终点即突破日）
	if ladder[0].BrokenDate != "2026-10-10" {
		t.Errorf("broken_up broken_date=%q want 2026-10-10", ladder[0].BrokenDate)
	}
	if ladder[0].DeadDate != "" {
		t.Errorf("broken_up box dead_date=%q want empty", ladder[0].DeadDate)
	}
	// broken_down：死亡日 = 破位当天；broken_date 为空（破位不是突破）
	if ladder[1].DeadDate != "2026-10-20" {
		t.Errorf("broken_down dead_date=%q want 2026-10-20", ladder[1].DeadDate)
	}
	if ladder[1].BrokenDate != "" {
		t.Errorf("broken_down broken_date=%q want empty（0029 语义分离）", ladder[1].BrokenDate)
	}
	// invalid：死亡日 = 作废当天
	if ladder[2].DeadDate != "2026-10-25" {
		t.Errorf("invalid dead_date=%q want 2026-10-25", ladder[2].DeadDate)
	}
	// LatestBox 也读回 dead_date（最新 = invalid 那个）
	lb, err := st.BoxRepo.LatestBox("sh600519")
	if err != nil || lb == nil {
		t.Fatalf("LatestBox err=%v lb=%v", err, lb)
	}
	if lb.DeadDate != "2026-10-25" {
		t.Errorf("LatestBox dead_date=%q want 2026-10-25", lb.DeadDate)
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

// TestUpsertBoxesStaleDeleteKeepsPrevRef archive/07 #6 回归：
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
		t.Fatalf("UpsertBoxes should tolerate prev_box_id self-reference (archive/07 #6): %v", err)
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
		t.Fatalf("ladder len=%d want 3 (prev-referenced stale box kept, archive/07 #6)", len(ladder))
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
	// sh600519 两次事件一起写入（UpsertBoxes 以「该 code 的完整箱体列表」为语义，
	// 分两次调用会把前一次未复用的行判为 stale 删除）：
	//   broken_up   → 按 broken_date 分桶
	//   broken_down → 按 dead_date 分桶（0029 语义分离后 broken_date 不再承载破位日）
	st.BoxRepo.UpsertBoxes([]contract.Box{
		{Code: "sh600519", Top: 12, Bottom: 10, Status: contract.BoxBrokenUp,
			BrokenDate: "2026-03-02", TopConfirmed: "2026-02-20", BottomConfirmed: "2026-02-25"},
		{Code: "sh600519", Top: 14, Bottom: 12, Status: contract.BoxBrokenDown,
			DeadDate: "2026-03-03", TopConfirmed: "2026-02-26", BottomConfirmed: "2026-03-01"},
	})
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
		DeadDate: "2026-03-02", TopConfirmed: "2026-02-20", BottomConfirmed: "2026-02-25",
	}})

	up, down, err := st.BoxRepo.BreadthAgg("2026-03-01", "2026-03-03", 60)
	if err != nil {
		t.Fatal(err)
	}
	if up["2026-03-02"] != 1 || len(up) != 1 {
		t.Fatalf("up=%v want only sh600519 2026-03-02=1（次新 sh600000 与指数剔除）", up)
	}
	if down["2026-03-03"] != 1 || len(down) != 1 {
		t.Fatalf("down=%v want only sh600519 2026-03-03=1（按 dead_date 分桶，指数剔除）", down)
	}
}

// TestUpsertBoxesIdentityStable 身份键含 bottom_confirmed：同价但不同生命周期的箱体
// 不得复用同一行，position 引用的历史箱体不可被重算改写（0029，docs/49 §五之二 特征一）。
//
// 复现真实缺陷：旧键仅 (top,bottom) 时，第二次重算的同价箱体会覆盖第一只箱体的
// status/broken_date —— 实测造成 34 笔持仓 opened_date 早于其箱体 broken_date
// （最多早 86 天）、11 笔买入挂在 broken_down 箱体上。
func TestUpsertBoxesIdentityStable(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")

	// 第一段生命周期：下轨 2026-01-05 确认，2026-01-10 向上突破 → 买入
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{{
		ID: 1, Code: "sh600519", Top: 10, Bottom: 9,
		TopConfirmed: "2026-01-02", BottomConfirmed: "2026-01-05",
		Status: contract.BoxBrokenUp, BrokenDate: "2026-01-10",
	}})
	if err != nil {
		t.Fatal(err)
	}
	entryID := ids[0]

	// 模拟 ledger 写入 position.entry_box_id
	if _, err := st.DB().Exec(`INSERT INTO position(code, opened_date, entry_box_id, qty,
		avg_cost, stop_price, status) VALUES ('sh600519','2026-01-11',?,100,10,9,'open')`, entryID); err != nil {
		t.Fatal(err)
	}

	// 第二段生命周期：完全相同的 top/bottom，但下轨 2026-03-05 才确认，且已向下破位
	if _, err := st.BoxRepo.UpsertBoxes([]contract.Box{{
		ID: 1, Code: "sh600519", Top: 10, Bottom: 9,
		TopConfirmed: "2026-03-02", BottomConfirmed: "2026-03-05",
		Status: contract.BoxBrokenDown, DeadDate: "2026-03-20",
	}}); err != nil {
		t.Fatal(err)
	}

	// 买入那只箱子必须原封不动：仍是 broken_up / broken_date=2026-01-10 / 无死亡日
	var status, broken, dead string
	if err := st.DB().QueryRow(`SELECT status, COALESCE(CAST(broken_date AS TEXT),''),
		COALESCE(CAST(dead_date AS TEXT),'') FROM box WHERE id=?`, entryID).
		Scan(&status, &broken, &dead); err != nil {
		t.Fatalf("entry box 被删除或读失败: %v", err)
	}
	if status != string(contract.BoxBrokenUp) || broken != "2026-01-10" || dead != "" {
		t.Errorf("entry box 被重算改写: status=%q broken=%q dead=%q want broken_up/2026-01-10/空",
			status, broken, dead)
	}

	// 新生命周期必须独立成行
	var n int
	if err := st.DB().QueryRow(`SELECT COUNT(*) FROM box WHERE code='sh600519'`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Errorf("box 行数=%d want 2（同价不同生命周期各占一行）", n)
	}
}

// TestUpsertBoxesInvalidIdentity invalid 箱体的身份追加 dead_date：
// 作废箱可能在下轨确认前就死（bottom=0、bottom_confirmed 为空），身份退化成只剩 top，
// 不同年份同样 top 的作废箱会撞在一起（实测 124 组）。dead_date 是终态且重算不变，
// 加入身份键后既唯一又稳定，且不影响其他状态的键（避免 confirmed→broken_down 时键漂移）。
func TestUpsertBoxesInvalidIdentity(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	boxes := []contract.Box{
		{ID: 1, Code: "sh600519", Top: 10.88, Bottom: 0, Status: contract.BoxInvalid, DeadDate: "2026-01-08"},
		{ID: 2, Code: "sh600519", Top: 10.88, Bottom: 0, Status: contract.BoxInvalid, DeadDate: "2026-03-30"},
	}
	if _, err := st.BoxRepo.UpsertBoxes(boxes); err != nil {
		t.Fatal(err)
	}
	// 重算同样的两只：必须各复用原行，不得合并成一行，也不得新增
	ids, err := st.BoxRepo.UpsertBoxes(boxes)
	if err != nil {
		t.Fatal(err)
	}
	if len(ids) != 2 || ids[0] == ids[1] {
		t.Fatalf("ids=%v want 2 个不同 id", ids)
	}
	var n int
	if err := st.DB().QueryRow(`SELECT COUNT(*) FROM box WHERE code='sh600519'`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Errorf("box 行数=%d want 2（同 top 不同作废日各占一行）", n)
	}
}

// TestLatestConfirmedBoxAsOf O6 (a) 时点隔离（docs/65 §4 用户 2026-11-08 裁定）。
// 手工造一组 id 序与确认日序背离的箱体（UpsertBoxes 复用旧 id 的真实后果），
// 验证新口径：上下轨都确立 + 两确认日均 ≤ planDate + 按「完全确立日」取最新。
func TestLatestConfirmedBoxAsOf(t *testing.T) {
	st := newTestStore(t)
	code := "sh600519"
	seedSecs(t, st, code)
	type boxRow struct {
		id         int
		top, bot   float64
		topC, botC string
		status     string
	}
	rows := []boxRow{
		{9, 26, 22, "2026-10-08", "2026-10-12", "confirmed"},  // 当期最新但 id 最小 ← 应选中
		{10, 10, 8, "2026-09-01", "2026-09-05", "confirmed"},  // 旧箱体，id 更大
		{11, 20, 18, "2026-10-01", "2026-10-05", "confirmed"}, // 当期次新
		{12, 30, 28, "2026-11-01", "2026-11-05", "confirmed"}, // 未来：10-16 时点须排除
		{13, 40, 38, "2026-10-10", "", "confirmed"},           // 下轨未确立
		{14, 50, 48, "2026-10-12", "2026-10-14", "forming"},   // forming 暂态
	}
	for _, r := range rows {
		var botC any
		if r.botC != "" {
			botC = r.botC
		}
		if _, err := st.DB().Exec(`INSERT INTO box(id,code,top,bottom,top_confirmed,bottom_confirmed,status)
			VALUES (?,?,?,?,?,?,?)`, r.id, code, r.top, r.bot, r.topC, botC, r.status); err != nil {
			t.Fatalf("insert box #%d: %v", r.id, err)
		}
	}
	// 旧口径（ORDER BY id DESC）确实拿错，证明本修复不是空转。
	old, err := st.BoxRepo.LatestNonFormingBox(code)
	if err != nil || old == nil {
		t.Fatalf("LatestNonFormingBox: box=%v err=%v", old, err)
	}
	if old.ID == 9 {
		t.Fatalf("旧口径恰好选中正确箱体 #9，测试构造失效（拿到 #%d）", old.ID)
	}

	got, err := st.BoxRepo.LatestConfirmedBoxAsOf(code, "2026-10-16")
	if err != nil {
		t.Fatal(err)
	}
	if got == nil || got.ID != 9 || got.Bottom != 22 || got.BottomConfirmed != "2026-10-12" {
		t.Fatalf("planDate=2026-10-16 取到 %+v，want #9 bottom=22 botC=2026-10-12", got)
	}
	// 时点提前到所有确认日之前 → 不得消费任何未来箱体。
	none, err := st.BoxRepo.LatestConfirmedBoxAsOf(code, "2026-01-01")
	if err != nil || none != nil {
		t.Fatalf("planDate=2026-01-01 应无合格箱体，got=%+v err=%v", none, err)
	}
	// 时点后移：原本的「未来箱体」完全确立日 11-05 最新，此时才可见。
	later, err := st.BoxRepo.LatestConfirmedBoxAsOf(code, "2026-11-10")
	if err != nil {
		t.Fatal(err)
	}
	if later == nil || later.ID != 12 {
		t.Fatalf("planDate=2026-11-10 取到 %+v，want #12", later)
	}
}
