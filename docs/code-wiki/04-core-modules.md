# 04 · 核心模块

[← 上一篇：数据模型](03-data-model.md) | [Wiki 索引](README.md) | [下一篇：编排层 →](05-orchestration.md)

> 本页覆盖 L0–L2：契约、数据源、导入、箱体、初选、跟踪池、信号、计划、账本、撮合。
> 编排层（pipeline/scheduler/reporter/gui/backtest/regime/regimesweep）见 [05](05-orchestration.md)。

---

## 1. `internal/contract` — 跨模块契约

**职责**：集中定义所有跨包 DTO、枚举与接口，纯类型无业务实现，只依赖标准库。是模块之间唯一的编译期契约。

**文件**：[contract.go](../../internal/contract/contract.go)

### 1.1 关键类型

| 类型 | 说明 |
| --- | --- |
| `DailyBar` | 日线（Code/Date/OHLC/Volume/Amount） |
| `Security` | 证券基础（含 `TrackLevel`） |
| `BoxStatus` | `forming/confirmed/broken_up/broken_down/invalid` |
| `Box` | 箱体（Top/Bottom/确认日/PrevBoxID/BrokenDate）；`ID` 在引擎输出中是**局部序号** |
| `SignalType` | `breakout/pyramid/stop/failed_breakout`（`pyramid` 保留枚举但不再产生） |
| `Signal` | 信号（Date/Code/Type/BoxID/ClosePx/BoxLevel/Payload） |
| `Candidate` | 初选候选（Score/Reasons/FundamentalFlag） |
| `WatchStage` | 六状态 |
| `TrackLevel` | `normal/winner/blacklist` |
| `Watch` | 跟踪池条目（含 `ResumeTrigger`） |
| `PlanOrder` / `TradePlan` | 计划订单/计划头 |
| `Fill` / `Position` / `EquitySnapshot` | 账本 DTO |
| `TaskResult` | 任务结果（Task/Date/Status/AffectedRows/DurationMS/Error） |
| `BoxParams` / `ScreenParams` / `PlanParams` | 参数载体 |

### 1.2 接口分层

| 层 | 接口 |
| --- | --- |
| 数据源 | `DayFileReader` |
| 仓储 | `BarRepo`、`BoxRepo`、`PoolRepo`、`SignalRepo`、`TradeRepo`、`RunRepo`、`ParamRepo` |
| 领域 | `BoxEngine`、`BoxScanner`、`Screener`、`PoolManager`、`SignalEngine`、`Planner`、`Ledger` |
| 编排 | `Pipeline`、`Scheduler`、`Ingester` |

**重要约定**：`contract.go` 中的仓储接口是构造期最小示意；完整仓储 API 的事实源是 `docs/04-api-contract.md §3.3`，实现类型在 `store` 包。不要为「补齐接口」而把 store 类型引入 contract（会造成 import 循环）。

---

## 2. `internal/datasource/tdx` — 通达信读取

**职责**：解析 `vipdoc` 下的 `.day` 日线文件与 `.tnf` 名称表，不触碰数据库。

**文件**：[reader.go](../../internal/datasource/tdx/reader.go)、[names.go](../../internal/datasource/tdx/names.go)

| 函数 | 说明 |
| --- | --- |
| `NewReader(root)` | `root` 指向 `vipdoc`（如 `H:/new_tdx64/vipdoc`） |
| `ListCodes()` | 枚举 sh/sz 全部代码（排除北交所/新三板） |
| `ReadBars(code, since)` | 32 字节定长记录 → `[]DailyBar`，升序，`since` 之后（含） |
| `ModTime(code)` | 文件 mtime，增量导入依据 |
| `ReadNames()` | `.tnf`（360 字节/条，+31 偏移 GBK 名）→ 代码→名称 |

- `.day` 记录 32 字节：日期/开高低收/成交额(float32)/量(uint32)/保留。
- 名称是可选增强数据：缺失只告警不失败。

---

## 3. `internal/ingest` — 数据导入

**职责**：把 TDX 文件写入 `daily_bar`/`security`，并提供新鲜度与更新量校验。

**文件**：[ingest.go](../../internal/ingest/ingest.go)

| 函数 | 说明 |
| --- | --- |
| `New(reader, st, fullSince)` | `fullSince` 空/`full` → `2024-01-01`；`all` → 全历史 |
| `FullImport(ctx)` | 幂等全量（`INSERT OR REPLACE`）；导入期降级索引、批量重建 |
| `SyncIncremental(ctx, date)` | 只读当日 mtime 之后变化的文件，从库内最新日期续读 |
| `Verify(ctx, date)` | 以 `sh000001` 为交易日历权威，检查新鲜度/更新量 |
| `syncNames()` | 回填 `security.name`，失败仅告警 |

