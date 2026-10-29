// cmd/a0check —— E17-A0/A1 Box 轨迹工程验证生成器（docs/experiments/e17_a0_determinism_framework.md）。
//
// 三路重建对照（零策略变更，只读源库，全程不写任何数据库）：
//   A 全量真值    ：个股 ≥histfrom 的全部 K 线逐日 Engine.Scan（数学参照）
//   B 800 窗口    ：最近 rescanWindow=800 根再 Scan（= scanner.rescanOne 生产计算路径）
//   C 增量预过滤  ：逐日只重算预过滤命中票（60日新高/量≥均量×volmult/confirmed下轨破），
//                   池=∅ 最严苛上界；未命中票保留昨日状态（= RescanIncremental 池外语义）
//
// 判定（框架 §3.2 定稿版，2026-10-29 勘误后）：
//   洁净区 = 共同后缀（commonSuffix）：从尾往前 seqA/seqB 语义键逐箱相同的段——
//   800 窗口重启后 B 家谱与 A 定义性分叉、之后经终态事件再同步，分叉区差异属窗口语义；
//   共同后缀内任何差异都是真不一致。锚比对另用 top_margin=10 判「洁净缺失」。
//   硬失败 = 锚真不一致 > 0 或 suffix_violation > 0 或 C 恒等式抽查失败。
//   （初版 topMargin/eventBar 判据被首跑证伪，勘误过程见框架 §3.2。）
//
// A1 事件视图（删失标注、终态占比、存续、身份复用）同一次运行产出（框架 §四）。
//
// 用法：
//
//	go run ./cmd/a0check -src var/bt/full_hist_10pct.db -from 2013-01-01 -to 2013-12-31 \
//	  -maxcodes 800 -out docs/experiments/e17_a0_determinism_data.json
package main

import (
	"database/sql"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"

	"darvas/internal/box"
	"darvas/internal/contract"

	_ "modernc.org/sqlite"
)

const (
	topMargin = 10 // 锚比对洁净判定：上轨确认距窗口起点至少 N 根
)

// semBox 语义箱体（忽略引擎局部 ID 与 DB 自增 ID——A/B 路起点不同局部 ID 必然不同）。
type semBox struct {
	Top, Bottom float64
	TopC, BotC  string
	Status      string
	Broken      string
}

func (s semBox) key() string {
	return fnum(s.Top) + "|" + fnum(s.Bottom) + "|" + s.TopC + "|" + s.BotC + "|" + s.Status + "|" + s.Broken
}

func sem(b contract.Box) semBox {
	return semBox{Top: b.Top, Bottom: b.Bottom, TopC: b.TopConfirmed, BotC: b.BottomConfirmed,
		Status: string(b.Status), Broken: b.BrokenDate}
}

func fnum(v float64) string { return strconv.FormatFloat(v, 'g', -1, 64) }

type diffEx struct {
	Code, Day, Kind, Detail string
}

type stats struct {
	CodeDays   int
	Equal      int
	WinSem     map[string]int // 窗口语义子桶
	TrueIncons int
	Examples   []diffEx
}

func (st *stats) add(bucket string) {
	if st.WinSem == nil {
		st.WinSem = map[string]int{}
	}
	st.WinSem[bucket]++
}

