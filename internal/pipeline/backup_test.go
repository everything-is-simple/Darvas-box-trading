package pipeline

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

func TestRotateBackups(t *testing.T) {
	dir := t.TempDir()
	// 造 35 份按日期命名的假备份（20260913 起 35 个连续日期）
	for i := 0; i < 35; i++ {
		d := time.Date(2026, 9, 13, 0, 0, 0, 0, time.UTC).AddDate(0, 0, i).
			Format("20060102")
		f := filepath.Join(dir, "darvas_"+d+".db")
		if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	// 干扰文件：不匹配 darvas_*.db 模式，不应被轮转删除
	junk := filepath.Join(dir, "notes.txt")
	os.WriteFile(junk, []byte("x"), 0o644)

	if kept := rotateBackups(dir); kept != backupKeep {
		t.Fatalf("kept=%d, want %d", kept, backupKeep)
	}
	files, _ := filepath.Glob(filepath.Join(dir, "darvas_*.db"))
	if len(files) != backupKeep {
		t.Fatalf("剩余 %d 份, want %d", len(files), backupKeep)
	}
	// 最旧的 5 份被删，最新的 30 份保留
	for _, f := range files {
		if strings.HasSuffix(f, "20260913.db") || strings.HasSuffix(f, "20260917.db") {
			t.Fatalf("旧备份未被删除: %s", f)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, "darvas_20261017.db")); err != nil {
		t.Fatalf("最新备份被误删: %v", err)
	}
	if _, err := os.Stat(junk); err != nil {
		t.Fatalf("无关文件被误删: %v", err)
	}
}

// TestBackupDailyAndRestore 备份+恢复演练（M7.3 验收：恢复成功）：
// 写入数据 → BackupDaily → 用 store.Open 打开备份 → 数据可读。
func TestBackupDailyAndRestore(t *testing.T) {
	t.Chdir(t.TempDir()) // var/backup 相对工作目录
	st, err := store.Open(filepath.Join(t.TempDir(), "src.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	if err := st.SecRepo.UpsertSecurities([]contract.Security{
		store.DeriveSecurity("sh600519", "2026-10-16"),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		{Code: "sh600519", Date: "2026-10-16", Open: 1300, High: 1340, Low: 1295, Close: 1329, Volume: 30000},
	}); err != nil {
		t.Fatal(err)
	}

	p := New(st, nil)
	p.BackupDaily("2026-10-16")
	p.BackupDaily("2026-10-16") // 重跑同日：覆盖不报错

	dest := filepath.Join("var", "backup", "darvas_20261016.db")
	if _, err := os.Stat(dest); err != nil {
		t.Fatalf("备份文件不存在: %v", err)
	}

	// 恢复演练：直接打开备份当运行库
	rb, err := store.Open(dest)
	if err != nil {
		t.Fatalf("恢复失败: %v", err)
	}
	defer rb.Close()
	c, found, err := rb.BarRepo.CloseOn("sh600519", "2026-10-16")
	if err != nil || !found || c != 1329 {
		t.Fatalf("恢复后数据不一致: close=%v found=%v err=%v", c, found, err)
	}
}
