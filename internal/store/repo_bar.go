package store

import (
	"database/sql"
	"fmt"
	"strings"

	"darvas/internal/contract"
)

// BarRepo daily_bar 仓储（docs/04-api-contract.md §3.3）。
type BarRepo struct{ db *sql.DB }

// NewBarRepo 构造。
func NewBarRepo(db *sql.DB) *BarRepo { return &BarRepo{db: db} }

// batchInsertSize 每条 INSERT 语句拼入的行数。
// 基准实测（internal/store/bench_test.go TestBenchInsertStrategies）：
//
//	batch=500 → 11k bars/sec；batch=100 → 35k bars/sec（最优）；batch=1000 → 12k。
//
// modernc.org/sqlite 对大批次参数绑定有惩罚，100 行×8列=800 参数为甜点。
const batchInsertSize = 100

// colCount daily_bar 写入列数（与 insertBarsSQL 对应）。
const colCount = 8

// placeholdersFull 预生成的满批占位符串（500 行 × 8 列），避免热路径重复拼接。
var placeholdersFull = buildPlaceholders(batchInsertSize, colCount)

// buildPlaceholders 生成 rows 行 cols 列的 VALUES 占位符串："(?,?,...),(?,?,...)"
func buildPlaceholders(rows, cols int) string {
	var b strings.Builder
	b.Grow(rows * (cols*2 + 3))
	for i := 0; i < rows; i++ {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteByte('(')
		for j := 0; j < cols; j++ {
			if j > 0 {
				b.WriteByte(',')
			}
			b.WriteByte('?')
		}
		b.WriteByte(')')
	}
	return b.String()
}

// insertBarsSQL 多行 INSERT OR REPLACE 前缀（占位符串按批次拼接）。
const insertBarsSQL = `INSERT OR REPLACE INTO daily_bar
	(code, date, open, high, low, close, volume, amount) VALUES `

// UpsertBars 批量幂等写入（INSERT OR REPLACE：同 code+date 重导即更新）。
// 实现要点（docs/05 M1.1）：
//   - 多行 VALUES：每 batchInsertSize 行拼一条 INSERT，大幅减少 Exec 往返；
//   - 单事务：整批在一个事务内提交，配合 store.Open 的 synchronous=NORMAL 提速；
//   - 幂等：复合主键 (code,date) + INSERT OR REPLACE，重跑零重复。
func (r *BarRepo) UpsertBars(bars []contract.DailyBar) (int64, error) {
	if len(bars) == 0 {
		return 0, nil
	}
	tx, err := r.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()

	total := int64(0)
	for start := 0; start < len(bars); start += batchInsertSize {
		end := start + batchInsertSize
		if end > len(bars) {
			end = len(bars)
		}
		n := end - start
		ph := placeholdersFull
		if n != batchInsertSize {
			ph = buildPlaceholders(n, colCount)
		}
		args := make([]any, 0, n*colCount)
		for i := start; i < end; i++ {
			b := bars[i]
			args = append(args, b.Code, b.Date, b.Open, b.High, b.Low, b.Close, b.Volume, b.Amount)
		}
		if _, err := tx.Exec(insertBarsSQL+ph, args...); err != nil {
			return total, fmt.Errorf("upsert batch [%d,%d): %w", start, end, err)
		}
		total += int64(n)
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return total, nil
}

// BarsWindow 读取 code 截至 endDate（含）的最近 n 根K线，升序返回。
// boxscan 主路径：三日法则只需一个有限窗口（docs/02-architecture.md §6）。
// 注意：CAST(date AS TEXT) 绕过 modernc 对 DATE 列的 time.Time 转换，
// 保证引擎拿到纯 "YYYY-MM-DD" 字符串（确定性纯函数要求）。
func (r *BarRepo) BarsWindow(code string, endDate string, n int) ([]contract.DailyBar, error) {
	rows, err := r.db.Query(`SELECT CAST(date AS TEXT), open, high, low, close, volume, amount
		FROM daily_bar WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?`, code, endDate, n)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.DailyBar
	for rows.Next() {
		var b contract.DailyBar
		b.Code = code
		if err := rows.Scan(&b.Date, &b.Open, &b.High, &b.Low, &b.Close, &b.Volume, &b.Amount); err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	// 反转为升序
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out, rows.Err()
}

// CloseOn 查某日收盘价。
func (r *BarRepo) CloseOn(code, date string) (float64, bool, error) {
	var c float64
	err := r.db.QueryRow(`SELECT close FROM daily_bar WHERE code = ? AND date = ?`, code, date).Scan(&c)
	if err == sql.ErrNoRows {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return c, true, nil
}

// AvgVolume 截至 endDate 的 n 日均成交量（手）。
func (r *BarRepo) AvgVolume(code string, endDate string, n int) (float64, error) {
	var v float64
	err := r.db.QueryRow(`SELECT COALESCE(AVG(volume), 0) FROM (
		SELECT volume FROM daily_bar WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?)`,
		code, endDate, n).Scan(&v)
	return v, err
}

// AvgAmount 截至 endDate 的 n 日均成交额（元，screener 流动性条件 FR-301）。
func (r *BarRepo) AvgAmount(code string, endDate string, n int) (float64, error) {
	var v float64
	err := r.db.QueryRow(`SELECT COALESCE(AVG(amount), 0) FROM (
		SELECT amount FROM daily_bar WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?)`,
		code, endDate, n).Scan(&v)
	return v, err
}

// LatestDate 某证券的最新交易日（交易日历与新鲜度校验依据，ADR-2）。
// 无数据时返回空串（COALESCE 避免 NULL 扫描错误）。
func (r *BarRepo) LatestDate(code string) (string, error) {
	var d string
	err := r.db.QueryRow(`SELECT COALESCE(MAX(date),'') FROM daily_bar WHERE code = ?`, code).Scan(&d)
	return d, err
}

// PrevDate 该证券在 before 之前的最近日期（scheduler CatchUp 判定前一交易日，ADR-2）。
func (r *BarRepo) PrevDate(code, before string) (string, error) {
	var d string
	err := r.db.QueryRow(`SELECT COALESCE(MAX(date),'') FROM daily_bar WHERE code = ? AND date < ?`,
		code, before).Scan(&d)
	return d, err
}

// HasBar 是否存在该日数据。
func (r *BarRepo) HasBar(code, date string) (bool, error) {
	var one int
	err := r.db.QueryRow(`SELECT 1 FROM daily_bar WHERE code = ? AND date = ? LIMIT 1`, code, date).Scan(&one)
	if err == sql.ErrNoRows {
		return false, nil
	}
	return err == nil, err
}

// CountBars 统计某证券的K线总数（doctor/质量报告）。
func (r *BarRepo) CountBars(code string) (int64, error) {
	var n int64
	err := r.db.QueryRow(`SELECT COUNT(*) FROM daily_bar WHERE code = ?`, code).Scan(&n)
	return n, err
}

// LastClose 某证券最近一根K线的收盘价（GUI 持仓浮盈亏/跟踪池最新价）。
// 无数据返回 (0, false, nil)。PK(code,date) WITHOUT ROWID 表上
// ORDER BY date DESC LIMIT 1 走索引倒扫，O(log n)。
func (r *BarRepo) LastClose(code string) (float64, bool, error) {
	var c float64
	err := r.db.QueryRow(`SELECT close FROM daily_bar WHERE code = ? ORDER BY date DESC LIMIT 1`, code).Scan(&c)
	if err == sql.ErrNoRows {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return c, true, nil
}
