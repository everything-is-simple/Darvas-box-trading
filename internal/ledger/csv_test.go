package ledger

import (
	"strings"
	"testing"

	"golang.org/x/text/encoding/simplifiedchinese"

	"darvas/internal/contract"
)

// gbkBytes 把 UTF-8 文本编码为 GBK 字节（模拟炒股手导出文件，fixture 自包含）。
func gbkBytes(t *testing.T, s string) []byte {
	t.Helper()
	b, err := simplifiedchinese.GBK.NewEncoder().Bytes([]byte(s))
	if err != nil {
		t.Fatalf("GBK 编码失败: %v", err)
	}
	return b
}

// 样本行（2026-10-18 取自 H:\2025炒股手训练软件\EXPORTT 四个年代样本，格式一致）。
const csvSample = `"序号","日期","操作","股票代码","股票名称","成交价","成本价","盈亏情况","盈亏比率","成交数量","成交金额","手续费","交易理由"
1,20150313,"买","300096"_,"易联众",19.76,19.82,0.00,0.00%,800.00,15808.00,47.42,""
2,20150313,"买","600519"_,"贵州茅台",1030.50,1030.50,0.00,0.00%,900.00,927450.00,278.24,""
3,20150316,"卖","600519"_,"贵州茅台",1062.00,1030.50,28000.00,3.02%,900.00,955800.00,286.74,"自动止损"`

// TestParseChaoGushouCsv C：炒股手 GBK CSV 解析（日期/方向/代码推断/数量浮点转整/费用）。
func TestParseChaoGushouCsv(t *testing.T) {
	l, _ := newLedger(t, "sz300096", "sh600519")
	fills, err := l.ParseChaoGushouCsv(gbkBytes(t, csvSample))
	if err != nil {
		t.Fatal(err)
	}
	if len(fills) != 3 {
		t.Fatalf("解析笔数=%d want 3", len(fills))
	}
	// 第1笔：买 sz300096
	f := fills[0]
	if f.Date != "2015-03-13" || f.Side != "buy" || f.Code != "sz300096" {
		t.Fatalf("第1笔基础字段: %+v", f)
	}
	if f.Price != 19.76 || f.Qty != 800 || f.Fee != 47.42 {
		t.Fatalf("第1笔价量费: price=%v qty=%d fee=%v", f.Price, f.Qty, f.Fee)
	}
	if !strings.Contains(f.RawText, "易联众") {
		t.Fatalf("原文审计留痕缺失: %q", f.RawText)
	}
	// 第3笔：卖 sh600519（首位6推断sh）、数量900.00→900
	f3 := fills[2]
	if f3.Code != "sh600519" || f3.Side != "sell" || f3.Qty != 900 {
		t.Fatalf("第3笔: %+v", f3)
	}
	if !strings.Contains(f3.RawText, "自动止损") {
		t.Fatalf("交易理由未留原文: %q", f3.RawText)
	}
}

// TestParseChaoGushouCsvRejects 非炒股手格式直接拒绝（宁可拒绝不可错账）。
func TestParseChaoGushouCsvRejects(t *testing.T) {
	l, _ := newLedger(t)
	cases := map[string][]byte{
		"UTF-8粘贴文本": []byte("2026-10-16  sh600519 买入 1030.50 900股"),
		"缺表头":       gbkBytes(t, "1,20150313,\"买\",\"300096\"_,\"易联众\",19.76,19.82,0.00,0.00%,800.00,15808.00,47.42,\"\"\n"),
		"坏行（日期非8位）": gbkBytes(t, `"序号","日期","操作","股票代码","股票名称","成交价","成本价","盈亏情况","盈亏比率","成交数量","成交金额","手续费","交易理由"
1,2015-3-13,"买","300096"_,"易联众",19.76,19.82,0.00,0.00%,800.00,15808.00,47.42,""`),
	}
	for name, raw := range cases {
		if _, err := l.ParseChaoGushouCsv(raw); err == nil {
			t.Fatalf("%s 应被拒绝", name)
		}
	}
}

// TestFillsAuditReport C：执行质量审核（漏执行/滑点/计划外）。
func TestFillsAuditReport(t *testing.T) {
	l, st := newLedger(t, "sh600519", "sz000001", "sz300096")
	// 计划：sh600519 买 1030×900（会被执行，滑点0.5）；sz000001 买 10.30×9000（漏执行）
	planID, err := st.TradeRepo.CreatePlan(contract.TradePlan{
		PlanDate: "2026-10-16", ExecuteDate: "2026-10-19", EquityBase: 1000000,
	})
	if err != nil {
		t.Fatal(err)
	}
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
	}
	must(st.TradeRepo.AddOrders(planID, []contract.PlanOrder{
		{Code: "sh600519", Side: "buy", OrderKind: "stop_buy", TriggerPrice: 1030, LimitPrice: 1030, Qty: 900, LegNo: 1},
		{Code: "sz000001", Side: "buy", OrderKind: "stop_buy", TriggerPrice: 10.30, LimitPrice: 10.30, Qty: 9000, LegNo: 1},
	}))

	// 成交：sh600519 买 1030.50（匹配订单，滑点）；sz300096 买（计划外）
	fills := []contract.Fill{
		{Code: "sh600519", Date: "2026-10-19", Side: "buy", Price: 1030.50, Qty: 900, Fee: 278},
		{Code: "sz300096", Date: "2026-10-19", Side: "buy", Price: 19.76, Qty: 800, Fee: 47},
	}
	results, err := l.ApplyFills(fills)
	if err != nil {
		t.Fatal(err)
	}

	report := l.FillsAuditReport(fills, results)
	for _, want := range []string{
		"订单 2 笔：执行1 漏执行1",
		"成交 2 笔：匹配订单1 计划外1",
		"滑点 sh600519 买：成交1030.50 vs 限价1030.00（+0.50）",
		"漏执行 订单#", "sz000001",
		"计划外成交 1 笔",
	} {
		if !strings.Contains(report, want) {
			t.Errorf("报告缺 %q\n报告:\n%s", want, report)
		}
	}
}
