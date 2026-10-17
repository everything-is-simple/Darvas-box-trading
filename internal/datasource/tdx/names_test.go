package tdx

import (
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/text/encoding/simplifiedchinese"
)

// writeTnf 生成合成 .tnf：50 字节头 + n 条 360 字节记录。
// 记录布局与实测格式一致：code@+0（6字节ASCII），name@+31（GBK，NUL结尾）。
func writeTnf(t *testing.T, path string, entries [][2]string) {
	t.Helper()
	data := make([]byte, tnfHeaderSize)
	for _, e := range entries {
		rec := make([]byte, tnfRecordSize)
		copy(rec[0:6], e[0])
		name, err := simplifiedchinese.GBK.NewEncoder().Bytes([]byte(e[1]))
		if err != nil {
			t.Fatal(err)
		}
		copy(rec[tnfNameOffset:], name)
		data = append(data, rec...)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestReadNames(t *testing.T) {
	dir := t.TempDir()
	vipdoc := filepath.Join(dir, "vipdoc")
	cache := filepath.Join(dir, "T0002", "hq_cache")

	writeTnf(t, filepath.Join(cache, "shs.tnf"), [][2]string{
		{"999999", ""},          // 占位记录（无名称，应跳过）
		{"600519", "贵州茅台"},   // 沪市主板
		{"688111", "金山办公"},   // 科创板
		{"589450", "转债示例"},   // 非股票代码照读（由 security 表过滤）
	})
	writeTnf(t, filepath.Join(cache, "szs.tnf"), [][2]string{
		{"000001", "平安银行"},
		{"300750", "宁德时代"},
	})

	r := NewReader(vipdoc)
	names, err := r.ReadNames()
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"sh600519": "贵州茅台",
		"sh688111": "金山办公",
		"sh589450": "转债示例",
		"sz000001": "平安银行",
		"sz300750": "宁德时代",
	}
	if len(names) != len(want) {
		t.Fatalf("got %d names, want %d: %v", len(names), len(want), names)
	}
	for k, v := range want {
		if names[k] != v {
			t.Errorf("names[%s] = %q, want %q", k, names[k], v)
		}
	}
}

func TestReadNamesMissingFiles(t *testing.T) {
	// 目录存在但无 tnf → 报错（名称同步需可感知缺失，便于排查）
	r := NewReader(filepath.Join(t.TempDir(), "vipdoc"))
	if _, err := r.ReadNames(); err == nil {
		t.Fatal("want error when no tnf files exist")
	}
}

func TestReadNamesSingleMarket(t *testing.T) {
	// 仅沪市 tnf 存在：深市跳过，沪市正常返回（可移植性：单市场数据机器）
	dir := t.TempDir()
	writeTnf(t, filepath.Join(dir, "T0002", "hq_cache", "shs.tnf"), [][2]string{
		{"600519", "贵州茅台"},
	})
	r := NewReader(filepath.Join(dir, "vipdoc"))
	names, err := r.ReadNames()
	if err != nil {
		t.Fatal(err)
	}
	if names["sh600519"] != "贵州茅台" {
		t.Fatalf("got %q", names["sh600519"])
	}
	if _, ok := names["sz000001"]; ok {
		t.Error("unexpected sz entry")
	}
}

func TestParseTnfCode(t *testing.T) {
	rec := make([]byte, tnfRecordSize)
	if got := parseTnfCode(rec); got != "" {
		t.Errorf("zero record should be empty, got %q", got)
	}
	copy(rec[0:6], "600519")
	if got := parseTnfCode(rec); got != "600519" {
		t.Errorf("got %q", got)
	}
	copy(rec[0:6], "60051A") // 非数字
	if got := parseTnfCode(rec); got != "" {
		t.Errorf("non-digit code should be empty, got %q", got)
	}
}
