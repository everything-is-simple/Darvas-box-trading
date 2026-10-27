# 05 · 编排层与入口

[← 上一篇：核心模块](04-core-modules.md) | [Wiki 索引](README.md) | [下一篇：运行方式 →](06-running.md)

> 覆盖 L3–L4：pipeline、scheduler、reporter、gui、backtest、regime、regimesweep、cmd/darvas。

---

## 1. `internal/pipeline` — 盘后流水线

**职责**：严格串行执行七个任务，每步写 `daily_run` 心跳；尾部自动备份。

**文件**：[pipeline.go](../../internal/pipeline/pipeline.go)、[backup.go](../../internal/pipeline/backup.go)

| 函数 | 说明 |
| --- | --- |
| `New(st, ingester)` | 注入存储与导入器；内部构造 Scanner/Reporter |
| `RunDaily(ctx, date)` | ingest → boxscan → screen → poolsync → signal → plan → report → BackupDaily |
| `RerunTask(ctx, date, task)` | 幂等单任务补跑（含 `weekly`），供 CLI 与调度器共用 |
| `runTask(...)` | `StartRun` → 执行 → `FinishRun` 留痕 |
| `SetReportDir(dir)` | 回测时重定向日报目录，绝不污染 `var/reports` |
| `ReportPath/WeeklyPath` | 报告路径查询 |
| `BackupDaily(date)` | `VACUUM INTO var/backup/darvas_YYYYMMDD.db` |
| `rotateBackups(dir)` | 按字典序保留最近 30 份 |

**失败策略**：

- `ingest` 失败 → 整链中止。
- `plan` 失败 → 标红，已完成任务保留，日报仍生成。
- 其他失败 → 记录并继续（可单任务补跑）。

**注意**：`RunDaily` 不包含 `match`；生产成交来自人工录入。备份不占 `daily_run` 心跳位。

---

## 2. `internal/scheduler` — 常驻调度

**职责**：交易日固定时刻触发任务链；启动补跑；周六周报。

**文件**：[scheduler.go](../../internal/scheduler/scheduler.go)

| 类型/函数 | 说明 |
| --- | --- |
| `TaskTime` / `Schedule` | 时刻表（`Clock` + `Task`），唯一事实源 |
| `WeeklyClock` | `"08:00"`（周六） |
| `ScheduleSummary()` | 生成 CLI/GUI 用摘要，避免数字散落 |
| `Runner` | `RerunTask` 接口（`*Pipeline` 满足，测试可注入假实现） |
| `New(st, runner, reader)` | `reader` 可为 nil（只查 DB 判交易日） |
| `CatchUp()` | 前一交易日无任何 ok → 整日补跑；今日已过点补跑；周六 weekly |
| `Start()` / `Stop()` | 每 30s tick；Stop 取消 ctx 并等待收尾 |
| `fireDue` / `runOverdue` | 到点消费 + 按顺序补跑未 ok 任务 |
| `fireWeekly` | 周六到点且未 ok → weekly |
| `OnDayDone` | report ok 后钩子（serve 接自动备份） |
| `isTradingDay` | `sh000001` 在 DB 或有当日 TDX 文件 |

**并发安全**：`running` 标志防重入；`fired[task]` 按日期只消费一次；`doneFired[date]` 只收尾一次。

**注意**：注释里 `fireWeekly` 写「周六 10:00」是历史残留，实际常量 `WeeklyClock=08:00`。

---

## 3. `internal/reporter` — 日报与周报

**职责**：把任务心跳、候选、信号、计划、持仓渲染为离线 HTML。

**文件**：[reporter.go](../../internal/reporter/reporter.go)、[weekly.go](../../internal/reporter/weekly.go)

| 函数 | 说明 |
| --- | --- |
| `New(st, outDir)` | 默认 `var/reports` |
| `Report(ctx, date)` | 生成 `daily_YYYYMMDD.html` |
| `Weekly(ctx, date)` | 生成周报（锚定周六，覆盖前 7 天） |
| `sparkline(eqs)` | 权益曲线 SVG polyline |
| `Path/WeeklyPath` | 路径查询 |