func main() {
	src := flag.String("src", "var/bt/full_hist_10pct.db", "源库（只读）")
	from := flag.String("from", "2013-01-01", "对照窗口起点（含）")
	to := flag.String("to", "2013-12-31", "对照窗口终点（含）")
	histFrom := flag.String("histfrom", "2005-01-01", "A 路全量历史起点")
	maxCodes := flag.Int("maxcodes", 800, "抽样代码数上限（等步长确定性抽样）")
	window := flag.Int("window", 800, "B/C 路窗口长度（=rescanWindow）")
	out := flag.String("out", "docs/experiments/e17_a0_determinism_data.json", "输出 JSON 路径")
	flag.Parse()

	db, err := openRO(*src)
	must(err, "打开源库")
	defer db.Close()

	// ── 参数（与 scanner.boxParams 同源：param 表种子，兜底 DefaultParams）──
	def := box.DefaultParams()
	volMult := getParamF(db, "box.volmult", def.VolMultiplier)
	prem := getParamF(db, "buy.premium", def.BreakPremium)
	minW := getParamF(db, "box.min_width", def.MinBoxWidth)
	volWin := int(getParamF(db, "box.volwindow", float64(def.VolWindow)))
	params := contract.BoxParams{VolMultiplier: volMult, VolWindow: volWin, BreakPremium: prem,
		MinBoxWidth: minW, ConfirmDays: def.ConfirmDays}
	fmt.Printf("[a0] 参数 volmult=%g volwindow=%d premium=%g min_width=%g confirm=%d\n",
		volMult, volWin, prem, minW, params.ConfirmDays)

	codes, step := sampleCodes(db, *from, *maxCodes)
	fmt.Printf("[a0] 样本 %d 只（步长 %d）\n", len(codes), step)

	days := calendar(db, *from, *to)
	if len(days) == 0 {
		fmt.Println("[a0] 窗口内无交易日")
		os.Exit(1)
	}
	dayIdx := map[string]int{}
	for i, d := range days {
		dayIdx[d] = i
	}
	fmt.Printf("[a0] 交易日 %d 天（%s ~ %s）\n", len(days), days[0], days[len(days)-1])

	type book struct {
		bars      []contract.DailyBar
		idx       int
		state     []semBox // C 路：上次触发日的 B 语义序列
		lastTrig  int      // 上次触发日序（-1=未触发）
		firstSeen []int    // A 路槽位 → 首现日序
		tailIdent map[string]int
	}
	books := map[string]*book{}
	for _, c := range codes {
		if bars := loadBars(db, c, *histFrom, *to); len(bars) > 0 {
			books[c] = &book{bars: bars, tailIdent: map[string]int{}, lastTrig: -1}
		}
	}
	fmt.Printf("[a0] 已加载 %d 只有效样本的 K 线\n", len(books))

	eng := box.New()
	codeList := make([]string, 0, len(books))
	for c := range books {
		codeList = append(codeList, c)
	}
	sort.Strings(codeList)

	// ── 统计容器 ──
	tailAB := &stats{}      // A vs B 尾箱（诊断桶，非硬闸门）
	anchorAB := &stats{}    // A vs B 最新 confirmed 锚（硬闸门：洁净缺失=真不一致）
	var (
		triggers    int
		codeDays    int
		tailDiffBC  int // B vs C 尾箱差异天数（滞后）
		lagDays     []int
		vacuumDays  int // A 尾箱 (0,0) 天数（身份真空）
	)
	const maxEx = 60
	addEx := func(st *stats, code, day, kind, detail string) {
		if len(st.Examples) < maxEx {
			st.Examples = append(st.Examples, diffEx{code, day, kind, detail})
		}
	}

	// ── 主循环：逐日三路 ──
	for di, d := range days {
		for _, c := range codeList {
			bk := books[c]
			bars := bk.bars
			for bk.idx < len(bars) && bars[bk.idx].Date <= d {
				bk.idx++
			}
			if bk.idx == 0 {
				continue
			}
			codeDays++
			ws := 0
			if bk.idx > *window {
				ws = bk.idx - *window
			}

			// A 路：全量真值
			seqA := eng.Scan(bars[:bk.idx], params)
			if len(seqA) == 0 {
				continue
			}
			tailA := sem(seqA[len(seqA)-1])
			if tailA.Top == 0 && tailA.Bottom == 0 {
				vacuumDays++
			}
			for k := len(bk.firstSeen); k < len(seqA); k++ {
				bk.firstSeen = append(bk.firstSeen, di)
			}
			if tk := fnum(tailA.Top) + "|" + fnum(tailA.Bottom); tailA.Top > 0 {
				if _, ok := bk.tailIdent[tk]; !ok {
					bk.tailIdent[tk] = di
				}
			}

			// B 路：800 窗口
			seqB := eng.Scan(bars[ws:bk.idx], params)
			tailB := sem(seqB[len(seqB)-1])

			// A vs B 尾箱（诊断桶：家谱分叉属窗口语义，非硬闸门）
			tailAB.CodeDays++
			if tailA.key() == tailB.key() {
				tailAB.Equal++
			} else {
				tailAB.add(tailBucket(tailA, tailB))
				addEx(tailAB, c, d, "tail", fmt.Sprintf("A(%.4g/%.4g %s~%s) B(%.4g/%.4g %s~%s)",
					tailA.Top, tailA.Bottom, tailA.TopC, tailA.BotC, tailB.Top, tailB.Bottom, tailB.TopC, tailB.BotC))
			}

			// A vs B 最新 confirmed 锚（止损研究基准；洁净缺失=硬失败）
			compareAnchors(anchorAB, c, d, seqA, seqB, bars, ws, addEx)

			// C 路：增量预过滤（池=∅ 上界）
			trig := len(bk.state) == 0
			if !trig {
				trig = prefilter(bars, bk.idx, bk.state, volMult)
			}
			if trig {
				bk.state = bk.state[:0]
				for _, b := range eng.Scan(bars[ws:bk.idx], params) {
					bk.state = append(bk.state, sem(b))
				}
				triggers++
				bk.lastTrig = di
			}
			if len(bk.state) > 0 {
				if tailC := bk.state[len(bk.state)-1]; tailC.key() != tailB.key() {
					tailDiffBC++
					lagDays = append(lagDays, di-bk.lastTrig)
				}
			}
		}
		if (di+1)%40 == 0 || di == len(days)-1 {
			fmt.Printf("[a0] 进度 %d/%d 日：尾差AB %d（等 %d/窗口语义 %v/真不一致 %d）锚真不一致 %d B/C滞后差 %d 触发率 %.2f%%\n",
				di+1, len(days), tailAB.CodeDays-tailAB.Equal, tailAB.Equal, tailAB.WinSem, tailAB.TrueIncons,
				anchorAB.TrueIncons, tailDiffBC, 100*float64(triggers)/math.Max(1, float64(codeDays)))
		}
	}

	// ── 终态事件集比对 + A1 实例账（窗口末日快照）──
	ev := map[string]int{} // matched / trunc_zone_A / trunc_zone_B / suffix_violation
	evStats := &stats{}
	suffixSum, suffixCnt, suffixMin := 0, 0, math.MaxInt
	suffixMinCode := ""
	spotCnt, spotFail := 0, 0
	termCnt := map[string]int{}
	survival := []int{}
	reuseKeys, reuseExtra := 0, 0
	phaseTop0, sampleBoundaryCensor, bWinLeftCensor, rightCensor := 0, 0, 0, 0
	obsCensored := 0
	preWindowTerminated := 0
	endPending := 0
	ledger := []map[string]any{}
	const ledgerCap = 120

	for _, c := range codeList {
		bk := books[c]
		bars := bk.bars
		if bk.idx == 0 {
			continue
		}
		ws := 0
		if bk.idx > *window {
			ws = bk.idx - *window
		}
		seqA := eng.Scan(bars[:bk.idx], params)
		seqB := eng.Scan(bars[ws:bk.idx], params)

		// 事件集比对（洁净区 = 共同后缀）：家谱分叉属窗口语义，非硬失败。
		// 共同后缀 = 从尾往前 seqA/seqB 语义键逐箱相同的段；其内箱体全等 ⟹ 事件必须全等（防御断言）。
		sufA, sufB := commonSuffix(seqA, seqB)
		evA, evB := map[string]bool{}, map[string]bool{}
		for i := len(seqA) - sufA; i < len(seqA); i++ {
			b := seqA[i]
			if b.BrokenDate != "" && (b.Status == contract.BoxBrokenUp || b.Status == contract.BoxBrokenDown) {
				evA[sem(b).key()] = true
			}
		}
		for i := len(seqB) - sufB; i < len(seqB); i++ {
			b := seqB[i]
			if b.BrokenDate != "" && (b.Status == contract.BoxBrokenUp || b.Status == contract.BoxBrokenDown) {
				evB[sem(b).key()] = true
			}
		}
		ev["suffix_violation"] += setDiffCount(evA, evB) + setDiffCount(evB, evA)
		for i := range seqA {
			b := seqA[i]
			if b.BrokenDate == "" || (b.Status != contract.BoxBrokenUp && b.Status != contract.BoxBrokenDown) {
				continue
			}
			if i >= len(seqA)-sufA {
				ev["matched"]++ // 共同后缀内 A 事件（已验证与 B 全等）
			} else {
				ev["trunc_zone_A"]++ // 分叉区 A 家谱事件（B 窗口语义下不可见，非丢失）
			}
		}
		for i := range seqB {
			b := seqB[i]
			if b.BrokenDate == "" || (b.Status != contract.BoxBrokenUp && b.Status != contract.BoxBrokenDown) {
				continue
			}
			if i < len(seqB)-sufB {
				ev["trunc_zone_B"]++ // 分叉区 B 家谱独立事件（重启家谱的假事件史，非多出）
			}
		}
		suffixSum += sufA
		suffixCnt++
		if sufA < suffixMin {
			suffixMin = sufA
			suffixMinCode = c
		}

		// C 路恒等式抽查（确定性抽前 50 只）：state 必须 == seqB(上次触发日的 800 窗口扫描)
		if spotCnt < 50 && len(bk.state) > 0 && bk.lastTrig >= 0 {
			spotCnt++
			idxT := barIndexOf(bars, days[bk.lastTrig]) + 1
			if idxT > 0 && idxT <= len(bars) {
				wsT := 0
				if idxT > *window {
					wsT = idxT - *window
				}
				re := eng.Scan(bars[wsT:idxT], params)
				if len(re) != len(bk.state) {
					spotFail++
					addEx(evStats, c, days[bk.lastTrig], "identity", fmt.Sprintf("C 恒等式长度不符: state=%d rescan=%d", len(bk.state), len(re)))
				} else {
					for i := range re {
						if sem(re[i]).key() != bk.state[i].key() {
							spotFail++
							addEx(evStats, c, days[bk.lastTrig], "identity", fmt.Sprintf("C 恒等式槽位 %d 不符", i))
							break
						}
					}
				}
			}
		}

		// B vs C 末端待追赶计数
		if len(bk.state) > 0 {
			if tailC := bk.state[len(bk.state)-1]; tailC.key() != sem(seqB[len(seqB)-1]).key() {
				endPending++
			}
		}

		// A1：实例账（A 路终态序列 = 全生命周期事实）
		for k := range seqA {
			b := seqA[k]
			term := string(b.Status)
			if b.Top == 0 {
				continue // 身份真空槽位（(0,0) forming）：已在逐日 vacuumDays 统计
			}
			birth, ok := bk.tailIdent[fnum(b.Top)+"|0"]
			if !ok {
				birth, ok = bk.tailIdent[fnum(b.Top)+"|"+fnum(b.Bottom)]
			}
			if !ok {
				birth = bk.firstSeen[k]
			}
			termDate := b.BrokenDate
			if term == "invalid" {
				if k+1 < len(bk.firstSeen) {
					termDate = days[bk.firstSeen[k+1]] + "(_derived)"
				} else {
					termDate = "(_pending)"
				}
			}
			if term == "forming" || term == "confirmed" {
				termCnt[term+"_open"]++
				rightCensor++
			} else {
				termCnt[term]++
			}
			// 存续：仅「身份出生可观测」的已终结实例（birth < 终态日）。
			// birth 回退到首现日（= 终态日）说明身份出生早于逐日观测窗 → 观测边界左删失，剔除不混入。
			survivalKnown := false
			if term != "forming" && term != "confirmed" && b.TopConfirmed != "" {
				if td := strings.TrimSuffix(termDate, "(_derived)"); td != "" {
					if ti, ok := dayIdx[td]; ok {
						if ti > birth {
							survival = append(survival, ti-birth)
							survivalKnown = true
						} else {
							obsCensored++
						}
					} else if td < *from {
						preWindowTerminated++ // 观测窗（from）之前已终结的全史实例：不入删失账，单列
					}
				}
			}
			if b.TopConfirmed != "" && b.TopConfirmed < *histFrom {
				sampleBoundaryCensor++
			}
			if b.TopConfirmed != "" && b.TopConfirmed < bars[ws].Date {
				bWinLeftCensor++
			}
			if term == "forming" && b.Bottom == 0 && b.TopConfirmed != "" {
				phaseTop0++ // (top,0) 中间相位实例
			}
			if len(ledger) < ledgerCap && term != "forming" {
				ledger = append(ledger, map[string]any{
					"code": c, "slot": k, "top": b.Top, "bottom": b.Bottom,
					"top_confirmed": b.TopConfirmed, "bottom_confirmed": b.BottomConfirmed,
					"status": term, "terminal_date": termDate,
					"birth_day_offset": birth,
					"left_censored":    !survivalKnown && term != "confirmed" && term != "forming",
					"right_censored":   term == "confirmed",
				})
			}
		}

		// 身份复用：同码同 (top,bottom) 多实例（UpsertBoxes 复用折叠两生命周期的潜在次数）
		cnt := map[string]int{}
		for k := range seqA {
			b := seqA[k]
			if b.Top > 0 && b.Bottom > 0 {
				cnt[fnum(b.Top)+"|"+fnum(b.Bottom)]++
			}
		}
		for _, n := range cnt {
			if n > 1 {
				reuseKeys++
				reuseExtra += n - 1
			}
		}
	}

	data := map[string]any{
		"config": map[string]any{
			"src": *src, "from": *from, "to": *to, "histfrom": *histFrom,
			"maxcodes": *maxCodes, "window": *window,
			"clean_zone": "共同后缀（commonSuffix）；锚比对另用 top_margin=10",
			"codes":      len(codeList), "trade_days": len(days), "params": params,
		},
		"a0": map[string]any{
			"code_days": codeDays,
			"tail_compare_AB": map[string]any{
				"code_days": tailAB.CodeDays, "equal": tailAB.Equal,
				"window_semantics": tailAB.WinSem, "true_inconsistency": tailAB.TrueIncons,
				"examples": tailAB.Examples,
			},
			"anchor_compare_AB": map[string]any{
				"code_days": anchorAB.CodeDays, "equal": anchorAB.Equal,
				"window_semantics": anchorAB.WinSem, "true_inconsistency": anchorAB.TrueIncons,
				"examples": anchorAB.Examples,
			},
			"event_compare_AB": map[string]any{
				"counts": ev, "examples": evStats.Examples,
				"common_suffix_avg":   float64(suffixSum) / math.Max(1, float64(suffixCnt)),
				"common_suffix_min":   suffixMin, "common_suffix_min_code": suffixMinCode,
			},
			"incr_vs_B": map[string]any{
				"diff_days": tailDiffBC, "lag_pctiles": pctiles(lagDays),
				"triggers":      triggers,
				"trigger_rate":  float64(triggers) / math.Max(1, float64(codeDays)),
				"end_pending":   endPending,
				"identity_spot": map[string]int{"checked": spotCnt, "failed": spotFail},
			},
		},
		"a1": map[string]any{
			"terminal_status":                termCnt,
			"survival_days":                  pctiles(survival),
			"left_censored_observation_boundary": obsCensored,
			"pre_window_terminated":          preWindowTerminated,
			"left_censored_sample_boundary":  sampleBoundaryCensor,
			"left_censored_b_window":         bWinLeftCensor,
			"right_censored":                 rightCensor,
			"identity_vacuum_day_pct":        float64(vacuumDays) / math.Max(1, float64(codeDays)),
			"identity_reuse_keys":            reuseKeys,
			"identity_reuse_extra_instances": reuseExtra,
			"phase_top0_instances":           phaseTop0,
			"ledger_examples":                ledger,
		},
	}

	raw, err := json.MarshalIndent(data, "", " ")
	must(err, "序列化")
	must(os.WriteFile(*out, raw, 0644), "写出 "+*out)
	fmt.Printf("[a0] 已写出 %s（%d 字节）\n", *out, len(raw))

	fmt.Println("==== A0④ 总账 ====")
	fmt.Printf("code_days=%d\n", codeDays)
	fmt.Printf("尾箱A/B：等 %d / 窗口语义 %v / 真不一致 %d\n", tailAB.Equal, tailAB.WinSem, tailAB.TrueIncons)
	fmt.Printf("confirmed锚A/B：等 %d / 窗口语义 %v / 真不一致 %d\n", anchorAB.Equal, anchorAB.WinSem, anchorAB.TrueIncons)
	fmt.Printf("事件A/B：%v 共同后缀均长=%.1f 最短=%d（%s）\n", ev,
		float64(suffixSum)/math.Max(1, float64(suffixCnt)), suffixMin, suffixMinCode)
	fmt.Printf("B/C：滞后差天 %d 滞后 %v 触发率 %.2f%% 末端待追赶 %d 恒等式抽查 %d/%d 败\n",
		tailDiffBC, pctiles(lagDays), 100*float64(triggers)/math.Max(1, float64(codeDays)), endPending, spotFail, spotCnt)
	fmt.Println("==== A1 总账 ====")
	tj, _ := json.Marshal(termCnt)
	fmt.Printf("终态=%s\n右删失=%d 左删失(观测边界)=%d 观测窗前已终结=%d 左删失(样本边界)=%d 左删失(B窗)=%d\n身份真空天占比=%.1f%% 复用键=%d 复用多余实例=%d (top,0)相位=%d\n",
		tj, rightCensor, obsCensored, preWindowTerminated, sampleBoundaryCensor, bWinLeftCensor,
		100*float64(vacuumDays)/math.Max(1, float64(codeDays)), reuseKeys, reuseExtra, phaseTop0)

	hardFail := tailAB.TrueIncons > 0 || anchorAB.TrueIncons > 0 ||
		ev["suffix_violation"] > 0 || spotFail > 0
	if hardFail {
		fmt.Println("!!!! 硬门槛失败：洁净区（共同后缀）存在真不一致 —— 不得放行第 2 项（升级 A3 schema 设计）")
		os.Exit(2)
	}
	fmt.Println("==== 硬门槛 PASS：尾箱/锚逐日全等 + 共同后缀事件全等 + C 恒等式成立 ====")
}

