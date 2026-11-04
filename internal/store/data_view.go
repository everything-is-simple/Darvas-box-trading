package store

import (
	"database/sql"
	"fmt"
)

// 数据口径视图（docs/53-data-spec-new-baseline.md §8.2 / §8.3，migration 0032）。
//
// 存在的唯一理由：后复权表灌没灌好，必须让引擎**显式知道**，而不是「查不到就退回原价」。
// 后者是 0019 误杀 tdx.root 的同型失败：能跑、结果错、不报错。
//
// 三态：
//   raw      —— daily_bar_adj 为空（旧库、生产库）→ 全部读原始价，行为与 0032 之前一致
//   adj      —— data_meta.adj_ready='1'          → 个股读后复权，指数读原始价
//   半成品   —— 表里有行但 adj_ready≠1           → Open 直接报错，禁止回放
type dataView struct {
	adj   bool
	index map[string]bool // security.kind='index' 的代码集合（预载，799 只）
}

// priceSource 一条价格类查询的表名与列名。列名不同是因为 adj 表用 adj_* 前缀。
type priceSource struct {
	table                                  string
	open, high, low, close, volume, amount string
}

var rawPrice = priceSource{
	table: "daily_bar",
	open:  "open", high: "high", low: "low", close: "close", volume: "volume", amount: "amount",
}

var adjPrice = priceSource{
	table: "daily_bar_adj",
	open:  "adj_open", high: "adj_high", low: "adj_low", close: "adj_close",
	volume: "adj_volume", amount: "amount", // amount 原值拷贝，不调整（B6）
}

// price 返回该代码应读的价格源：指数永远原始价（adj 表不含指数），
// 个股在 adj 模式下读后复权。
func (v *dataView) price(code string) priceSource {
	if v == nil || !v.adj || v.index[code] {
		return rawPrice
	}
	return adjPrice
}

// Mode 数据口径标识，写进 baseline.data_mode。
func (v *dataView) Mode() string {
	if v != nil && v.adj {
		return "adj"
	}
	return "raw"
}

// DataMode 暴露给 backtest 记基线身份。
func (s *Store) DataMode() string { return s.dv.Mode() }

// loadDataView 迁移后调用：读 data_meta 与 security.kind，判定数据口径。
// 半成品直接报错——不允许「灌了一半的库」被当成完整后复权库跑 14 年。
func loadDataView(db *sql.DB) (*dataView, error) {
	v := &dataView{index: map[string]bool{}}

	var ready string
	err := db.QueryRow(`SELECT v FROM data_meta WHERE k = 'adj_ready'`).Scan(&ready)
	if err != nil && err != sql.ErrNoRows {
		return nil, fmt.Errorf("读 data_meta.adj_ready: %w", err)
	}

	var hasRow int
	if err := db.QueryRow(`SELECT 1 FROM daily_bar_adj LIMIT 1`).Scan(&hasRow); err != nil &&
		err != sql.ErrNoRows {
		return nil, fmt.Errorf("探测 daily_bar_adj: %w", err)
	}

	switch {
	case ready == "1":
		v.adj = true
	case hasRow == 1:
		return nil, fmt.Errorf("后复权数据未通过验收：daily_bar_adj 已有数据但 data_meta.adj_ready != '1'。" +
			"禁止回放（半灌库会产生静默错价）。请先跑 scripts/load_bar_adj.py + verify_bar_adj.py")
	default:
		// raw：表空，保持 0032 之前的行为
	}

	rows, err := db.Query(`SELECT code FROM security WHERE kind = 'index'`)
	if err != nil {
		return nil, fmt.Errorf("读指数代码集合: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var c string
		if err := rows.Scan(&c); err != nil {
			return nil, err
		}
		v.index[c] = true
	}
	return v, rows.Err()
}
