# 04 · API契约（模块间接口）

> **单一事实源**：`internal/contract/contract.go`（Go interface定义）。本文档是它的 readable 版本；两者必须同步修改（流程见《06-consistency》）。
> 原则：**模块间只通过contract包的接口对话**；接口即边界，实现细节不越界。

---

## 1. 契约分层总览

```
┌─ L4 GUI绑定层（本地HTTP JSON API，gui包）── 给前端暴露的只读端点
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
type SignalType string // breakout / stop / failed_breakout（pyramid 于 0017 退役：一律不加码）

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

// 池与持仓（B2 跟踪池生命周期，0003迁移）
type WatchStage string
// watching / pending / holding / exited / suspended / blacklisted
//   watching→pending→holding→exited 主链；suspended=条件挂起（收盘≥ResumeTrigger恢复）；
//   blacklisted=战绩分级黑名单终态（最近一笔亏损，冷却12月；0017 替换旧两亏拉黑）

type Watch struct {
    ID int64; Code string; CandidateID int64
    AddedDate string
    Stage WatchStage
    CurrentBoxID int64; BoxCount int
    ExitReason string
    ResumeTrigger float64  // 挂起激活价（stage=suspended 时有效）
    Note string
}

type Position struct {
    ID int64; Code string; OpenedDate string; EntryBoxID int64
    Qty int; AvgCost, StopPrice float64
    Status string   // open / closed
    ClosedDate, CloseReason string
    RealizedPnl float64  // 清仓时由 ClosePosition 计算写入（战绩分级状态机流转依据，0017）
}

// 战绩等级（0017）
type TrackLevel string // normal / winner / blacklist
```

---

## 3. L1 数据层接口

### 3.1 TDX读取器（datasource/tdx）

```go
type DayFileReader interface {
    ListCodes() ([]string, error)                       // sh600519, sz000001, ...
    ReadBars(code string, since string) ([]contract.DailyBar, error)
    ModTime(code string) (time.Time, error)             // 增量导入依据
    ReadNames() (map[string]string, error)              // 代码→名称（T0002/hq_cache/*.tnf，GUI展示用）
}
```

> ReadNames 实现（tdx/names.go）：解析通达信 `{TDX根}/T0002/hq_cache/{shs,szs}.tnf`
> 二进制名称表（50字节头 + 360字节/条；code@+0 六位ASCII，name@+31 GBK最长32字节）。
> 名称是可选增强数据：文件缺失仅告警、名称留空，不影响行情导入。

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
    BarsRange(code, from, to string, max int) ([]DailyBar, error)     // 区间K线（GUI U2时间范围，M6）
    LatestDate(code string) (string, error)                            // 个股最新交易日（doctor日历检查）
    NextDate(code, after string) (string, error)                       // 下一交易日（执行日计算ADR-2）
    PrevDate(code, before string) (string, error)                      // 前一交易日（信号昨日状态对比）
    CloseOn(code, date string) (float64, bool, error)
    AvgVolume(code string, endDate string, n int) (float64, error)
    AvgAmount(code string, endDate string, n int) (float64, error)      // 日均成交额（初选流动性）
    LastClose(code string) (float64, bool, error)          // 最新收盘（GUI持仓浮盈亏，M6）
    BarCount(code, after, until string) (int, error)       // (after, until]区间K线根数（太晚规则/3周约束交易日计数，B2）
    CountBars(code string) (int64, error)                  // 总根数（doctor）
    HasBar(code, date string) (bool, error)                // 幂等检查（ingest）
}

type BoxRepo interface {
    UpsertBoxes(boxes []Box) ([]int64, error)
    LatestBox(code string) (*Box, error)
    Ladder(code string) ([]Box, error)          // 阶梯链
    LatestNonFormingBox(code string) (*Box, error) // 决策用最新落定箱体（修复 archive/07 #1）
    BreadthAgg(from, to string, minHistory int) (upByDate, downByDate map[string]int, err error) // M8.8 市场宽度事件聚合
}

