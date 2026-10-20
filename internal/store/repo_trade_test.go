package store

import (
	"testing"

	"darvas/internal/contract"
)

// TestOpenExposureUnits M8.8 组合级预算口径：只有 leg1=试探仓；有 leg2+=主力仓（全部腿计入 main）。
func TestOpenExposureUnits(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600001", "sh600002", "sh600003")
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		mkBar("sh600001", "2026-03-02", 25, 25, 25, 25, 100, 1e5),
		mkBar("sh600002", "2026-03-02", 25, 25, 25, 25, 100, 1e5),
	}); err != nil {
		t.Fatal(err)
	}
	unitValue := 20000.0

	// A：只有 leg1 → 试探仓；最新收盘 25，qty400=0.5 单位。
	posAID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: "sh600001", OpenedDate: "2026-03-02", Qty: 400, AvgCost: 24, StopPrice: 20, Status: "open",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := st.TradeRepo.AddLeg(posAID, 1, "2026-03-02", 400, 25, 20, 0); err != nil {
		t.Fatal(err)
	}

	// B：leg1+leg2 → 主力仓；全部 800 股按最新收盘 25 计 1.0 单位。
	posBID, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: "sh600002", OpenedDate: "2026-03-02", Qty: 800, AvgCost: 25, StopPrice: 20, Status: "open",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := st.TradeRepo.AddLeg(posBID, 1, "2026-03-02", 300, 25, 20, 0); err != nil {
		t.Fatal(err)
	}
	if err := st.TradeRepo.AddLeg(posBID, 2, "2026-03-03", 500, 26, 22, 0); err != nil {
		t.Fatal(err)
	}

	// C：无 K 线、无 leg → 回退 avg_cost 计试探仓。
	if _, err := st.TradeRepo.CreatePosition(contract.Position{
		Code: "sh600003", OpenedDate: "2026-03-02", Qty: 100, AvgCost: 20, StopPrice: 16, Status: "open",
	}); err != nil {
		t.Fatal(err)
	}

	probe, main, err := st.TradeRepo.OpenExposureUnits(unitValue)
	if err != nil {
		t.Fatal(err)
	}
	if !near(probe, 0.6) {
		t.Fatalf("probeUnits=%.3f want 0.6（A 0.5 + C 0.1）", probe)
	}
	if !near(main, 1.0) {
		t.Fatalf("mainUnits=%.3f want 1.0（B 全部腿计入主力）", main)
	}
	if _, _, err := st.TradeRepo.OpenExposureUnits(0); err == nil {
		t.Fatal("unitValue=0 should return error")
	}
}

func near(a, b float64) bool {
	d := a - b
	if d < 0 {
		d = -d
	}
	return d < 1e-9
}
