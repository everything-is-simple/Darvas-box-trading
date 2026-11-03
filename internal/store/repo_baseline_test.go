package store

import (
	"os"
	"path/filepath"
	"testing"
)

// TestRecordBaseline 验证基线身份：指纹稳定、baseline_id 自增、latest 翻转。
func TestRecordBaseline(t *testing.T) {
	dir := t.TempDir()
	db := filepath.Join(dir, "t.db")
	st, err := Open(db)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer st.Close()

	// 灌两个参数，保证指纹可算
	if err := st.ParamRepo.Set("a.key", "1"); err != nil {
		t.Fatalf("set param: %v", err)
	}
	if err := st.ParamRepo.Set("b.key", "2"); err != nil {
		t.Fatalf("set param: %v", err)
	}

	id1, err := st.RecordBaseline("0.5.0-test", "")
	if err != nil {
		t.Fatalf("record #1: %v", err)
	}
	// 同参数再记一次，指纹必须稳定、id 自增
	id2, err := st.RecordBaseline("0.5.0-test", "")
	if err != nil {
		t.Fatalf("record #2: %v", err)
	}
	if id1 == id2 {
		t.Fatalf("baseline_id 应自增，得到 %s == %s", id1, id2)
	}
	if id1[len(id1)-1:] != "1" || id2[len(id2)-1:] != "2" {
		t.Fatalf("序号应递增 v1/v2：%s / %s", id1, id2)
	}

	// latest 只能有一个
	var latest int
	if err := st.db.QueryRow(`SELECT COUNT(*) FROM baseline WHERE latest=1`).Scan(&latest); err != nil {
		t.Fatalf("count latest: %v", err)
	}
	if latest != 1 {
		t.Fatalf("latest 应为 1，得 %d", latest)
	}

	// 指纹与快照非空且一致
	var fp1, fp2, snap string
	if err := st.db.QueryRow(`SELECT param_fingerprint, params_json FROM baseline WHERE baseline_id=?`, id1).
		Scan(&fp1, &snap); err != nil {
		t.Fatalf("read fp1: %v", err)
	}
	if err := st.db.QueryRow(`SELECT param_fingerprint FROM baseline WHERE baseline_id=?`, id2).
		Scan(&fp2); err != nil {
		t.Fatalf("read fp2: %v", err)
	}
	if fp1 == "" || fp2 == "" || snap == "" {
		t.Fatalf("指纹/快照不应为空：fp1=%s fp2=%s", fp1, fp2)
	}
	if fp1 != fp2 {
		t.Fatalf("同参数指纹应一致：%s != %s", fp1, fp2)
	}
	// 引擎 hash 非空（读自身 test 二进制）
	var eh string
	if err := st.db.QueryRow(`SELECT engine_sha256 FROM baseline WHERE baseline_id=?`, id1).Scan(&eh); err != nil {
		t.Fatalf("read engine_sha256: %v", err)
	}
	if len(eh) != 64 {
		t.Fatalf("engine_sha256 应为 64 位 hex，得 %q", eh)
	}
	if _, err := os.Stat(db); err != nil {
		t.Fatalf("db 不存在: %v", err)
	}
}
