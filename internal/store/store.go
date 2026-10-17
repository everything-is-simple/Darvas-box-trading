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
	*RunRepo
	*ParamRepo
}

// Open 打开（或创建）数据库并执行迁移。
// dsn: darvas.db 文件路径。
func Open(dsn string) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(dsn), 0o755); err != nil {
		return nil, fmt.Errorf("create db dir: %w", err)
	}
	// 纯Go驱动，WAL提升GUI读+调度写并发（ADR-5）
	dsn = "file:" + dsn + "?_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)&_pragma=busy_timeout(5000)"
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
