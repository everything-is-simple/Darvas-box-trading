package store

import (
	"testing"

	"darvas/internal/contract"
)

func TestSignalsRange(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600000", "sh600001", "sh600002")
	sigs := []contract.Signal{
		{Date: "2026-10-12", Code: "sh600000", Type: contract.SignalStop, ClosePx: 9.5, BoxLevel: 10.0},
		{Date: "2026-10-15", Code: "sh600001", Type: contract.SignalBreakout, ClosePx: 11.0, BoxLevel: 10.5},
		{Date: "2026-10-18", Code: "sh600002", Type: contract.SignalStop, ClosePx: 8.0, BoxLevel: 8.5}, // 界外
	}
	if err := st.SignalRepo.AppendSignals(sigs); err != nil {
		t.Fatal(err)
	}

	got, err := st.SignalRepo.SignalsRange("2026-10-11", "2026-10-17")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("SignalsRange 期望 2 条，得到 %d", len(got))
	}
	if got[0].Date != "2026-10-12" || got[0].Code != "sh600000" {
		t.Fatalf("首条错误: %+v", got[0])
	}
	if got[1].Date != "2026-10-15" || got[1].Code != "sh600001" {
		t.Fatalf("次条错误: %+v", got[1])
	}

	// 空区间
	got, err = st.SignalRepo.SignalsRange("2026-11-01", "2026-11-30")
	if err != nil || len(got) != 0 {
		t.Fatalf("空区间期望 0 条，err=%v n=%d", err, len(got))
	}
}
