package store

import "testing"

// TestActiveStockCodesScope 股票池范围（docs/00 §7）：仅沪深主板+创业板+科创板，
// 排除指数（sh000xxx/sz399xxx）、深B股（sz200xxx，board=b）、沪B股与其他（kind=other）。
func TestActiveStockCodesScope(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st,
		"sh600519", // 沪主板
		"sz000001", // 深主板
		"sz300750", // 创业板
		"sh688981", // 科创板
		"sz200002", // 深B股（board=b，须排除）
		"sh900901", // 沪B股（kind=other，须排除）
		"sh000001", // 上证指数（kind=index，须排除）
		"sz399001", // 深证指数（kind=index，须排除）
	)
	codes, err := st.SecRepo.ActiveStockCodes()
	if err != nil {
		t.Fatalf("ActiveStockCodes: %v", err)
	}
	want := []string{"sh600519", "sh688981", "sz000001", "sz300750"} // code 升序
	if len(codes) != len(want) {
		t.Fatalf("got %v, want %v", codes, want)
	}
	for i, c := range codes {
		if c != want[i] {
			t.Fatalf("codes[%d]=%s, want %s (got %v)", i, c, want[i], codes)
		}
	}
}

// TestActiveStockCodesExcludesST A2（2026-10-22，docs/21 F3 裁决 + docs/18 R2）：
// 按名称排除 ST/*ST——ST 涨跌停 ±5%、退市风险，交易规则与正常股不同，达瓦斯池不含 ST。
// 名称未知（空）的证券无从判定，保守保留（回测源库 368 支无名股的诚实兜底）。
func TestActiveStockCodesExcludesST(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519", "sh600053", "sz000001", "sz300750")
	if _, err := st.SecRepo.UpdateNames(map[string]string{
		"sh600519": "贵州茅台",
		"sh600053": "*ST九鼎",
		"sz000001": "ST人福",  // 无 * 前缀的 ST 族
	}); err != nil {
		t.Fatalf("UpdateNames: %v", err)
	}
	// sz300750 不回填名称（name=''）→ 保守保留
	codes, err := st.SecRepo.ActiveStockCodes()
	if err != nil {
		t.Fatalf("ActiveStockCodes: %v", err)
	}
	got := map[string]bool{}
	for _, c := range codes {
		got[c] = true
	}
	if got["sh600053"] {
		t.Fatal("*ST 股票应被排除（sh600053 *ST九鼎）")
	}
	if got["sz000001"] {
		t.Fatal("ST 股票应被排除（sz000001 ST人福）")
	}
	if !got["sh600519"] {
		t.Fatal("正常命名的股票不应被误伤（sh600519 贵州茅台）")
	}
	if !got["sz300750"] {
		t.Fatal("名称未知（空）的股票应保守保留（无从判定是否 ST）")
	}
}