**性能策略**（[docs/05]）：索引降级 + 跨 code 累积缓冲（2000 code/事务）+ 多行 VALUES + `synchronous=NORMAL`。

---

## 4. `internal/box` — 箱体引擎与扫描器

### 4.1 `Engine.Scan`（纯函数，`internal/box/engine.go`）

**职责**：把单只股票升序 K 线扫描为箱体阶梯。零 IO、可并发、同输入必同输出。

状态机：

```
forming(确认上轨) --3日无新高--> forming(确认下轨) --3日无新低+宽度达标--> confirmed
   │ 创新高(重置)                  │ 创新低(重置)              │ 收盘>上轨×premium 且放量 且非一字板
   │                              │ 下轨确立前创新高→invalid   ↓ → broken_up（新箱体 prev=本箱）
   │                              │ 从新高重启                 │ 收盘<下轨 → broken_down
```

| 函数/类型 | 说明 |
| --- | --- |
| `New()` | 无状态引擎 |
| `Engine.Scan(bars, params)` | 输出按时间排序的 `[]Box`；`ID` 为局部 1..N；`PrevBoxID` 指向最近 confirmed 箱体 |
| `isBreakout(i, bars, cur)` | 收盘 > 上轨×`BreakPremium` ∧ 量 ≥ 前 `VolWindow` 日均量×`VolMultiplier` ∧ 非一字板 |
| `avgVolumeBefore` | 突破日**之前** N 日均量 |
| `isOnePriceBoard` | 开=高=低=收（1e-9 容差） |
| `DefaultParams()` | 与迁移种子一致 |

要点：每根 K 线必属且仅属一个箱体；突破/跌破/作废当日即开启下一个箱体；`invalid` 箱体不计入阶梯。

### 4.2 `Scanner`（`internal/box/scanner.go`）

**职责**：读 DB K 线窗口 → `Engine.Scan` → `UpsertBoxes`。

| 函数 | 说明 |
| --- | --- |
| `NewScanner(eng, st)` | 注入引擎与存储 |
| `rescanOne(ctx, code, date)` | 单股读窗（800 根）→ 扫描 → 持久化 |
| `RescanAll(ctx, date)` | 全市场重算（首次/参数变更） |
| `RescanIncremental(ctx, date)` | 跟踪池全量 + 其余全市场预过滤 |
| `prefilter(...)` | 创 60 日新高 ∨ 当日量≥均量×倍数 ∨ 跌破最新 confirmed 下轨 |

**依赖**：`contract` + `store`。**注意**：必须使用 `LatestNonFormingBox` 而非 `LatestBox`，否则会取到 `forming(0,0)` 使下游判定全部失效（历史 bug archive/07 #1）。

---

## 5. `internal/screener` — 四条件初选

**职责**：对活跃 A 股逐只打分，写入 `candidate` 快照。

**文件**：[screener.go](../../internal/screener/screener.go)

| 条件 | 判定 | 分值 |
| --- | --- | --- |
| 1 流动性 | 5 日均成交额 ≥ `screen.turnover` | 25 |
| 2 RS 超额 | 个股 20 日涨幅 − 上证 20 日涨幅 ≥ `screen.rs_excess_min` | 25 |
| 3 放量比 | 5 日均量 / 20 日均量 ≥ `2.0`（硬编码；文件头注释写 `[2.0,5.0]`，但实现未设上限） | 25 |
| 4 箱体形态（**门槛**） | 最新非 forming 箱体为 `confirmed`，或 `broken_up` 且突破后 < `LateDays` 交易日 | 25 |

| 函数 | 说明 |
| --- | --- |
| `Screen(ctx, date)` | 取 `ActiveStockCodes` → 评估 → 过滤条件 4 → 按分降序 → `AppendCandidates` |
| `evaluate(code, date, p, idxRet)` | 返回 reasons 明细与分数 |
| `retOver(st, code, date, n)` | `close(today)/close(n日前)`；不足返回 false |

要点：

- 条件 4 是**候选门槛**：`box_pass` 不存在直接丢弃。
- ST 排除在 `repo_sec.ActiveStockCodes`（按名称前缀），不是筛选项。
- 旧比值法 `screen.rsthreshold` 已退役，改为超额收益百分点。
- 太晚规则：突破后已走过 ≥ `screen.latedays` 个交易日 → 不入池。

---

## 6. `internal/pool` — 跟踪池状态机

**职责**：候选入池、当前箱体更新、跌破/连箱淘汰、黑名单解禁、条件挂起恢复。

**文件**：[pool.go](../../internal/pool/pool.go)

