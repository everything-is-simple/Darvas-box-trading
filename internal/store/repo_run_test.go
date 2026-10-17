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