type PoolRepo interface {
    AppendCandidates(date string, cands []Candidate) error
    CandidatesOn(date string) ([]Candidate, error)
    LatestCandidateDate() (string, error)       // 最近初选日（GUI缺省日期）
    UpsertWatch(w Watch) error
    WatchByStage(stage string) ([]Watch, error)
    WatchByCode(code string) (*Watch, error)
    WatchOverview() ([]WatchOverview, error)   // 每code最新行+名称+当前箱体+最新收盘（GUI跟踪池视图，M6）
    ActiveWatchCount() (int, error)             // watching+pending+holding 计数（5-8上限判定，B2）
    RelaxPendingWatches() error                // 买单过期 pending→watching 回退（B2接线）
}

// SecRepo 不在 contract 接口面（证券识别=数据层内部职责，见 repo 注释），
// 但属 store 包公共 API：UpsertSecurities / UpdateNames(tnf回填) / Names /
// ActiveStockCodes / ActiveWatchCodes / TrackLevel / SetTrackLevel / BlacklistUntil（0017 战绩分级）。

type SignalRepo interface {
    AppendSignals(sigs []Signal) error
    SignalsOn(date string) ([]Signal, error)
    SignalsRange(from, to string) ([]Signal, error)   // GUI U2时间范围
    LatestSignalDate() (string, error)                 // GUI缺省日期
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
    // —— M4.5/M4.6 扩展 ——
    FillsAll() ([]Fill, error)                             // 账本重算/审计
    FillsOn(date string) ([]Fill, error)
    FillExists(code, date, side string, price float64, qty int) (bool, error)  // 幂等粘贴
    OrderStatus(orderID int64) (string, error)
    PendingOrdersFor(code, side, date string) ([]PendingOrder, error)          // 成交匹配
    PendingOrdersForExecuteDate(date string) ([]MatchOrder, error)  // 撮合用：执行日==date 全部待成交单（M8.2）
    PendingSellCodes() ([]string, error)                // 已有在途卖出单的股票（planner 防重复挂单，M8.3）
    CancelOpenOrders(code string) (int64, error)         // 取消某股全部未成交单（复权清仓联动，M8.3）
    ExpireStaleOrders(today string) (int64, error)         // 过期未成交订单
    PositionByCode(code string) (*Position, error)
    CreatePosition(p Position) (int64, error)
    UpdatePositionQtyCost(id int64, qty int, avgCost float64) error
    ClosePosition(id int64, date, reason string) error    // 清仓：status=closed + realized_pnl落库 + 战绩分级流转（0017）
    LossCountByCode(code string) (int, error)              // 亏损交易计数（历史审计用，0017 起黑名单改走 track_level）
    PositionPnlHistory(code string) ([]map[string]any, error) // 个股历史持仓盈亏视图（B3战绩复用）
    AddLeg(positionID int64, legNo int, date string, qty int, price, stopAfter float64, planOrderID int64) error
    LegsByPosition(positionID int64) ([]map[string]any, error)
    LegCountByCode(code string) (int, error)             // 当前未平仓持仓腿数（历史审计；0017 起无 pyramid 钳制）
    OpenPositionCount() (int, error)                       // 当前 open 持仓数（0017 组合约束：同时持仓数上限）
    OpenExposureUnits(unitValue float64) (probeUnits, mainUnits float64, err error) // 历史审计用（0017 起不再读）
    AllPositions() ([]Position, error)                     // 含closed（交易记录视图）
    EquityAll() ([]EquitySnapshot, error)
    LatestEquity() (*EquitySnapshot, error)
    SetEquityDefault(date string, total float64) error
    PlansRecent(n int) ([]PlanView, error)                 // 最近n个计划+订单+状态（GUI交易记录，M6）
    // —— 日期范围查询（GUI U2 / M7.2 周报）——
    FillsRange(from, to string) ([]Fill, error)               // 成交按日期范围（from/to空=不限）
    PlansRange(from, to string, limit int) ([]PlanView, error) // 计划按日期范围
    EquityRange(from, to string) ([]EquitySnapshot, error)     // 权益按日期范围（周报曲线）
    StalePendingCount(today string) (int, error)                // 执行日已过仍pending（周报纪律审计）
    // —— B3 已交易股票历史库 / C 执行质量审核 ——
    AddReview(positionID int64, ruleFollowed, errorType, lesson string) error // 复盘录入（UPSERT by position_id）
    ReviewSummary(code string) ([]ReviewSummaryRow, error)      // 战绩汇总（code空=全部股票）
    ReviewDetail(code string) ([]ReviewRow, error)               // 单股明细+复盘（review list）
    TrackRecordByCode(code string) (trades, losses int, totalPnl float64, err error) // 池再纳入提示
    OrdersBetween(from, to string) ([]OrderAuditRow, error)      // 区间订单（CSV审核报告四维度）
}

