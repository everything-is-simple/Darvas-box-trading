// 备份轮转（M7.3 / NFR-3）：流水线尾部 VACUUM INTO var/backup/ + 保留30份滚动删除。
// 与 reporter 的 var/reports 同一 var/ 运行时目录约定（相对工作目录）。
package pipeline

import (
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// backupKeep 保留份数（NFR-3：每日备份保留30份 ≈ 6周自然日）。
const backupKeep = 30

// BackupDaily 当日备份：VACUUM INTO var/backup/darvas_YYYYMMDD.db + 滚动轮转。
// 非 daily_run 任务（不占心跳位）；失败只告警不影响主流程。
// 重跑同日覆盖（当日尾部状态以最后一次流水线为准），文件名按日期 → 每日至多一份。
func (p *Pipeline) BackupDaily(date string) {
	dir := filepath.Join("var", "backup")
	dest := filepath.Join(dir, fmt.Sprintf("darvas_%s.db", strings.ReplaceAll(date, "-", "")))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		p.log.Printf("warn: backup mkdir: %v", err)
		return
	}
	os.Remove(dest) // VACUUM INTO 要求目标不存在
	if err := p.st.Backup(dest); err != nil {
		p.log.Printf("warn: backup %s: %v", dest, err)
		return
	}
	kept := rotateBackups(dir)
	p.log.Printf("backup: %s（目录保留 %d 份）", dest, kept)
}

// rotateBackups 滚动删除超出保留份数的旧备份（文件名含YYYYMMDD，字典序即时间序）。
func rotateBackups(dir string) int {
	files, err := filepath.Glob(filepath.Join(dir, "darvas_*.db"))
	if err != nil || len(files) <= backupKeep {
		return len(files)
	}
	sort.Strings(files)
	for _, f := range files[:len(files)-backupKeep] {
		if err := os.Remove(f); err != nil {
			log.Printf("[backup] warn: remove %s: %v", f, err)
		}
	}
	return backupKeep
}
