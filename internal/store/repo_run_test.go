package store

import (
	"testing"

	"darvas/internal/contract"
)

// TestStartRunIdempotent 同日同任务重跑：UNIQUE(date,task) 冲突时重置状态而非报错。
func TestStartRunIdempotent(t *testing.T) {
	st := newTestStore(t)
	id1, err := st.RunRepo.StartRun("2026-10-17", "ingest")
	if err != nil {
		t.Fatal(err)
	}
	if id1 == 0 {
		t.Fatal("first StartRun id=0")
	}
	// 标记成功
	st.RunRepo.FinishRun(id1, contract.TaskResult{Status: "ok", AffectedRows: 100})

	// 同日重跑 → 应重置为 running，返回的 id 可能复用
	if _, err := st.RunRepo.StartRun("2026-10-17", "ingest"); err != nil {
		t.Fatalf("rerun StartRun: %v", err)
	}
	var status string
	st.DB().QueryRow(`SELECT status FROM daily_run WHERE date='2026-10-17' AND task='ingest'`).Scan(&status)
	if status != "running" {
		t.Fatalf("rerun status=%s want running (reset)", status)
	}
	// 只应有一行
	var cnt int
	st.DB().QueryRow(`SELECT COUNT(*) FROM daily_run WHERE date='2026-10-17' AND task='ingest'`).Scan(&cnt)
	if cnt != 1 {
		t.Fatalf("daily_run rows=%d want 1 (UNIQUE enforced)", cnt)
	}
}

// TestFinishRun 登记结果。
func TestFinishRun(t *testing.T) {
	st := newTestStore(t)
	id, _ := st.RunRepo.StartRun("2026-10-17", "boxscan")
	err := st.RunRepo.FinishRun(id, contract.TaskResult{
		Task: "boxscan", Date: "2026-10-17", Status: "failed", AffectedRows: 0, Error: "boom",
	})
	if err != nil {
		t.Fatal(err)
	}
	var status, e string
	var rows int
	st.DB().QueryRow(`SELECT status, COALESCE(error,''), COALESCE(affected_rows,0) FROM daily_run WHERE id=?`, id).
		Scan(&status, &e, &rows)
	if status != "failed" || e != "boom" || rows != 0 {
		t.Fatalf("finish stored status=%s err=%s rows=%d want failed/boom/0", status, e, rows)
	}
}

// TestLastOKRun 补跑判断：只返回 status=ok 的记录。
func TestLastOKRun(t *testing.T) {
	st := newTestStore(t)
	// 第一次失败
	id1, _ := st.RunRepo.StartRun("2026-10-17", "plan")
	st.RunRepo.FinishRun(id1, contract.TaskResult{Status: "failed", Error: "x"})
	// 第二次成功
	id2, _ := st.RunRepo.StartRun("2026-10-17", "plan") // 重置
	st.RunRepo.FinishRun(id2, contract.TaskResult{Status: "ok", AffectedRows: 5})

	res, ok, err := st.RunRepo.LastOKRun("2026-10-17", "plan")
	if err != nil {
		t.Fatal(err)
	}
	if !ok {
		t.Fatal("LastOKRun should find the ok run")
	}
	if res.Status != "ok" || res.AffectedRows != 5 {
		t.Fatalf("LastOKRun res=%+v want ok/5", res)
	}

	// 没跑过的任务
	_, ok2, _ := st.RunRepo.LastOKRun("2026-10-17", "report")
	if ok2 {
		t.Fatal("unrun task should return ok=false")
	}
}

// TestRunRepoDifferentTasks 同日不同任务互不干扰。
func TestRunRepoDifferentTasks(t *testing.T) {
	st := newTestStore(t)
	for _, task := range []string{"ingest", "boxscan", "screen", "plan"} {
		id, err := st.RunRepo.StartRun("2026-10-17", task)
		if err != nil {
			t.Fatalf("StartRun %s: %v", task, err)
		}
		st.RunRepo.FinishRun(id, contract.TaskResult{Status: "ok", AffectedRows: 1})
	}
	var cnt int
	st.DB().QueryRow(`SELECT COUNT(*) FROM daily_run WHERE date='2026-10-17'`).Scan(&cnt)
	if cnt != 4 {
		t.Fatalf("daily_run rows=%d want 4 (one per task)", cnt)
	}
}

// replayTasks 回放六任务链（与 backtest.sixTasks 一致）。
var replayTasks = []string{"boxscan", "screen", "poolsync", "signal", "plan", "report"}

// markCompleteDay 标记某日回放六任务全 ok（模拟一天完整跑完）。
func markCompleteDay(t *testing.T, st *Store, date string) {
	t.Helper()
	for _, task := range replayTasks {
		id, err := st.RunRepo.StartRun(date, task)
		if err != nil {
			t.Fatalf("StartRun %s %s: %v", date, task, err)
		}
		if err := st.RunRepo.FinishRun(id, contract.TaskResult{Status: "ok", AffectedRows: 1}); err != nil {
			t.Fatalf("FinishRun %s %s: %v", date, task, err)
		}
	}
}

// TestLastCompleteDate 验证断点判据：六任务全 ok 的最大日期；未满六任务不算完成日。
func TestLastCompleteDate(t *testing.T) {
	st := newTestStore(t)
	defer st.Close()

	// 初始无任何完整日 → 空串
	d, err := st.RunRepo.LastCompleteDate()
	if err != nil {
		t.Fatalf("LastCompleteDate 空库: %v", err)
	}
	if d != "" {
		t.Fatalf("空库断点应为空串，得 %q", d)
	}

	// 2026-10-15 完整跑完
	markCompleteDay(t, st, "2026-10-15")
	// 2026-10-16 只跑完 5 个任务（1 个 failed）→ 不算完成日
	for i, task := range replayTasks {
		id, err := st.RunRepo.StartRun("2026-10-16", task)
		if err != nil {
			t.Fatalf("StartRun: %v", err)
		}
		status := "ok"
		if i == 5 { // 最后一个 report 失败
			status = "failed"
		}
		st.RunRepo.FinishRun(id, contract.TaskResult{Status: status})
	}
	// 2026-10-17 完整跑完
	markCompleteDay(t, st, "2026-10-17")

	d, err = st.RunRepo.LastCompleteDate()
	if err != nil {
		t.Fatalf("LastCompleteDate: %v", err)
	}
	if d != "2026-10-17" {
		t.Fatalf("断点应为 2026-10-17（16 日 report 失败不算完成），得 %q", d)
	}

	// 断点之后（17）若再插 ingest 等非回放任务，不影响判据
	if _, err := st.RunRepo.StartRun("2026-10-18", "ingest"); err != nil {
		t.Fatalf("StartRun ingest: %v", err)
	}
	d2, _ := st.RunRepo.LastCompleteDate()
	if d2 != "2026-10-17" {
		t.Fatalf("ingest 不应影响断点，得 %q", d2)
	}
}
