// Package backtest 实现 M8.1 内置回放骨架（docs/01 §9 M8 修订 / PROMPT.md P4/M8）：
// scratch 测试库引导 + 交易日历循环 + as-of 纪律差分审计。
//
// M8 附加纪律（PROMPT.md，违反任何一条=返工）：
//   1. 观察期内规则冻结：回放只产出「问题清单」（archive/07-m8-issue-list.md），不改规则；
//   2. 回放必须跑独立测试库，禁止污染实时库心跳——本包所有写入仅落在 scratch 库；
//   3. 源库只读（mode=ro 打开，铁律 7：var/perf_test.db 禁止破坏性操作）。
//
// as-of 语义：任何任务读到的 K 线不得晚于回放日 D。唯一豁免（ADR-2，docs/02 §6）：
// planner 经 NextDate 读 sh000001 的未来「日历」——交易日历是公开先验信息，非价格。
//
// 审计方法（差分测试，M8.1 首任务）：
//   - prefill     全历史 K 线预灌，未来数据物理在场；
//   - progressive 逐日只灌 D 日 K 线，未来数据物理缺席（地面真值）。
//   两模式按同一交易日历重放六任务链（boxscan→screen→poolsync→signal→plan→report，
//   生产代码路径 pipeline.RerunTask，规则零重写）。若任何任务偷读未来 K 线，
//   决策输出必然分叉；Compare 全等 = as-of 纪律成立的运行时证明。
//   两模式均保留 sh000001 全历史（日历权威），其余差异仅在「D 日之后的价格是否存在」。
//
// 引导教训（2026-10-17 演练复盘，archive/07 #4）：演练 reset 未清 box 表，回放从
// 「未来箱体状态」上启动，候选池被污染——本包引导把 box 一并清空，箱体状态必须
// 由回放逐日重算累积（as-of by construction）。
package backtest

import (
	"context"
	"database/sql"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"darvas/internal/contract"
	"darvas/internal/match"
	"darvas/internal/pipeline"
	"darvas/internal/regime"
	"darvas/internal/store"

	_ "modernc.org/sqlite"
)

// Mode 回放模式。
type Mode string

const (
	// ModePrefill 全历史预灌（M8.2+ 模拟撮合的常规模式：K线全量在场，任务靠查询边界自律）。
	ModePrefill Mode = "prefill"
	// ModeProgressive 逐日灌K线（差分审计的地面真值：D 日之后价格物理缺席）。
	ModeProgressive Mode = "progressive"
	// ModeVerify 双模式重放 + 差分（M8.1 as-of 审计入口）。
	ModeVerify Mode = "verify"
	// ModeRegime 长窗口 prefill 回放 + 分体制报告（M8.3 体制运行器）。
	ModeRegime Mode = "regime"
)

// Options 回放入口参数。
type Options struct {
	From, To       string // 窗口 [From, To]（含）；空则取源库最近 20 个交易日
	SrcDB          string // 源库（只读；灌历史K线/证券表/日历）
	ScratchDB      string // 单模式目标库；空则默认 var/bt/bt_<mode>.db；verify 模式忽略
	Mode           Mode
	ParamOverrides map[string]string // 仅写入 scratch，源库与生产参数不变
}

// sixTasks 回放的六任务链（PROMPT.md「as-of 六任务链」；ingest 由引导替代——
// scratch 库数据预灌，逐日数据由 progressive 模式从源库拷贝，不依赖 TDX 文件）。
var sixTasks = []string{"boxscan", "screen", "poolsync", "signal", "plan", "report"}