// Fill 成交回录（M4.5 起含 side，0002迁移）
type Fill struct {
    PlanOrderID int64
    Code        string
    Date        string
    Side        string  // buy / sell
    Price       float64
    Qty         int
    Fee         float64
    RawText     string  // 粘贴原文（审计）
}

type RunRepo interface {
    StartRun(date, task string) (int64, error)
    FinishRun(id int64, res TaskResult) error
    LastOKRun(date, task string) (*TaskResult, bool, error) // 补跑判断
    RunsOn(date string) ([]TaskResult, error)               // 当日全部心跳（GUI仪表盘，M6）
    LatestRunDate() (string, error)                         // 最近有留痕的日期（GUI缺省日期，M6）
    DatesOverview() ([]DateOverview, error)                 // 有数据的日期列表（GUI U1 全站日期维度）
}

type ParamRepo interface {
    GetFloat(key string, def float64) float64
    GetInt(key string, def int) int
    GetString(key string, def string) string
    Set(key, value string) error                          // 留old_value审计
    All() (map[string]string, error)                      // 全量参数（param list）
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
    MinBoxWidth   float64  // 箱体最小宽度%（工程假设非原书规则），默认0.05，参数化 box.min_width（0015，A2 校订）
    ConfirmDays   int      // 轨道确认天数（三日法则），默认3；0由引擎归一为3（M8.4a）
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
    RSExcessMin float64  // RS超额收益下限（20日个股涨幅−上证涨幅），默认0.20（+20pp）。
                         // A1（2026-10-22）：旧 RSThreshold 比值法退役（极值失真，docs/00 §6 注2）
    VolRatioMin float64 // 放量比下限，默认2.0（A1 校订 2026-10-22：删上限，原书只要求「显著放大」）
    LateDays    int      // B2①太晚规则：broken_up后≥N个交易日→不入候选（默认4，原书"四天=太晚"）
}
```

### 4.3 跟踪池（pool）

```go
type PoolManager interface {
    Sync(ctx context.Context, date string) (TaskResult, error)
    // candidate→watching 入池；状态迁移见 ERD §2.2
    // B2 淘汰生命周期（0003）+ 0017 战绩分级：
    //   入池拦截：track_level=blacklist 且 blacklist_until > date（黑名单冷却期内）/ ActiveWatchCount ≥ pool.maxwatch（软参考）
    //   巡检：黑名单冷却期满→解禁 normal；suspended 收盘≥ResumeTrigger→watching（激活价清零）
    MarkFundamental(code, date string, flag string) error  // 人工复核 yes/no
    Remove(code string, reason string) error               // 人工移除
    Suspend(code string, resumeTrigger float64) error      // 条件挂起（"停传报价"：exited/blacklisted拒绝）
}
```

### 4.4 信号（signal）

```go
type SignalEngine interface {
    Evaluate(ctx context.Context, date string) (TaskResult, error)
    // 对 watching/pending/holding 池逐股判定信号——首次事件语义：
    // 信号是"状态迁移事件"，只在迁移发生当日发一次，条件持续满足不重复发。
    // breakout:  箱体当日迁移为 broken_up（BrokenDate==当日；放量/非一字板由boxscan引擎判定）
    // stop:      收盘首次跌破最新箱体下轨（昨收≥下轨 且 今收<下轨）
    // failed_breakout: 突破后3日内收盘首次跌回箱体上轨之下（昨收≥上轨 且 今收<上轨）
    // 0017：无 pyramid（一律不加码，持仓股再突破不发信号）
    // 另：AppendSignals 同日幂等（同 date+code+type+box 已存在则跳过），重跑/调度重试不重复
}
```

### 4.5 计划器（planner）★六问出口

```go
type Planner interface {
    Build(ctx context.Context, planDate string) (TaskResult, error)
    // 输入：当日signal + 持仓 + param + equity_snapshot + 市场趋势（A 判据）
    // 输出：trade_plan + plan_order（停损买/卖单，含触发价、委托价、数量、腿号）
    // A M1（2026-10-23，docs/23 §9 D0）：只首买、一律不加码。首买档位由 **A 判据升势**决定：
    //   regime.ClassifyDateA(planDate) == Bull（60 日低点逐级抬升）→ 2 单位；其余 → 1 单位。
    //   winner 5x 退役（D1 意图由 D0 实现）；track_level 保留作审计字段不驱动 units。
    //   不择时、不做熊市闸门（2026-10-23 六轮回测证伪，见 docs/00 第 0 条）。
    //   组合约束=同时持仓数上限 pool.max_hold。
    // 数量公式：1单位=权益×pos.unit(2%)；单位股数=floor(单位值/价/100)×100；qty=units×单位股数。
    // reason_json：sizing_basis="A_regime_up"/"default"、units=2/1、regime=A判据标签、budget_bucket="normal"、track_level=审计。
    //   （regime 字段完整契约见 §9.1，时点为 planDate——不是 execute_date）
}

