package store

import (
	"database/sql"
	"encoding/json"
	"fmt"

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
	err := r.db.QueryRow(`SELECT id, plan_date, execute_date, equity_base, open_position_count
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

// AppendFill 成交回录（raw_text 留痕审计）。
func (r *TradeRepo) AppendFill(f contract.Fill) error {
	_, err := r.db.Exec(`INSERT INTO trade_fill(plan_order_id, code, date, price, qty, fee, raw_text)
		VALUES (?, ?, ?, ?, ?, ?, ?)`,
		nullInt64(f.PlanOrderID), f.Code, f.Date, f.Price, f.Qty, f.Fee, nullEmpty(f.RawText))
	return err
}

// OpenPositions 取全部 open 持仓（GUI 持仓视图）。
func (r *TradeRepo) OpenPositions() ([]contract.Position, error) {
	rows, err := r.db.Query(`SELECT id, code, opened_date, COALESCE(entry_box_id,0), qty, avg_cost,
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