// Run 回放主入口。
func Run(o Options) error {
	if o.SrcDB == "" {
		return fmt.Errorf("必须指定 --src <源库路径>")
	}
	switch o.Mode {
	case ModePrefill, ModeProgressive:
		scratch := o.ScratchDB
		if scratch == "" {
			scratch = defaultScratch(string(o.Mode))
		}
		if err := runOne(o, scratch); err != nil {
			return err
		}
		fmt.Printf("[bt] %s 模式回放完成: %s\n", o.Mode, scratch)
		return nil
	case ModeVerify:
		return runVerify(o)
	case ModeRegime:
		return runRegime(o)
	case "":
		return fmt.Errorf("必须指定 --mode prefill|progressive|verify|regime")
	default:
		return fmt.Errorf("未知模式 %q（可选 prefill|progressive|verify|regime）", o.Mode)
	}
}

// defaultScratch 默认 scratch 路径（var/bt/，gitignored）。
func defaultScratch(mode string) string {
	return filepath.ToSlash(filepath.Join("var", "bt", "bt_"+mode+".db"))
}

// runOne 单模式：窗口推导 → 引导 → 重放。
func runOne(o Options, scratch string) error {
	from, to, days, err := resolveWindow(o)
	if err != nil {
		return err
	}
	fmt.Printf("[bt] 窗口 %s ~ %s（%d 个交易日）\n", from, to, len(days))

	var dayBars map[string][]contract.DailyBar
	if o.Mode == ModeProgressive {
		if dayBars, err = loadWindowBars(o.SrcDB, from, to); err != nil {
			return fmt.Errorf("预载窗口K线: %w", err)
		}
	}
	if err = Bootstrap(o.SrcDB, scratch, from, o.Mode == ModeProgressive); err != nil {
		return err
	}
	hashes, fails, err := Replay(scratch, days, dayBars)
	if err != nil {
		return err
	}
	_ = hashes
	if len(fails) > 0 {
		return fmt.Errorf("回放存在失败任务:\n  %s", strings.Join(fails, "\n  "))
	}
	return nil
}

// runVerify 差分审计：progressive（地面真值）与 prefill 双回放，决策输出必须全等。
func runVerify(o Options) error {
	prog := defaultScratch(string(ModeProgressive))
	pre := defaultScratch(string(ModePrefill))

	from, to, days, err := resolveWindow(o)
	if err != nil {
		return err
	}
	fmt.Printf("[bt] verify: 窗口 %s ~ %s（%d 个交易日），双模式重放开始\n", from, to, len(days))

	// 地面真值：progressive
	dayBars, err := loadWindowBars(o.SrcDB, from, to)
	if err != nil {
		return fmt.Errorf("预载窗口K线: %w", err)
	}
	if err = Bootstrap(o.SrcDB, prog, from, true); err != nil {
		return err
	}
	progHashes, fails, err := Replay(prog, days, dayBars)
	if err != nil {
		return err
	}
	if len(fails) > 0 {
		return fmt.Errorf("progressive 回放失败:\n  %s", strings.Join(fails, "\n  "))
	}

	// 对照组：prefill（未来K线物理在场）
	if err = Bootstrap(o.SrcDB, pre, from, false); err != nil {
		return err
	}
	preHashes, fails, err := Replay(pre, days, nil)
	if err != nil {
		return err
	}
	if len(fails) > 0 {
		return fmt.Errorf("prefill 回放失败:\n  %s", strings.Join(fails, "\n  "))
	}

	diffs := Compare(prog, pre, progHashes, preHashes)
	if len(diffs) == 0 {
		fmt.Printf("[bt] ✓ as-of 审计通过：%d 个交易日，prefill 与 progressive 决策输出全等"+
			"（六任务链未读到任何未来K线）\n", len(days))
		return nil
	}
	fmt.Printf("[bt] ✗ as-of 审计不通过：发现 %d 处差异（任何任务读到「未来」K线即返工）：\n", len(diffs))
	for i, d := range diffs {
		if i >= 30 {
			fmt.Printf("  ……（其余 %d 处省略）\n", len(diffs)-i)
			break
		}
		fmt.Println("  " + d)
	}
	return fmt.Errorf("as-of 差分审计失败：%d 处差异", len(diffs))
}