| 函数 | 说明 |
| --- | --- |
| `Sync(ctx, date)` | ①候选入池 ②在池更新/出池 ③生命周期巡检 |
| `lifecycle(date)` | 黑名单冷却到期 → normal；suspended 收盘 ≥ `resume_trigger` → watching |
| `MarkFundamental(code, date, flag)` | 人工标记 yes/no |
| `Suspend(code, trigger)` | 条件挂起 |
| `Remove(code, reason)` | 人工移除 |

**入池规则**：

- 新候选且无活跃 watch → `watching`，`added_date=date`，`current_box_id=最新箱体`。
- `pool.maxwatch` 仅软告警（原书电报费约束已废弃）。
- 曾交易过的股票重新入池 → 查 `TrackRecordByCode`，在 `watch.note` 写入历史战绩提示。

**出池规则**：

- 最新非 forming 箱体 `broken_down` → `exited`（`box_broken_down`）。
- 换箱即 `box_count++`；`box_count >= pool.flatboxmax` → `exited`（`flat_too_long`）。
- 首次入池不累加 `box_count`。

**状态补充**：`holding/pending` 由 planner/ledger 迁移；`blacklisted` 拉黑在清仓时由 `TradeRepo.ClosePosition` 完成。

---

## 7. `internal/signal` — 首次事件信号

**职责**：对在池股票产生当日首次状态迁移信号，append-only。

**文件**：[signal.go](../../internal/signal/signal.go)

| 函数 | 说明 |
| --- | --- |
| `Evaluate(ctx, date)` | 遍历 watching/pending/holding → `evalOne` → `AppendSignals` |
| `evalOne(w, date, premium)` | 优先级 `stop > failed_breakout > breakout`；`holding` 再突破不发信号（不加码） |
| `checkFailedBreakout(...)` | 最近 `broken_up` 箱体 3 个日历日内，首次收盘跌回上轨下方 |
| `withinDays(brokenDate, date, n)` | 日历日近似 |

**首次事件判定**：取最近 2 根 K 线，`today` 必须等于 `date`；只有当「昨日不满足、今日满足」才发信号。

| 信号 | 条件 |
| --- | --- |
| `stop` | 今收 < 最新箱体下轨，且昨收 ≥ 下轨 |
| `failed_breakout` | broken_up 3 日内，今收 < 上轨，且昨收 ≥ 上轨 |
| `breakout` | 箱体 `broken_up` 且 `broken_date == date`；持仓股不生成 |

---

## 8. `internal/planner` — 六问交易计划

**职责**：信号 + 持仓 + 权益 + 参数 → 下一交易日 `trade_plan` / `plan_order`。

**文件**：[planner.go](../../internal/planner/planner.go)

| 函数/类型 | 说明 |
| --- | --- |
| `Build(ctx, planDate)` | 主流程：读信号/持仓 → 止损上移 → 生成买卖单 → 建计划 → watch 迁移 → 过期订单 → 权益快照 |
| `regimeLabel(date)` | `regime.ClassifyDateA`：升势 2 单位，其余 1 单位 |
| `raiseStops(positions, sellDisc)` | 最新 confirmed 箱体下轨×sellDisc 高于现止损则上移 |
| `buyOrder(...)` | 停损买单：trigger=上轨×premium，stop=上轨×sellDisc，1 手起 |
| `sellOrder(...)` | 停损卖单：一次全出，trigger=下轨×sellDisc |
| `unitShares(unitValue, price)` | `floor(unitValue/price/100)×100` |
| `lotQty(shares, units)` | 按单位数缩放整手 |
| `nextTradingDate(planDate)` | 优先取 `sh000001` 未来最近日期；无未来数据退化为下一工作日 |
| `newHighLadder` | E17 实验开关（`pos.newhigh_ladder=on`）按距 52 周新高分档 |

### 8.1 买入规则（当前生产）

1. 只做**首次信号**：已持仓一律不再买（`pyramid` 退役）。
2. 首买单位：A 判据升势 2，其余 1；实验开关可改 52 周新高分档。
3. `trigger = box.Top × buy.premium`；若 `ClosePx/trigger > match.max_entry_gap` → 拒单（陈旧箱体）。
4. 止损 = `box.Top × sell.discount`（被突破箱体上轨下方，紧止损）。
5. 同时持仓数 ≥ `pool.max_hold` → 不再开新仓。
6. 入池不足 `pool.min_watch_days` 个交易日 → 观察期拦截（仅首买）。
7. `gate.ma60.mode`（实验）：`stop` 拦截，`half` 半仓。
8. 同日同票只生成一张买单。

### 8.2 卖出规则

