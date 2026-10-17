package reporter

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// seedWeeklyStore 构造周报测试库：1 只持仓（收盘已破止损）+ 箱体阶梯 +
// 当周买入成交 + 无卖出的止损信号 + 两条权益快照。
func seedWeeklyStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })

	if err := st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sh600000", "")}); err != nil {
		t.Fatal(err)
	}
	bars := []contract.DailyBar{
		{Code: "sh600000", Date: "2026-10-15", Open: 9.2, High: 9.6, Low: 9.0, Close: 9.4, Volume: 1000, Amount: 9400},
		{Code: "sh600000", Date: "2026-10-16", Open: 9.4, High: 9.5, Low: 9.0, Close: 9.1, Volume: 1000, Amount: 9100},
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatal(err)
	}
	if _, err := st.BoxRepo.UpsertBoxes([]contract.Box{
		{Code: "sh600000", Top: 10.0, Bottom: 9.0, TopConfirmed: "2026-09-20", BottomConfirmed: "2026-09-25", Status: contract.BoxConfirmed},
		{Code: "sh600000", Top: 9.8, Bottom: 9.1, TopConfirmed: "2026-10-10", BottomConfirmed: "2026-10-14", Status: contract.BoxForming},
	}); err != nil {
		t.Fatal(err)
	}
	posID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: "sh600000", OpenedDate: "2026-10-13", Qty: 100, AvgCost: 9.2, StopPrice: 9.5, Status: "open",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := st.TradeRepo.AddLeg(posID, 1, "2026-10-13", 100, 9.2, 9.0, 0); err != nil {
		t.Fatal(err)
	}
	if err := st.TradeRepo.AppendFill(contract.Fill{Code: "sh600000", Date: "2026-10-13", Side: "buy", Price: 9.2, Qty: 100}); err != nil {
		t.Fatal(err)
	}
	if err := st.SignalRepo.AppendSignals([]contract.Signal{
		{Date: "2026-10-15", Code: "sh600000", Type: contract.SignalStop, ClosePx: 9.4, BoxLevel: 9.0},
	}); err != nil {
		t.Fatal(err)
	}
	if err := st.TradeRepo.UpsertEquity(contract.EquitySnapshot{Date: "2026-10-12", Total: 1000000, PeakTotal: 1000000}); err != nil {
		t.Fatal(err)
	}
	if err := st.TradeRepo.UpsertEquity(contract.EquitySnapshot{Date: "2026-10-16", Total: 1005000, PeakTotal: 1005000}); err != nil {
		t.Fatal(err)
	}
	return st
}

func TestWeeklyReport(t *testing.T) {
	st := seedWeeklyStore(t)
	outDir := filepath.Join(t.TempDir(), "reports")
	rep := New(st, outDir)

	res, err := rep.Weekly(context.Background(), "2026-10-17")
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != "ok" || res.Task != "weekly" {
		t.Fatalf("结果错误: %+v", res)
	}

	raw, err := os.ReadFile(rep.WeeklyPath("2026-10-17"))
	if err != nil {
		t.Fatalf("周报文件未生成: %v", err)
	}
	html := string(raw)

	for _, want := range []string{
		"达瓦斯周度复盘 · 2026-10-11 ~ 2026-10-17", // 区间标题（date-6 起）
		"sh600000",                                  // 持仓阶梯全景
		"confirmed",                                 // 阶梯箱体状态
		"+5000",                                     // 权益增减 1000000→1005000
		"买入",                                       // 当周成交
		"未止损：sh600000",                           // 纪律审计①：收盘9.1 < 止损9.5
		"信号未执行",                                  // 纪律审计②：stop 信号无卖出成交
		"账本恒等式审计",                              // 纪律审计④
		"<polyline points=",                          // 权益曲线 sparkline
	} {
		if !strings.Contains(html, want) {
			t.Errorf("周报缺少关键内容: %q", want)
		}
	}
}

func TestWeeklyEmptyState(t *testing.T) {
	// 空库：四段都应显示引导文案而非报错/空表
	st, err := store.Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	rep := New(st, filepath.Join(t.TempDir(), "reports"))

	res, err := rep.Weekly(context.Background(), "2026-10-17")
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != "ok" {
		t.Fatalf("空库周报应 ok: %+v", res)
	}
	raw, err := os.ReadFile(rep.WeeklyPath("2026-10-17"))
	if err != nil {
		t.Fatal(err)
	}
	html := string(raw)
	for _, want := range []string{"当前无持仓", "本周无成交", "本周权益快照不足"} {
		if !strings.Contains(html, want) {
			t.Errorf("空库周报缺少引导文案: %q", want)
		}
	}
}
