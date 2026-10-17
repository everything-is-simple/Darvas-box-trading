// Package contract 定义所有跨模块调用的类型与接口（单一事实源）。
// 规则：修改本文件必须同步修改 docs/04-api-contract.md（一个commit内完成）。
package contract

import (
	"context"
	"time"
)

// ---------------------------------------------------------------------------
// 公共类型
// ---------------------------------------------------------------------------

// DailyBar 日线行情（与 daily_bar 表对应）
type DailyBar struct {
	Code   string // sh600519
	Date   string // YYYY-MM-DD
	Open   float64
	High   float64
	Low    float64
	Close  float64
	Volume int64   // 手
	Amount float64 // 元
}

// Security 证券基础信息
type Security struct {
	Code     string
	Name     string
	Market   string // sh / sz
	Kind     string // stock / index
	Board    string // main / gem / star / st
	Industry string
	ListDate string
	Status   string // active / delisted
}

// BoxStatus 箱体状态机
type BoxStatus string

const (
	BoxForming    BoxStatus = "forming"
	BoxConfirmed  BoxStatus = "confirmed"
	BoxBrokenUp   BoxStatus = "broken_up"
	BoxBrokenDown BoxStatus = "broken_down"
	BoxInvalid    BoxStatus = "invalid"
)

// Box 箱体（与 box 表对应）
type Box struct {
	ID              int64
	Code            string
	Top             float64
	Bottom          float64
	TopConfirmed    string
	BottomConfirmed string
	Status          BoxStatus
	PrevBoxID       int64 // 阶梯链：前一箱体ID（0=无）
	BrokenDate      string
}

// SignalType 信号类型（四类，封闭集合）
type SignalType string

const (
	SignalBreakout SignalType = "breakout"        // 突破箱体上轨 → 买
	SignalPyramid  SignalType = "pyramid"         // 新箱体再突破 → 加码
	SignalStop     SignalType = "stop"            // 跌破箱体下轨 → 卖
	SignalFailed   SignalType = "failed_breakout" // 突破失败跌回 → 卖
)

// Signal 信号（与 signal 表对应）
type Signal struct {
	Date     string
	Code     string
	Type     SignalType
	BoxID    int64
	ClosePx  float64
	BoxLevel float64
	Payload  map[string]any
}

// Candidate 初选候选（与 candidate 表对应）
type Candidate struct {
	ScreenDate      string
	Code            string
	Score           float64
	Reasons         map[string]any
	FundamentalFlag string // yes / no / unknown（人工复核）
}

// WatchStage 跟踪池状态机（B2 淘汰生命周期，0003迁移扩六值）
type WatchStage string

const (
	StageWatching    WatchStage = "watching"
	StagePending     WatchStage = "pending"
	StageHolding     WatchStage = "holding"
	StageExited      WatchStage = "exited"
	StageSuspended   WatchStage = "suspended"   // 条件挂起（"停传报价"：跌破/远离后暂停，收盘≥resume_trigger再激活）
	StageBlacklisted WatchStage = "blacklisted" // 两亏拉黑（同一股票亏损交易满 pool.maxloss 次，永久）
)

// Watch 跟踪池条目
type Watch struct {
	ID            int64
	Code          string
	CandidateID   int64
	AddedDate     string
	Stage         WatchStage
	CurrentBoxID  int64
	BoxCount      int
	ExitReason    string
	ResumeTrigger float64 // 挂起激活价（stage=suspended 时有效）
	Note          string
}

// PlanOrder 订单计划（六问的答案单元）
type PlanOrder struct {
	PlanDate     string
	ExecuteDate  string
	Code         string
	Side         string // buy / sell
	OrderKind    string // stop_buy / stop_sell
	TriggerPrice float64
	LimitPrice   float64
	Qty          int
	LegNo        int
	Reason       map[string]any
}

// TradePlan 交易计划头
type TradePlan struct {
	PlanDate          string
	ExecuteDate       string
	EquityBase        float64
	OpenPositionCount int
}

// Fill 成交回录
type Fill struct {
	PlanOrderID int64
	Code        string
	Date        string
	Side        string // buy / sell（0002迁移；粘贴解析或订单方向回填）
	Price       float64
	Qty         int
	Fee         float64
	RawText     string
}

// Position 持仓
type Position struct {
	ID          int64
	Code        string
	OpenedDate  string
	EntryBoxID  int64
	Qty         int
	AvgCost     float64
	StopPrice   float64 // 只升不降（DB触发器+应用层双防护）
	Status      string  // open / closed
	ClosedDate  string
	CloseReason string
	RealizedPnl float64 // 清仓时由 ClosePosition 计算写入（两亏拉黑统计基础，0003）
}

// EquitySnapshot 权益快照
type EquitySnapshot struct {
	Date        string
	Cash        float64
	MarketValue float64
	Total       float64
	PeakTotal   float64
	Drawdown    float64
}

// TaskResult 任务运行结果（调度留痕）
type TaskResult struct {
	Task         string
	Date         string
	Status       string // ok / failed
	AffectedRows int64
	DurationMS   int64
	Error        string
}

// DataQuality 数据质量校验结果
type DataQuality struct {
	LatestTradeDate string
	UpdatedCount    int
	BadRecords      int
	Fresh           bool
}

// ---------------------------------------------------------------------------
// 领域参数
// ---------------------------------------------------------------------------

// BoxParams 箱体引擎参数
type BoxParams struct {
	VolMultiplier float64 // 突破放量倍数，默认2.0
	VolWindow     int     // 均量窗口，默认50
	BreakPremium  float64 // 收盘>上轨×该值确认突破，默认1.03
	MinBoxWidth   float64 // 箱体最小宽度（比例），默认0.05
}