// runRegime 长窗口分体制回放（M8.3）：prefill 全历史回放 + 分体制 HTML 报告。
// 必须指定 --from --to（长窗口覆盖牛熊猴；默认 20 日窗口对体制切分无意义）。
func runRegime(o Options) error {
	if o.From == "" || o.To == "" {
		return fmt.Errorf("regime 模式必须指定 --from --to（长窗口，如 --from 2014-01-01）")
	}
	from, to, days, err := resolveWindow(o)
	if err != nil {
		return err
	}
	fmt.Printf("[bt] regime: 窗口 %s ~ %s（%d 个交易日）\n", from, to, len(days))

	scratch := o.ScratchDB
	if scratch == "" {
		scratch = defaultScratch("regime")
	}
	if err = Bootstrap(o.SrcDB, scratch, from, false); err != nil {
		return err
	}
	if err = applyParamOverrides(scratch, o.ParamOverrides); err != nil {
		return err
	}
	if _, fails, err := Replay(scratch, days, nil); err != nil {
		return err
	} else if len(fails) > 0 {
		return fmt.Errorf("regime 回放存在失败任务:\n  %s", strings.Join(fails, "\n  "))
	}

	st, err := store.Open(scratch)
	if err != nil {
		return err
	}
	defer st.Close()
	reportDir := strings.TrimSuffix(scratch, ".db") + "_reports"
	path, err := regime.Run(context.Background(), st, from, to, reportDir)
	if err != nil {
		return err
	}
	fmt.Printf("[bt] regime 报告已生成: %s\n", path)
	return nil
}

func applyParamOverrides(scratch string, overrides map[string]string) error {
	if len(overrides) == 0 {
		return nil
	}
	st, err := store.Open(scratch)
	if err != nil {
		return fmt.Errorf("打开 scratch 应用参数覆盖: %w", err)
	}
	defer st.Close()
	keys := make([]string, 0, len(overrides))
	for key := range overrides {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		value := overrides[key]
		if _, err := st.DB().Exec(`INSERT INTO param(key,value) VALUES(?,?)
			ON CONFLICT(key) DO UPDATE SET old_value=param.value, value=excluded.value,
			changed_at=datetime('now','localtime')`, key, value); err != nil {
			return fmt.Errorf("覆盖参数 %s: %w", key, err)
		}
		fmt.Printf("[bt] 参数覆盖 %s=%s\n", key, value)
	}
	return nil
}

// resolveWindow 推导窗口与交易日历（ADR-2：sh000001 为交易日历权威）。
// From/To 为空时取源库最近 20 个交易日（对齐观察期 20 日口径）。
func resolveWindow(o Options) (from, to string, days []string, err error) {
	src, err := openSourceRO(o.SrcDB)
	if err != nil {
		return "", "", nil, err
	}
	defer src.Close()

	if o.From == "" || o.To == "" {
		rows, e := src.Query(`SELECT CAST(date AS TEXT) FROM daily_bar
			WHERE code='sh000001' ORDER BY date DESC LIMIT 20`)
		if e != nil {
			return "", "", nil, fmt.Errorf("读源库日历: %w", e)
		}
		defer rows.Close()
		for rows.Next() {
			var d string
			if e := rows.Scan(&d); e != nil {
				return "", "", nil, e
			}
			days = append(days, d)
		}
		if len(days) == 0 {
			return "", "", nil, fmt.Errorf("源库 %s 无 sh000001 日线", o.SrcDB)
		}
		sort.Sort(sort.Reverse(sort.StringSlice(days)))
		return days[0], days[len(days)-1], days, nil
	}
	rows, e := src.Query(`SELECT CAST(date AS TEXT) FROM daily_bar
		WHERE code='sh000001' AND date >= ? AND date <= ? ORDER BY date`, o.From, o.To)
	if e != nil {
		return "", "", nil, fmt.Errorf("读源库日历: %w", e)
	}
	defer rows.Close()
	for rows.Next() {
		var d string
		if e := rows.Scan(&d); e != nil {
			return "", "", nil, e
		}
		days = append(days, d)
	}
	if len(days) == 0 {
		return "", "", nil, fmt.Errorf("窗口 %s~%s 内源库无交易日", o.From, o.To)
	}
	return o.From, o.To, days, nil
}

