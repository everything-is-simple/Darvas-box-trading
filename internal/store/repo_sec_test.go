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