| 来源 | 触发 |
| --- | --- |
| `stop` / `failed_breakout` 信号 | `box.Bottom × sell.discount`，全出 |
| `time_stop_noprofit` | 持有交易日 ≥ `position.staledays` 且收盘 ≤ 成本 |
| `initial_stop` | 每个无卖出单的持仓，按 `position.stop_price` 挂常驻止损 |

**防重**：`pendingSell` 集合确保同批次同票只有一张卖单（历史 M8.3 bug）。

**执行日**：由 `nextTradingDate` 推断；实盘无未来数据时退化为下一工作日，节假日偏差由订单过期兜底。

---

## 9. `internal/ledger` — 账本

**职责**：成交解析、持仓状态机、权益重算、恒等式审计。

**文件**：[ledger.go](../../internal/ledger/ledger.go)

| 函数/类型 | 说明 |
| --- | --- |
| `CanTransition(from,to)` / `SetOrderStatus` | 订单状态机校验 |
| `ParseFill(raw)` | 支持同花顺/通达信/CSV 三种文本格式；失败即拒绝 |
| `ApplyFills(fills)` | 批量应用；同 code+date+side+price+qty 幂等跳过 |
| `ApplyFill(f)` | 单笔：匹配 pending 订单 → 买开/加码或卖减/清仓 |
| `applyBuy` / `applySell` | 持仓与腿更新、止损上移、清仓流转 |
| `onPositionClosed` | watch `holding/pending → exited` |
| `computeEquity(date)` | 现金 = 本金 + Σ卖出 − Σ买入；市值 = open 持仓×收盘 |
| `Snapshot(ctx, date)` | 权益快照，幂等覆盖，peak/drawdown 衔接 |
| `AuditEquation(ctx, date)` | 权益恒等式 + 持仓数量闭合 + filled 订单必有成交 |

**清仓分级**（`TradeRepo.ClosePosition` → `applyTrackTransition`）：

- 盈利 → `winner`
- `winner` 亏损 → `normal`
- 普通/黑名单亏损 → `blacklist`，冷却 `pool.blacklist_months`（0 = 不拉黑，<0 = 永久）

**已实现盈亏**：按该持仓生命周期内全部成交净现金流计算，不信任券商盈亏列。

---

## 10. `internal/match` — 模拟撮合（仅回测）

**职责**：按日撮合 `pending` 停损单，处理跳空、滑点、涨跌停、除权与费用。

**文件**：[match.go](../../internal/match/match.go)

| 函数 | 说明 |
| --- | --- |
| `New(st)` / `Match(ctx, date)` | 撮合 `execute_date==date` 的订单；除权日强制清仓 |
| `matchOrder(side, trigger, limit, bar)` | 上/下穿触发；跳空按开盘价（最坏价） |
| `isLimitBoard(bar, prevClose)` | 一字板不成交（按昨收方向） |
| `slip(side, px, slippage)` | 买贵卖贱 |
| `feeOf(side, px, qty, p)` | 佣金（最低 5）+ 印花税（卖）+ 过户费 |
| `isDividendGap(...)` | 单日跌幅超涨跌停限制 → 视为除权 |
| `gapThreshold(code)` | 主板 10%，创业板/科创板 20%，留 1% 余量 |

**买入闸门**：成交价/trigger > `match.max_entry_gap` → 弃单记 `skipped_gap`。

**调用时序**：必须在当日 `plan` 之后、`report` 之前（回测 `Replay` 中体现）。生产不使用撮合器。

---

## 11. 模块协作速查

```
tdx.Reader ──▶ ingest.Ingester ──▶ daily_bar
                                      │
box.Scanner ◀── box.Engine ◀──────────┘
     │
     ▼ box
screener.Screener ──▶ candidate ──▶ pool.Manager ──▶ watch
                                                      │
signal.Engine ◀───────────────────────────────────────┘
     │ signal
     ▼
planner.Planner ──┬──▶ trade_plan / plan_order
                  ├──▶ watch.stage / position.stop_price
                  └──▶ equity_snapshot
trade_fill ──▶ ledger.Ledger ──▶ position / position_leg / equity_snapshot / track_level
match.Matcher（仅回测）──▶ 生成 trade_fill
```

| 想改什么 | 首选文件 |
| --- | --- |
| 箱体识别规则 | `box/engine.go` |
| 每日重算范围 | `box/scanner.go` |
| 初选条件 | `screener/screener.go` |
| 入池/出池 | `pool/pool.go` |
| 信号触发 | `signal/signal.go` |
| 买卖价/仓位/止损 | `planner/planner.go` |
| 成交入账/盈亏 | `ledger/ledger.go` + `store/repo_trade.go` |
| 回测成交假设 | `match/match.go` |