// ---------------------------------------------------------------------------
// 引导（Bootstrap）
// ---------------------------------------------------------------------------

// wipeTables 引导清空的事务态表（子先于父，FK 安全顺序）。
// 注意必须包含 box：箱体是回放逐日重算的状态（10-17 演练教训，archive/07 #4）。
var wipeTables = []string{
	"trade_review", "trade_fill", "position_leg", "position",
	"plan_order", "trade_plan", "signal", "equity_snapshot",
	"candidate", "watch", "box", "daily_run",
}

// Bootstrap 引导 scratch 库：整库拷贝源库 → 清空全部事务态 → progressive 模式
// 额外裁掉窗口起点之后的非指数K线。scratch 是一次性回放库，存在即删（重建）。
//
// 实现要点（perf_test schema 版本偏差修复）：
//   - 源库可能停在旧 schema 版本（perf_test = v2，缺 0003/0004/0005）。
//   - store.Open 会自动跑迁移：0003 重建 watch（INSERT INTO watch_rebuild SELECT FROM watch），
//     若 watch 有孤儿 current_box_id（box 已删但引用残留）+ FK ON → INSERT 撞 FK 787。
//   - 源库只读（铁律 7）不能修；但在 scratch 拷贝上修：raw 连接（默认 FK OFF）先清空
//     全部事务态表（含 watch 孤儿），再让 store.Open 在空表上跑迁移——INSERT 空、无 FK 风险。
func Bootstrap(src, scratch string, from string, progressive bool) error {
	srcAbs, _ := filepath.Abs(src)
	scrAbs, _ := filepath.Abs(scratch)
	if srcAbs == scrAbs {
		return fmt.Errorf("scratch 与源库同路径：%s", scratch)
	}
	base := strings.ToLower(filepath.Base(scratch))
	if base == "darvas.db" || base == "perf_test.db" {
		return fmt.Errorf("拒绝把 %s 用作 scratch（保护生产/测试库）", scratch)
	}
	// 源库 WAL 有未合并内容时整库拷贝会丢数据 → 拒绝（先 checkpoint 再回放）
	if fi, err := os.Stat(src + "-wal"); err == nil && fi.Size() > 0 {
		return fmt.Errorf("源库存在未合并 WAL（%s，%d 字节）：请先 checkpoint 或关闭占用进程", src+"-wal", fi.Size())
	}
	for _, ext := range []string{"", "-wal", "-shm"} {
		if _, err := os.Stat(scratch + ext); err == nil {
			if err := os.Remove(scratch + ext); err != nil {
				return fmt.Errorf("删除旧 scratch: %w", err)
			}
		}
	}
	if err := copyFile(src, scratch); err != nil {
		return fmt.Errorf("拷贝源库: %w", err)
	}

	// 阶段一：raw 连接（FK OFF）清空全部已存在的事务态表——
	// 避免 store.Open 迁移 0003 时撞 watch 孤儿外键。
	if err := rawWipeExisting(scratch); err != nil {
		return err
	}

	// 阶段二：store.Open 跑迁移（空表上安全：0003 watch 重建 INSERT 空、0004 建 trade_review、0005 灌参数）
	st, err := store.Open(scratch)
	if err != nil {
		return err
	}
	defer st.Close()

	// 阶段三：迁移后所有事务态表已空（迁移不触 box/signal/daily_bar 等），保险再清一次（FK ON，空表无风险）
	var wiped int64
	for _, t := range wipeTables {
		res, err := st.DB().Exec("DELETE FROM " + t)
		if err != nil {
			return fmt.Errorf("清空 %s: %w", t, err)
		}
		n, _ := res.RowsAffected()
		wiped += n
	}
	if progressive {
		res, err := st.DB().Exec(`DELETE FROM daily_bar WHERE date >= ? AND code != 'sh000001'`, from)
		if err != nil {
			return fmt.Errorf("裁剪未来K线: %w", err)
		}
		n, _ := res.RowsAffected()
		fmt.Printf("[bt] bootstrap(%s): 已拷贝源库，清空事务态 %d 行，裁掉窗口后K线 %d 行\n",
			map[bool]string{true: "progressive", false: "prefill"}[progressive], wiped, n)
	} else {
		fmt.Printf("[bt] bootstrap(prefill): 已拷贝源库，清空事务态 %d 行，K线全量保留（未来数据在场）\n", wiped)
	}
	return nil
}

