// Package regimesweep 实现 M8.4a 体制标签参数敏感性扫描（PROMPT.md P4 / HANDOFF.md 任务书）。
//
// 目标：体制标注复用箱体引擎（regime.LabelBoxes），只依赖 sh000001 日线——
// 对箱体参数做网格扫描，回答「2014 翻牛滞后是否可通过调参显著提前，
// 且不引入假牛/假熊」。纯 scratch 库只读研究，不碰生产；评价函数先验固定，禁摘樱桃。
//
// 评价函数（2026-10-19 立项时固定，全部基于公开市场事实，先于扫描结果设定）：
//   先验锚点（上证指数公开行情）：
//     杠杆牛启动 2014-07-22；顶部 2015-06-12（5178）；股灾首日 2015-06-15；
//     股灾主跌段结束 2015-08-31（二次股灾 08-24 -8.5%）
//   基线（现生产参数 1.03/0.05/2.0/50/3 的 M8.3 实测）：
//     2014 翻牛 2014-11-11（滞后近 4 个月）；2015 翻熊 2015-06-29；
//     2016/2018 全年 0 牛日
//   达标（7 条全过）：
//     G1 2014-01-01~07-21 无假牛（牛市启动前的阴跌/横盘段不得标牛）
//     G2 2014 翻牛日存在且早于基线 11-11
//     G3 2015-01-01~06-12 无假熊（主升段不得标熊）
//     G4 2015 翻熊日存在且不晚于基线 06-29（≥06-15 起算，股灾响应性）
//     G5 2015-06-15~08-31 股灾段无假牛（反弹陷阱不得放行闸门）
//     G6 2016 全年无假牛
//     G7 2018 全年无假牛
//   显著提前档：达标且翻牛日 ≤ 2014-08-31（牛市启动后约一个月内跟上）。
//   稳定改善区：达标组合在参数网格中的相邻组合（单参数±1步）达标率 ≥ 50%
//   ——孤立达标点视为过拟合，不构成「稳定改善区」。
//
// 附加信息列（不做硬性约束，供人工研判）：2015H2 牛日数（Q4 有真实反弹，
// 是否算假牛有争议，仅报告）；各年牛/熊段收益（M8.3 回放权益按候选标签重切分，
// 注意：分拆收益≠闸门模拟，闸门精确影响需整链重放，不在 M8.4a 范围）；
// 闸门拦截买入率（回放买单落在非牛日的比例，闸门代价的粗代理）。
package regimesweep

import (
	"database/sql"
	"fmt"
	"html/template"
	"math"
	"os"
	"path/filepath"
	"sort"

	"darvas/internal/contract"
	"darvas/internal/regime"

	_ "modernc.org/sqlite"
)

// ---------------------------------------------------------------------------
// 先验常量（评价函数固定，禁摘樱桃——改动即评价函数重定义，须重新立项）
// ---------------------------------------------------------------------------

const (
	rallyStart2014  = "2014-07-22" // 杠杆牛启动日
	crashOnset2015  = "2015-06-15" // 股灾首日（06-12 见顶后首个交易日）
	peak2015        = "2015-06-12" // 上证 5178 顶
	crashEnd2015    = "2015-08-31" // 股灾主跌段终点
	significantEarl = "2014-08-31" // 显著提前档阈值（牛市启动+约1个月）

	baselineFlip2014 = "2014-11-11" // 基线：2014 翻牛日（M8.3 实测）
	baselineBear2015 = "2015-06-29" // 基线：2015 翻熊日（M8.3 实测）

	indexTo = "2018-12-31" // 指数标签扫描终点（覆盖全部评价年份）
)

// YearCase 评价年份（回放库为 M8.3 单年独立回放产物，每年 100 万起跑）。
type YearCase struct {
	Year     int
	From, To string
	ReplayDB string // 相对 --src 所在目录
}

// DefaultYears 四个评价年份。2018 回放落在 bt_regime.db（regime 模式
// 支持 --db 前的默认 scratch 名，见 81ffd00 提交历史）。
func DefaultYears() []YearCase {
	return []YearCase{
		{2014, "2014-01-01", "2014-12-31", "bt_2014.db"},
		{2015, "2015-01-01", "2015-12-31", "bt_2015.db"},
		{2016, "2016-01-01", "2016-12-31", "bt_2016.db"},
		{2018, "2018-01-01", "2018-12-31", "bt_regime.db"},
	}
}

// ---------------------------------------------------------------------------
// 参数网格
// ---------------------------------------------------------------------------

// Combo 一组体制标注参数。
type Combo struct {
	Premium float64 // BreakPremium 突破溢价
	Width   float64 // MinBoxWidth 箱体最小宽度
	VolMult float64 // VolMultiplier 放量倍数
	VolWin  int     // VolWindow 均量窗口
	Confirm int     // ConfirmDays 轨道确认天数（三日法则）
}

// Baseline 现生产参数（与 regime.Run / scanner.boxParams 一致）。
func Baseline() Combo { return Combo{1.03, 0.05, 2.0, 50, 3} }

// 网格取值（先验设定：围绕基线两侧、含基线本身）。
var (
	gridPremium = []float64{1.00, 1.01, 1.02, 1.03, 1.05}
	gridWidth   = []float64{0.02, 0.03, 0.05, 0.08}
	gridVolMult = []float64{1.0, 1.5, 2.0, 3.0}
	gridVolWin  = []int{20, 50}
	gridConfirm = []int{2, 3, 4}
)