// ───────────────────────── 比对辅助 ─────────────────────────

// tailBucket 尾箱差异的窗口语义子桶（非硬闸门）。
func tailBucket(a, b semBox) string {
	switch {
	case a.TopC == "" && b.TopC == "":
		return "候选期差异(双方未确认)"
	case a.TopC == "":
		return "A未确认_B已确认"
	case b.TopC == "":
		return "B未确认_A已确认"
	default:
		return "双方确认_家谱分叉"
	}
}

// compareAnchors 最新 confirmed 箱比对：相等 / 交叉存在（窗口语义）/ 洁净缺失（硬失败）。
func compareAnchors(st *stats, code, day string, seqA, seqB []contract.Box, bars []contract.DailyBar, ws int,
	addEx func(*stats, string, string, string, string)) {
	st.CodeDays++
	var la, lb *semBox
	for i := len(seqA) - 1; i >= 0; i-- {
		if seqA[i].Status == contract.BoxConfirmed {
			s := sem(seqA[i])
			la = &s
			break
		}
	}
	for i := len(seqB) - 1; i >= 0; i-- {
		if seqB[i].Status == contract.BoxConfirmed {
			s := sem(seqB[i])
			lb = &s
			break
		}
	}
	if la == nil && lb == nil {
		st.Equal++
		return
	}
	if la != nil && lb != nil && la.key() == lb.key() {
		st.Equal++
		return
	}
	clean := func(s *semBox) bool {
		if s == nil || s.TopC == "" {
			return false
		}
		return barIndexOf(bars, s.TopC) >= ws+topMargin
	}
	inSeq := func(s *semBox, seq []contract.Box) bool {
		for i := range seq {
			if sem(seq[i]).key() == s.key() {
				return true
			}
		}
		return false
	}
	switch {
	case la != nil && lb != nil && inSeq(la, seqB) && inSeq(lb, seqA):
		st.add("交叉存在_互为家谱滞后") // 双方各自存在于对方序列（相位差），窗口语义
	case la != nil && lb != nil && inSeq(lb, seqA):
		st.add("B滞后_A超前")
	case la != nil && lb != nil && inSeq(la, seqB):
		st.add("A滞后_B超前")
	case clean(la) && lb != nil && !inSeq(la, seqB) && !inSeq(lb, seqA):
		st.TrueIncons++
		addEx(st, code, day, "anchor", fmt.Sprintf("A洁净confirmed(%.4g/%.4g top=%s) 不在B序列；B=(%.4g/%.4g top=%s)",
			la.Top, la.Bottom, la.TopC, lb.Top, lb.Bottom, lb.TopC))
	case clean(lb) && la != nil && !inSeq(la, seqB) && !inSeq(lb, seqA):
		st.TrueIncons++
		addEx(st, code, day, "anchor", fmt.Sprintf("B洁净confirmed(%.4g/%.4g top=%s) 不在A序列；A=(%.4g/%.4g top=%s)",
			lb.Top, lb.Bottom, lb.TopC, la.Top, la.Bottom, la.TopC))
	default:
		st.add("非洁净家谱差")
	}
}