// rawWipeExisting 用 raw 连接（默认 FK OFF）清空 scratch 中已存在的事务态表。
// 仅 DELETE sqlite_master 中实际存在的表（源库 schema 版本可能未含 trade_review）。
func rawWipeExisting(scratch string) error {
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(scratch))
	if err != nil {
		return fmt.Errorf("raw open scratch: %w", err)
	}
	defer db.Close()
	// 枚举已存在的事务态表
	rows, err := db.Query(`SELECT name FROM sqlite_master WHERE type='table' AND name IN
		('trade_review','trade_fill','position_leg','position','plan_order','trade_plan',
		 'signal','equity_snapshot','candidate','watch','box','daily_run')`)
	if err != nil {
		return fmt.Errorf("枚举事务态表: %w", err)
	}
	var tables []string
	for rows.Next() {
		var t string
		if err := rows.Scan(&t); err != nil {
			rows.Close()
			return err
		}
		tables = append(tables, t)
	}
	rows.Close()
	// FK OFF（默认）：box 删除不受 watch 孤儿引用阻塞
	for _, t := range tables {
		if _, err := db.Exec("DELETE FROM " + t); err != nil {
			return fmt.Errorf("raw 清空 %s: %w", t, err)
		}
	}
	return nil
}

// copyFile 整库拷贝（源库 WAL 已确认为空，主文件即完整快照）。
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	buf := make([]byte, 8*1024*1024)
	if _, err := io.CopyBuffer(out, in, buf); err != nil {
		return err
	}
	return out.Sync()
}

// ---------------------------------------------------------------------------
// 回放循环（Replay）
// ---------------------------------------------------------------------------

// Replay 按交易日历逐日重放六任务链（生产代码路径 pipeline.RerunTask）。
// progressive 模式在每个回放日前把该日K线从源库灌入（ingest 的回放等价物）。
// 返回逐日箱体状态哈希（差分审计用）与失败任务清单。
func Replay(scratch string, days []string, dayBars map[string][]contract.DailyBar) (map[string]uint64, []string, error) {
	st, err := store.Open(scratch)
	if err != nil {
		return nil, nil, err
	}
	defer st.Close()

	// 回放日报绝不写生产 var/reports（archive/07 #5）：按 scratch 名派生输出目录
	reportDir := strings.TrimSuffix(scratch, ".db") + "_reports"
	pipe := pipeline.New(st, nil) // ingest 不在回放链上，ingester 传 nil（禁用 ingest 任务）
	pipe.SetReportDir(reportDir)
	matcher := match.New(st) // M8.2 撮合器：report 之前撮合当日订单

	ctx := context.Background()
	hashes := make(map[string]uint64, len(days))
	var fails []string
	for _, d := range days {
		if dayBars != nil { // progressive：先灌当日K线再跑链
			if _, err := st.BarRepo.UpsertBars(dayBars[d]); err != nil {
				return nil, nil, fmt.Errorf("%s 灌当日K线: %w", d, err)
			}
		}
		var rows []string
		for _, task := range sixTasks {
			if task == "report" {
				// M8.2：report 之前撮合当日订单（match 插在 plan 之后、report 之前，archive/08 §5）
				if n, err := matcher.Match(ctx, d); err != nil {
					fails = append(fails, fmt.Sprintf("%s match: %v", d, err))
					rows = append(rows, "match=FAIL")
				} else if n > 0 {
					rows = append(rows, fmt.Sprintf("match=%d", n))
				}
			}
			res, err := pipe.RerunTask(ctx, d, task)
			if err != nil || res.Status != "ok" {
				msg := fmt.Sprintf("%s %s: status=%s err=%v", d, task, res.Status, err)
				fails = append(fails, msg)
				rows = append(rows, task+"=FAIL")
				continue
			}
			rows = append(rows, fmt.Sprintf("%s=%d", task, res.AffectedRows))
		}
		h, err := boxStateHash(st)
		if err != nil {
			return nil, nil, fmt.Errorf("%s 箱体哈希: %w", d, err)
		}
		hashes[d] = h
		fmt.Printf("[bt] %s  %s\n", d, strings.Join(rows, " "))
	}
	return hashes, fails, nil
}