type PlanParams struct {
    UnitPct float64            // 0.02（1 单位 = 2% 权益）
    MaxHold int                // 7（同时持仓上限，A M1 升势 2 单位 × 7 只 ≈ 28%，留 30%+ 现金）
    BuyPremium, SellDiscount float64 // 1.03 / 0.97
    MaxLegs int                // 3（0017 后恒 1 腿，保留字段向后兼容）
    // 注：WinnerUnits / pos.units.winner 随 D0 退役（planner 不再读取，参数保留作审计）。
}
```

> **A 判据查询路径**（planner → regime）：`regimeLabel(planDate)` → `regime.ClassifyDateA(st, date)`
> 读 sh000001 最近 61 根 low（`BarRepo.BarsWindow(DefaultIndex, date, 61)`），算
> `cur=min(末60根) / prev=min(前60根)`，`cur>prev×1.002`→升势(Bull)。零未来信息。
> M8.8 宽度法 `ClassifyDate` 不再被 planner 调用，仅分体制报告 `regime.Run` 保留。

### 4.6 账本（ledger）

```go
type Ledger interface {
    Snapshot(ctx context.Context, date string) error      // 权益快照
    AuditEquation(ctx context.Context, date string) (bool, string, error) // 恒等式校验
    ParseFill(rawText string) ([]Fill, error)             // 券商成交文本粘贴解析
}
```

实现要点（M4.5/M4.6，internal/ledger）：

- **ParseFill**：支持三种常见券商格式（同花顺空白分隔 / 通达信带名称 / CSV单字母BS），
  表头行与空行自动跳过；解析失败整批拒绝（宁可拒绝不可错账）。fee 可选缺省0。
- **ApplyFills**：fill → 匹配 pending 订单（执行日≤成交日、触发价最接近）→ 订单 filled
  （状态机：pending→placed→filled/cancelled/expired，`CanTransition` 封闭集合）→
  持仓状态机（买=开仓/加码+腿记录+止损上移；卖=减仓/清仓）→ 幂等（code+date+side+price+qty）。
- **Snapshot**：现金 = 本金(param `equity.base`，默认100万) + Σ卖出所得 − Σ买入支出；
  市值 = open持仓 × 当日收盘（停牌回退最近收盘）。现金不落增量字段，永远可从成交重算。
- **AuditEquation** 三重校验：① 权益恒等式 cash+mv=total（重算 vs 快照 + 快照内部自洽）；
  ② 持仓闭合 Σ买−Σ卖=qty（逐股，孤儿净持仓报警）；③ 订单闭合 filled 订单必有 fill。
- **CLI**：`darvas fills [file] [--yes]`（预览→确认→入库→快照→审计）；
  doctor 增账本检查项（触发器在场/恒等式/滞留订单）。

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
    Start() error      // 时刻表：见 scheduler.Schedule（与 docs/01 §4.1 由一致性检查 C4 锁定）；周六 weekly 周报（M7.2已接入）
    CatchUp() error    // 启动时检查daily_run，补跑「交易日已过但未ok」的任务；周六启动补查weekly
    Stop() error
}
```