模板使用 `html/template`，无外部 CDN/JS 依赖。

---

## 4. `internal/gui` — 本地只读 UI

**职责**：`embed` 单页前端 + 8 个只读 JSON API。

**文件**：[gui.go](../../internal/gui/gui.go)、`internal/gui/static/index.html`

| 组件 | 说明 |
| --- | --- |
| `New(st)` | 只接收只读 Store |
| `Handler()` | 路由表（`darvas ui` 与 `httptest` 共用） |
| `Run(st, addr, openBrowser)` | 监听 + 可选打开浏览器 |
| `handleDashboard` | 心跳 + 计划订单 + 权益 |
| `handleDates` | 有数据的日期（UNION 心跳/候选/计划/权益） |
| `handleWatch` | 跟踪池看板 |
| `handleCandidates` / `handleSignals` | 按日快照 |
| `handleStock` | K 线 + 箱体阶梯（`days` 或 `from/to`） |
| `handleTrades` | 计划/成交/持仓/权益四合一 |
| `handleSchedule` | 从 `scheduler.Schedule` 派生 |

**只读原则**：所有端点为 GET；nil 切片序列化为 `[]` 而非 `null`；前端纯原生 JS。

---

## 5. `internal/backtest` — 历史回放

**职责**：在独立 scratch 库重放生产任务链，用于验证与参数实验。

**文件**：[backtest.go](../../internal/backtest/backtest.go)

| 类型/函数 | 说明 |
| --- | --- |
| `Mode` | `prefill / progressive / regime / verify` |
| `Options` | 源库、窗口、scratch、参数覆盖等 |
| `Run(o)` | 模式分发 |
| `Bootstrap(src, scratch, from, progressive)` | 整库拷贝 → 清事务态 →（progressive）裁未来 K 线 |
| `Replay(scratch, days, dayBars)` | 逐日 `RerunTask` 六链路 + `match` |
| `boxStateHash(st)` | 箱体全表语义哈希 |
| `Compare(dbA, dbB, ...)` | 比较全部决策输出 |
| `runVerify` | progressive 为地面真值与 prefill 双回放 |
| `runRegime` | 长窗口 + 分体制报告 |

**关键约束**：

- 源库以 `mode=ro` 只读打开，物理上禁止写。
- scratch 是一次性库，存在即删重建。
- progressive 逐日灌 K 线，未来数据物理缺席，是差分审计的地面真值。
- prefill 更快，但依赖查询边界自律；两者语义哈希必须全等。
- 回测日报目录重定向，避免污染生产报告。

---

## 6. `internal/regime` — 市场体制

**职责**：三种互相独立的标签能力 + 分体制报告。

**文件**：[regime.go](../../internal/regime/regime.go)

| 函数 | 用途 |
| --- | --- |
| `ClassifyDate(st, date, ...)` | **宽度法**：全市场 broken_up/broken_down 20 日滚动比率，供报告 |
| `ClassifyDateA(st, date)` | **A 判据**：上证 60 日低点相邻日比，供 planner 仓位 |
| `CloseVsMA(st, date, n)` | 上证收盘 vs MA60，供 MA60 闸门实验 |
| `LabelBoxes(bars, params)` | 用箱体引擎给指数序列打标签（历史/测试） |
| `Summarize(equities, labels, ...)` | 按体制切分收益/胜率/暴露 |
| `Run(ctx, st, from, to, outDir)` | 生成分体制 HTML 报告 |

要点：

- `ClassifyDateA`：升势 = 当前 60 日低点 > 前一日 60 日低点×1.002；降势反之；历史不足返回 Range。
- 宽度法与 A 判据**互不替代**：前者是市场温度计，后者是下轨速度计。
- 指数体制择时（M8.7）已证伪退役，planner 不再据此择时。

---

## 7. `internal/regimesweep` — 参数梯度扫描

**职责**：对箱体五参数做全网格 + 邻域稳定性 + 分年度收益评估，生成 HTML 报告。

**文件**：[sweep.go](../../internal/regimesweep/sweep.go)

