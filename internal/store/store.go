// Package store 是 SQLite 的唯一入口（Repository 模式）。
// 设计依据 docs/02-architecture.md §1 分层铁律、docs/03-erd.md（schema事实源）。
package store

import (
	"database/sql"
	"embed"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	_ "modernc.org/sqlite"
)

//go:embed migrations/*.sql
var migrationsFS embed.FS

// Store 持有数据库连接，聚合各仓储。
type Store struct {
	db *sql.DB
	*BarRepo
	*SecRepo
	*BoxRepo
	*PoolRepo
	*SignalRepo
	*TradeRepo
	*RunRepo
	*ParamRepo
}

// Open 打开（或创建）数据库并执行迁移。
// dsn: darvas.db 文件路径。
func Open(dsn string) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(dsn), 0o755); err != nil {
		return nil, fmt.Errorf("create db dir: %w", err)
	}
	// 纯Go驱动，WAL提升GUI读+调度写并发（ADR-5）。
	// PRAGMA 调优（docs/05 M1.1）：
	//   - journal_mode=WAL      单写者+多读者，崩溃安全
	//   - synchronous=NORMAL    WAL下足够安全，比默认 FULL 快数倍（NFR-1）
	//   - cache_size=-65536     64MB 页缓存，减少磁盘往返
	//   - temp_store=MEMORY     临时表/索引走内存
	//   - foreign_keys=ON       外键强制（03-erd §3）
	//   - busy_timeout=5000ms   写锁竞争等待
	dsn = "file:" + dsn +
		"?_pragma=journal_mode(WAL)" +
		"&_pragma=synchronous(NORMAL)" +
		"&_pragma=cache_size(-65536)" +
		"&_pragma=temp_store(MEMORY)" +
		"&_pragma=foreign_keys(1)" +
		"&_pragma=busy_timeout(5000)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1) // SQLite单写者，避免database is locked
	s := &Store{db: db}
	if err := s.migrate(); err != nil {
		db.Close()
		return nil, fmt.Errorf("migrate: %w", err)
	}
	s.BarRepo = NewBarRepo(db)
	s.SecRepo = NewSecRepo(db)
	s.BoxRepo = NewBoxRepo(db)
	s.PoolRepo = NewPoolRepo(db)
	s.SignalRepo = NewSignalRepo(db)
	s.TradeRepo = NewTradeRepo(db)
	s.RunRepo = NewRunRepo(db)
	s.ParamRepo = NewParamRepo(db)
	return s, nil
}

// Close 关闭数据库。
func (s *Store) Close() error { return s.db.Close() }

// DB 暴露原始连接（仅迁移/诊断用，业务代码禁止绕过Repo）。
func (s *Store) DB() *sql.DB { return s.db }

// migrate 按 schema_version 顺序执行追加式迁移。
// 严禁修改已发布迁移文件（docs/06-consistency.md §2 防线三）。
func (s *Store) migrate() error {
	if _, err := s.db.Exec(`CREATE TABLE IF NOT EXISTS schema_version (
		version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL DEFAULT (datetime('now','localtime')), description TEXT)`); err != nil {
		return err
	}
	var cur int
	if err := s.db.QueryRow(`SELECT COALESCE(MAX(version),0) FROM schema_version`).Scan(&cur); err != nil {
		return err
	}
	entries, err := migrationsFS.ReadDir("migrations")
	if err != nil {
		return err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	for i, name := range names {
		version := i + 1
		if version <= cur {
			continue
		}
		body, err := migrationsFS.ReadFile("migrations/" + name)
		if err != nil {
			return err
		}
		tx, err := s.db.Begin()
		if err != nil {
			return err
		}
		if _, err := tx.Exec(string(body)); err != nil {
			tx.Rollback()
			return fmt.Errorf("migration %s: %w", name, err)
		}
		if _, err := tx.Exec(`INSERT INTO schema_version(version, description) VALUES(?, ?)`,
			version, name); err != nil {
			tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
		fmt.Printf("[store] applied migration %s (v%d)\n", name, version)
	}
	return nil
}

// Backup 用 VACUUM INTO 做每日备份（ADR-5，保留30份由调用方轮转）。
func (s *Store) Backup(dest string) error {
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	_, err := s.db.Exec("VACUUM INTO ?", dest)
	return err
}

// BulkLoadToggle 切换全量导入的性能优化（docs/01 D1 / docs/05 M1.1）：
//
//   on=true  删除 daily_bar 的 (date) 二级索引 → 导入期每行少维护一棵 B 树，
//            对 11942 只×散列 code 的随机插入提速一个数量级；
//   on=false 重建该索引（SQLite 用排序构建，远快于逐行维护）。
//
// PK(code,date) 是 WITHOUT ROWID 表本体，不可删；只动二级索引。
// 仅限全量导入（幂等可重跑），增量导入不必调用。
func (s *Store) BulkLoadToggle(on bool) error {
	if on {
		_, err := s.db.Exec(`DROP INDEX IF EXISTS idx_daily_bar_date`)
		return err
	}
	_, err := s.db.Exec(`CREATE INDEX IF NOT EXISTS idx_daily_bar_date ON daily_bar(date)`)
	return err
}