实现要点（M7.1，internal/scheduler）：

- **触发点语义**：每任务一个触发点（`Schedule` 常量表，顺序即依赖链）。触发点到达时
  把「计划时刻已过且尚未ok」的任务按链序一次性补齐；每触发点每日只消费一次，
  失败任务在**后续触发点**自动重试（ingest 失败→中止本链，等下个触发点，不空转）。
- **weekly 触发（M7.2）**：`fireWeekly` 独立于每日时刻表——周六本地时刻≥10:00 触发一次
  `weekly` 周报任务（`RerunTask(today,"weekly")`），不查交易日历（周六必非交易日）；
  当日只消费一次，且先查 `LastOKRun` 幂等跳过（手动跑过/serve重启不重复执行）；
  错过当日不自动回放，用 `darvas weekly --date X` 手动补。
- **CatchUp**：① 前一交易日（`BarRepo.PrevDate(sh000001)`）完全无 ok 心跳 → 整日补跑；
  ② 今天已过点未 ok → 链式补跑。更早历史缺口用 `darvas run --date X` 手动补。
- **交易日历（ADR-2）**：`isTradingDay` = DB `HasBar(sh000001,date)`，未入库时直读
  TDX `sh000001.day`（用户盘后已下载当日数据即算交易日，ingest 前可判定；下载时点见 docs/01 §4.2）。
- **OnDayDone 钩子**：某日 report 刚转 ok 时触发一次（serve 接 `Pipeline.BackupDaily`，
  NFR-3 备份轮转）。
- 时钟/任务执行器可注入（`now func()` / `Runner` 接口），单测用模拟时钟全覆盖。

---

## 6. L4 GUI绑定层（本地HTTP JSON API，internal/gui）

> M6 实际交付：`net/http` + embed 静态单文件前端（`darvas ui` 起 127.0.0.1:8787）。
> **全部只读**：所有写操作（fills/param/run/pool 人工操作）仍走 CLI，GUI 不越权。
> 响应约定：nil 集合一律序列化为 `[]`（不得为 null，前端直接 `.map`）；错误返回 `{"error": msg}` + 4xx/5xx。
> 静态资源响应带 `Cache-Control: no-cache`（embed 无真实 mtime，防浏览器缓存旧页面）。

| 端点 | 入参 | 返回（JSON字段） | 对应视图 |
|------|------|------|---------|
| `GET /api/dashboard` | `?date=`（缺省=LatestRunDate） | `{date, runs[], plan, orders[], names, equity}` | 仪表盘：7任务心跳+计划订单+权益卡 |
| `GET /api/dates` | - | `{dates: DateOverview[]}`（date/runs/candidates/has_plan/has_equity；UNION daily_run/candidate/trade_plan/equity_snapshot，降序） | 全站日期下拉（有数据的日期+每日附注） |
| `GET /api/watch` | - | `{items: WatchOverview[]}`（code/name/stage/added_date/box_count/box_top/box_bottom/box_status/last_close/exit_reason/resume_trigger；含 exited 行；stage 排序 holding→pending→watching→suspended→blacklisted→其他，B2） | 跟踪池看板 |
| `GET /api/candidates` | `?date=`（缺省=最近候选快照日） | `{date, items: Candidate[], names}` | 候选池按日快照（score/reasons明细） |
| `GET /api/signals` | `?date=`（缺省=最近信号日） | `{date, items: Signal[], names}` | 信号按日浏览 |
| `GET /api/stock/{code}` | `?days=250`（上限1000）或 `?from=&to=`（区间，上限1000根） | `{code, name, days, from, to, bars[], boxes[], watch, position}` | 个股K线+箱体叠加+阶梯表（区间窗口） |
| `GET /api/trades` | `?from=&to=`（缺省=全部） | `{from, to, plans[], fills[], positions[], equity[], names}`（positions 含浮盈亏 pnl/pnl_pct；fills/plans/equity 按范围过滤，positions 当前态不滤） | 交易记录：计划/成交/持仓+权益曲线（范围切换） |
| `GET /` | - | index.html（embed） | 单文件前端（四视图+日期下拉+池子页，canvas自绘，无外部JS库） |

