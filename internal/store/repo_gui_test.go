package store

import (
	"testing"

	"darvas/internal/contract"
)

func TestSecRepoNames(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519", "sz000001")

	n, err := st.SecRepo.UpdateNames(map[string]string{
		"sh600519": "贵州茅台",
		"sz000001": "平安银行",
		"sh999999": "不在库中的代码", // 应跳过
	})
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("updated = %d, want 2", n)
	}

	names, err := st.SecRepo.Names()
	if err != nil {
		t.Fatal(err)
	}
	if names["sh600519"] != "贵州茅台" || names["sz000001"] != "平安银行" {
		t.Fatalf("names = %v", names)
	}
	if _, ok := names["sh999999"]; ok {
		t.Error("unknown code should not appear")
	}

	// 幂等：同名重写不计数
	n, err = st.SecRepo.UpdateNames(map[string]string{"sh600519": "贵州茅台"})
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("idempotent rewrite updated = %d, want 0", n)
	}
	// 改名要生效
	n, _ = st.SecRepo.UpdateNames(map[string]string{"sh600519": "贵州茅台改名"})
	if n != 1 {
		t.Fatalf("rename updated = %d, want 1", n)
	}
}

func TestPlansRecent(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519", "sz000001")

	// 三天计划：D1 有买单，D2 空，D3 有卖单
	pid1, err := st.TradeRepo.CreatePlan(contract.TradePlan{
		PlanDate: "2026-10-14", ExecuteDate: "2026-10-15", EquityBase: 1000000, OpenPositionCount: 0})
	if err != nil {
		t.Fatal(err)
	}
	must(t, st.TradeRepo.AddOrders(pid1, []contract.PlanOrder{{
		Code: "sh600519", Side: "buy", OrderKind: "stop_buy",
		TriggerPrice: 100, LimitPrice: 101, Qty: 100, LegNo: 1,
	}}))
	pid2, err := st.TradeRepo.CreatePlan(contract.TradePlan{
		PlanDate: "2026-10-15", ExecuteDate: "2026-10-16", EquityBase: 1000000, OpenPositionCount: 1})
	if err != nil {
		t.Fatal(err)
	}
	_ = pid2
	pid3, err := st.TradeRepo.CreatePlan(contract.TradePlan{
		PlanDate: "2026-10-16", ExecuteDate: "2026-10-17", EquityBase: 990000, OpenPositionCount: 1})
	if err != nil {
		t.Fatal(err)
	}
	must(t, st.TradeRepo.AddOrders(pid3, []contract.PlanOrder{{
		Code: "sz000001", Side: "sell", OrderKind: "stop_sell",
		TriggerPrice: 50, LimitPrice: 49.5, Qty: 200, LegNo: 1,
	}}))

	plans, err := st.TradeRepo.PlansRecent(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(plans) != 3 {
		t.Fatalf("plans = %d, want 3", len(plans))
	}
	// 倒序：最新在前
	if plans[0].Plan.PlanDate != "2026-10-16" || plans[2].Plan.PlanDate != "2026-10-14" {
		t.Fatalf("order wrong: %v", plans)
	}
	// D3 卖单 + 状态字段
	if len(plans[0].Orders) != 1 || plans[0].Orders[0].Side != "sell" {
		t.Fatalf("D3 orders = %+v", plans[0].Orders)
	}
	if plans[0].Orders[0].Status != "pending" || plans[0].Orders[0].ID == 0 {
		t.Fatalf("order id/status missing: %+v", plans[0].Orders[0])
	}
	// D2 空计划也返回
	if len(plans[1].Orders) != 0 {
		t.Fatalf("D2 orders = %+v, want empty", plans[1].Orders)
	}
	// D1 买单
	if len(plans[2].Orders) != 1 || plans[2].Orders[0].Code != "sh600519" {
		t.Fatalf("D1 orders = %+v", plans[2].Orders)
	}

	// 限量 n=2 只取最近两天
	plans2, err := st.TradeRepo.PlansRecent(2)
	if err != nil {
		t.Fatal(err)
	}
	if len(plans2) != 2 || plans2[1].Plan.PlanDate != "2026-10-15" {
		t.Fatalf("PlansRecent(2) = %+v", plans2)
	}
}

func TestWatchOverview(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519", "sz000001")

	// 行情：两根K线，最新收盘 1500
	_, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		mkBar("sh600519", "2026-10-15", 1400, 1450, 1390, 1450, 10000, 1.4e9),
		mkBar("sh600519", "2026-10-16", 1450, 1510, 1440, 1500, 12000, 1.8e9),
		mkBar("sz000001", "2026-10-16", 10, 11, 9.9, 10.5, 500000, 5.2e8),
	})
	if err != nil {
		t.Fatal(err)
	}

	// 箱体：茅台 confirmed 箱 1450-1500
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{{
		Code: "sh600519", Top: 1500, Bottom: 1450,
		TopConfirmed: "2026-09-01", BottomConfirmed: "2026-09-10", Status: contract.BoxConfirmed,
	}})
	if err != nil {
		t.Fatal(err)
	}

	// 平安银行 exited 后重新入池（两行 watch，应取最新行 watching）
	must(t, st.PoolRepo.UpsertWatch(contract.Watch{
		Code: "sz000001", AddedDate: "2026-09-01", Stage: contract.StageExited, ExitReason: "broken_down",
	}))
	must(t, st.PoolRepo.UpsertWatch(contract.Watch{
		Code: "sz000001", AddedDate: "2026-10-01", Stage: contract.StageWatching, BoxCount: 1,
	}))
	must(t, st.PoolRepo.UpsertWatch(contract.Watch{
		Code: "sh600519", AddedDate: "2026-10-10", Stage: contract.StagePending,
		CurrentBoxID: ids[0], BoxCount: 2,
	}))

	// 名称
	_, err = st.SecRepo.UpdateNames(map[string]string{"sh600519": "贵州茅台", "sz000001": "平安银行"})
	if err != nil {
		t.Fatal(err)
	}

	rows, err := st.PoolRepo.WatchOverview()
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 {
		t.Fatalf("rows = %d, want 2: %+v", len(rows), rows)
	}
	// holding > pending > watching 排序：茅台(pending) 在前
	mt, pa := rows[0], rows[1]
	if mt.Code != "sh600519" || mt.Stage != "pending" {
		t.Fatalf("row0 = %+v", rows[0])
	}
	if mt.Name != "贵州茅台" || mt.BoxTop != 1500 || mt.BoxBottom != 1450 {
		t.Fatalf("maotai row = %+v", mt)
	}
	if mt.BoxStatus != "confirmed" || mt.LastClose != 1500 || mt.BoxCount != 2 {
		t.Fatalf("maotai row = %+v", mt)
	}
	if pa.Code != "sz000001" || pa.Stage != "watching" {
		t.Fatalf("row1 = %+v（应取最新 watch 行 watching 而非 exited）", rows[1])
	}
	if pa.BoxTop != 0 || pa.LastClose != 10.5 {
		t.Fatalf("pingan row = %+v", pa)
	}
}

func TestLastClose(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	_, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		mkBar("sh600519", "2026-10-15", 1400, 1450, 1390, 1450, 10000, 1.4e9),
		mkBar("sh600519", "2026-10-16", 1450, 1510, 1440, 1500, 12000, 1.8e9),
	})
	if err != nil {
		t.Fatal(err)
	}
	c, ok, err := st.BarRepo.LastClose("sh600519")
	if err != nil || !ok || c != 1500 {
		t.Fatalf("LastClose = %v %v %v", c, ok, err)
	}
	c, ok, err = st.BarRepo.LastClose("sz999999")
	if err != nil || ok || c != 0 {
		t.Fatalf("missing LastClose = %v %v %v, want (0,false,nil)", c, ok, err)
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