// boxStateHash 箱体全表语义哈希（含 id：两模式插入顺序一致，id 分叉即状态分叉）。
func boxStateHash(st *store.Store) (uint64, error) {
	rows, err := st.DB().Query(`SELECT id, code, top, bottom,
		COALESCE(CAST(top_confirmed AS TEXT),''), COALESCE(CAST(bottom_confirmed AS TEXT),''),
		status, COALESCE(prev_box_id,0), COALESCE(CAST(broken_date AS TEXT),'')
		FROM box ORDER BY id`)
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	h := fnvNew()
	for rows.Next() {
		var id, prev int64
		var code, topC, botC, status, broken string
		var top, bot float64
		if err := rows.Scan(&id, &code, &top, &bot, &topC, &botC, &status, &prev, &broken); err != nil {
			return 0, err
		}
		h = fnvWrite(h, fmt.Sprintf("%d|%s|%s|%s|%s|%s|%s|%d|%s\n",
			id, code, fnum(top), fnum(bot), topC, botC, status, prev, broken))
	}
	return h, rows.Err()
}

// ---------------------------------------------------------------------------
// 差分比较（Compare）
// ---------------------------------------------------------------------------

// Compare 逐项比较两座 scratch 库的全部决策输出。返回差异清单（空=全等）。
// 比较项：逐日箱体哈希、box/candidate/watch/signal/trade_plan/plan_order/
// equity_snapshot/position/daily_run 语义 dump、fill/leg/review 行数。
// 不比较：自增 id 的绝对值之外的时序产物（created_at 时间戳、耗时、HTML 文案）。
func Compare(dbA, dbB string, hashA, hashB map[string]uint64) []string {
	var diffs []string
	// 1) 逐日箱体哈希：定位首个分叉日
	var days []string
	for d := range hashA {
		days = append(days, d)
	}
	sort.Strings(days)
	for _, d := range days {
		if hashA[d] != hashB[d] {
			diffs = append(diffs, fmt.Sprintf("箱体哈希分叉于 %s（progressive=%#x prefill=%#x）——首个偷读未来数据的任务在 %s 当日", d, hashA[d], hashB[d], d))
			break
		}
	}
	// 2) 决策表语义 dump
	a, err := openScratchDump(dbA)
	if err != nil {
		return append(diffs, "打开 "+dbA+" 失败: "+err.Error())
	}
	defer a.Close()
	b, err := openScratchDump(dbB)
	if err != nil {
		return append(diffs, "打开 "+dbB+" 失败: "+err.Error())
	}
	defer b.Close()

	linesA, err := dumpDecisionState(a)
	if err != nil {
		return append(diffs, "dump "+dbA+" 失败: "+err.Error())
	}
	linesB, err := dumpDecisionState(b)
	if err != nil {
		return append(diffs, "dump "+dbB+" 失败: "+err.Error())
	}
	n := len(linesA)
	if len(linesB) > n {
		n = len(linesB)
	}
	table := ""
	for i := 0; i < n; i++ {
		la, lb := "<缺失>", "<缺失>"
		if i < len(linesA) {
			la = linesA[i]
		}
		if i < len(linesB) {
			lb = linesB[i]
		}
		if strings.HasPrefix(la, "== ") {
			table = la
		}
		if la != lb {
			diffs = append(diffs, fmt.Sprintf("[%s] progressive: %s | prefill: %s", table, la, lb))
		}
	}
	return diffs
}