// AllCombos 全网格（480 组）。
func AllCombos() []Combo {
	var out []Combo
	for _, p := range gridPremium {
		for _, w := range gridWidth {
			for _, v := range gridVolMult {
				for _, vw := range gridVolWin {
					for _, c := range gridConfirm {
						out = append(out, Combo{p, w, v, vw, c})
					}
				}
			}
		}
	}
	return out
}

func (c Combo) params() contract.BoxParams {
	return contract.BoxParams{
		VolMultiplier: c.VolMult, VolWindow: c.VolWin,
		BreakPremium: c.Premium, MinBoxWidth: c.Width, ConfirmDays: c.Confirm,
	}
}

// ParamStr 展示用参数串（溢价/宽度/放量/窗口/确认）。
func (c Combo) ParamStr() string {
	return fmt.Sprintf("%.2f / %.0f%% / %.1f× / %d / %d日", c.Premium, c.Width*100, c.VolMult, c.VolWin, c.Confirm)
}

func (c Combo) key() string {
	return fmt.Sprintf("%.2f|%.2f|%.1f|%d|%d", c.Premium, c.Width, c.VolMult, c.VolWin, c.Confirm)
}

// neighbors 网格相邻组合（每个维度 ±1 步）。
func neighbors(c Combo) []Combo {
	var out []Combo
	stepF := func(vals []float64, cur float64, mk func(float64) Combo) {
		for i, v := range vals {
			if v == cur {
				if i > 0 {
					out = append(out, mk(vals[i-1]))
				}
				if i < len(vals)-1 {
					out = append(out, mk(vals[i+1]))
				}
				return
			}
		}
	}
	stepI := func(vals []int, cur int, mk func(int) Combo) {
		for i, v := range vals {
			if v == cur {
				if i > 0 {
					out = append(out, mk(vals[i-1]))
				}
				if i < len(vals)-1 {
					out = append(out, mk(vals[i+1]))
				}
				return
			}
		}
	}
	stepF(gridPremium, c.Premium, func(v float64) Combo { return Combo{v, c.Width, c.VolMult, c.VolWin, c.Confirm} })
	stepF(gridWidth, c.Width, func(v float64) Combo { return Combo{c.Premium, v, c.VolMult, c.VolWin, c.Confirm} })
	stepF(gridVolMult, c.VolMult, func(v float64) Combo { return Combo{c.Premium, c.Width, v, c.VolWin, c.Confirm} })
	stepI(gridVolWin, c.VolWin, func(v int) Combo { return Combo{c.Premium, c.Width, c.VolMult, v, c.Confirm} })
	stepI(gridConfirm, c.Confirm, func(v int) Combo { return Combo{c.Premium, c.Width, c.VolMult, c.VolWin, v} })
	return out
}

// ---------------------------------------------------------------------------
// 评价结果
// ---------------------------------------------------------------------------

// SplitRet 单年体制分拆收益（M8.3 回放权益按候选标签重切分）。
type SplitRet struct {
	Bull, Bear, Range float64
}

// Result 单组合评价结果。
type Result struct {
	Combo Combo

	Flip2014        string // 2014 翻牛日（牛市窗口内首个牛日；""=全年无牛）
	FalseBull2014H1 int    // 2014-01-01~07-21 假牛日数（G1，目标0）
	BearFlip2015    string // 2015 翻熊日（≥06-15 首个熊日；""=股灾段无熊）
	FalseBear2015H1 int    // 2015-01-01~06-12 假熊日数（G3，目标0）
	CrashBull2015   int    // 2015-06-15~08-31 股灾段假牛日数（G5，目标0）
	H2Bull2015      int    // 2015H2 牛日数（信息列：Q4反弹争议段）
	FalseBull2016   int    // 2016 假牛日数（G6，目标0）
	FalseBull2018   int    // 2018 假牛日数（G7，目标0）

	BullDays map[int]int      // 各年牛日数（信息列）
	Ret      map[int]SplitRet // 各年分体收益（信息列）
	Blocked  map[int]int      // 各年被闸门拦截的买单数
	Buys     map[int]int      // 各年买单总数

	// 派生（computeQualification / computeNeighborhood 填充）
	Qualifies    bool
	Significant  bool    // 达标且翻牛 ≤ 2014-08-31
	NeighborTot  int     // 相邻组合数
	NeighborQual int     // 相邻达标数
	NeighborRate float64 // 邻域达标率
	Stable       bool    // 达标且邻域达标率 ≥ 50%
}

// computeQualification 七条先验 guard（固定评价函数）。
func (r *Result) computeQualification() {
	r.Qualifies = r.Flip2014 != "" && r.Flip2014 < baselineFlip2014 &&
		r.FalseBull2014H1 == 0 &&
		r.BearFlip2015 != "" && r.BearFlip2015 <= baselineBear2015 &&
		r.FalseBear2015H1 == 0 && r.CrashBull2015 == 0 &&
		r.FalseBull2016 == 0 && r.FalseBull2018 == 0
	r.Significant = r.Qualifies && r.Flip2014 <= significantEarl
}

// ---------------------------------------------------------------------------
// 标签统计（纯函数，可单测）
// ---------------------------------------------------------------------------

// countRegime 统计 [from,to] 内标为 want 的交易日数。
func countRegime(dates []string, labels map[string]regime.Regime, from, to string, want regime.Regime) int {
	n := 0
	for _, d := range dates {
		if d >= from && d <= to && labels[d] == want {
			n++
		}
	}
	return n
}

