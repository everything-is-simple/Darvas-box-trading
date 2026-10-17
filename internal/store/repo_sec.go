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
