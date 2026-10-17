package store

import (
	"strings"
	"testing"
)

// seedClosedPosition 构造一笔已清仓交易（realized_pnl 直接写入，绕开 fills 路径）。
func seedClosedPosition(t *testing.T, st *Store, code, opened, closed string, pnl float64) int64 {
	t.Helper()
	res, err := st.DB().Exec(`INSERT INTO position(code, opened_date, qty, avg_cost, stop_price, status,
		closed_date, close_reason, realized_pnl) VALUES (?, ?, 100, 24, 23, 'closed', ?, 'stop', ?)`,
		code, opened, closed, pnl)
	if err != nil {
		t.Fatal(err)
	}
	id, err := res.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	return id
}

// TestAddReview B3 复盘录入：UPSERT 每笔 position 一条（重录覆盖）；position 不存在报错。
func TestAddReview(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	posID := seedClosedPosition(t, st, "sh600519", "2026-10-01", "2026-10-20", -500)

	// 首次录入
	if err := st.TradeRepo.AddReview(posID, "violated", "不止损", "跌破止损犹豫未卖，亏损扩大"); err != nil {
		t.Fatal(err)
	}
	rows, err := st.TradeRepo.ReviewDetail("sh600519")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("明细行数=%d want 1", len(rows))
	}
	d := rows[0]
	if d.RuleFollowed != "violated" || d.ErrorType != "不止损" || !strings.Contains(d.Lesson, "犹豫") {
		t.Fatalf("复盘字段未落库: %+v", d)
	}
	if d.PositionID != posID || d.OpenedDate != "2026-10-01" || d.ClosedDate != "2026-10-20" {
		t.Fatalf("交易字段不对: %+v", d)
	}

	// 重录覆盖（原书：复盘可以修正，但每笔交易只有一条）
	if err := st.TradeRepo.AddReview(posID, "followed", "", "复盘修正：止损执行了，是入场太晚"); err != nil {
		t.Fatal(err)
	}
	rows, _ = st.TradeRepo.ReviewDetail("sh600519")
	if rows[0].RuleFollowed != "followed" || rows[0].ErrorType != "" {
		t.Fatalf("重录未覆盖: %+v", rows[0])
	}

	// position 不存在 → 报错（code 反查无行）
	if err := st.TradeRepo.AddReview(99999, "", "", "x"); err == nil {
		t.Fatal("position 99999 不存在应报错")
	}
}

// TestReviewSummary B3 战绩汇总：每股交易次数/胜负/总盈亏/最新教训（只统计 closed）。
func TestReviewSummary(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519", "sz000001")
	p1 := seedClosedPosition(t, st, "sh600519", "2026-08-01", "2026-08-10", -500)
	seedClosedPosition(t, st, "sh600519", "2026-09-01", "2026-09-10", +800)
	seedClosedPosition(t, st, "sz000001", "2026-09-01", "2026-09-10", +300)
	// open 持仓不计入战绩
	st.DB().Exec(`INSERT INTO position(code, opened_date, qty, avg_cost, stop_price, status)
		VALUES ('sh600519', '2026-10-01', 100, 24, 23, 'open')`)
	// 仅一笔写教训（另一笔留空 → 汇总取最新一条）
	must(t, st.TradeRepo.AddReview(p1, "violated", "追高", "突破第5日才追，买在顶部"))

	// 全部汇总
	rows, err := st.TradeRepo.ReviewSummary("")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 {
		t.Fatalf("汇总股票数=%d want 2", len(rows))
	}
	var m519 *ReviewSummaryRow
	for i := range rows {
		if rows[i].Code == "sh600519" {
			m519 = &rows[i]
		}
	}
	if m519 == nil {
		t.Fatal("缺 sh600519 汇总")
	}
	if m519.Trades != 2 || m519.Wins != 1 || m519.Losses != 1 {
		t.Fatalf("sh600519 战绩 %d笔%d胜%d负 want 2笔1胜1负", m519.Trades, m519.Wins, m519.Losses)
	}
	if m519.TotalPnl < 299.99 || m519.TotalPnl > 300.01 {
		t.Fatalf("累计盈亏=%.2f want 300.00", m519.TotalPnl)
	}
	if !strings.Contains(m519.Lesson, "突破第5日") {
		t.Fatalf("最新教训未带出: %q", m519.Lesson)
	}

	// 单股过滤
	one, err := st.TradeRepo.ReviewSummary("sz000001")
	if err != nil {
		t.Fatal(err)
	}
	if len(one) != 1 || one[0].Trades != 1 || one[0].Wins != 1 || one[0].Lesson != "" {
		t.Fatalf("sz000001 汇总不对: %+v", one)
	}
}

// TestTrackRecordByCode B3 再纳入提示的数据源：交易笔数/亏损笔数/累计盈亏。
func TestTrackRecordByCode(t *testing.T) {
	st := newTestStore(t)
	seedSecs(t, st, "sh600519")
	seedClosedPosition(t, st, "sh600519", "2026-08-01", "2026-08-10", -500)
	seedClosedPosition(t, st, "sh600519", "2026-09-01", "2026-09-10", +200)

	trades, losses, pnl, err := st.TradeRepo.TrackRecordByCode("sh600519")
	if err != nil {
		t.Fatal(err)
	}
	if trades != 2 || losses != 1 {
		t.Fatalf("trades=%d losses=%d want 2/1", trades, losses)
	}
	if pnl < -300.01 || pnl > -299.99 {
		t.Fatalf("累计盈亏=%.2f want -300.00", pnl)
	}

	// 从未交易 → 0/0/0（不报错）
	trades, losses, pnl, err = st.TradeRepo.TrackRecordByCode("sz000002")
	if err != nil || trades != 0 || losses != 0 || pnl != 0 {
		t.Fatalf("未交易股票应 0/0/0 无错: %d/%d/%f/%v", trades, losses, pnl, err)
	}
}
