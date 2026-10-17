# 04 · API契约（模块间接口）

> **单一事实源**：`internal/contract/contract.go`（Go interface定义）。本文档是它的 readable 版本；两者必须同步修改（流程见《06-consistency》）。
> 原则：**模块间只通过contract包的接口对话**；接口即边界，实现细节不越界。

---

## 1. 契约分层总览

```
┌─ L4 GUI绑定层（Wails Methods）──────── 给前端暴露的只读+操作方法
├─ L3 编排层（pipeline/scheduler）────── 任务的顺序、重试、留痕
├─ L2 领域层（box/screener/pool/signal/planner/position/ledger）
├─ L1 数据层（ingest/datasource/store）
└─ L0 契约包（contract）：所有跨层调用引用的类型与方法签名
```

---

## 2. 公共类型（contract包）

```go
// 行情与证券
type DailyBar struct {
    Code string; Date string  // YYYY-MM-DD
    Open, High, Low, Close float64
    Volume int64; Amount float64
}

type Security struct {
    Code, Name, Market string
    Kind, Board, Industry string
    ListDate string; Status string
}

// 箱体
type Box struct {
    ID int64; Code string
    Top, Bottom float64
    TopConfirmed, BottomConfirmed string
    Status string  // forming/confirmed/broken_up/broken_down/invalid
    PrevBoxID int64; BrokenDate string
}

// 信号
type SignalType string // breakout / pyramid / stop / failed_breakout

type Signal struct {
    Date, Code string
    Type SignalType
    BoxID int64
    ClosePx, BoxLevel float64
    Payload map[string]any
}

// 订单计划（六问的答案单元）
type PlanOrder struct {
    PlanDate, ExecuteDate, Code string
    Side string   // buy / sell
    OrderKind string // stop_buy / stop_sell
    TriggerPrice, LimitPrice float64
    Qty int
    LegNo int
    Reason map[string]any  // 推导明细：箱体轨、风险、权益、信号引用
}

// 任务结果
type TaskResult struct {
    Task, Date string
    Status string   // ok / failed
    AffectedRows int64
    DurationMS int64
    Error string
}
```

---

## 3. L1 数据层接口

### 3.1 TDX读取器（datasource/tdx）

```go
type DayFileReader interface {
    ListCodes() ([]string, error)                       // sh600519, sz000001, ...
    ReadBars(code string, since string) ([]contract.DailyBar, error)
    ModTime(code string) (time.Time, error)             // 增量导入依据
}
```

### 3.2 导入器（ingest）

```go
type Ingester interface {
    FullImport(ctx context.Context) (TaskResult, error)      // 全量
    SyncIncremental(ctx context.Context, date string) (TaskResult, error) // 当日增量
    Verify(ctx context.Context, date string) (DataQuality, error)         // 新鲜度/数量校验
}

type DataQuality struct {
    LatestTradeDate string   // 数据中最新交易日（对照sh000001）
    UpdatedCount    int      // 当日更新股票数（<1000 → 阻断告警）
    BadRecords      int
    Fresh           bool     // LatestTradeDate == 期望交易日
}
```

### 3.3 仓储（store，按聚合拆分Repo）