// prefilter 复刻 scanner.RescanIncremental 的预过滤（池外路径，语义 1:1）：
// 60 日新高 / 今日量≥均量(50)×volmult / 最新非 forming 箱体为 confirmed 且今日收盘跌破其下轨（M8.8 兜底）。
func prefilter(bars []contract.DailyBar, idx int, state []semBox, volMult float64) bool {
	today := bars[idx-1]
	lo := idx - 60
	if lo < 0 {
		lo = 0
	}
	maxPrev := 0.0
	for k := lo; k < idx-1; k++ {
		if bars[k].High > maxPrev {
			maxPrev = bars[k].High
		}
	}
	if maxPrev > 0 && today.High > maxPrev {
		return true
	}
	lo2 := idx - 1 - 50
	if lo2 < 0 {
		lo2 = 0
	}
	var sum float64
	cnt := 0
	for k := lo2; k < idx-1; k++ {
		sum += float64(bars[k].Volume)
		cnt++
	}
	if cnt > 0 {
		if avg := sum / float64(cnt); avg > 0 && float64(today.Volume) >= avg*volMult {
			return true
		}
	}
	for k := len(state) - 1; k >= 0; k-- {
		st := state[k].Status
		if st == "confirmed" {
			return state[k].Bottom > 0 && today.Close < state[k].Bottom
		}
		if st == "broken_up" || st == "broken_down" {
			return false
		}
	}
	return false
}

