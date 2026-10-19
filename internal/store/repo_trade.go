package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"

	"darvas/internal/contract"
)

// TradeRepo position/plan/fill/ledger 仓储（docs/04-api-contract.md §3.3）。
type TradeRepo struct{ db *sql.DB }

// NewTradeRepo 构造。
func NewTradeRepo(db *sql.DB) *TradeRepo { return &TradeRepo{db: db} }

// CreatePlan 创建交易计划头，返回 plan id（UNIQUE(plan_date) 冲突时重置）。
func (r *TradeRepo) CreatePlan(p contract.TradePlan) (int64, error) {
	_, err := r.db.Exec(`INSERT INTO trade_plan(plan_date, execute_date, equity_base, open_position_count)
		VALUES (?, ?, ?, ?)
		ON CONFLICT(plan_date) DO UPDATE SET
			execute_date=excluded.execute_date, equity_base=excluded.equity_base,
			open_position_count=excluded.open_position_count`,
		p.PlanDate, p.ExecuteDate, p.EquityBase, p.OpenPositionCount)
	if err != nil {
		return 0, err
	}
	var id int64
	r.db.QueryRow(`SELECT id FROM trade_plan WHERE plan_date = ?`, p.PlanDate).Scan(&id)
	return id, err
}

// AddOrders 批量追加订单（reason map → reason_json）。
func (r *TradeRepo) AddOrders(planID int64, orders []contract.PlanOrder) error {
	if len(orders) == 0 {
		return nil
	}
	tx, err := r.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`INSERT INTO plan_order(trade_plan_id, code, side, order_kind,
		trigger_price, limit_price, qty, leg_no, reason_json, status)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, o := range orders {
		j := "{}"
		if o.Reason != nil {
			if b, err := json.Marshal(o.Reason); err == nil {
				j = string(b)
			}
		}
		leg := nullInt64(int64(o.LegNo))
		if _, err := stmt.Exec(planID, o.Code, o.Side, o.OrderKind,
			o.TriggerPrice, o.LimitPrice, o.Qty, leg, j); err != nil {
			return fmt.Errorf("add order %s %s: %w", o.Code, o.Side, err)
		}
	}
	return tx.Commit()
}

// PlanOn 取某日计划头 + 订单清单（GUI 计划视图 / 六问答案）。
func (r *TradeRepo) PlanOn(date string) (*contract.TradePlan, []contract.PlanOrder, error) {
	var p contract.TradePlan
	var pid int64
	err := r.db.QueryRow(`SELECT id, CAST(plan_date AS TEXT), CAST(execute_date AS TEXT),
			equity_base, open_position_count
		FROM trade_plan WHERE plan_date = ?`, date).
		Scan(&pid, &p.PlanDate, &p.ExecuteDate, &p.EquityBase, &p.OpenPositionCount)
	if err == sql.ErrNoRows {
		return nil, nil, nil
	}
	if err != nil {
		return nil, nil, err
	}
	rows, err := r.db.Query(`SELECT code, side, order_kind, trigger_price, limit_price, qty,
		COALESCE(leg_no,0), reason_json FROM plan_order WHERE trade_plan_id = ? ORDER BY side, code`, pid)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	var out []contract.PlanOrder
	for rows.Next() {
		var o contract.PlanOrder
		var rj string
		if err := rows.Scan(&o.Code, &o.Side, &o.OrderKind, &o.TriggerPrice, &o.LimitPrice,
			&o.Qty, &o.LegNo, &rj); err != nil {
			return nil, nil, err
		}
		o.PlanDate = p.PlanDate
		o.ExecuteDate = p.ExecuteDate
		_ = json.Unmarshal([]byte(rj), &o.Reason)
		out = append(out, o)
	}
	return &p, out, rows.Err()
}

// UpdateOrderStatus 更新订单状态（placed/filled/cancelled/expired）。
func (r *TradeRepo) UpdateOrderStatus(orderID int64, status string) error {
	_, err := r.db.Exec(`UPDATE plan_order SET status = ?, updated_at = datetime('now','localtime') WHERE id = ?`,
		status, orderID)
	return err
}

// AppendFill 成交回录（side+raw_text 留痕审计）。
func (r *TradeRepo) AppendFill(f contract.Fill) error {
	_, err := r.db.Exec(`INSERT INTO trade_fill(plan_order_id, code, date, side, price, qty, fee, raw_text)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		nullInt64(f.PlanOrderID), f.Code, f.Date, f.Side, f.Price, f.Qty, f.Fee, nullEmpty(f.RawText))
	return err
}