原 Wails 方法表（RunTask/PasteFills/MarkFundamental 等写操作）转入 M8+ 候选：届时以 POST 端点形式补齐，须带审计留痕。

---

## 7. CLI子命令（与GUI方法同构）

```
darvas run    [--date]        # 全流水线（尾部自动备份+轮转）
darvas ingest [--date]        # 单任务补跑（其余同理）
darvas boxscan|screen|poolsync|signal|plan|report [--date]
darvas weekly [--date]        # M7.2 周报：缺省锚定最近周六，覆盖往前7天；产出后自动打开
darvas ui     [--addr 127.0.0.1:8787] [--db path]   # 本地HTTP UI（起服务+系统浏览器打开，M6）
darvas serve  [--addr 127.0.0.1:8787] [--db path]   # 常驻模式（M7.1）：CatchUp补跑+时刻表调度+周六weekly+备份+GUI
darvas ladder  <code>         # 终端打印箱体阶梯
darvas param   get|set        # 参数读写
darvas pool    list [stage]   # 跟踪池查看（可按stage过滤，B2）
darvas pool    suspend <code> --resume <价>   # 条件挂起：收盘≥激活价自动恢复跟踪
darvas pool    remove <code> --reason <文本>  # 人工移除（留痕）
darvas review list [code]     # 已交易股票历史库·战绩视图（B3：无code=每股汇总，有code=每笔明细+复盘）
darvas review add <position_id> --lesson <文本> [--error-type <文本>] [--rule <followed|violated>]
                              # 复盘录入（三记录制度之三：亏损原因/教训，人工写，重录覆盖）
darvas fills  [file] [--yes]   # 交互式成交录入（粘贴文本/M4.5；file为.csv时走炒股手导入器C）
darvas fills  <炒股手导出.csv> --yes     # C：GBK解析→入库→执行质量审核报告
                               # （指令vs执行：漏执行/滑点/数量偏差/计划外；幂等重导全跳过）
darvas doctor                 # 一致性体检（含账本三重校验）
darvas backtest --src <库> [--from <起>] [--to <止>] [--db <scratch路径>] --mode prefill|progressive|verify
                              # M8 内置回放器（docs/01 §9）：scratch独立库重放as-of六任务链；
                              #   prefill=全历史预灌（M8.2+常规模式）
                              #   progressive=逐日灌K线（差分审计的地面真值）
                              #   verify=双模式差分（as-of纪律运行时证明，详见 archive/07）
                              # 默认窗口=源库最近20交易日；scratch默认 var/bt/bt_<mode>.db（gitignored）
                              # 铁律 M8-2：scratch 独立库不污染实时库心跳；--src 以 mode=ro 只读（铁律 7）
```

> serve 与 ui 的区别：ui 只起 GUI（一次性看盘）；serve = scheduler + GUI 双连接
> （WAL读写分离：调度写 `stW`、GUI读 `stR`，互不阻塞，ADR-5），无人值守日常入口。

---

## 7.5 模拟撮合器成交语义（← 沉淀自 archive/08）

> 本节是回测执行模型的契约，定义「停损单如何被 OHLC 撮合」。当前实现缺跳空上限（见 docs/18 P0-2），
> 沉淀本节的目的是让后续修复有对照契约。完整设计文档 `archive/08-m8.2-matcher.md` 仍活跃。

**触发判定**（T = trigger_price，O/H/L/C 为当日 OHLC）：