func pctiles(v []int) map[string]any {
	if len(v) == 0 {
		return map[string]any{"n": 0}
	}
	s := append([]int(nil), v...)
	sort.Ints(s)
	p := func(q float64) int { return s[int(q*float64(len(s)-1))] }
	return map[string]any{"n": len(s), "p25": p(0.25), "p50": p(0.50), "p75": p(0.75), "p90": p(0.90), "max": s[len(s)-1]}
}

// commonSuffix 从尾往前逐箱比较语义键，返回 (A 共同后缀长度, B 共同后缀长度)。
// 800 窗口重启后 B 家谱与 A 可能定义性分叉；分叉点之后（更近端）两家谱必然再同步，
// 共同后缀即「输入上下文完备」的洁净区。
func commonSuffix(seqA, seqB []contract.Box) (int, int) {
	i, j := len(seqA)-1, len(seqB)-1
	n := 0
	for i >= 0 && j >= 0 && sem(seqA[i]).key() == sem(seqB[j]).key() {
		i--
		j--
		n++
	}
	return n, n
}

func setDiffCount(a, b map[string]bool) int {
	n := 0
	for k := range a {
		if !b[k] {
			n++
		}
	}
	return n
}

func barIndexOf(bars []contract.DailyBar, date string) int {
	if date == "" {
		return -1
	}
	lo, hi := 0, len(bars)-1
	for lo <= hi {
		mid := (lo + hi) / 2
		switch {
		case bars[mid].Date == date:
			return mid
		case bars[mid].Date < date:
			lo = mid + 1
		default:
			hi = mid - 1
		}
	}
	return -1
}