// firstRegime 找 [from,to] 内首个标为 want 的交易日（""=无）。
func firstRegime(dates []string, labels map[string]regime.Regime, from, to string, want regime.Regime) string {
	for _, d := range dates {
		if d > to {
			break
		}
		if d >= from && labels[d] == want {
			return d
		}
	}
	return ""
}

// ---------------------------------------------------------------------------
// 数据装载（源库/回放库全部只读）
// ---------------------------------------------------------------------------

type replayData struct {
	equities  []contract.EquitySnapshot
	fills     []contract.Fill
	positions []contract.Position
	buyDates  []string
}

// openRO 只读打开（WAL 库 ro 失败时 immutable 兜底，同 backtest.openSourceRO）。
func openRO(path string) (*sql.DB, error) {
	dsn := "file:" + filepath.ToSlash(path) + "?mode=ro"
	db, err := sql.Open("sqlite", dsn)
	if err == nil {
		if err = db.Ping(); err == nil {
			return db, nil
		}
		db.Close()
	}
	db2, err2 := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro&immutable=1")
	if err2 == nil {
		if err2 = db2.Ping(); err2 == nil {
			return db2, nil
		}
		db2.Close()
	}
	return nil, fmt.Errorf("只读打开失败 %s: %w", path, err)
}