```go
type BarRepo interface {
    UpsertBars(bars []DailyBar) (int64, error)
    BarsWindow(code string, endDate string, n int) ([]DailyBar, error) // 最近n根K线（boxscan主路径）
    LatestDates(code string, n int) ([]string, error)
    CloseOn(code, date string) (float64, bool, error)
    AvgVolume(code string, endDate string, n int) (float64, error)
}

type BoxRepo interface {
    UpsertBoxes(boxes []Box) ([]int64, error)
    LatestBox(code string) (*Box, error)
    Ladder(code string) ([]Box, error)          // 阶梯链
    ActiveBoxes(status string) ([]Box, error)   // 全市场当前confirmed/forming
}

type PoolRepo interface {
    AppendCandidates(date string, cands []Candidate) error
    CandidatesOn(date string) ([]Candidate, error)
    UpsertWatch(w Watch) error
    WatchByStage(stage string) ([]Watch, error)
    WatchByCode(code string) (*Watch, error)
}

type SignalRepo interface {
    AppendSignals(sigs []Signal) error
    SignalsOn(date string) ([]Signal, error)
}

type TradeRepo interface {
    CreatePlan(p TradePlan) (int64, error)
    AddOrders(planID int64, orders []PlanOrder) error
    PlanOn(date string) (*TradePlan, []PlanOrder, error)   // GUI计划视图
    UpdateOrderStatus(orderID int64, status string) error
    AppendFill(f Fill) error
    OpenPositions() ([]Position, error)
    UpdateStop(positionID int64, newStop float64) error    // 触发器保证只升不降
    UpsertEquity(e EquitySnapshot) error
}

type RunRepo interface {
    StartRun(date, task string) (int64, error)
    FinishRun(id int64, res TaskResult) error
    LastOKRun(date, task string) (*TaskResult, bool, error) // 补跑判断
}

type ParamRepo interface {
    GetFloat(key string, def float64) float64
    GetInt(key string, def int) int
    GetString(key string, def string) string
    Set(key, value string) error                          // 留old_value审计
}
```

---

## 4. L2 领域层接口

### 4.1 箱体引擎（box）★核心

```go
// Engine.Scan：确定性纯函数 —— 同一K线序列输入必得同一箱体序列输出
type Engine interface {
    Scan(bars []contract.DailyBar, params BoxParams) []contract.Box
}

type BoxParams struct {
    VolMultiplier float64  // 放量倍数（突破确认），默认2.0
    VolWindow     int      // 均量窗口，默认50
    BreakPremium  float64  // 收盘>上轨×该值确认突破，默认1.03
    MinBoxWidth   float64  // 箱体最小宽度%，默认0.05
}

// BoxScanner：面向DB的每日批处理
type BoxScanner interface {
    RescanAll(ctx context.Context, date string) (TaskResult, error)      // 全量
    RescanIncremental(ctx context.Context, date string) (TaskResult, error) // 预过滤增量
}
```

**契约要点**：
- `Scan`不做任何IO，只吃K线吐箱体 → 单元测试即规范（含书中案例断言）
- 突破当日成交量必须≥`VolWindow日均量×VolMultiplier`，否则不算broken_up

### 4.2 初选器（screener）

```go
type Screener interface {
    Screen(ctx context.Context, date string) (TaskResult, error)
    // 四条件：流动性 / RS / 放量 / 箱体形态 → candidate_pool
    // 输出打分规则：每条件0-25分，合计score降序
}

type ScreenParams struct {
    TurnoverMin float64  // 日均成交额（元），默认5000万
    RSWindow    int      // 默认20
    RSThreshold float64  // 默认1.2
    VolRatioMin, VolRatioMax float64 // 2.0 / 5.0
}
```

### 4.3 跟踪池（pool）

```go
type PoolManager interface {
    Sync(ctx context.Context, date string) (TaskResult, error)
    // candidate→watching 入池；状态迁移见 ERD §2.2
    MarkFundamental(code, date string, flag string) error  // 人工复核 yes/no
    Remove(code string, reason string) error               // 人工移除
}
```

### 4.4 信号（signal）

```go
type SignalEngine interface {
    Evaluate(ctx context.Context, date string) (TaskResult, error)
    // 对 watching/pending/holding 池逐股判定四类信号：
    // breakout:  收盘>confirmed箱体上轨×premium 且放量且非一字板
    // pyramid:   持仓股新箱体再突破（leg_no<maxlegs）
    // stop:      收盘<最新箱体下轨（持仓→全卖；watching→出池）
    // failed_breakout: 突破后3日内收盘跌回箱体内
}
```

### 4.5 计划器（planner）★六问出口

```go
type Planner interface {
    Build(ctx context.Context, planDate string) (TaskResult, error)
    // 输入：当日signal + 持仓 + param + equity_snapshot
    // 输出：trade_plan + plan_order（停损买/卖单，含触发价、委托价、数量、腿号）
    // 数量公式：qty = floor(equity×risk% / (trigger−stop) / 100)×100
}

type PlanParams struct {
    RiskLeg1, RiskLeg23 float64  // 0.01 / 0.02
    MaxPosition  float64         // 0.20 单股上限
    BuyPremium   float64         // 1.03
    SellDiscount float64         // 0.97
    MaxLegs      int             // 3
}
```