// ───────────────────────── DB 访问（全部只读） ─────────────────────────

func openRO(path string) (*sql.DB, error) {
	p := strings.ReplaceAll(path, "\\", "/")
	db, err := sql.Open("sqlite", "file:"+p+"?mode=ro")
	if err == nil {
		if err = db.Ping(); err == nil {
			return db, nil
		}
		db.Close()
	}
	db2, err2 := sql.Open("sqlite", "file:"+p+"?mode=ro&immutable=1")
	if err2 == nil {
		if err2 = db2.Ping(); err2 == nil {
			return db2, nil
		}
		db2.Close()
	}
	return nil, err
}

func sampleCodes(db *sql.DB, from string, maxCodes int) ([]string, int) {
	rows, err := db.Query(`SELECT s.code FROM security s
		WHERE s.kind='stock' AND s.board IN ('main','gem','star') AND s.status='active'
		  AND (s.name IS NULL OR s.name='' OR s.name NOT LIKE '%ST%')
		  AND (SELECT COUNT(*) FROM daily_bar d WHERE d.code=s.code AND d.date < ?) >= 200
		ORDER BY s.code`, from)
	must(err, "查询样本代码")
	defer rows.Close()
	var all []string
	for rows.Next() {
		var c string
		must(rows.Scan(&c), "scan code")
		all = append(all, c)
	}
	must(rows.Err(), "rows")
	if len(all) <= maxCodes {
		return all, 1
	}
	step := (len(all) + maxCodes - 1) / maxCodes
	var out []string
	for i := 0; i < len(all); i += step {
		out = append(out, all[i])
	}
	return out, step
}