| 单类 | 触发条件 | 成交价 |
|------|----------|--------|
| stop_buy | `O >= T`（跳空高开越过）| O（市价成交）|
| stop_buy | `H >= T` 且 `O < T`（盘中上穿）| T |
| stop_sell | `O <= T`（跳空低开跌破）| O |
| stop_sell | `L <= T` 且 `O > T`（盘中下穿）| T |
| 其他 | — | 不触发，次日 planner 过期 |

**涨跌停约束**（简化版）：一字板判定 `H == L`。一字涨停买入单跳过、卖出单正常成交；一字跌停卖出单跳过、买入单正常成交。非一字板按上表处理。

**T+1 与费用**：撮合顺序「先卖后买」；qty 由 planner 整手锁定。费用参数 `fee.commission_rate`(0.00025 双向)、`fee.commission_min`(5)、`fee.stamp_tax`(0.001 卖出单边)、`fee.transfer_rate`(0.00001 双向)。

**复权日处理（不复权方案，用户拍板 2026-10-19）**：跳空检测（当日跌幅超涨跌停限制+ε）→ 判定除权日。持仓股当日撮合器最前强制清仓（价=前日收盘，reason=dividend），清仓后 CancelOpenOrders 标记该股在途订单 cancelled。watch 股不额外处理。

**✅ 已修复（2026-10-21 P0-2，migration 0013）**：加 `match.max_entry_gap`（默认 1.05）。撮合器买入单 `成交价/触发价 > max_entry_gap` 则弃单置 cancelled；planner 生成买单前校验 `trigger 与现价 ClosePx 比值 > max_entry_gap` 则拒单。拦截了 71 笔买腿中 63.4% 高于计划买点 >5% 的跳空追高（含 sz002640 +63%、sz300168 +162% 两笔假利润来源）与 sh600825 型陈旧箱体灾难订单。

**滑点（C4，2026-10-22，migration 0014）**：全部模拟成交（订单撮合 + 除权清仓）按 `match.slippage`（默认 0.002）买贵卖贱，0=关闭；先加滑点再过 max_entry_gap 闸门（闸门校验实际要付的价）。docs/18 R5 估算 20-40 笔/年换手下，滑点缺失约让年化乐观 0.3-0.8%。

