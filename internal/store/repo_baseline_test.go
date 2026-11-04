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

	// 灌两个参数，保证指纹可算（0031 加固后 Set 对不存在的键报错，改用 INSERT）
	if _, err := st.DB().Exec(`INSERT INTO param(key,value) VALUES('a.key','1'),('b.key','2')`); err != nil {
		t.Fatalf("seed param: %v", err)
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

// TestLatestBaseline 验证读最新基线身份：latest=1 那一行、未记录时返回 ErrNoRows。
func TestLatestBaseline(t *testing.T) {
	st := newTestStore(t)
	defer st.Close()

	// 未记录身份时 → ErrNoRows
	if _, err := st.LatestBaseline(); err == nil {
		t.Fatalf("未记录身份时 LatestBaseline 应报错，得到 nil")
	}

	// 记录一次
	id, err := st.RecordBaseline("0.5.0-test", "")
	if err != nil {
		t.Fatalf("record: %v", err)
	}
	b, err := st.LatestBaseline()
	if err != nil {
		t.Fatalf("LatestBaseline: %v", err)
	}
	if b.BaselineID != id {
		t.Fatalf("BaselineID=%s want %s", b.BaselineID, id)
	}
	if len(b.ParamFingerprint) != 64 || len(b.EngineSHA256) != 64 {
		t.Fatalf("指纹/引擎hash 应为 64 位 hex，得 %q / %q", b.ParamFingerprint, b.EngineSHA256)
	}

	// 再记一次 → latest 翻转，读到的是新的
	id2, err := st.RecordBaseline("0.5.0-test", "")
	if err != nil {
		t.Fatalf("record #2: %v", err)
	}
	b2, err := st.LatestBaseline()
	if err != nil {
		t.Fatalf("LatestBaseline #2: %v", err)
	}
	if b2.BaselineID != id2 {
		t.Fatalf("翻转后 BaselineID=%s want %s", b2.BaselineID, id2)
	}
}

// TestParamFingerprint 验证指纹稳定性：key 顺序无关、值敏感。
func TestParamFingerprint(t *testing.T) {
	a := ParamFingerprint(map[string]string{"k1": "v1", "k2": "v2"})
	b := ParamFingerprint(map[string]string{"k2": "v2", "k1": "v1"}) // key 乱序
	c := ParamFingerprint(map[string]string{"k1": "v1", "k2": "v3"}) // 值变了
	if a != b {
		t.Fatalf("key 顺序不应影响指纹：%s != %s", a, b)
	}
	if a == c {
		t.Fatalf("值变化应改变指纹：%s == %s", a, c)
	}
	if len(a) != 64 {
		t.Fatalf("指纹应为 64 位 hex，得 %q", a)
	}
}