### 4.6 账本（ledger）

```go
type Ledger interface {
    Snapshot(ctx context.Context, date string) error      // 权益快照
    AuditEquation(ctx context.Context, date string) (bool, string, error) // 恒等式校验
    ParseFill(rawText string) ([]Fill, error)             // 券商成交文本粘贴解析
}
```

---

## 5. L3 编排层接口

```go
// pipeline：盘后流水线，任务严格串行
type Pipeline interface {
    RunDaily(ctx context.Context, date string) ([]TaskResult, error)
    // 顺序: ingest→boxscan→screen→poolsync→signal→plan→report
    // 失败策略：ingest失败→整体中止；plan失败→标红但已完成任务保留；其余可续跑
    RerunTask(ctx context.Context, date, task string) (TaskResult, error) // 幂等补跑
}

// scheduler：常驻调度
type Scheduler interface {
    Start() error      // 时刻表：15:30/16:00/16:10/16:20/16:30/16:40/16:45 + 周六10:00
    CatchUp() error    // 启动时检查daily_run，补跑「交易日已过但未ok」的任务
    Stop() error
}
```

---

## 6. L4 GUI绑定层（Wails Methods）

> 前端（HTML/JS）只允许调用以下方法；命名=资源+动词，与CLI子命令一一对应。

| 方法 | 入参 | 返回 | 对应能力 |
|------|------|------|---------|
| `GetTodayBrief(date)` | - | `{tasks, signals, plan, equity}` | 今日流程视图（心跳+六问） |
| `RunTask(date, task)` | - | `TaskResult` | 手动触发单任务（补跑） |
| `ListCandidates(date)` | - | `Candidate[]` | 候选池表 |
| `ListWatch(stage)` | - | `Watch[]` | 跟踪池表 |
| `MarkFundamental(code, flag)` | - | bool | 人工复核 |
| `RemoveFromWatch(code, reason)` | - | bool | 人工移除 |
| `GetLadder(code)` | - | `{boxes[], bars[]}` | 个股箱体阶梯+K线（画图） |
| `ListPositions()` | - | `Position[]`（含腿、当前箱体、止损位） | 持仓视图 |
| `GetPlan(date)` | - | `{plan, orders[]}` | 明日交易计划 |
| `PasteFills(rawText)` | - | `Fill[]`（预览，需Confirm） | 成交录入 |
| `ConfirmFills(fills)` | - | `int`（入库数） | 成交确认 |
| `GetEquityCurve(from, to)` | - | `EquitySnapshot[]` | 权益曲线 |
| `GetParams()` / `SetParam(k, v)` | - | `map` / bool | 参数热调 |
| `OpenReport(date)` | - | `string`（文件路径） | 打开HTML日报 |

**GUI只读领域数据、只经编排层写**：`RunTask`是唯一的写入口，`PasteFills/ConfirmFills/SetParam`三个例外直通对应Repo（均有审计留痕）。

---

## 7. CLI子命令（与GUI方法同构）

```
darvas run    [--date]        # 全流水线
darvas ingest [--date]        # 单任务补跑（其余同理）
darvas boxscan|screen|poolsync|signal|plan|report|weekly [--date]
darvas gui                    # 启动桌面GUI（常驻+调度）
darvas ladder  <code>         # 终端打印箱体阶梯
darvas param   get|set        # 参数读写
darvas fills   paste          # 交互式成交录入
```

---

## 8. 契约变更规则

1. 任何跨模块调用的**方法签名、类型字段、状态枚举**变更 = 契约变更
2. 契约变更必须：改`internal/contract/contract.go` → 改本文档 → 跑`go build ./...`与全部测试 → 一个commit内完成
3. 状态枚举（box.status/watch.stage/signal.type/order.status）新增值需在ERD状态机表中同步登记迁移规则
4. 破坏性变更（删字段/改语义）：主版本号+1，提供数据迁移SQL
