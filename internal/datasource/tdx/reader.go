// Package tdx 读取通达信 vipdoc 二进制日线文件（.day）。
// 格式（已验证，2026-10-16实盘数据）：每条记录32字节，小端：
//
//	uint32 date   YYYYMMDD
//	uint32 open   ×100
//	uint32 high   ×100
//	uint32 low    ×100
//	uint32 close  ×100
//	float32 amount 元
//	uint32 volume 手
//	uint32 count  保留
//
// 移植自 tradebuddy 项目（已在其生产环境验证），按 contract.DayFileReader 接口适配。
package tdx

import (
	"encoding/binary"
	"io"
	"math"
	"os"
	"path/filepath"
	"strings"
	"time"

	"darvas/internal/contract"
)

// Reader TDX数据读取器。rootPath 指向 vipdoc 目录，如 H:\new_tdx64\vipdoc。
type Reader struct {
	rootPath string
}

// NewReader 构造。
func NewReader(rootPath string) *Reader {
	return &Reader{rootPath: rootPath}
}

const recordSize = 32

// ListCodes 列出 sh/sz 全部 .day 代码（含指数）。
// 依据 docs/00-darvas-theory.md §7：北交所（bj）与新三板排除，不读取。
func (r *Reader) ListCodes() ([]string, error) {
	var codes []string
	for _, market := range []string{"sh", "sz"} {
		dir := filepath.Join(r.rootPath, market, "lday")
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue // 目录缺失时跳过（可移植性：目标机可能只有单市场数据）
		}
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			name := e.Name()
			if strings.HasPrefix(name, market) && strings.HasSuffix(name, ".day") {
				codes = append(codes, strings.TrimSuffix(name, ".day"))
			}
		}
	}
	return codes, nil
}

// ReadBars 读取单个 .day 文件，since（YYYY-MM-DD，可空）之后（含当日）的记录，升序。
func (r *Reader) ReadBars(code string, since string) ([]contract.DailyBar, error) {
	path := r.filePath(code)
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	stat, err := f.Stat()
	if err != nil {
		return nil, err
	}
	count := stat.Size() / recordSize

	sinceYYYYMMDD := 0
	if since != "" {
		sinceYYYYMMDD = dateToInt(since)
	}

	bars := make([]contract.DailyBar, 0, count)
	buf := make([]byte, recordSize)
	for i := int64(0); i < count; i++ {
		if _, err := io.ReadFull(f, buf); err != nil {
			break // 尾部残缺记录跳过（质量日报统计）
		}
		var rec dayRecord
		rec.date = binary.LittleEndian.Uint32(buf[0:4])
		rec.open = binary.LittleEndian.Uint32(buf[4:8])
		rec.high = binary.LittleEndian.Uint32(buf[8:12])
		rec.low = binary.LittleEndian.Uint32(buf[12:16])
		rec.close = binary.LittleEndian.Uint32(buf[16:20])
		rec.amount = math.Float32frombits(binary.LittleEndian.Uint32(buf[20:24])) // float32位模式
		rec.volume = binary.LittleEndian.Uint32(buf[24:28])
		if int(rec.date) < sinceYYYYMMDD {
			continue
		}
		bars = append(bars, rec.toBar(code))
	}
	return bars, nil
}

// ModTime 文件最后修改时间（增量导入依据，docs/04-api-contract.md §3.1）。
func (r *Reader) ModTime(code string) (time.Time, error) {
	path := r.filePath(code)
	stat, err := os.Stat(path)
	if err != nil {
		return time.Time{}, err
	}
	return stat.ModTime(), nil
}

func (r *Reader) filePath(code string) string {
	code = strings.ToLower(code)
	market := "sz"
	if strings.HasPrefix(code, "sh") {
		market = "sh"
	}
	// rootPath/{sh|sz}/lday/{code}.day
	return filepath.Join(r.rootPath, market, "lday", code+".day")
}

type dayRecord struct {
	date, open, high, low, close uint32
	amount                       float32
	volume                       uint32
}

func (rec dayRecord) toBar(code string) contract.DailyBar {
	return contract.DailyBar{
		Code:   code,
		Date:   intToDate(int(rec.date)),
		Open:   float64(rec.open) / 100.0,
		High:   float64(rec.high) / 100.0,
		Low:    float64(rec.low) / 100.0,
		Close:  float64(rec.close) / 100.0,
		Volume: int64(rec.volume),
		Amount: float64(rec.amount), // 元
	}
}

// intToDate 20241008 -> 2024-10-08
func intToDate(n int) string {
	if n < 19000101 || n > 29991231 {
		return ""
	}
	s := []byte{
		byte('0' + n/10000000%10), byte('0' + n/1000000%10), byte('0' + n/100000%10), byte('0' + n/10000%10),
		'-',
		byte('0' + n/1000%10), byte('0' + n/100%10),
		'-',
		byte('0' + n/10%10), byte('0' + n%10),
	}
	return string(s)
}

// dateToInt 2024-10-08 -> 20241008
func dateToInt(s string) int {
	if len(s) != 10 {
		return 0
	}
	n := 0
	for _, c := range s {
		if c == '-' {
			continue
		}
		if c < '0' || c > '9' {
			return 0
		}
		n = n*10 + int(c-'0')
	}
	return n
}