| 函数/类型 | 说明 |
| --- | --- |
| `Combo` / `Baseline()` | 五参数（premium/宽度/放量/窗口/确认日） |
| `AllCombos()` | 480 组全网格 |
| `DefaultYears()` | 四个评价年份（对应已生成的回放库） |
| `Run(src, out)` | 读全历史源库 + 单年回放库 → 评估 → HTML |
| `computeQualification` | 七条先验 guard |
| `computeNeighborhood` | 邻域达标率（防过拟合） |
| `buildReport/buildConclusion` | OFAT 敏感性、二维矩阵、分年分体收益 |

不写生产库，只读源库与 scratch 回放库。

---

## 8. `cmd/darvas` — CLI 入口

**文件**：[main.go](../../cmd/darvas/main.go)

### 8.1 子命令分发

| 命令 | 处理函数 | 作用 |
| --- | --- | --- |
| `version` | — | 打印 `0.5.0-m8` |
| `ingest` | `runIngest` | 增量/全量导入 + Verify |
| `doctor` | `runDoctor` | schema/数据量/日历/触发器/恒等式体检 |
| `ladder <code>` | `runLadder` | 单股箱体阶梯（boxscan 缩影） |
| `param get/set` | `runParam` | 参数读写 |
| `run` | `runPipeline` | 完整盘后链 + 打开日报 |
| `boxscan/screen/poolsync/signal/plan/report` | `runRerun` | 单任务幂等补跑 |
| `pool list/suspend/remove` | `runPool` | 跟踪池人工操作 |
| `review list/add` | `runReview` | 已交易股票历史库/复盘 |
| `weekly` | `runWeekly` | 周报（默认最近周六） |
| `fills [file]` | `runFills` | 成交文本/炒股手 CSV 录入 + 审核 |
| `ui` | `runUI` | 只读 GUI |
| `serve` | `runServe` | 调度 + GUI + 备份 |
| `backtest` | `runBacktest` | 内置回放器 |

### 8.2 全局标志

| Flag | 默认 | 说明 |
| --- | --- | --- |
| `--date` | 今天 | 交易日 |
| `--db` | exe 同目录 `var/darvas.db` | 也可用 `DARVAS_DB` |
| `--since` | `2024-01-01` | ingest 起始；`full`/`all` 触发全量 |
| `--yes` | false | fills 直接入库 |
| `--addr` | `127.0.0.1:8787` | ui/serve 监听 |
| `--resume` | 0 | pool suspend 激活价 |
| `--reason` | 空 | pool remove 原因 |
| `--lesson/--error-type/--rule` | 空 | review add 字段 |
| `--from/--to/--src/--mode/--set` | 空 | backtest 参数 |

**路径解析**：`--db` > `DARVAS_DB` > `exe目录/var/darvas.db`；TDX 根优先读 `param tdx.root`（0001 种子默认 `H:/new_tdx64/vipdoc`），仅当该参数缺失时才回退 `DARVAS_TDX` 或硬编码默认。

### 8.3 `serve` 装配

```
stW = store.Open(db)        // 调度写
stR = store.Open(db)        // GUI 读
pipe = pipeline.New(stW, ingest.New(tdx.NewReader(root), stW, ""))
sch  = scheduler.New(stW, pipe, reader)
sch.OnDayDone = pipe.BackupDaily
sch.CatchUp(); go sch.Start()
gui.Run(stR, addr, false)
```

**注意**：两个 `*Store` 是 WAL 双连接读写分离；停止时必须先 `sch.Stop()`。

---

## 9. 编排层依赖速查

| 包 | import |
| --- | --- |
| `pipeline` | box、ingest、planner、pool、reporter、screener、signal、store、contract |
| `scheduler` | store、contract |
| `reporter` | ledger、store、contract |
| `gui` | scheduler、store、contract |
| `backtest` | match、pipeline、regime、store、contract |
| `regime` | box、store、contract |
| `regimesweep` | regime、contract |
| `cmd/darvas` | backtest、box、gui、ingest、ledger、pipeline、pool、scheduler、store、tdx |
