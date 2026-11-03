package store

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"time"
)

// RecordBaseline 为当前库生成一份基线身份并落 baseline 表（docs/49 §四，阶段 B）。
//
// 三样身份一次算完：
//   1. param_fingerprint：param 表 (key, value) 按 key 排序序列的 sha256 —— 两个库是否同一基线，
//      比指纹即可。
//   2. baseline_id：按「重建日期 + 策略 + 序号」自动生成，如 2026-11-04-newhigh-ladder-v1；
//      序号在同一天内自增（先查当天已有几条再 +1）。
//   3. engine_sha256：跑出这批账本的二进制（os.Executable()）的 sha256 —— HARD① 锚，
//      跨二进制不可横比，必须先有身份才能谈对账。
//
// engineVersion 与 cmd/darvas/main.go 的 const version 对应，由调用方传入（store 层不持有）。
// sourceHash 是源库 sha256（从哪来），可选；由调用方传入，空则留 NULL。
func (s *Store) RecordBaseline(engineVersion, sourceHash string) (baselineID string, err error) {
	// 1. 参数快照 + 指纹（key 排序，值原样拼，sha256）
	params, err := s.ParamRepo.All()
	if err != nil {
		return "", fmt.Errorf("读参数算指纹: %w", err)
	}
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := sha256.New()
	for _, k := range keys {
		h.Write([]byte(k))
		h.Write([]byte{0})
		h.Write([]byte(params[k]))
		h.Write([]byte{0})
	}
	fingerprint := hex.EncodeToString(h.Sum(nil))
	snapshot, err := json.Marshal(params)
	if err != nil {
		return "", fmt.Errorf("序列化参数快照: %w", err)
	}

	// 2. 引擎 hash：读自身可执行文件算 sha256
	exe, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("定位可执行文件: %w", err)
	}
	bin, err := os.ReadFile(exe)
	if err != nil {
		return "", fmt.Errorf("读可执行文件算引擎 hash: %w", err)
	}
	eh := sha256.Sum256(bin)
	engineHash := hex.EncodeToString(eh[:])

	// 3. baseline_id：YYYY-MM-DD-newhigh-ladder-v<n>，n 当天自增
	day := time.Now().Format("2006-01-02")
	base := day + "-newhigh-ladder"
	var n int
	if err := s.db.QueryRow(`SELECT COUNT(*) FROM baseline WHERE baseline_id LIKE ?`,
		base+"-%").Scan(&n); err != nil {
		return "", fmt.Errorf("查当天基线序号: %w", err)
	}
	baselineID = fmt.Sprintf("%s-v%d", base, n+1)

	// 4. 旧 latest 清零，新行 latest=1
	if _, err := s.db.Exec(`UPDATE baseline SET latest = 0 WHERE latest = 1`); err != nil {
		return "", fmt.Errorf("清旧 latest: %w", err)
	}
	_, err = s.db.Exec(`INSERT INTO baseline
		(baseline_id, engine_version, engine_sha256, source_hash, param_fingerprint, params_json, latest)
		VALUES (?, ?, ?, ?, ?, ?, 1)`,
		baselineID, engineVersion, engineHash, nullIfEmpty(sourceHash), fingerprint, string(snapshot))
	if err != nil {
		return "", fmt.Errorf("写 baseline: %w", err)
	}
	return baselineID, nil
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