// openScratchDump 打开回放完成的 scratch 库（重放连接已关闭，WAL 已落盘）。
func openScratchDump(path string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro")
	if err != nil {
		return nil, err
	}
	if err := db.Ping(); err != nil {
		db.Close()
		db, err = sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro&immutable=1")
		if err != nil {
			return nil, err
		}
		if err := db.Ping(); err != nil {
			db.Close()
			return nil, err
		}
	}
	return db, nil
}

// dumpDecisionState 决策输出语义 dump（铁律 5：日期列一律 CAST AS TEXT）。
// 逐行返回带表头的有序行，供逐行 diff。
func dumpDecisionState(db *sql.DB) ([]string, error) {
	var out []string
	dump := func(header, query string, scanCols int) error {
		out = append(out, "== "+header)
		rows, err := db.Query(query)
		if err != nil {
			return fmt.Errorf("%s: %w", header, err)
		}
		defer rows.Close()
		for rows.Next() {
			vals := make([]any, scanCols)
			ptrs := make([]any, scanCols)
			for i := range vals {
				ptrs[i] = &vals[i]
			}
			if err := rows.Scan(ptrs...); err != nil {
				return fmt.Errorf("%s scan: %w", header, err)
			}
			parts := make([]string, scanCols)
			for i, v := range vals {
				switch t := v.(type) {
				case nil:
					parts[i] = ""
				case float64:
					parts[i] = fnum(t)
				case int64:
					parts[i] = fmt.Sprintf("%d", t)
				case []byte:
					parts[i] = string(t)
				case string:
					parts[i] = t
				default:
					parts[i] = fmt.Sprintf("%v", t)
				}
			}
			out = append(out, strings.Join(parts, "|"))
		}
		return rows.Err()
	}
	if err := dump("box", `SELECT id, code, top, bottom,
		COALESCE(CAST(top_confirmed AS TEXT),''), COALESCE(CAST(bottom_confirmed AS TEXT),''),
		status, COALESCE(prev_box_id,0), COALESCE(CAST(broken_date AS TEXT),'')
		FROM box ORDER BY id`, 9); err != nil {
		return nil, err
	}
	if err := dump("candidate", `SELECT CAST(screen_date AS TEXT), code, score, reasons_json, fundamental_flag
		FROM candidate ORDER BY screen_date, code, id`, 5); err != nil {
		return nil, err
	}
	if err := dump("watch", `SELECT id, code, stage, CAST(added_date AS TEXT), COALESCE(candidate_id,0),
		COALESCE(current_box_id,0), box_count, COALESCE(exit_reason,''), COALESCE(resume_trigger,0), note
		FROM watch ORDER BY id`, 10); err != nil {
		return nil, err
	}
	if err := dump("signal", `SELECT CAST(date AS TEXT), code, type, COALESCE(box_id,0), close_px, box_level, payload_json
		FROM signal ORDER BY date, code, type, id`, 7); err != nil {
		return nil, err
	}
	if err := dump("trade_plan", `SELECT CAST(plan_date AS TEXT), CAST(execute_date AS TEXT), equity_base, open_position_count
		FROM trade_plan ORDER BY plan_date`, 4); err != nil {
		return nil, err
	}
	if err := dump("plan_order", `SELECT CAST(p.plan_date AS TEXT), o.code, o.side, o.order_kind,
		o.trigger_price, o.limit_price, o.qty, COALESCE(o.leg_no,0), o.status, o.reason_json
		FROM plan_order o JOIN trade_plan p ON p.id = o.trade_plan_id
		ORDER BY p.plan_date, o.code, o.side, o.id`, 10); err != nil {
		return nil, err
	}
	if err := dump("equity_snapshot", `SELECT CAST(date AS TEXT), cash, market_value, total, peak_total, drawdown
		FROM equity_snapshot ORDER BY date`, 6); err != nil {
		return nil, err
	}
	if err := dump("position", `SELECT id, code, CAST(opened_date AS TEXT), qty, avg_cost, stop_price,
		COALESCE(entry_box_id,0), status FROM position ORDER BY id`, 8); err != nil {
		return nil, err
	}
	if err := dump("fill/leg/review", `SELECT
		(SELECT COUNT(*) FROM trade_fill), (SELECT COUNT(*) FROM position_leg), (SELECT COUNT(*) FROM trade_review)`, 3); err != nil {
		return nil, err
	}
	if err := dump("daily_run", `SELECT CAST(date AS TEXT), task, status, COALESCE(affected_rows,0)
		FROM daily_run ORDER BY date, task, id`, 4); err != nil {
		return nil, err
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// 源库只读访问
// ---------------------------------------------------------------------------

// openSourceRO 以只读模式打开源库（铁律 7：源库禁止任何写操作；
// mode=ro 由 SQLite 强制，程序错误也写不进去）。
func openSourceRO(path string) (*sql.DB, error) {
	dsn := "file:" + filepath.ToSlash(path) + "?mode=ro"
	db, err := sql.Open("sqlite", dsn)
	if err == nil {
		if err = db.Ping(); err == nil {
			return db, nil
		}
		db.Close()
	}
	// 兜底：-shm 缺失的 WAL 库 ro 打不开时，immutable=1（承诺库不可变）。
	// 仅当源库 WAL 为空时安全——Bootstrap 已确保这一点。
	db2, err2 := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro&immutable=1")
	if err2 == nil {
		if err2 = db2.Ping(); err2 == nil {
			return db2, nil
		}
		db2.Close()
	}
	return nil, fmt.Errorf("源库只读打开失败 %s: %w", path, err)
}

// loadWindowBars 预载窗口内全部非指数K线，按日期分组（progressive 逐日灌入用）。
func loadWindowBars(src, from, to string) (map[string][]contract.DailyBar, error) {
	db, err := openSourceRO(src)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	rows, err := db.Query(`SELECT CAST(date AS TEXT), code, open, high, low, close, volume, amount
		FROM daily_bar WHERE date >= ? AND date <= ? AND code != 'sh000001'
		ORDER BY code, date`, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][]contract.DailyBar{}
	for rows.Next() {
		var b contract.DailyBar
		if err := rows.Scan(&b.Date, &b.Code, &b.Open, &b.High, &b.Low, &b.Close, &b.Volume, &b.Amount); err != nil {
			return nil, err
		}
		out[b.Date] = append(out[b.Date], b)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

// fnum 浮点数规范化文本（round-trip 精确，两模式同值必得同串）。
func fnum(v float64) string {
	return strconv.FormatFloat(v, 'g', -1, 64)
}

// fnv-1a 64 位哈希（无依赖、确定性）。
const (
	fnvOffset = 14695981039346656037
	fnvPrime  = 1099511628211
)

func fnvNew() uint64 { return fnvOffset }

func fnvWrite(h uint64, s string) uint64 {
	for i := 0; i < len(s); i++ {
		h ^= uint64(s[i])
		h *= fnvPrime
	}
	return h
}