**股票池排除（A2，2026-10-22，纯代码）**：`SecRepo.ActiveStockCodes` 按名称排除 ST/*ST（±5% 涨跌停、退市风险，交易规则不同；实测 full_hist.db `%ST%` 匹配零误伤）。名称未知（空）保守保留；TDX 名称为当前快照，历史 ST 状态不可回溯（诚实标注）。

---

## 8. 契约变更规则

1. 任何跨模块调用的**方法签名、类型字段、状态枚举**变更 = 契约变更
2. 契约变更必须：改`internal/contract/contract.go` → 改本文档 → 跑`go build ./...`与全部测试 → 一个commit内完成
3. 状态枚举（box.status/watch.stage/signal.type/order.status）新增值需在ERD状态机表中同步登记迁移规则
4. 破坏性变更（删字段/改语义）：主版本号+1，提供数据迁移SQL

---

## 9. 研究字段产出契约（2026-10-25 立项）

> box 十维迭代实验（理论框架见 docs/00 §3.6，字段清单见 docs/03 §6）在契约侧的产出规则。本节定义 `planner` 生成买入订单时写入 `plan_order.reason_json` 的契约义务——离线分析脚本依赖这些字段做梯度分布统计。

### 9.1 reason_json 产出契约（planner → plan_order）

`planner.GenerateBuyOrder` 生成买入订单时，**必须**把以下字段写入 `plan_order.reason_json`（JSON 文本）：

| 字段 | 来源 | 必填 | 说明 |
|------|------|------|------|
| `box_bottom` | signal 上下文 | ✅ | 被突破箱体下轨 |
| `box_top` | signal 上下文 | ✅ | 被突破箱体上轨 |
| `trigger` | signal 上下文 | ✅ | 触发价 = box_top × buy.premium |
| `high_52w` | signal 上下文 | ✅ | 截至突破日的 52 周最高 |
| `newhigh_distance_pct` | signal 上下文 | ✅ | (high_52w - close) / high_52w × 100 |
| `sizing_basis` | planner 计算 | ✅ | newhigh_t1/t2/t3/t4 之一（按 newhigh_distance_pct 分档） |
| `units` | planner 计算 | ✅ | 该笔订单仓位单位数 |
| `regime` | planner（`regime.ClassifyDateA(planDate)`） | ✅ | A 判据标签 `bull`/`bear`/`range`，**按 plan_date 当日计算**；仅作第 10 维归因原料，**不作择时依据** |

- **sizing_basis 分档规则**（E17 阶梯，与 docs/01 §10.3 一致）：
  - `newhigh_t1`：0% ≤ distance < 5%，units=3
  - `newhigh_t2`：5% ≤ distance < 10%，units=2
  - `newhigh_t3`：10% ≤ distance < 20%，units=1
  - `newhigh_t4`：20% ≤ distance ≤ 40%，units=0.5
  - distance > 40%：**不生成订单**（拒买，不进 plan_order 表）

- **`regime` 时点语义（2026-10-26 补）**：写入的是 **T 日（`plan_date`）** 的 A 判据标签，订单在 **T+1** 执行。
  因此 `regime` 与「成交日的大盘状态」存在 1 日错位——离线归因若按 `opened_date` 重算会得到不同分布
  （实测：2014 年 plan 日与 opened 日一致率 86/100；14 年全样本升势笔数 107 vs 91）。
  报告必须显式声明用的是哪个时点，并与本字段逐笔对账，**不得混用**。

### 9.2 待加字段的契约扩展（解锁第 4/6/7 维，未实现）

以下字段目前**未产出**，待 docs/05 M9 立项后按本契约扩展：

| 字段 | 来源 | 解锁维度 | 契约改动 |
|------|------|---------|---------|
| `volume_ratio` | signal 上下文（突破日量 / N 日均量） | 第 7 维 | `contract.SignalContext` 加字段 + `planner` 写入 reason_json |
| `is_top_box` | box 引擎（box 序列历史查询） | 第 4 维 | `box.Scanner` 加序列状态查询 + signal 上下文携带 + planner 写入 |
| `break_strength_pct` | `(trigger - box_top) / box_top × 100` | 第 6 维 | 需配合 `buy.premium` 参数化（多轮跑不同值）才有梯度 |

### 9.3 参数化契约（解锁第 1/6 维）

第 1 维（box 宽度）与第 6 维（突破强度）的梯度实验需要把当前固定值参数化：

| 参数 | 当前值 | 参数化键 | 解锁维度 | 改动位置 |
|------|--------|---------|---------|---------|
| box 周期长度 | 3 日（固定） | `box.period`（待加） | 第 1 维 | `internal/box` 三日法则→N 日法则 + migration 加参数 |
| 突破溢价 | ×1.03（固定） | `buy.premium`（已存在但单一） | 第 6 维 | 多轮跑不同值（1.01/1.02/1.03/1.05），每轮独立回测库 |

- **参数化铁律**：参数化后，**每一轮回测必须用单一参数值跑完全部年份**，不得在同一年内混用不同参数——跨年可比性依赖于此。
- **参数轮次隔离**：每轮参数用独立回测库（如 `bt_e17_<year>_p3.db` 表示 box.period=3），不覆盖默认库。

### 9.4 离线分析脚本的只读契约

`scripts/e17_watch.py`、`scripts/e17_annual_tier_report.py` 等离线分析脚本：

- ✅ 只读查询 `bt_e17_<year>.db`（`mode=ro`，URI 连接）。
- ✅ 用 `json.loads(reason_json)` 解析字段，缺失字段返回 None。
- ❌ 不得反向修改回测库的任何表或字段。
- ❌ 不得在分析脚本里重算 box 几何或信号——一切以回测器写入的 reason_json 为准。
- ❌ 不得编造未产出的字段（如当前无 `volume_ratio`，报告必须诚实标注"该维度需扩展回测器"，不留空也不填 0）。