// OpenPositions 取全部 open 持仓（GUI 持仓视图）。
func (r *TradeRepo) OpenPositions() ([]contract.Position, error) {
	rows, err := r.db.Query(`SELECT id, code, CAST(opened_date AS TEXT), COALESCE(entry_box_id,0), qty, avg_cost,
		stop_price, status FROM position WHERE status = 'open' ORDER BY code`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.Position
	for rows.Next() {
		var p contract.Position
		if err := rows.Scan(&p.ID, &p.Code, &p.OpenedDate, &p.EntryBoxID, &p.Qty, &p.AvgCost, &p.StopPrice, &p.Status); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// UpdateStop 更新止损位（DB 触发器保证只升不降，docs/03 §2.3）。
func (r *TradeRepo) UpdateStop(positionID int64, newStop float64) error {
	_, err := r.db.Exec(`UPDATE position SET stop_price = ? WHERE id = ?`, newStop, positionID)
	return err
}

// UpsertEquity 权益快照（date PK，重跑覆盖）。
func (r *TradeRepo) UpsertEquity(e contract.EquitySnapshot) error {
	_, err := r.db.Exec(`INSERT INTO equity_snapshot(date, cash, market_value, total, peak_total, drawdown)
		VALUES (?, ?, ?, ?, ?, ?)
		ON CONFLICT(date) DO UPDATE SET
			cash=excluded.cash, market_value=excluded.market_value, total=excluded.total,
			peak_total=excluded.peak_total, drawdown=excluded.drawdown`,
		e.Date, e.Cash, e.MarketValue, e.Total, e.PeakTotal, e.Drawdown)
	return err
}

// LatestEquity 取最新权益快照（planner 仓位计算基准，无则 nil）。
func (r *TradeRepo) LatestEquity() (*contract.EquitySnapshot, error) {
	var e contract.EquitySnapshot
	err := r.db.QueryRow(`SELECT date, cash, market_value, total, peak_total, drawdown
		FROM equity_snapshot ORDER BY date DESC LIMIT 1`).
		Scan(&e.Date, &e.Cash, &e.MarketValue, &e.Total, &e.PeakTotal, &e.Drawdown)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &e, nil
}

// SetEquityDefault 初始化权益（首次运行无持仓时设定基准，planner 用）。
func (r *TradeRepo) SetEquityDefault(date string, total float64) error {
	return r.UpsertEquity(contract.EquitySnapshot{
		Date: date, Cash: total, MarketValue: 0, Total: total, PeakTotal: total, Drawdown: 0,
	})
}

// ---------------------------------------------------------------------------
// 成交查询（账本审计 / fills CLI / GUI 交易记录视图）
// ---------------------------------------------------------------------------

// FillsAll 取全部成交（按日期+ID序，账本重算用）。
func (r *TradeRepo) FillsAll() ([]contract.Fill, error) {
	rows, err := r.db.Query(`SELECT id, COALESCE(plan_order_id,0), code, CAST(date AS TEXT), side, price, qty, fee,
		COALESCE(raw_text,'') FROM trade_fill ORDER BY date, id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanFills(rows)
}

// FillsRange 按日期范围取成交（GUI U2 时间范围查询；from/to 传空=不限）。
func (r *TradeRepo) FillsRange(from, to string) ([]contract.Fill, error) {
	q, args := `SELECT id, COALESCE(plan_order_id,0), code, CAST(date AS TEXT), side, price, qty, fee,
		COALESCE(raw_text,'') FROM trade_fill WHERE 1=1`, []any{}
	if from != "" {
		q += " AND date >= ?"
		args = append(args, from)
	}
	if to != "" {
		q += " AND date <= ?"
		args = append(args, to)
	}
	q += " ORDER BY date, id"
	rows, err := r.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanFills(rows)
}

// FillsOn 取某日成交。
func (r *TradeRepo) FillsOn(date string) ([]contract.Fill, error) {
	rows, err := r.db.Query(`SELECT id, COALESCE(plan_order_id,0), code, CAST(date AS TEXT), side, price, qty, fee,
		COALESCE(raw_text,'') FROM trade_fill WHERE date = ? ORDER BY id`, date)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanFills(rows)
}

// FillExists 幂等粘贴检查：同 code+date+side+price+qty 已存在则视为重复。
func (r *TradeRepo) FillExists(code, date, side string, price float64, qty int) (bool, error) {
	var n int
	err := r.db.QueryRow(`SELECT COUNT(*) FROM trade_fill
		WHERE code=? AND date=? AND side=? AND ABS(price-?)<0.0001 AND qty=?`,
		code, date, side, price, qty).Scan(&n)
	return n > 0, err
}

func scanFills(rows *sql.Rows) ([]contract.Fill, error) {
	var out []contract.Fill
	for rows.Next() {
		var f contract.Fill
		var id int64
		if err := rows.Scan(&id, &f.PlanOrderID, &f.Code, &f.Date, &f.Side, &f.Price, &f.Qty, &f.Fee, &f.RawText); err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------------------
// 计划订单查询（成交匹配 / 状态机）
// ---------------------------------------------------------------------------

// OrderStatus 单条订单状态。
func (r *TradeRepo) OrderStatus(orderID int64) (string, error) {
	var s string
	err := r.db.QueryRow(`SELECT status FROM plan_order WHERE id = ?`, orderID).Scan(&s)
	return s, err
}

// PendingOrder 待成交订单（成交匹配用视图）。
type PendingOrder struct {
	ID          int64
	LimitPrice  float64
	Qty         int
	LegNo       int
	ExecuteDate string
	ReasonJSON  string
}

// MatchOrder 撮合用订单视图（M8.2：execute_date==当日 的全部待成交订单，
// 含 code/side/trigger，撮合器据此用当日 OHLC 判定触发）。
type MatchOrder struct {
	ID           int64
	Code         string
	Side         string
	TriggerPrice float64
	LimitPrice   float64
	Qty          int
	LegNo        int
}

// PendingOrdersForExecuteDate 取执行日==date 且仍可成交的订单（pending/placed），
// 按 side 排序（sell 先于 buy，撮合器先卖后买满足 T+1）。M8.2 撮合入口。
func (r *TradeRepo) PendingOrdersForExecuteDate(date string) ([]MatchOrder, error) {
	rows, err := r.db.Query(`SELECT o.id, o.code, o.side, o.trigger_price, o.limit_price,
			o.qty, COALESCE(o.leg_no,0)
		FROM plan_order o JOIN trade_plan p ON p.id = o.trade_plan_id
		WHERE o.status IN ('pending','placed') AND CAST(p.execute_date AS TEXT) = ?
		ORDER BY CASE o.side WHEN 'sell' THEN 0 ELSE 1 END, o.code, o.id`, date)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []MatchOrder
	for rows.Next() {
		var o MatchOrder
		if err := rows.Scan(&o.ID, &o.Code, &o.Side, &o.TriggerPrice, &o.LimitPrice, &o.Qty, &o.LegNo); err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

// PendingSellCodes 返回已有 pending/placed 卖出单的股票代码（planner 避免重复生成
// 卖出单：time_stop 等持续条件每日重复挂单，撮合清仓后剩余单「卖出无持仓」——M8.3 暴露）。
func (r *TradeRepo) PendingSellCodes() ([]string, error) {
	rows, err := r.db.Query(`SELECT DISTINCT code FROM plan_order
		WHERE side='sell' AND status IN ('pending','placed')`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var c string
		if err := rows.Scan(&c); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// PendingOrdersFor 取某股某方向仍可成交的订单（pending/placed，execute_date≤date，按计划日先后）。
// 成交回录时用它把 fill 关联到 plan_order。
func (r *TradeRepo) PendingOrdersFor(code, side, date string) ([]PendingOrder, error) {
	rows, err := r.db.Query(`SELECT o.id, o.limit_price, o.qty, COALESCE(o.leg_no,0), CAST(p.execute_date AS TEXT), o.reason_json
		FROM plan_order o JOIN trade_plan p ON p.id = o.trade_plan_id
		WHERE o.code = ? AND o.side = ? AND o.status IN ('pending','placed') AND p.execute_date <= ?
		ORDER BY p.execute_date, o.id`, code, side, date)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PendingOrder
	for rows.Next() {
		var o PendingOrder
		if err := rows.Scan(&o.ID, &o.LimitPrice, &o.Qty, &o.LegNo, &o.ExecuteDate, &o.ReasonJSON); err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

// ExpireStaleOrders 把执行日已过仍 pending 的订单置为 expired（每日流水线收尾）。
func (r *TradeRepo) ExpireStaleOrders(today string) (int64, error) {
	res, err := r.db.Exec(`UPDATE plan_order SET status='expired', updated_at=datetime('now','localtime')
		WHERE status='pending' AND trade_plan_id IN (SELECT id FROM trade_plan WHERE execute_date < ?)`, today)
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

// ---------------------------------------------------------------------------
// 持仓写路径（成交回录驱动的状态机，docs/04 §3.3）
// ---------------------------------------------------------------------------

// PositionByCode 取某股 open 持仓（无则 nil）。
func (r *TradeRepo) PositionByCode(code string) (*contract.Position, error) {
	var p contract.Position
	err := r.db.QueryRow(`SELECT id, code, CAST(opened_date AS TEXT), COALESCE(entry_box_id,0), qty, avg_cost,
		stop_price, status FROM position WHERE code = ? AND status = 'open'`, code).
		Scan(&p.ID, &p.Code, &p.OpenedDate, &p.EntryBoxID, &p.Qty, &p.AvgCost, &p.StopPrice, &p.Status)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &p, nil
}

// CreatePosition 开仓（首腿成交）。
func (r *TradeRepo) CreatePosition(p contract.Position) (int64, error) {
	res, err := r.db.Exec(`INSERT INTO position(code, opened_date, entry_box_id, qty, avg_cost, stop_price, status)
		VALUES (?, ?, ?, ?, ?, ?, 'open')`,
		p.Code, p.OpenedDate, nullInt64(p.EntryBoxID), p.Qty, p.AvgCost, p.StopPrice)
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

// UpdatePositionQtyCost 加码/减仓后更新数量与摊薄成本。
func (r *TradeRepo) UpdatePositionQtyCost(id int64, qty int, avgCost float64) error {
	_, err := r.db.Exec(`UPDATE position SET qty = ?, avg_cost = ? WHERE id = ?`, qty, avgCost, id)
	return err
}

// ClosePosition 清仓（qty 归零）。
func (r *TradeRepo) ClosePosition(id int64, date, reason string) error {
	// B2（0003）：清仓时落库本次交易已实现盈亏（两亏拉黑的统计基础）。
	// = 该持仓生命周期内（opened_date~date）该股全部成交的净现金流：卖出所得−买入支出（含费）。
	_, err := r.db.Exec(`UPDATE position SET status='closed', closed_date=?, close_reason=?, qty=0,
		realized_pnl = (
			SELECT COALESCE(SUM(CASE WHEN f.side='sell' THEN f.price*f.qty - f.fee
			                         ELSE -(f.price*f.qty + f.fee) END), 0)
			FROM trade_fill f
			WHERE f.code = position.code AND f.date >= position.opened_date AND f.date <= ?
		) WHERE id=?`, date, reason, date, id)
	return err
}

// LossCountByCode 某证券历史亏损交易次数（closed 且 realized_pnl<0；两亏拉黑判定，B2）。
func (r *TradeRepo) LossCountByCode(code string) (int, error) {
	var n int
	err := r.db.QueryRow(`SELECT COUNT(*) FROM position
		WHERE code = ? AND status = 'closed' AND realized_pnl < 0`, code).Scan(&n)
	return n, err
}

// PositionPnlHistory 某证券全部历史交易战绩（B2/B3：几进几出、累计盈亏、亏损次数）。
// qty 取 position_leg 聚合（清仓后 position.qty 已归零）。
func (r *TradeRepo) PositionPnlHistory(code string) ([]map[string]any, error) {
	rows, err := r.db.Query(`SELECT p.id, CAST(p.opened_date AS TEXT), COALESCE(CAST(p.closed_date AS TEXT),''),
		COALESCE((SELECT SUM(l.qty) FROM position_leg l WHERE l.position_id = p.id), 0),
		p.avg_cost, COALESCE(p.realized_pnl,0), COALESCE(p.close_reason,'')
		FROM position p WHERE p.code = ? AND p.status='closed' ORDER BY p.opened_date`, code)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id int64
		var opened, closed string
		var qty int
		var avgCost, pnl float64
		var reason string
		if err := rows.Scan(&id, &opened, &closed, &qty, &avgCost, &pnl, &reason); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{
			"position_id": id, "opened_date": opened, "closed_date": closed,
			"qty": qty, "avg_cost": avgCost, "realized_pnl": pnl, "close_reason": reason,
		})
	}
	return out, rows.Err()
}

// AddLeg 记录金字塔腿（stop_after 留审计）。
func (r *TradeRepo) AddLeg(positionID int64, legNo int, date string, qty int, price, stopAfter float64, planOrderID int64) error {
	_, err := r.db.Exec(`INSERT INTO position_leg(position_id, leg_no, date, qty, price, stop_after, plan_order_id)
		VALUES (?, ?, ?, ?, ?, ?, ?)`,
		positionID, legNo, date, qty, price, stopAfter, nullInt64(planOrderID))
	return err
}

// LegsByPosition 取持仓腿（GUI 持仓视图 / 审计）。
func (r *TradeRepo) LegsByPosition(positionID int64) ([]map[string]any, error) {
	rows, err := r.db.Query(`SELECT leg_no, date, qty, price, stop_after, COALESCE(plan_order_id,0)
		FROM position_leg WHERE position_id = ? ORDER BY leg_no`, positionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var legNo, qty, planID int64
		var date string
		var price, stopAfter float64
		if err := rows.Scan(&legNo, &date, &qty, &price, &stopAfter, &planID); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{
			"leg_no": legNo, "date": date, "qty": qty, "price": price,
			"stop_after": stopAfter, "plan_order_id": planID,
		})
	}
	return out, rows.Err()
}

// LegCountByCode 当前未平仓持仓的金字塔腿数（用于 signal pyramid 钳制，docs/07 #2）。
// 取 position_leg 数量；position_status='open' 时有效；非持仓/已平仓返回 0。
func (r *TradeRepo) LegCountByCode(code string) (int, error) {
	var n int
	err := r.db.QueryRow(`SELECT COUNT(*) FROM position_leg pl
		JOIN position p ON p.id = pl.position_id
		WHERE p.code = ? AND p.status = 'open'`, code).Scan(&n)
	if err != nil {
		return 0, err
	}
	return n, nil
}

// AllPositions 取全部持仓含 closed（交易记录视图）。
func (r *TradeRepo) AllPositions() ([]contract.Position, error) {
	rows, err := r.db.Query(`SELECT id, code, CAST(opened_date AS TEXT), COALESCE(entry_box_id,0), qty, avg_cost,
		stop_price, status, COALESCE(closed_date,''), COALESCE(close_reason,'')
		FROM position ORDER BY status, opened_date, code`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.Position
	for rows.Next() {
		var p contract.Position
		if err := rows.Scan(&p.ID, &p.Code, &p.OpenedDate, &p.EntryBoxID, &p.Qty, &p.AvgCost,
			&p.StopPrice, &p.Status, &p.ClosedDate, &p.CloseReason); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// PlanOrderView 订单运行时视图（GUI 交易记录视图：contract.PlanOrder + 持久层字段）。
type PlanOrderView struct {
	contract.PlanOrder
	ID     int64  `json:"id"`
	Status string `json:"status"` // pending/placed/filled/cancelled/expired
}

// PlanView 计划头 + 订单清单（GUI 交易记录视图）。
type PlanView struct {
	Plan   contract.TradePlan `json:"plan"`
	Orders []PlanOrderView    `json:"orders"`
}

// PlansRecent 最近 n 个交易计划（含订单与状态，GUI 交易记录视图）。
func (r *TradeRepo) PlansRecent(n int) ([]PlanView, error) {
	if n <= 0 {
		n = 10
	}
	return r.plansQuery("", "", n)
}

// PlansRange 按日期范围取计划（GUI U2 时间范围查询；from/to 空=不限；limit 0=不限）。
func (r *TradeRepo) PlansRange(from, to string, limit int) ([]PlanView, error) {
	return r.plansQuery(from, to, limit)
}

// plansQuery 计划头+订单两段查询（无订单的计划也返回，空计划日展示"无操作"；
// 先头后单避免 JOIN 行平铺）。from/to 空=不限，limit<=0=不限。
func (r *TradeRepo) plansQuery(from, to string, limit int) ([]PlanView, error) {
	q, args := `SELECT id, CAST(plan_date AS TEXT), CAST(execute_date AS TEXT),
		equity_base, open_position_count FROM trade_plan WHERE 1=1`, []any{}
	if from != "" {
		q += " AND plan_date >= ?"
		args = append(args, from)
	}
	if to != "" {
		q += " AND plan_date <= ?"
		args = append(args, to)
	}
	q += " ORDER BY plan_date DESC"
	if limit > 0 {
		q += " LIMIT ?"
		args = append(args, limit)
	}
	headRows, err := r.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer headRows.Close()

	var out []PlanView
	for headRows.Next() {
		var id int64
		// Orders 初始化为空切片：nil 会序列化成 JSON null，前端 .map 直接崩
		pv := PlanView{Orders: []PlanOrderView{}}
		if err := headRows.Scan(&id, &pv.Plan.PlanDate, &pv.Plan.ExecuteDate,
			&pv.Plan.EquityBase, &pv.Plan.OpenPositionCount); err != nil {
			return nil, err
		}
		out = append(out, pv)
	}
	if err := headRows.Err(); err != nil {
		return nil, err
	}
	if len(out) == 0 {
		return out, nil
	}

	// 订单按计划日期归并回各自 PlanView（计划数≤n，线性查找足够）
	ph := make([]string, len(out))
	orderArgs := make([]any, len(out))
	for i := range out {
		ph[i] = "?"
		orderArgs[i] = out[i].Plan.PlanDate
	}
	orderRows, err := r.db.Query(`SELECT o.id, CAST(p.plan_date AS TEXT), o.code, o.side, o.order_kind,
		o.trigger_price, o.limit_price, o.qty, COALESCE(o.leg_no,0), o.reason_json, o.status
		FROM plan_order o JOIN trade_plan p ON p.id = o.trade_plan_id
		WHERE CAST(p.plan_date AS TEXT) IN (`+strings.Join(ph, ",")+`)
		ORDER BY p.plan_date DESC, o.side, o.code`, orderArgs...)
	if err != nil {
		return nil, err
	}
	defer orderRows.Close()
	for orderRows.Next() {
		var (
			o           PlanOrderView
			rj, status  string
			planDateStr string
		)
		if err := orderRows.Scan(&o.ID, &planDateStr, &o.Code, &o.Side, &o.OrderKind,
			&o.TriggerPrice, &o.LimitPrice, &o.Qty, &o.LegNo, &rj, &status); err != nil {
			return nil, err
		}
		o.Status = status
		_ = json.Unmarshal([]byte(rj), &o.Reason)
		for i := range out {
			if out[i].Plan.PlanDate == planDateStr {
				o.PlanDate = out[i].Plan.PlanDate
				o.ExecuteDate = out[i].Plan.ExecuteDate
				out[i].Orders = append(out[i].Orders, o)
				break
			}
		}
	}
	return out, orderRows.Err()
}

// EquityAll 全部快照按日期升序（权益曲线 / 审计）。
func (r *TradeRepo) EquityAll() ([]contract.EquitySnapshot, error) {
	return r.EquityRange("", "")
}

// EquityRange 按日期范围取权益快照（升序；GUI U2 范围切换 / M7.2 周报；from/to 空=不限）。
func (r *TradeRepo) EquityRange(from, to string) ([]contract.EquitySnapshot, error) {
	q, args := `SELECT CAST(date AS TEXT), cash, market_value, total, peak_total, drawdown
		FROM equity_snapshot WHERE 1=1`, []any{}
	if from != "" {
		q += " AND date >= ?"
		args = append(args, from)
	}
	if to != "" {
		q += " AND date <= ?"
		args = append(args, to)
	}
	q += " ORDER BY date"
	rows, err := r.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []contract.EquitySnapshot
	for rows.Next() {
		var e contract.EquitySnapshot
		if err := rows.Scan(&e.Date, &e.Cash, &e.MarketValue, &e.Total, &e.PeakTotal, &e.Drawdown); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// StalePendingCount 执行日已过仍 pending 的订单数（M7.2 周报纪律审计）。
func (r *TradeRepo) StalePendingCount(today string) (int, error) {
	var n int
	err := r.db.QueryRow(`SELECT COUNT(*) FROM plan_order o JOIN trade_plan p ON p.id = o.trade_plan_id
		WHERE o.status = 'pending' AND p.execute_date < ?`, today).Scan(&n)
	return n, err
}

// ---------------------------------------------------------------------------
// 已交易股票历史库（B3，0004，docs/00 §6 三记录制度之三：亏损原因/教训）
// 原书（197页 OCR 校对）：达瓦斯自称最重要的内容——每笔已完成的交易记录
// ①买入理由（reason_json）②卖出理由（close_reason+realized_pnl，自动落库）
// ③亏损原因/教训（trade_review，人工复盘录入）。教训不自动生成，人工写。
// ---------------------------------------------------------------------------

// ReviewRow 单笔已完结交易 + 复盘（review list <code> 明细行）。
type ReviewRow struct {
	PositionID   int64   `json:"position_id"`
	Code         string  `json:"code"`
	Name         string  `json:"name"`
	OpenedDate   string  `json:"opened_date"`
	ClosedDate   string  `json:"closed_date"`
	Qty          int     `json:"qty"`
	RealizedPnl  float64 `json:"realized_pnl"`
	CloseReason  string  `json:"close_reason"`
	RuleFollowed string  `json:"rule_followed"`
	ErrorType    string  `json:"error_type"`
	Lesson       string  `json:"lesson"`
}

// ReviewSummaryRow 某股历史战绩汇总（review list 汇总行）。
type ReviewSummaryRow struct {
	Code     string  `json:"code"`
	Name     string  `json:"name"`
	Trades   int     `json:"trades"`
	Wins     int     `json:"wins"`
	Losses   int     `json:"losses"`
	TotalPnl float64 `json:"total_pnl"`
	Lesson   string  `json:"lesson"` // 最新一条教训（未写复盘则空）
}

// AddReview 复盘录入（每笔 position 一条，UPSERT 重录覆盖）。
// code 从 position 表反查（防手输错码）；position 不存在时无行受影响 → 报错。
func (r *TradeRepo) AddReview(positionID int64, ruleFollowed, errorType, lesson string) error {
	res, err := r.db.Exec(`INSERT INTO trade_review(position_id, code, rule_followed, error_type, lesson)
		SELECT ?, code, ?, ?, ? FROM position WHERE id = ?
		ON CONFLICT(position_id) DO UPDATE SET
			rule_followed=excluded.rule_followed, error_type=excluded.error_type, lesson=excluded.lesson`,
		positionID, ruleFollowed, errorType, lesson, positionID)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return fmt.Errorf("position %d 不存在", positionID)
	}
	return nil
}

// ReviewSummary 历史战绩汇总视图（code 空=全部股票；只统计已清仓交易）。
func (r *TradeRepo) ReviewSummary(code string) ([]ReviewSummaryRow, error) {
	q, args := `SELECT p.code, COALESCE(s.name,''), COUNT(*),
			SUM(CASE WHEN p.realized_pnl > 0 THEN 1 ELSE 0 END),
			SUM(CASE WHEN p.realized_pnl < 0 THEN 1 ELSE 0 END),
			COALESCE(SUM(p.realized_pnl), 0),
			COALESCE((SELECT lesson FROM trade_review tr WHERE tr.code = p.code
				ORDER BY tr.id DESC LIMIT 1), '')
		FROM position p LEFT JOIN security s ON s.code = p.code
		WHERE p.status = 'closed'`, []any{}
	if code != "" {
		q += " AND p.code = ?"
		args = append(args, code)
	}
	q += " GROUP BY p.code ORDER BY p.code"
	rows, err := r.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ReviewSummaryRow
	for rows.Next() {
		var x ReviewSummaryRow
		if err := rows.Scan(&x.Code, &x.Name, &x.Trades, &x.Wins, &x.Losses, &x.TotalPnl, &x.Lesson); err != nil {
			return nil, err
		}
		out = append(out, x)
	}
	return out, rows.Err()
}

// ReviewDetail 单股全部已完结交易明细（含复盘三字段；qty 取 position_leg 聚合）。
func (r *TradeRepo) ReviewDetail(code string) ([]ReviewRow, error) {
	rows, err := r.db.Query(`SELECT p.id, p.code, COALESCE(s.name,''),
			CAST(p.opened_date AS TEXT), COALESCE(CAST(p.closed_date AS TEXT),''),
			COALESCE((SELECT SUM(l.qty) FROM position_leg l WHERE l.position_id = p.id), 0),
			COALESCE(p.realized_pnl, 0), COALESCE(p.close_reason,''),
			COALESCE(tr.rule_followed,''), COALESCE(tr.error_type,''), COALESCE(tr.lesson,'')
		FROM position p
		LEFT JOIN security s ON s.code = p.code
		LEFT JOIN trade_review tr ON tr.position_id = p.id
		WHERE p.status = 'closed' AND p.code = ?
		ORDER BY p.opened_date`, code)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ReviewRow
	for rows.Next() {
		var x ReviewRow
		if err := rows.Scan(&x.PositionID, &x.Code, &x.Name, &x.OpenedDate, &x.ClosedDate,
			&x.Qty, &x.RealizedPnl, &x.CloseReason, &x.RuleFollowed, &x.ErrorType, &x.Lesson); err != nil {
			return nil, err
		}
		out = append(out, x)
	}
	return out, rows.Err()
}

// TrackRecordByCode 某证券历史战绩汇总（B3 再纳入提示：交易笔数/亏损笔数/累计盈亏）。
func (r *TradeRepo) TrackRecordByCode(code string) (trades, losses int, totalPnl float64, err error) {
	err = r.db.QueryRow(`SELECT COUNT(*),
			COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END), 0),
			COALESCE(SUM(realized_pnl), 0)
		FROM position WHERE code = ? AND status = 'closed'`, code).
		Scan(&trades, &losses, &totalPnl)
	return
}

// ---------------------------------------------------------------------------
// 订单审核（C：炒股手成交导入后的执行质量对照）
// ---------------------------------------------------------------------------

// OrderAuditRow 订单审核视图（执行质量报告：漏执行/偏差对照）。
type OrderAuditRow struct {
	ID          int64
	Code        string
	Side        string
	ExecuteDate string
	TriggerPx   float64
	LimitPx     float64
	Qty         int
	Status      string
}

// OrdersBetween 取执行日落在 [from,to] 的全部订单（含各状态；审核报告用）。
func (r *TradeRepo) OrdersBetween(from, to string) ([]OrderAuditRow, error) {
	rows, err := r.db.Query(`SELECT o.id, o.code, o.side, CAST(p.execute_date AS TEXT),
			o.trigger_price, o.limit_price, o.qty, o.status
		FROM plan_order o JOIN trade_plan p ON p.id = o.trade_plan_id
		WHERE p.execute_date >= ? AND p.execute_date <= ?
		ORDER BY p.execute_date, o.code`, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []OrderAuditRow
	for rows.Next() {
		var x OrderAuditRow
		if err := rows.Scan(&x.ID, &x.Code, &x.Side, &x.ExecuteDate,
			&x.TriggerPx, &x.LimitPx, &x.Qty, &x.Status); err != nil {
			return nil, err
		}
		out = append(out, x)
	}
	return out, rows.Err()
}
