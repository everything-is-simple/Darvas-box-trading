package backtest

import (
	"database/sql"
	"fmt"
	"math"
	"path/filepath"
	"testing"
	"time"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// TestSyntheticAsOfDifferential 合成数据差分审计（M8.1 首任务的 CI 常驻版）：
//
// 构造能走完「箱体确认 → 入池 → 放量突破 → 信号 → 订单 → 过期回退」全链条的合成序列
// （sh600001）+ 未来才物化的箱体泄漏陷阱股（sh600003）+ 永不达标对照股（sh600002），
// 对同一窗口分别以 progressive（未来K线物理缺席）与 prefill（全历史在场）重放，
// 断言全部决策输出全等——任何任务偷读未来K线都会导致 dump 分叉。
//
// 机制断言锁定 docs/07 #1 方案A 修复 + M8.2 撮合后的语义（2026-10-19）：
// signal/poolsync/planner 改用 LatestNonFormingBox 后，突破日 t15 出 breakout 信号 →
// 买单（leg1，exec t16）→ M8.2 撮合器在 t16 盘中上穿 trigger 成交 → watch 持仓化 holding。
// 修复前四类信号全死（signal=0）；M8.2 撮合前该买单会在 t17 过期（无成交、watch 回 watching）。
func TestSyntheticAsOfDifferential(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.db")
	buildSyntheticSource(t, src)

	const fromI, toI = 8, 24 // 窗口 t8..t24（17 个回放日）
	from, to := d(fromI), d(toI)
	_, _, days, err := resolveWindow(Options{From: from, To: to, SrcDB: src})
	if err != nil {
		t.Fatalf("resolveWindow: %v", err)
	}
	if len(days) != 17 {
		t.Fatalf("窗口交易日数 = %d, 期望 17", len(days))
	}

	// 地面真值：progressive
	dayBars, err := loadWindowBars(src, from, to)
	if err != nil {
		t.Fatalf("loadWindowBars: %v", err)
	}
	prog := filepath.Join(dir, "bt_progressive.db")
	if err := Bootstrap(src, prog, from, true); err != nil {
		t.Fatalf("bootstrap progressive: %v", err)
	}
	progHashes, fails, err := Replay(prog, days, dayBars)
	if err != nil {
		t.Fatalf("replay progressive: %v", err)
	}
	if len(fails) > 0 {
		t.Fatalf("progressive 回放失败: %v", fails)
	}

	// 对照组：prefill（未来K线物理在场）
	pre := filepath.Join(dir, "bt_prefill.db")
	if err := Bootstrap(src, pre, from, false); err != nil {
		t.Fatalf("bootstrap prefill: %v", err)
	}
	preHashes, fails, err := Replay(pre, days, nil)
	if err != nil {
		t.Fatalf("replay prefill: %v", err)
	}
	if len(fails) > 0 {
		t.Fatalf("prefill 回放失败: %v", fails)
	}

	// as-of 差分：必须零差异
	diffs := Compare(prog, pre, progHashes, preHashes)
	if len(diffs) > 0 {
		t.Fatalf("as-of 差分审计不通过（%d 处差异）:\n%s", len(diffs), joinN(diffs, 20))
	}

	assertMechanics(t, pre, days)
}

// assertMechanics 锁定 #1 方案A 修复后的确定性行为（docs/07 #1-#3 修复后）。
func assertMechanics(t *testing.T, scratch string, days []string) {
	t.Helper()
	db, err := openScratchDump(scratch)
	if err != nil {
		t.Fatalf("打开 scratch: %v", err)
	}
	defer db.Close()

	one := func(q string, args ...any) *sql.Row { return db.QueryRow(q, args...) }

	// ① 信号表：1 条 breakout@t15 sh600001（#1 修复前为 0——死路已通）
	var nSig int
	if err := one(`SELECT COUNT(*) FROM signal`).Scan(&nSig); err != nil || nSig != 1 {
		t.Fatalf("signal 行数=%d err=%v（期望 1——breakout@t15，docs/07 #1 修复后）", nSig, err)
	}
	var sigDate, sigCode, sigType string
	if err := one(`SELECT CAST(date AS TEXT), code, type FROM signal ORDER BY date LIMIT 1`).
		Scan(&sigDate, &sigCode, &sigType); err != nil {
		t.Fatalf("读 signal: %v", err)
	}
	if sigDate != d(15) || sigCode != "sh600001" || sigType != "breakout" {
		t.Fatalf("signal = %s/%s/%s, 期望 t15/sh600001/breakout", sigDate, sigCode, sigType)
	}

	// ② sh600001 阶梯：确认→突破（broken_up@t15）→ 第二箱确认（11.8/11.0）
	var nBroken int
	if err := one(`SELECT COUNT(*) FROM box WHERE code='sh600001' AND status='broken_up'
		AND CAST(broken_date AS TEXT)=?`, d(15)).Scan(&nBroken); err != nil || nBroken != 1 {
		t.Fatalf("sh600001 broken_up@t15 = %d err=%v", nBroken, err)
	}
	var nBox2 int
	if err := one(`SELECT COUNT(*) FROM box WHERE code='sh600001' AND top=? AND bottom=? AND status='confirmed'`,
		11.8, 11.0).Scan(&nBox2); err != nil || nBox2 != 1 {
		t.Fatalf("sh600001 第二箱(11.8/11.0) confirmed = %d err=%v", nBox2, err)
	}

	// ③ 候选池：t13/t14 有（第一箱确认），t15~t20 断档（LatestBox=forming），
	//    t21~ 恢复（第二箱确认）；sh600003 只在 t20 出现（箱体在 t20 才物化）
	countCand := func(code, d string) int {
		var n int
		if err := one(`SELECT COUNT(*) FROM candidate WHERE code=? AND CAST(screen_date AS TEXT)=?`,
			code, d).Scan(&n); err != nil {
			t.Fatalf("candidate 查询 %s %s: %v", code, d, err)
		}
		return n
	}
	for _, dd := range []string{d(13), d(14), d(21), d(22), d(23), d(24)} {
		if got := countCand("sh600001", dd); got != 1 {
			t.Fatalf("sh600001 candidate@%s = %d, 期望 1", dd, got)
		}
	}
	for _, dd := range []string{d(15), d(16), d(17), d(18), d(19), d(20)} {
		if got := countCand("sh600001", dd); got != 0 {
			t.Fatalf("sh600001 candidate@%s = %d, 期望 0（forming 箱体不可选）", dd, got)
		}
	}
	if got := countCand("sh600003", d(20)); got != 1 {
		t.Fatalf("sh600003 candidate@t20 = %d, 期望 1（t20 才物化——若 >0 提前出现即 as-of 泄漏）", got)
	}
	if got := countCand("sh600002", d(24)); got != 0 {
		t.Fatalf("sh600002 对照股不应有候选，got %d", got)
	}

	// ④ 跟踪池：t13/t20 入池；sh600001 经历 watching→pending(t15 买单)→holding(t16 撮合成交)；
	//    sh600003 保持 watching（无人退出——本合成无跌破/flat_too_long 触发）
	type wrow struct {
		code, stage, added string
	}
	rows, err := db.Query(`SELECT code, stage, CAST(added_date AS TEXT) FROM watch ORDER BY code`)
	if err != nil {
		t.Fatal(err)
	}
	var ws []wrow
	for rows.Next() {
		var w wrow
		if err := rows.Scan(&w.code, &w.stage, &w.added); err != nil {
			t.Fatal(err)
		}
		ws = append(ws, w)
	}
	rows.Close()
	if len(ws) != 2 {
		t.Fatalf("watch 行数 = %d, 期望 2（sh600001+sh600003）", len(ws))
	}
	for _, w := range ws {
		switch w.code {
		case "sh600001":
			if w.stage != "holding" {
				t.Fatalf("sh600001 watch stage = %s, 期望 holding（t16 撮合成交→持仓化）", w.stage)
			}
		case "sh600003":
			if w.stage != "watching" {
				t.Fatalf("sh600003 watch stage = %s, 期望 watching", w.stage)
			}
		}
	}
	if ws[0].added != d(13) || ws[1].added != d(20) {
		t.Fatalf("入池日错: %+v（期望 sh600001@t13, sh600003@t20）", ws)
	}

	// ⑤ 计划与权益：每日一份计划；t15 breakout 信号 → 1 笔买单（leg1，exec t16），
	//    M8.2 撮合器 t16 成交（status=filled）；权益 ≈ 本金−买入支出+市值 ≈ 999774
	var nPlan, nOrder, nEq int
	if err := one(`SELECT COUNT(*) FROM trade_plan`).Scan(&nPlan); err != nil || nPlan != len(days) {
		t.Fatalf("trade_plan = %d err=%v, 期望 %d", nPlan, err, len(days))
	}
	if err := one(`SELECT COUNT(*) FROM plan_order`).Scan(&nOrder); err != nil || nOrder != 1 {
		t.Fatalf("plan_order = %d err=%v, 期望 1（t15 breakout → leg1 买单）", nOrder, err)
	}
	// 订单明细：buy/leg1/filled，执行日 t16，数量 1700（M8.8 单位制：1单位=20000元，
	//   触发价 11.33 → 1单位股数=floor(20000/11.33/100)*100=1700）
	var oSide, oKind, oStatus string
	var oQty, oLeg int
	var oExec string
	if err := one(`SELECT o.side, o.order_kind, o.qty, COALESCE(o.leg_no,0), o.status, CAST(p.execute_date AS TEXT)
		FROM plan_order o JOIN trade_plan p ON p.id=o.trade_plan_id LIMIT 1`).
		Scan(&oSide, &oKind, &oQty, &oLeg, &oStatus, &oExec); err != nil {
		t.Fatalf("读 plan_order: %v", err)
	}
	if oSide != "buy" || oKind != "stop_buy" || oLeg != 1 || oStatus != "filled" || oExec != d(16) {
		t.Fatalf("plan_order = %s/%s/leg%d/%s/exec%s, 期望 buy/stop_buy/leg1/filled/exec%s", oSide, oKind, oLeg, oStatus, oExec, d(16))
	}
	if oQty != 1700 {
		t.Fatalf("plan_order qty = %d, 期望 1700（M8.8 单位制：20000/11.33 取整手）", oQty)
	}
	if err := one(`SELECT COUNT(*) FROM equity_snapshot`).Scan(&nEq); err != nil || nEq != len(days) {
		t.Fatalf("equity_snapshot = %d err=%v, 期望 %d", nEq, err, len(days))
	}
	var eq float64
	if err := one(`SELECT total FROM equity_snapshot ORDER BY date DESC LIMIT 1`).Scan(&eq); err != nil || math.Abs(eq-999773.81) > 1 {
		t.Fatalf("期末权益 = %v err=%v, 期望 ≈999773.81（t16 成交 1700@11.33 + 手续费，t24 收盘 11.2）", eq, err)
	}

	// ⑥ 成交与持仓（M8.2 撮合）：t16 成交 1 笔买入，sh600001 开仓 1700 股
	var nFill int
	if err := one(`SELECT COUNT(*) FROM trade_fill`).Scan(&nFill); err != nil || nFill != 1 {
		t.Fatalf("trade_fill = %d err=%v, 期望 1（t16 buy 1700）", nFill, err)
	}
	var fCode, fSide string
	var fPx float64
	var fQty int
	if err := one(`SELECT code, side, price, qty FROM trade_fill LIMIT 1`).
		Scan(&fCode, &fSide, &fPx, &fQty); err != nil {
		t.Fatalf("读 trade_fill: %v", err)
	}
	if fCode != "sh600001" || fSide != "buy" || fQty != 1700 || math.Abs(fPx-11.33) > 1e-6 {
		t.Fatalf("fill = %s/%s/%d@%.4f, 期望 sh600001/buy/1700@11.33", fCode, fSide, fQty, fPx)
	}
	var nPos int
	if err := one(`SELECT COUNT(*) FROM position WHERE status='open'`).Scan(&nPos); err != nil || nPos != 1 {
		t.Fatalf("open position = %d err=%v, 期望 1", nPos, err)
	}

	// ⑦ 心跳：每日 6 任务全部 ok（回放心跳只落 scratch，铁律 M8-2）
	var nBad int
	if err := one(`SELECT COUNT(*) FROM daily_run WHERE status != 'ok'`).Scan(&nBad); err != nil || nBad != 0 {
		t.Fatalf("daily_run 非 ok 行 = %d err=%v", nBad, err)
	}
	var nRuns int
	if err := one(`SELECT COUNT(*) FROM daily_run`).Scan(&nRuns); err != nil || nRuns != 6*len(days) {
		t.Fatalf("daily_run = %d err=%v, 期望 %d", nRuns, err, 6*len(days))
	}
}

// TestBootstrapGuards 引导护栏：拒用生产库/源库同路径（铁律 M8-2/铁律 7）。
func TestBootstrapGuards(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src.db")
	buildSyntheticSource(t, src)

	if err := Bootstrap(src, src, "2026-01-01", false); err == nil {
		t.Fatal("scratch==src 应被拒绝")
	}
	if err := Bootstrap(src, filepath.Join(dir, "darvas.db"), "2026-01-01", false); err == nil {
		t.Fatal("darvas.db 应被拒绝")
	}
	if err := Bootstrap(src, filepath.Join(dir, "perf_test.db"), "2026-01-01", false); err == nil {
		t.Fatal("perf_test.db 应被拒绝")
	}
	// 合法路径：可重复引导（重建）
	good := filepath.Join(dir, "bt_test.db")
	if err := Bootstrap(src, good, "2026-01-01", true); err != nil {
		t.Fatalf("合法引导失败: %v", err)
	}
	if err := Bootstrap(src, good, "2026-01-01", false); err != nil {
		t.Fatalf("重复引导（模式切换重建）失败: %v", err)
	}
}

// ---------------------------------------------------------------------------
// 合成数据源
// ---------------------------------------------------------------------------

// d 第 i 个交易日（t1=2026-07-01，日历连续；交易日历由 sh000001 K线定义）。
func d(i int) string {
	base := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	return base.AddDate(0, 0, i-1).Format("2006-01-02")
}

// buildSyntheticSource 构造合成源库：
//   - sh000001  指数（日历权威 + RS 分母），30 日平盘
//   - sh600001  全链条主角：t1-t6 升至 11.0 → t9 上轨确认 → t13 下轨确认（t13 放量
//     触发预过滤→入箱入池）→ t15 放量突破（close 11.5 > 11.33）→ t21 第二箱确认
//   - sh600002  对照组：平盘窄幅（宽度不足→箱体永远 invalid），永不入候选
//   - sh600003  泄漏陷阱：t14 已确认箱体，但 t20 才放量物化入箱——prefill 若偷读
//     未来，该箱体会提前出现，差分立即可见
func buildSyntheticSource(t *testing.T, path string) {
	t.Helper()
	st, err := store.Open(path)
	if err != nil {
		t.Fatalf("建源库: %v", err)
	}
	defer func() {
		if _, err := st.DB().Exec(`PRAGMA wal_checkpoint(TRUNCATE)`); err != nil {
			t.Fatalf("checkpoint 源库: %v", err)
		}
		st.Close()
	}()

	securities := [][6]string{
		{"sh000001", "sh", "上证指数", "index", "main", "active"},
		{"sh600001", "sh", "主角股", "stock", "main", "active"},
		{"sh600002", "sh", "对照股", "stock", "main", "active"},
		{"sh600003", "sh", "陷阱股", "stock", "main", "active"},
	}
	for _, s := range securities {
		if _, err := st.DB().Exec(`INSERT INTO security(code, market, name, kind, board, status)
			VALUES (?,?,?,?,?,?)`, s[0], s[1], s[2], s[3], s[4], s[5]); err != nil {
			t.Fatalf("插证券 %s: %v", s[0], err)
		}
	}

	var bars []contract.DailyBar
	for i := 1; i <= 30; i++ {
		bars = append(bars, bar("sh000001", i, 2995, 3010, 2990, 3000, 100000))
		bars = append(bars, bar("sh600002", i, 9.98, 10.05, 9.95, 10.0, 60000))
		bars = append(bars, synth600001(i)...)
		bars = append(bars, synth600003(i)...)
	}
	if _, err := st.BarRepo.UpsertBars(bars); err != nil {
		t.Fatalf("灌K线: %v", err)
	}
}

func synth600001(i int) []contract.DailyBar {
	var o, h, l, c float64
	v := int64(50000)
	switch {
	case i <= 6:
		h = 8.0 + 0.6*float64(i-1)
		c, o, l = h-0.1, h-0.15, h-0.3
	case i <= 9:
		o, h, l, c = 10.6, 10.8, 10.3, 10.5
	case i == 10:
		o, h, l, c = 10.5, 10.7, 9.85, 10.4
	case i <= 13:
		o, h, l, c = 10.6, 10.9, 10.0, 10.5
		if i == 13 {
			v = 150000 // 预过滤触发：箱体确认日放量 → 物化入箱
		}
	case i == 14:
		o, h, l, c = 10.5, 10.9, 10.1, 10.6
	case i == 15:
		o, h, l, c = 11.0, 11.8, 10.9, 11.5 // 突破：close 11.5 > 11.0×1.03
		v = 200000                          // 放量 ≥ 2×50日均量
	default:
		o, h, l, c = 11.3, 11.6, 11.0, 11.2 // 第二箱 t18 上轨确认 / t21 下轨确认
	}
	return []contract.DailyBar{bar("sh600001", i, o, h, l, c, v)}
}

func synth600003(i int) []contract.DailyBar {
	var o, h, l, c float64
	v := int64(50000)
	switch {
	case i <= 7:
		h = 7.8 + 0.2*float64(i-1)
		c, o, l = h-0.1, h-0.15, h-0.25
	case i <= 10:
		o, h, l, c = 8.65, 8.9, 8.7, 8.6 // t10 上轨确认（9.0），bottomCandidate=8.7
	case i == 11:
		o, h, l, c = 8.6, 8.8, 8.4, 8.5 // 新低候选 8.4
	case i <= 14:
		o, h, l, c = 8.6, 8.8, 8.5, 8.6 // t14 下轨确认 → 宽度 6.7% 达标
		if i == 14 {
			// 注意：t14 不放量——箱体虽确认但不物化，留待 t20（泄漏陷阱）
		}
	case i == 20:
		o, h, l, c = 8.6, 8.8, 8.5, 8.6
		v = 150000 // 预过滤触发：t20 才物化
	default:
		o, h, l, c = 8.6, 8.8, 8.5, 8.6
	}
	return []contract.DailyBar{bar("sh600003", i, o, h, l, c, v)}
}

func bar(code string, i int, o, h, l, c float64, v int64) contract.DailyBar {
	return contract.DailyBar{
		Code: code, Date: d(i), Open: o, High: h, Low: l, Close: c,
		Volume: v, Amount: c * float64(v) * 100,
	}
}

func joinN(ss []string, n int) string {
	out := ""
	for i, s := range ss {
		if i >= n {
			out += fmt.Sprintf("  ……（其余 %d 处省略）\n", len(ss)-n)
			break
		}
		out += "  " + s + "\n"
	}
	return out
}
