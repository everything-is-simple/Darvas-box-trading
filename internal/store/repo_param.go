package store

import (
	"database/sql"
	"strconv"
)

// ParamRepo param 仓储：热调参数+审计留痕（docs/03-erd.md §12）。
type ParamRepo struct{ db *sql.DB }

// NewParamRepo 构造。
func NewParamRepo(db *sql.DB) *ParamRepo { return &ParamRepo{db: db} }

// GetFloat 读浮点参数，缺省用 def。
func (r *ParamRepo) GetFloat(key string, def float64) float64 {
	var v string
	if err := r.db.QueryRow(`SELECT value FROM param WHERE key = ?`, key).Scan(&v); err != nil {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return def
	}
	return f
}

// GetInt 读整型参数。
func (r *ParamRepo) GetInt(key string, def int) int {
	var v string
	if err := r.db.QueryRow(`SELECT value FROM param WHERE key = ?`, key).Scan(&v); err != nil {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

// GetString 读字符串参数。
func (r *ParamRepo) GetString(key string, def string) string {
	var v string
	if err := r.db.QueryRow(`SELECT value FROM param WHERE key = ?`, key).Scan(&v); err != nil {
		if err == sql.ErrNoRows {
			return def
		}
		return def
	}
	return v
}

// Set 写参数，old_value留痕审计（docs/06-consistency.md §3.1）。
func (r *ParamRepo) Set(key, value string) error {
	_, err := r.db.Exec(`UPDATE param SET old_value = value, value = ?, changed_at = datetime('now','localtime')
		WHERE key = ?`, value, key)
	if err != nil {
		return err
	}
	return nil
}

// All 读全部参数（GUI参数视图）。
func (r *ParamRepo) All() (map[string]string, error) {
	rows, err := r.db.Query(`SELECT key, value FROM param ORDER BY key`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]string{}
	for rows.Next() {
		var k, v string
		if err := rows.Scan(&k, &v); err != nil {
			return nil, err
		}
		out[k] = v
	}
	return out, rows.Err()
}
