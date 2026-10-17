// names.go —— 通达信代码↔名称表读取（.tnf 二进制格式）。
//
// 文件位置：{TDX根}/T0002/hq_cache/{shs,szs}.tnf（沪市/深市各一份）。
// 格式（2026-10-17 实测逆向，H:\new_tdx64 实盘数据验证）：
//
//	文件头 50 字节（跳过）；
//	之后每条记录 360 字节：
//	  +0   代码 6 字节 ASCII（"600519"），非 6 位数字的记录跳过；
//	  +31  名称 GBK 编码，NUL 结尾（最长按 32 字节截断）。
//
// 前几条为占位记录（代码 999999 递减），按"6位数字"过滤自然排除。
package tdx

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/text/encoding/simplifiedchinese"
)

// tnfRecordSize .tnf 单条记录字节数。
const tnfRecordSize = 360

// tnfHeaderSize .tnf 文件头字节数。
const tnfHeaderSize = 50

// tnfNameOffset 记录内名称字段偏移。
const tnfNameOffset = 31

// tnfNameMaxLen 名称字段最大读取长度（防越界，实际名称≤8个汉字）。
const tnfNameMaxLen = 32

// ReadNames 读取沪深代码→名称映射（GUI 展示用）。
// 返回 map 的键为带市场前缀的完整代码（sh600519 / sz000001）。
// 单个文件缺失或损坏不返回错误（名称为可选增强数据，可移植性优先），
// 全部缺失才报错。rootPath 指向 vipdoc 目录，tnf 在其上级的 T0002/hq_cache。
func (r *Reader) ReadNames() (map[string]string, error) {
	base := filepath.Dir(strings.TrimRight(r.rootPath, `\/`))
	cacheDir := filepath.Join(base, "T0002", "hq_cache")

	names := map[string]string{}
	found := 0
	for _, mf := range []struct{ market, file string }{
		{"sh", "shs.tnf"},
		{"sz", "szs.tnf"},
	} {
		path := filepath.Join(cacheDir, mf.file)
		n, err := parseTnf(path, mf.market)
		if err != nil {
			continue // 缺文件/坏文件：跳过该市场（名称可选，不阻断行情导入）
		}
		found++
		for k, v := range n {
			names[k] = v
		}
	}
	if found == 0 {
		return nil, fmt.Errorf("tnf not found under %s", cacheDir)
	}
	return names, nil
}

// parseTnf 解析单个 .tnf 文件，返回 {market}{code} → 名称。
func parseTnf(path, market string) (map[string]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(data) <= tnfHeaderSize {
		return nil, fmt.Errorf("tnf too short: %s", path)
	}
	names := map[string]string{}
	for off := tnfHeaderSize; off+tnfRecordSize <= len(data); off += tnfRecordSize {
		rec := data[off : off+tnfRecordSize]
		code := parseTnfCode(rec)
		if code == "" {
			continue
		}
		name := parseTnfName(rec)
		if name == "" {
			continue
		}
		names[market+code] = name
	}
	return names, nil
}

// parseTnfCode 取记录 +0 起 6 字节 ASCII 代码；非 6 位数字返回空。
func parseTnfCode(rec []byte) string {
	for i := 0; i < 6; i++ {
		if rec[i] < '0' || rec[i] > '9' {
			return ""
		}
	}
	return string(rec[:6])
}

// parseTnfName 取记录 +31 起 GBK 名称（NUL 结尾），转 UTF-8。
func parseTnfName(rec []byte) string {
	end := tnfNameOffset
	limit := tnfNameOffset + tnfNameMaxLen
	if limit > len(rec) {
		limit = len(rec)
	}
	for end < limit && rec[end] != 0 {
		end++
	}
	raw := rec[tnfNameOffset:end]
	if len(raw) == 0 {
		return ""
	}
	name, err := simplifiedchinese.GBK.NewDecoder().Bytes(raw)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(name))
}