// loadIndex 从源库读 sh000001 全历史日线（至 indexTo；箱体引擎需完整序列）。
func loadIndex(src string) (bars []contract.DailyBar, dates []string, err error) {
	db, err := openRO(src)
	if err != nil {
		return nil, nil, err
	}
	defer db.Close()
	rows, err := db.Query(`SELECT CAST(date AS TEXT), code, open, high, low, close, volume, amount
		FROM daily_bar WHERE code='sh000001' AND date <= ? ORDER BY date`, indexTo)
	if err != nil {
		return nil, nil, fmt.Errorf("读指数日线: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var b contract.DailyBar
		if e := rows.Scan(&b.Date, &b.Code, &b.Open, &b.High, &b.Low, &b.Close, &b.Volume, &b.Amount); e != nil {
			return nil, nil, e
		}
		bars = append(bars, b)
		dates = append(dates, b.Date)
	}
	if len(bars) == 0 {
		return nil, nil, fmt.Errorf("%s 无 sh000001 日线（至 %s）", src, indexTo)
	}
	return bars, dates, rows.Err()
}

// loadReplay 从单年回放库读权益/成交/持仓（体制分拆与闸门代理的数据源）。
func loadReplay(path string) (replayData, error) {
	var rp replayData
	db, err := openRO(path)
	if err != nil {
		return rp, err
	}
	defer db.Close()

	rows, err := db.Query(`SELECT CAST(date AS TEXT), cash, market_value, total, peak_total, drawdown
		FROM equity_snapshot ORDER BY date`)
	if err != nil {
		return rp, fmt.Errorf("读权益 %s: %w", path, err)
	}
	for rows.Next() {
		var e contract.EquitySnapshot
		if e2 := rows.Scan(&e.Date, &e.Cash, &e.MarketValue, &e.Total, &e.PeakTotal, &e.Drawdown); e2 != nil {
			rows.Close()
			return rp, e2
		}
		rp.equities = append(rp.equities, e)
	}
	rows.Close()

	rows, err = db.Query(`SELECT CAST(date AS TEXT), side FROM trade_fill ORDER BY date, id`)
	if err != nil {
		return rp, fmt.Errorf("读成交 %s: %w", path, err)
	}
	for rows.Next() {
		var date, side string
		if e := rows.Scan(&date, &side); e != nil {
			rows.Close()
			return rp, e
		}
		rp.fills = append(rp.fills, contract.Fill{Date: date, Side: side})
		if side == "buy" {
			rp.buyDates = append(rp.buyDates, date)
		}
	}
	rows.Close()

	rows, err = db.Query(`SELECT CAST(opened_date AS TEXT), status,
		COALESCE(CAST(closed_date AS TEXT),''), COALESCE(realized_pnl,0)
		FROM position ORDER BY id`)
	if err != nil {
		return rp, fmt.Errorf("读持仓 %s: %w", path, err)
	}
	for rows.Next() {
		var p contract.Position
		if e := rows.Scan(&p.OpenedDate, &p.Status, &p.ClosedDate, &p.RealizedPnl); e != nil {
			rows.Close()
			return rp, e
		}
		rp.positions = append(rp.positions, p)
	}
	rows.Close()
	return rp, rows.Err()
}

// ---------------------------------------------------------------------------
// 评价主循环
// ---------------------------------------------------------------------------

// evaluate 单组合：标签 → 逐年指标 + 分体收益 + 闸门代理。
func evaluate(c Combo, bars []contract.DailyBar, dates []string,
	years []YearCase, replays map[int]replayData) Result {
	labels := regime.LabelBoxes(bars, c.params())
	r := Result{
		Combo:    c,
		BullDays: map[int]int{},
		Ret:      map[int]SplitRet{},
		Blocked:  map[int]int{},
		Buys:     map[int]int{},
	}
	// 2014/2015 关键事件（评价函数固定）
	r.Flip2014 = firstRegime(dates, labels, rallyStart2014, "2014-12-31", regime.Bull)
	r.FalseBull2014H1 = countRegime(dates, labels, "2014-01-01", "2014-07-21", regime.Bull)
	r.BearFlip2015 = firstRegime(dates, labels, crashOnset2015, "2015-12-31", regime.Bear)
	r.FalseBear2015H1 = countRegime(dates, labels, "2015-01-01", peak2015, regime.Bear)
	r.CrashBull2015 = countRegime(dates, labels, crashOnset2015, crashEnd2015, regime.Bull)
	r.H2Bull2015 = countRegime(dates, labels, "2015-07-01", "2015-12-31", regime.Bull)
	r.FalseBull2016 = countRegime(dates, labels, "2016-01-01", "2016-12-31", regime.Bull)
	r.FalseBull2018 = countRegime(dates, labels, "2018-01-01", "2018-12-31", regime.Bull)
	r.computeQualification()

	for _, yc := range years {
		r.BullDays[yc.Year] = countRegime(dates, labels, yc.From, yc.To, regime.Bull)
		rp, ok := replays[yc.Year]
		if !ok {
			continue
		}
		// 分体收益：M8.3 回放权益按候选标签重切分（复用 regime.Summarize）
		_, stats := regime.Summarize(rp.equities, labels, rp.fills, rp.positions)
		var sr SplitRet
		for _, st := range stats {
			switch st.Regime {
			case regime.Bull:
				sr.Bull = st.Return
			case regime.Bear:
				sr.Bear = st.Return
			case regime.Range:
				sr.Range = st.Return
			}
		}
		r.Ret[yc.Year] = sr
		// 闸门代理：买单落在非牛日 = 会被「只在牛市买入」闸门拦截
		r.Buys[yc.Year] = len(rp.buyDates)
		for _, d := range rp.buyDates {
			if labels[d] != regime.Bull {
				r.Blocked[yc.Year]++
			}
		}
	}
	return r
}

// computeNeighborhood 邻域达标率（稳定改善区判定：孤立达标=过拟合嫌疑）。
func computeNeighborhood(results map[string]*Result) {
	for _, r := range results {
		ns := neighbors(r.Combo)
		r.NeighborTot = len(ns)
		for _, n := range ns {
			if nr, ok := results[n.key()]; ok && nr.Qualifies {
				r.NeighborQual++
			}
		}
		if r.NeighborTot > 0 {
			r.NeighborRate = float64(r.NeighborQual) / float64(r.NeighborTot)
		}
		r.Stable = r.Qualifies && r.NeighborRate >= 0.5
	}
}

// ---------------------------------------------------------------------------
// 报告
// ---------------------------------------------------------------------------

// ReportData 模板数据。
type ReportData struct {
	TotalCombos      int
	Baseline         Result
	BaselineChecks   []string // 基线自检（与 M8.3 报告一致性）
	OFAT             []OFATTable
	QualifyCount     int
	SignificantCount int
	StableCount      int
	QualTotalNote    string // 截断说明（空=未截断）
	Qualifying       []Result // 按 Flip2014 升序（展示截断至 30）
	MatrixPW         Matrix   // Premium × Width
	MatrixPV         Matrix   // Premium × VolMult
	Years            []int
	SplitTable       []SplitRow // 基线 + 前5达标
	Conclusion       []string
}

// OFATTable 单因子敏感性表。
type OFATTable struct {
	Name string
	Rows []OFATRow
}

// OFATRow 一行 = 一个参数值。
type OFATRow struct {
	Value     string
	IsBase    bool
	Flip2014  string
	Bear2015  string
	FB14      int
	FE15      int
	CB15      int
	FB16      int
	FB18      int
	Qualifies bool
}

// Matrix 二维敏感性矩阵（其余维度=基线）。
type Matrix struct {
	Title, XName, YName string
	XVals, YVals        []string
	Cells               [][]MatrixCell
}

// MatrixCell 单元格。
type MatrixCell struct {
	Flip2014 string
	FB16     int
	FB18     int
	Qual     bool
	IsBase   bool
}

// SplitRow 分年分体收益对比行（单元格串预计算，模板零算术）。
type SplitRow struct {
	Label      string
	IsBase     bool
	Flip2014   string
	YearCells  []string // 每年一格："牛 +x.x% / 熊 -x.x%"
	BlockedStr string   // "37/102"
}

// Run 执行扫描并生成报告。src=全历史源库（full_hist.db），out=报告输出路径。
func Run(src, out string) error {
	fmt.Printf("[sweep] 源库 %s；装载指数与回放数据…\n", src)
	bars, dates, err := loadIndex(src)
	if err != nil {
		return err
	}
	dir := filepath.Dir(src)
	years := DefaultYears()
	replays := map[int]replayData{}
	for _, yc := range years {
		rp, err := loadReplay(filepath.Join(dir, yc.ReplayDB))
		if err != nil {
			return fmt.Errorf("评价年份 %d 需要回放库（先跑 M8.3 单年回放）: %w", yc.Year, err)
		}
		replays[yc.Year] = rp
		fmt.Printf("[sweep] %d 回放库 %s：权益 %d 天 / 买单 %d 笔 / 持仓 %d 个\n",
			yc.Year, yc.ReplayDB, len(rp.equities), len(rp.buyDates), len(rp.positions))
	}
	combos := AllCombos()
	fmt.Printf("[sweep] 指数 %d 根（%s ~ %s）；网格 %d 组开始扫描…\n",
		len(bars), bars[0].Date, bars[len(bars)-1].Date, len(combos))

	// 基线自检：必须复现 M8.3 报告关键事件（工具正确性前置条件，失败即拒出报告）
	base := evaluate(Baseline(), bars, dates, years, replays)
	type check struct {
		name    string
		got     string
		want    string
	}
	checks := []check{
		{"2014 翻牛", base.Flip2014, baselineFlip2014},
		{"2015 翻熊", base.BearFlip2015, baselineBear2015},
		{"2016 牛日数", fmt.Sprint(base.FalseBull2016), "0"},
		{"2018 牛日数", fmt.Sprint(base.FalseBull2018), "0"},
	}
	var bc []string
	for _, c := range checks {
		if c.got != c.want {
			return fmt.Errorf("基线自检失败：%s = %q ≠ M8.3 实测 %q，工具与既有报告不一致，禁止出报告",
				c.name, c.got, c.want)
		}
		bc = append(bc, fmt.Sprintf("%s = %s ✓（与 M8.3 报告一致）", c.name, c.got))
	}

	// 全网格
	results := map[string]*Result{}
	for i, c := range combos {
		r := evaluate(c, bars, dates, years, replays)
		results[r.Combo.key()] = &r
		if (i+1)%120 == 0 {
			fmt.Printf("[sweep] 已扫 %d/%d\n", i+1, len(combos))
		}
	}
	computeNeighborhood(results)

	data := buildReport(base, results, years)
	data.BaselineChecks = bc
	if err := os.MkdirAll(filepath.Dir(out), 0o755); err != nil {
		return err
	}
	f, err := os.Create(out)
	if err != nil {
		return err
	}
	defer f.Close()
	if err := tmpl.Execute(f, data); err != nil {
		return err
	}
	fmt.Printf("[sweep] 完成：达标 %d / 显著提前 %d / 稳定 %d（共 %d 组）→ %s\n",
		data.QualifyCount, data.SignificantCount, data.StableCount, data.TotalCombos, out)
	return nil
}

func orDash(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

// buildReport 汇总模板数据（含自动结论；BaselineChecks 由 Run 的自检填入）。
func buildReport(base Result, results map[string]*Result, years []YearCase) ReportData {
	data := ReportData{
		TotalCombos: len(results),
		Baseline:    base,
		Years:       []int{2014, 2015, 2016, 2018},
	}

	// ④ 达标组合（全量，结论用；展示截断）
	var allQuals []Result
	for _, r := range results {
		if r.Qualifies {
			allQuals = append(allQuals, *r)
		}
		if r.Significant {
			data.SignificantCount++
		}
		if r.Stable {
			data.StableCount++
		}
	}
	sort.Slice(allQuals, func(i, j int) bool { return allQuals[i].Flip2014 < allQuals[j].Flip2014 })
	data.QualifyCount = len(allQuals)
	if len(allQuals) > 30 {
		data.QualTotalNote = fmt.Sprintf("仅列最早翻牛的前 30 组（共 %d 组达标）", len(allQuals))
		allQuals = allQuals[:30]
	}
	data.Qualifying = allQuals

	// ③ OFAT 单因子
	ofat := func(name string, vals []string, pick func(i int) Combo) OFATTable {
		t := OFATTable{Name: name}
		for i, v := range vals {
			c := pick(i)
			r := results[c.key()]
			t.Rows = append(t.Rows, OFATRow{
				Value: v, IsBase: c == Baseline(),
				Flip2014: orDash(r.Flip2014), Bear2015: orDash(r.BearFlip2015),
				FB14: r.FalseBull2014H1, FE15: r.FalseBear2015H1, CB15: r.CrashBull2015,
				FB16: r.FalseBull2016, FB18: r.FalseBull2018, Qualifies: r.Qualifies,
			})
		}
		return t
	}
	var sFmt []string
	for _, v := range gridPremium {
		sFmt = append(sFmt, fmt.Sprintf("%.2f", v))
	}
	data.OFAT = append(data.OFAT, ofat("突破溢价 BreakPremium（基线 1.03）", sFmt,
		func(i int) Combo { c := Baseline(); c.Premium = gridPremium[i]; return c }))
	sFmt = nil
	for _, v := range gridWidth {
		sFmt = append(sFmt, fmt.Sprintf("%.0f%%", v*100))
	}
	data.OFAT = append(data.OFAT, ofat("箱体最小宽度 MinBoxWidth（基线 5%）", sFmt,
		func(i int) Combo { c := Baseline(); c.Width = gridWidth[i]; return c }))
	sFmt = nil
	for _, v := range gridVolMult {
		sFmt = append(sFmt, fmt.Sprintf("%.1f×", v))
	}
	data.OFAT = append(data.OFAT, ofat("放量倍数 VolMultiplier（基线 2.0）", sFmt,
		func(i int) Combo { c := Baseline(); c.VolMult = gridVolMult[i]; return c }))
	data.OFAT = append(data.OFAT, ofat("均量窗口 VolWindow（基线 50）", []string{"20", "50"},
		func(i int) Combo { c := Baseline(); c.VolWin = gridVolWin[i]; return c }))
	data.OFAT = append(data.OFAT, ofat("轨道确认天数 ConfirmDays·三日法则（基线 3）", []string{"2", "3", "4"},
		func(i int) Combo { c := Baseline(); c.Confirm = gridConfirm[i]; return c }))

	// ⑤ 二维矩阵（其余维度=基线）
	buildMatrix := func(title, xName, yName string, xs []string, xg []float64, ys []string, yg []float64,
		xset func(c *Combo, v float64), yset func(c *Combo, v float64)) Matrix {
		m := Matrix{Title: title, XName: xName, YName: yName, XVals: xs, YVals: ys}
		for _, yv := range yg {
			var row []MatrixCell
			for _, xv := range xg {
				c := Baseline()
				xset(&c, xv)
				yset(&c, yv)
				r := results[c.key()]
				row = append(row, MatrixCell{
					Flip2014: orDash(r.Flip2014), FB16: r.FalseBull2016, FB18: r.FalseBull2018,
					Qual: r.Qualifies, IsBase: c == Baseline(),
				})
			}
			m.Cells = append(m.Cells, row)
		}
		return m
	}
	var xsW, ysV []string
	for _, v := range gridWidth {
		xsW = append(xsW, fmt.Sprintf("%.0f%%", v*100))
	}
	for _, v := range gridVolMult {
		ysV = append(ysV, fmt.Sprintf("%.1f×", v))
	}
	var ps []string
	for _, v := range gridPremium {
		ps = append(ps, fmt.Sprintf("%.2f", v))
	}
	data.MatrixPW = buildMatrix("突破溢价 × 箱体宽度（放量2.0×/窗口50/三日法则，其余=基线）",
		"MinBoxWidth →", "BreakPremium", xsW, gridWidth, ps, gridPremium,
		func(c *Combo, v float64) { c.Width = v }, func(c *Combo, v float64) { c.Premium = v })
	data.MatrixPV = buildMatrix("突破溢价 × 放量倍数（宽度5%/窗口50/三日法则，其余=基线）",
		"BreakPremium →", "VolMultiplier", ps, gridPremium, ysV, gridVolMult,
		func(c *Combo, v float64) { c.Premium = v }, func(c *Combo, v float64) { c.VolMult = v })

	// ⑥ 分年分体收益（基线 + 前 5 达标；单元格串预计算）
	splitRow := func(label string, isBase bool, r Result) SplitRow {
		sr := SplitRow{Label: label, IsBase: isBase, Flip2014: orDash(r.Flip2014)}
		blocked, total := 0, 0
		for _, y := range data.Years {
			ret := r.Ret[y]
			sr.YearCells = append(sr.YearCells, fmt.Sprintf("牛 %s / 熊 %s", pct(ret.Bull), pct(ret.Bear)))
			blocked += r.Blocked[y]
			total += r.Buys[y]
		}
		sr.BlockedStr = fmt.Sprintf("%d/%d", blocked, total)
		return sr
	}
	data.SplitTable = append(data.SplitTable,
		splitRow("基线（现参数 "+Baseline().ParamStr()+"）", true, base))
	for i, q := range data.Qualifying {
		if i >= 5 {
			break
		}
		data.SplitTable = append(data.SplitTable, splitRow(q.Combo.ParamStr(), false, q))
	}

	// ⑦ 自动结论（数据驱动，措辞逻辑固定）
	data.Conclusion = buildConclusion(data, allQualsFull(results))
	return data
}

// allQualsFull 全量达标列表（结论用，不截断）。
func allQualsFull(results map[string]*Result) []Result {
	var out []Result
	for _, r := range results {
		if r.Stable {
			out = append(out, *r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Flip2014 < out[j].Flip2014 })
	return out
}

// buildConclusion 依据达标/显著/稳定计数生成结论段（固定措辞逻辑）。
func buildConclusion(data ReportData, stable []Result) []string {
	var out []string
	if data.QualifyCount == 0 {
		out = append(out, fmt.Sprintf(
			"① 全网格 %d 组中无任何组合同时满足七条先验条件——在箱体法框架内，"+
				"无法通过调参在不引入假牛/假熊的前提下提前 2014 翻牛。基线参数已处于该理论的前沿。", data.TotalCombos))
		out = append(out,
			"② 体制标签的滞后（2014 翻牛 11-11，落后行情起点近 4 个月）是趋势跟随的固有代价，"+
				"单参数无解；若大盘闸门需要更灵敏的牛熊判定，需另立项讨论（如接受假牛风险，或明确不启用闸门）。")
		return out
	}
	out = append(out, fmt.Sprintf(
		"① 全网格 %d 组中 %d 组达标（%.1f%%），其中显著提前（≤2014-08-31）%d 组，邻域稳定 %d 组。",
		data.TotalCombos, data.QualifyCount, float64(data.QualifyCount)*100/float64(data.TotalCombos),
		data.SignificantCount, data.StableCount))
	if data.StableCount == 0 {
		out = append(out,
			"② 达标组合全部为孤立点（邻域达标率<50%）——参数稍动即失效，过拟合嫌疑大，"+
				"不构成稳定改善区，不建议据此改参数。建议维持现参数。")
		return out
	}
	// 稳定组合的参数范围
	var lo, hi [5]float64
	first := true
	for _, r := range stable {
		v := [5]float64{r.Combo.Premium, r.Combo.Width, r.Combo.VolMult, float64(r.Combo.VolWin), float64(r.Combo.Confirm)}
		if first {
			lo, hi = v, v
			first = false
			continue
		}
		for i := range v {
			lo[i] = math.Min(lo[i], v[i])
			hi[i] = math.Max(hi[i], v[i])
		}
	}
	out = append(out, fmt.Sprintf(
		"② 存在稳定改善区：达标且邻域稳定的参数范围——溢价 %.2f~%.2f / 宽度 %.0f%%~%.0f%% / "+
			"放量 %.1f×~%.1f× / 窗口 %.0f~%.0f / 确认 %.0f~%.0f 日。",
		lo[0], hi[0], lo[1]*100, hi[1]*100, lo[2], hi[2], lo[3], hi[3], lo[4], hi[4]))
	if data.SignificantCount == 0 {
		out = append(out,
			"③ 稳定区内的提前幅度有限（无组合达到显著提前档 ≤2014-08-31）——属温和改善，"+
				"是否值得偏离现参数，留待用户拍板（4 年样本少，任何单点最优都有过拟合风险）。")
	} else {
		out = append(out,
			"③ 存在显著提前的组合；但 4 年样本少，本报告只给敏感性不给单点最优（防过拟合），"+
				"候选参数需经更多年份（1990s/2000s/2020s）walk-forward 验证后方可考虑启用。")
	}
	return out
}

// ---------------------------------------------------------------------------
// HTML 模板
// ---------------------------------------------------------------------------

func pct(v float64) string {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return "—"
	}
	return fmt.Sprintf("%+.1f%%", v*100)
}

var tmpl = template.Must(template.New("sweep").Funcs(template.FuncMap{
	"pct":    pct,
	"orDash": orDash,
	"zeroOK": func(n int) string {
		if n == 0 {
			return "0 ✓"
		}
		return fmt.Sprintf("%d ✗", n)
	},
	"noBull": func(n int) string {
		if n == 0 {
			return "无牛 ✓"
		}
		return fmt.Sprintf("%d日 ✗", n)
	},
}).Parse(`<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>M8.4a 体制标签参数敏感性报告</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:2em;background:#1a1a1a;color:#e0e0e0;line-height:1.55}
h1{color:#f0c040} h2{color:#80c0ff;border-bottom:1px solid #444;padding-bottom:4px;margin-top:32px}
table{border-collapse:collapse;margin:8px 0;font-size:13px}
th,td{border:1px solid #555;padding:4px 9px;text-align:center;white-space:nowrap}
th{background:#2a2a2a;color:#ffd060}
td.l,th.l{text-align:left}
tr.base td{background:#3a3020;font-weight:bold}
.pos{color:#ff6b6b} .neg{color:#5fd96f} .warn{color:#ffd060}
.note{color:#888;font-size:0.88em}
.good{color:#5fd96f} .bad{color:#ff6b6b}
.matrix td{min-width:92px;font-size:12px}
ul{font-size:13.5px}
</style></head><body>
<h1>M8.4a · 体制标签参数敏感性报告</h1>
<p class="note">网格 {{.TotalCombos}} 组｜体制标注=箱体法（regime.LabelBoxes，零未来信息）｜
评价函数先验固定（2026-10-19 立项），禁摘樱桃｜只给敏感性，不给单点最优（4 年样本少，防过拟合）｜
生成：darvas regimesweep（scratch 库只读，不碰生产）</p>

<h2>① 评价函数（先验固定）</h2>
<table>
<tr><th class="l">守卫</th><th class="l">定义</th></tr>
<tr><td class="l">G1 2014上半年无假牛</td><td class="l">2014-01-01~07-21（杠杆牛 07-22 启动前）牛日数 = 0</td></tr>
<tr><td class="l">G2 2014翻牛提前</td><td class="l">牛市窗口（07-22 起）首个牛日 &lt; 基线 2014-11-11</td></tr>
<tr><td class="l">G3 2015主升无假熊</td><td class="l">2015-01-01~06-12（5178 见顶前）熊日数 = 0</td></tr>
<tr><td class="l">G4 2015翻熊不回退</td><td class="l">≥06-15（股灾首日）首个熊日 ≤ 基线 2015-06-29</td></tr>
<tr><td class="l">G5 股灾段无假牛</td><td class="l">2015-06-15~08-31（两次股灾主跌段）牛日数 = 0</td></tr>
<tr><td class="l">G6 2016无假牛</td><td class="l">2016 全年牛日数 = 0（熔断熊）</td></tr>
<tr><td class="l">G7 2018无假牛</td><td class="l">2018 全年牛日数 = 0（贸易战熊）</td></tr>
</table>
<p class="note">显著提前档 = 达标且翻牛 ≤ 2014-08-31（牛市启动+约1个月）。稳定 = 相邻参数组合（单参数±1步）达标率 ≥ 50%，
孤立达标点=过拟合嫌疑。基线 = 现生产参数（溢价1.03/宽度5%/放量2.0×/窗口50/三日法则）。</p>

<h2>② 基线自检（工具正确性前置条件，失败即拒出报告）</h2>
<ul>{{range .BaselineChecks}}<li>{{.}}</li>{{end}}</ul>
<table>
<tr><th class="l">组合</th><th>2014翻牛</th><th>2015翻熊</th><th>14H1假牛</th><th>15假熊</th><th>股灾假牛</th><th>2016牛日</th><th>2018牛日</th><th>达标</th></tr>
<tr class="base"><td class="l">基线 {{.Baseline.Combo.ParamStr}}</td>
<td>{{orDash .Baseline.Flip2014}}</td><td>{{orDash .Baseline.BearFlip2015}}</td>
<td>{{zeroOK .Baseline.FalseBull2014H1}}</td><td>{{zeroOK .Baseline.FalseBear2015H1}}</td>
<td>{{zeroOK .Baseline.CrashBull2015}}</td><td>{{noBull .Baseline.FalseBull2016}}</td><td>{{noBull .Baseline.FalseBull2018}}</td>
<td class="warn">基线即锚点</td></tr>
</table>

<h2>③ 单因子敏感性（其余维度=基线）</h2>
{{range .OFAT}}
<p style="margin:14px 0 2px"><b>{{.Name}}</b></p>
<table>
<tr><th class="l">参数值</th><th>2014翻牛</th><th>2015翻熊</th><th>14H1假牛</th><th>15假熊</th><th>股灾假牛</th><th>2016牛日</th><th>2018牛日</th><th>达标</th></tr>
{{range .Rows}}{{if .IsBase}}<tr class="base">{{else}}<tr>{{end}}
<td class="l">{{.Value}}</td>
<td>{{if ne .Flip2014 "—"}}{{.Flip2014}}{{if lt .Flip2014 "2014-11-11"}} <span class="good">提前</span>{{end}}{{else}}—{{end}}</td>
<td>{{if ne .Bear2015 "—"}}{{.Bear2015}}{{if gt .Bear2015 "2015-06-29"}} <span class="bad">回退</span>{{end}}{{else}}<span class="bad">无熊</span>{{end}}</td>
<td>{{zeroOK .FB14}}</td><td>{{zeroOK .FE15}}</td><td>{{zeroOK .CB15}}</td>
<td>{{noBull .FB16}}</td><td>{{noBull .FB18}}</td>
<td>{{if .Qualifies}}<span class="good">✓</span>{{else}}<span class="bad">✗</span>{{end}}</td></tr>
{{end}}
</table>
{{end}}

<h2>④ 达标组合（{{.QualifyCount}} / {{.TotalCombos}}；显著提前 {{.SignificantCount}}；邻域稳定 {{.StableCount}}）</h2>
{{if .Qualifying}}
<table>
<tr><th class="l">组合（溢价/宽度/放量/窗口/确认）</th><th>2014翻牛</th><th>2015翻熊</th><th>15H2牛日</th><th>邻域达标</th><th>稳定</th><th>显著提前</th></tr>
{{range .Qualifying}}<tr>
<td class="l">{{.Combo.ParamStr}}</td>
<td class="good">{{.Flip2014}}</td><td>{{.BearFlip2015}}</td><td>{{.H2Bull2015}}</td>
<td>{{.NeighborQual}}/{{.NeighborTot}}</td>
<td>{{if .Stable}}<span class="good">✓</span>{{else}}<span class="warn">孤立</span>{{end}}</td>
<td>{{if .Significant}}<span class="good">✓</span>{{else}}—{{end}}</td></tr>{{end}}
</table>
{{if .QualTotalNote}}<p class="note">{{.QualTotalNote}}</p>{{end}}
{{else}}<p><b class="bad">无达标组合。</b></p>{{end}}

<h2>⑤ 二维敏感性矩阵（单元格：2014翻牛日；红标=假牛；绿✓=达标）</h2>
<p><b>{{.MatrixPW.Title}}</b></p>
<table class="matrix">
<tr><th class="l">{{.MatrixPW.YName}} \ {{.MatrixPW.XName}}</th>{{range .MatrixPW.XVals}}<th>{{.}}</th>{{end}}</tr>
{{range $yi, $row := .MatrixPW.Cells}}<tr>
<th>{{index $.MatrixPW.YVals $yi}}</th>
{{range $row}}<td{{if .IsBase}} class="base"{{end}}>{{.Flip2014}}{{if .FB16}}<span class="bad"> /16牛{{.FB16}}</span>{{end}}{{if .FB18}}<span class="bad"> /18牛{{.FB18}}</span>{{end}}{{if .Qual}} <span class="good">✓</span>{{end}}</td>{{end}}
</tr>{{end}}
</table>
<p><b>{{.MatrixPV.Title}}</b></p>
<table class="matrix">
<tr><th class="l">{{.MatrixPV.YName}} \ {{.MatrixPV.XName}}</th>{{range .MatrixPV.XVals}}<th>{{.}}</th>{{end}}</tr>
{{range $yi, $row := .MatrixPV.Cells}}<tr>
<th>{{index $.MatrixPV.YVals $yi}}</th>
{{range $row}}<td{{if .IsBase}} class="base"{{end}}>{{.Flip2014}}{{if .FB16}}<span class="bad"> /16牛{{.FB16}}</span>{{end}}{{if .FB18}}<span class="bad"> /18牛{{.FB18}}</span>{{end}}{{if .Qual}} <span class="good">✓</span>{{end}}</td>{{end}}
</tr>{{end}}
</table>

<h2>⑥ 分年体制分拆收益对比（基线 + 前5达标；M8.3 回放权益按候选标签重切分）</h2>
<table>
<tr><th class="l">组合（翻牛日）</th>{{range .Years}}<th>{{.}} 牛段 / 熊段</th>{{end}}<th>闸门拦截买入(四年)</th></tr>
{{range .SplitTable}}{{if .IsBase}}<tr class="base">{{else}}<tr>{{end}}
<td class="l">{{.Label}}（{{.Flip2014}}）</td>
{{range .YearCells}}<td>{{.}}</td>{{end}}
<td>{{.BlockedStr}}</td>
</tr>{{end}}
</table>
<p class="note">口径：分拆收益 ≠ 闸门模拟。牛段收益=「若闸门只放行牛日买入，大致保留的收益」的粗代理
（权益复合与金字塔联动的精确影响需整链重放，不在 M8.4a 范围）；闸门拦截买入率=买单落在非牛日的比例。</p>

<h2>⑦ 结论</h2>
<ul>{{range .Conclusion}}<li>{{.}}</li>{{end}}</ul>
<p class="note">本报告为 M8.4a 敏感性研究。是否调整体制标注参数 / 是否启用大盘闸门，均属规则变更，
须用户拍板（铁律：观察期规则冻结）。</p>
</body></html>`))
