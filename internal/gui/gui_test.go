package gui

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// seedGUI 构造一个最小但覆盖四视图的测试库：
// 心跳×7、计划+订单、watch+箱体+K线、权益快照。
func seedGUI(t *testing.T) *store.Store {
	t.Helper()
	dsn := t.TempDir() + "/gui.db"
	st, err := store.Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })

	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}

	// 证券 + 名称 + K线
	must(st.SecRepo.UpsertSecurities([]contract.Security{store.DeriveSecurity("sh600519", "")}))
	if _, err := st.BarRepo.UpsertBars([]contract.DailyBar{
		{Code: "sh600519", Date: "2026-10-15", Open: 1400, High: 1450, Low: 1390, Close: 1450, Volume: 10000, Amount: 1.4e9},
		{Code: "sh600519", Date: "2026-10-16", Open: 1450, High: 1510, Low: 1440, Close: 1500, Volume: 12000, Amount: 1.8e9},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.SecRepo.UpdateNames(map[string]string{"sh600519": "贵州茅台"}); err != nil {
		t.Fatal(err)
	}

	// 心跳（7任务全ok）
	for _, task := range []string{"ingest", "boxscan", "screen", "poolsync", "signal", "plan", "report"} {
		id, err := st.RunRepo.StartRun("2026-10-16", task)
		must(err)
		must(st.RunRepo.FinishRun(id, contract.TaskResult{Task: task, Date: "2026-10-16", Status: "ok", AffectedRows: 1}))
	}

	// 箱体 + watch
	ids, err := st.BoxRepo.UpsertBoxes([]contract.Box{{
		Code: "sh600519", Top: 1500, Bottom: 1450,
		TopConfirmed: "2026-09-01", BottomConfirmed: "2026-09-10",
		Status: contract.BoxConfirmed,
	}})
	must(err)
	must(st.PoolRepo.UpsertWatch(contract.Watch{
		Code: "sh600519", AddedDate: "2026-10-10", Stage: contract.StageWatching,
		CurrentBoxID: ids[0], BoxCount: 1,
	}))

	// 计划 + 订单
	pid, err := st.TradeRepo.CreatePlan(contract.TradePlan{
		PlanDate: "2026-10-16", ExecuteDate: "2026-10-17", EquityBase: 1000000,
	})
	must(err)
	must(st.TradeRepo.AddOrders(pid, []contract.PlanOrder{{
		Code: "sh600519", Side: "buy", OrderKind: "stop_buy",
		TriggerPrice: 1545, LimitPrice: 1560, Qty: 100, LegNo: 1,
	}}))

	// 权益
	must(st.TradeRepo.UpsertEquity(contract.EquitySnapshot{
		Date: "2026-10-16", Cash: 1000000, Total: 1000000, PeakTotal: 1000000,
	}))
	return st
}

func TestAPIEndpoints(t *testing.T) {
	st := seedGUI(t)
	srv := httptest.NewServer(New(st).Handler())
	defer srv.Close()

	get := func(path string) (int, map[string]any) {
		t.Helper()
		resp, err := http.Get(srv.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var m map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
			t.Fatalf("%s: decode: %v", path, err)
		}
		return resp.StatusCode, m
	}

	// 首页
	resp, err := http.Get(srv.URL + "/")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("index status = %d", resp.StatusCode)
	}

	// 仪表盘：默认取最近心跳日，7任务+计划+订单+权益+名称
	code, m := get("/api/dashboard")
	if code != 200 {
		t.Fatalf("dashboard %d: %v", code, m)
	}
	if m["date"] != "2026-10-16" {
		t.Fatalf("dashboard date = %v", m["date"])
	}
	if runs, ok := m["runs"].([]any); !ok || len(runs) != 7 {
		t.Fatalf("runs = %v", m["runs"])
	}
	if orders, ok := m["orders"].([]any); !ok || len(orders) != 1 {
		t.Fatalf("orders = %v", m["orders"])
	}
	names, _ := m["names"].(map[string]any)
	if names["sh600519"] != "贵州茅台" {
		t.Fatalf("names = %v", names)
	}

	// 跟踪池
	code, m = get("/api/watch")
	if code != 200 {
		t.Fatalf("watch %d", code)
	}
	items, _ := m["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("watch items = %v", items)
	}
	item := items[0].(map[string]any)
	if item["name"] != "贵州茅台" || item["last_close"] != 1500.0 {
		t.Fatalf("watch item = %v", item)
	}

	// 个股
	code, m = get("/api/stock/sh600519?days=100")
	if code != 200 {
		t.Fatalf("stock %d: %v", code, m)
	}
	if m["name"] != "贵州茅台" {
		t.Fatalf("stock name = %v", m["name"])
	}
	if bars, ok := m["bars"].([]any); !ok || len(bars) != 2 {
		t.Fatalf("bars = %v", m["bars"])
	}
	if boxes, ok := m["boxes"].([]any); !ok || len(boxes) != 1 {
		t.Fatalf("boxes = %v", m["boxes"])
	}
	if m["watch"] == nil {
		t.Fatal("watch context missing")
	}

	// 个股不存在
	code, m = get("/api/stock/sz999999")
	if code != 404 {
		t.Fatalf("missing stock code = %d", code)
	}
	if !strings.Contains(m["error"].(string), "999999") {
		t.Fatalf("error = %v", m["error"])
	}

	// 交易记录
	code, m = get("/api/trades")
	if code != 200 {
		t.Fatalf("trades %d", code)
	}
	if plans, ok := m["plans"].([]any); !ok || len(plans) != 1 {
		t.Fatalf("plans = %v", m["plans"])
	}
	if eq, ok := m["equity"].([]any); !ok || len(eq) != 1 {
		t.Fatalf("equity = %v", m["equity"])
	}
}
