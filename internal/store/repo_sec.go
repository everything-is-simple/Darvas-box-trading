package store

import (
	"database/sql"
	"fmt"
	"strings"

	"darvas/internal/contract"
)

// SecRepo security 仓储：从代码推导证券基础属性（FR-103：板块/类型识别）。
// 不在 contract 接口面（证券识别为数据层内部职责），但留在 store 包内
// 以守住「store 是 SQLite 唯一入口」分层铁律（docs/02 §1）。
type SecRepo struct{ db *sql.DB }

// NewSecRepo 构造。
func NewSecRepo(db *sql.DB) *SecRepo { return &SecRepo{db: db} }

// secBatchSize 每条 INSERT 拼入的证券行数。
const secBatchSize = 200

// UpsertSecurities 批量写入证券基础信息（INSERT OR IGNORE：已存在则不动）。
// 从代码规则推导 market/kind/board（docs/00-darvas-theory.md §7 A股适配）。
func (r *SecRepo) UpsertSecurities(secs []contract.Security) error {
	if len(secs) == 0 {
		return nil
	}
	tx, err := r.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for start := 0; start < len(secs); start += secBatchSize {
		end := start + secBatchSize
		if end > len(secs) {
			end = len(secs)
		}
		n := end - start
		ph := buildPlaceholders(n, 5) // code, market, kind, board, list_date
		args := make([]any, 0, n*5)
		for i := start; i < end; i++ {
			s := secs[i]
			listDate := any(nil)
			if s.ListDate != "" {
				listDate = s.ListDate
			}
			args = append(args, s.Code, s.Market, s.Kind, s.Board, listDate)
		}
		if _, err := tx.Exec(`INSERT OR IGNORE INTO security(code, market, kind, board, list_date) VALUES `+ph, args...); err != nil {
			return fmt.Errorf("upsert securities [%d,%d): %w", start, end, err)
		}
	}
	return tx.Commit()
}

// Classify 按市场前缀+数字代码分类（docs/00-darvas-theory.md §7 A股适配）。
// 返回 (kind, board)：kind=index/stock/other，board=index/main/gem/star/b/other。
func Classify(market, num string) (kind, board string) {
	if len(num) != 6 {
		return "other", "other"
	}
	// 指数：sh000xxx / sz399xxx
	if (market == "sh" && strings.HasPrefix(num, "000")) ||
		(market == "sz" && strings.HasPrefix(num, "399")) {
		return "index", "index"
	}
	switch {
	case strings.HasPrefix(num, "60"): // 沪主板
		return "stock", "main"
	case strings.HasPrefix(num, "68"): // 科创板
		return "stock", "star"
	case strings.HasPrefix(num, "30"): // 创业板
		return "stock", "gem"
	case strings.HasPrefix(num, "00"): // 深主板（000/001/002/003）
		return "stock", "main"
	case strings.HasPrefix(num, "20"): // B股
		return "stock", "b"
	default:
		return "other", "other"
	}
}

// DeriveSecurity 从代码推导 Security（不含 name/industry，导入时仅定基础属性）。
func DeriveSecurity(code, firstDate string) contract.Security {
	market := "sh"
	if strings.HasPrefix(code, "sz") {
		market = "sz"
	}
	num := strings.TrimPrefix(strings.TrimPrefix(code, "sh"), "sz")
	kind, board := Classify(market, num)
	return contract.Security{
		Code:     code,
		Market:   market,
		Kind:     kind,
		Board:    board,
		ListDate: firstDate,
	}
}

// ActiveStockCodes 返回全部活跃股票代码（docs/00 §7 股票池：沪深主板+创业板+科创板），
// 排除指数/other/B股，供 boxscan/screen 遍历。按 code 升序，便于稳定迭代与进度日志。
// A2（2026-10-22，docs/21 F3 裁决 + docs/18 R2）：按名称排除 ST/*ST——ST 涨跌停 ±5%、
// 退市风险，交易规则与正常股不同，达瓦斯池不含 ST（match gapThreshold 同此假设）。
// 实测 full_hist.db %ST% 匹配零误伤（201 支全部 ST 族前缀，无「含ST但非ST」名）。
// 边界：名称未知（空）的证券无从判定，保守保留；TDX 名称是当前时点快照，
// 历史 ST 状态不可回溯（回测诚实标注的已知瑕疵）。
func (r *SecRepo) ActiveStockCodes() ([]string, error) {
	rows, err := r.db.Query(`SELECT code FROM security
		WHERE kind = 'stock' AND board IN ('main', 'gem', 'star') AND status = 'active'
		  AND (name IS NULL OR name = '' OR name NOT LIKE '%ST%')
		ORDER BY code`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var c string
		if err := rows.Scan(&c); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// ActiveWatchCodes 返回跟踪池中未退出的股票代码（boxscan 增量必扫集合）。
func (r *SecRepo) ActiveWatchCodes() ([]string, error) {
	rows, err := r.db.Query(`SELECT DISTINCT code FROM watch WHERE stage IN ('watching','pending','holding')`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var c string
		if err := rows.Scan(&c); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// UpdateNames 批量回填证券名称（TDX tnf 代码→名称表，GUI 展示用）。
// 单事务逐行 UPDATE（约1.2万行<1s）；返回实际更新的行数。
// tnf 中存在而 security 表没有的代码（可转债/基金等）自然跳过。
func (r *SecRepo) UpdateNames(names map[string]string) (int64, error) {
	if len(names) == 0 {
		return 0, nil
	}
	tx, err := r.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`UPDATE security SET name = ?, updated_at = datetime('now','localtime')
		WHERE code = ? AND name <> ?`)
	if err != nil {
		return 0, err
	}
	defer stmt.Close()
	var total int64
	for code, name := range names {
		res, err := stmt.Exec(name, code, name)
		if err != nil {
			return total, fmt.Errorf("update name %s: %w", code, err)
		}
		if n, err := res.RowsAffected(); err == nil {
			total += n
		}
	}
	return total, tx.Commit()
}

// Names 取全部非空 code→名称映射（GUI 各视图展示用）。
func (r *SecRepo) Names() (map[string]string, error) {
	rows, err := r.db.Query(`SELECT code, name FROM security WHERE name <> ''`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]string{}
	for rows.Next() {
		var c, n string
		if err := rows.Scan(&c, &n); err != nil {
			return nil, err
		}
		out[c] = n
	}
	return out, rows.Err()
}
