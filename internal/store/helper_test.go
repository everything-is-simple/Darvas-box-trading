package store

import (
	"testing"

	"darvas/internal/contract"
)

// newTestStore 构造一个临时 DB 的 Store（含迁移），测试结束自动清理。
// 用于 BarRepo/RunRepo/BoxRepo 等仓储单测——不依赖真实 TDX 数据。
// 接受 testing.TB 以同时服务 *testing.T 与 *testing.B。
func newTestStore(tb testing.TB) *Store {
	tb.Helper()
	dsn := tb.TempDir() + "/test.db"
	st, err := Open(dsn)
	if err != nil {
		tb.Fatalf("Open test store: %v", err)
	}
	tb.Cleanup(func() { st.Close() })
	return st
}

// mkBar 构造一根 K线（便捷方法）。
func mkBar(code, date string, o, h, l, c float64, v int64, amt float64) contract.DailyBar {
	return contract.DailyBar{Code: code, Date: date, Open: o, High: h, Low: l, Close: c, Volume: v, Amount: amt}
}

// seedSecs 先写入证券基础信息（daily_bar.code 有 FK 指向 security.code，
// 测试插 bar 前必须先 seed 对应证券，03-erd §3 外键强制）。
func seedSecs(tb testing.TB, st *Store, codes ...string) {
	tb.Helper()
	secs := make([]contract.Security, len(codes))
	for i, c := range codes {
		secs[i] = DeriveSecurity(c, "")
	}
	if err := st.SecRepo.UpsertSecurities(secs); err != nil {
		tb.Fatalf("seedSecs: %v", err)
	}
}