// ScreenParams 初选参数
type ScreenParams struct {
	TurnoverMin float64 // 日均成交额下限（元），默认5000万
	RSWindow    int     // 相对强度窗口，默认20
	RSThreshold float64 // RS阈值，默认1.2
	VolRatioMin float64 // 放量比下限，默认2.0
	VolRatioMax float64 // 放量比上限，默认5.0
	LateDays    int     // B2①太晚规则：突破后第N交易日（默认4，原书"四天已超箱体=太晚"）
}

// PlanParams 计划参数
type PlanParams struct {
	RiskLeg1     float64 // 第1腿风险，默认0.01
	RiskLeg23    float64 // 第2/3腿风险，默认0.02
	MaxPosition  float64 // 单股仓位上限，默认0.20
	BuyPremium   float64 // 买入触发溢价，默认1.03
	SellDiscount float64 // 卖出触发折价，默认0.97
	MaxLegs      int     // 最大腿数，默认3
}

// ---------------------------------------------------------------------------
// L1 数据层接口
// ---------------------------------------------------------------------------

// DayFileReader TDX .day 文件读取器
type DayFileReader interface {
	ListCodes() ([]string, error)
	ReadBars(code string, since string) ([]DailyBar, error)
	ModTime(code string) (time.Time, error)
	ReadNames() (map[string]string, error) // 代码→名称（T0002/hq_cache/*.tnf，GUI展示用）
}

// Ingester 导入器
type Ingester interface {
	FullImport(ctx context.Context) (TaskResult, error)
	SyncIncremental(ctx context.Context, date string) (TaskResult, error)
	Verify(ctx context.Context, date string) (DataQuality, error)
}

// BarRepo 行情仓储
type BarRepo interface {
	UpsertBars(bars []DailyBar) (int64, error)
	BarsWindow(code string, endDate string, n int) ([]DailyBar, error)
	CloseOn(code, date string) (float64, bool, error)
	AvgVolume(code string, endDate string, n int) (float64, error)
}

// BoxRepo 箱体仓储
type BoxRepo interface {
	UpsertBoxes(boxes []Box) ([]int64, error)
	LatestBox(code string) (*Box, error)
	Ladder(code string) ([]Box, error)
}

// PoolRepo 池仓储
type PoolRepo interface {
	AppendCandidates(date string, cands []Candidate) error
	CandidatesOn(date string) ([]Candidate, error)
	UpsertWatch(w Watch) error
	WatchByStage(stage WatchStage) ([]Watch, error)
	WatchByCode(code string) (*Watch, error)
}

// SignalRepo 信号仓储
type SignalRepo interface {
	AppendSignals(sigs []Signal) error
	SignalsOn(date string) ([]Signal, error)
}

// TradeRepo 交易仓储
type TradeRepo interface {
	CreatePlan(p TradePlan) (int64, error)
	AddOrders(planID int64, orders []PlanOrder) error
	PlanOn(date string) (*TradePlan, []PlanOrder, error)
	UpdateOrderStatus(orderID int64, status string) error
	AppendFill(f Fill) error
	OpenPositions() ([]Position, error)
	UpdateStop(positionID int64, newStop float64) error
	UpsertEquity(e EquitySnapshot) error
}

// RunRepo 运行日志仓储
type RunRepo interface {
	StartRun(date, task string) (int64, error)
	FinishRun(id int64, res TaskResult) error
	LastOKRun(date, task string) (TaskResult, bool, error)
}

// ParamRepo 参数仓储
type ParamRepo interface {
	GetFloat(key string, def float64) float64
	GetInt(key string, def int) int
	GetString(key string, def string) string
	Set(key, value string) error
}

// ---------------------------------------------------------------------------
// L2 领域层接口
// ---------------------------------------------------------------------------

// BoxEngine 箱体引擎：确定性纯函数，同输入必同输出
type BoxEngine interface {
	Scan(bars []DailyBar, params BoxParams) []Box
}

// BoxScanner 面向DB的每日箱体批处理
type BoxScanner interface {
	RescanAll(ctx context.Context, date string) (TaskResult, error)
	RescanIncremental(ctx context.Context, date string) (TaskResult, error)
}

// Screener 四条件初选
type Screener interface {
	Screen(ctx context.Context, date string) (TaskResult, error)
}

// PoolManager 跟踪池状态机
type PoolManager interface {
	Sync(ctx context.Context, date string) (TaskResult, error)
	MarkFundamental(code, date string, flag string) error
	Remove(code string, reason string) error
	Suspend(code string, resumeTrigger float64) error // 条件挂起（"停传报价"：收盘≥激活价自动恢复watching）
}

// SignalEngine 信号判定
type SignalEngine interface {
	Evaluate(ctx context.Context, date string) (TaskResult, error)
}

// Planner 交易计划生成（六问出口）
type Planner interface {
	Build(ctx context.Context, planDate string) (TaskResult, error)
}

// Ledger 账本
type Ledger interface {
	Snapshot(ctx context.Context, date string) error
	AuditEquation(ctx context.Context, date string) (bool, string, error)
	ParseFill(rawText string) ([]Fill, error)
}

// ---------------------------------------------------------------------------
// L3 编排层接口
// ---------------------------------------------------------------------------

// Pipeline 盘后流水线（任务严格串行：ingest→boxscan→screen→poolsync→signal→plan→report）
type Pipeline interface {
	RunDaily(ctx context.Context, date string) ([]TaskResult, error)
	RerunTask(ctx context.Context, date, task string) (TaskResult, error)
}

// Scheduler 常驻调度
type Scheduler interface {
	Start() error
	CatchUp() error
	Stop() error
}