func calendar(db *sql.DB, from, to string) []string {
	rows, err := db.Query(`SELECT CAST(date AS TEXT) FROM daily_bar
		WHERE code='sh000001' AND date >= ? AND date <= ? ORDER BY date`, from, to)
	must(err, "查询日历")
	defer rows.Close()
	var out []string
	for rows.Next() {
		var d string
		must(rows.Scan(&d), "scan date")
		out = append(out, d)
	}
	must(rows.Err(), "rows")
	return out
}

func loadBars(db *sql.DB, code, from, to string) []contract.DailyBar {
	rows, err := db.Query(`SELECT CAST(date AS TEXT), open, high, low, close, volume
		FROM daily_bar WHERE code=? AND date >= ? AND date <= ? ORDER BY date`, code, from, to)
	must(err, "loadBars "+code)
	defer rows.Close()
	var out []contract.DailyBar
	for rows.Next() {
		var b contract.DailyBar
		must(rows.Scan(&b.Date, &b.Open, &b.High, &b.Low, &b.Close, &b.Volume), "scan bar "+code)
		b.Code = code
		out = append(out, b)
	}
	must(rows.Err(), "rows "+code)
	return out
}

func getParamF(db *sql.DB, key string, def float64) float64 {
	var v float64
	if err := db.QueryRow(`SELECT CAST(value AS REAL) FROM param WHERE key=?`, key).Scan(&v); err != nil {
		return def
	}
	return v
}

func must(err error, what string) {
	if err != nil {
		fmt.Fprintf(os.Stderr, "[a0] %s: %v\n", what, err)
		os.Exit(1)
	}
}
