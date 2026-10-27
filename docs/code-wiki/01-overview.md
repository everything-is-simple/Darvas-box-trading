# 01 · 项目总览

[← Wiki 索引](README.md) | [下一篇：架构 →](02-architecture.md)

---

## 1. 项目定位

Darvas-box-trading 是对尼古拉斯·达瓦斯《我如何在股市赚了 200 万》交易体系的软件化复刻，目标不是"做一个全能量化平台"，而是把一套**纪律**固化为每个交易日固定时点执行的流水线：

```
数据同步 → 全市场箱体重算 → 四条件初选 → 跟踪池状态迁移
→ 信号判定 → 六问交易计划 → 日报 + 备份 → 人工挂单/录成交
```

系统每天用书面计划回答六个问题：买什么、何时买、买多少、卖什么、何时卖、卖多少。所有决策写入数据库并可审计，日报是最终人机接口。

### 1.1 它是什么

- 单机、离线、可移植的 A 股盘后决策系统
- 以 SQLite 为唯一事实源，成交（fill）为账本唯一事实源
- 人类负责：通达信下载数据、次日券商挂单、成交回录、复盘
- 机器负责：识别箱体、筛选、跟踪、信号、仓位、订单、日报、备份、回测

### 1.2 它不是什么

- 不是自动下单系统（无券商 API）
- 不是多策略平台（理论单一）
- 不是 Web 服务（默认监听 `127.0.0.1`，只读 GUI）
- 不是实时行情/日内系统（只处理日线，收盘后运行）

---

## 2. 设计原则（代码中可验证）

| 原则 | 代码体现 |
| --- | --- |
| **理论单一** | 全仓库只出现达瓦斯箱体规则；`archive/` 记录了引入其他理论的证伪教训 |
| **确定性引擎** | `box.Engine.Scan` 是零 IO 纯函数，同输入必同输出；单测即规范 |
| **数据层唯一入口** | 业务包不直接持有 `*sql.DB`，统一经 `store.Store` 与 Repository |
| **幂等重跑** | 导入 `INSERT OR REPLACE`、箱体按 `(top,bottom)` 复用、信号同日去重、计划头 upsert |
| **诚实审计** | `daily_run` 心跳、`reason_json` 记录决策依据、`equity_snapshot` + `AuditEquation` |
| **零外部依赖部署** | 纯 Go SQLite + `embed` 前端 + `net/http`，无 Node/CGO/CDN |
| **只读 GUI** | GUI 只提供 GET 查询；所有状态修改走 CLI |
| **时间集中管理** | 调度时刻只在 `scheduler.Schedule` 定义，CLI/GUI/测试/一致性检查全部派生 |

---

## 3. 仓库地图

| 路径 | 角色 | 关键入口 |
| --- | --- | --- |
| `cmd/darvas/main.go` | CLI 入口、子命令分发、serve 装配 | `main`、`runServe`、`runPipeline` |
| `internal/contract/` | 跨模块类型 + 接口（L0） | `contract.go` |
| `internal/datasource/tdx/` | 通达信 `.day`/`.tnf` 读取 | `Reader` |
| `internal/ingest/` | 全量/增量导入、质量校验 | `Ingester.FullImport/SyncIncremental/Verify` |
| `internal/store/` | SQLite 唯一入口、迁移、8 个仓储 | `Open`、`Store`、`*Repo` |
| `internal/box/` | 箱体状态机 + 面向 DB 的扫描器 | `Engine.Scan`、`Scanner.Rescan*` |
| `internal/screener/` | 四条件初选 | `Screener.Screen` |
| `internal/pool/` | 跟踪池状态机 | `Manager.Sync/Suspend/Remove` |
| `internal/signal/` | 首次事件信号 | `Engine.Evaluate` |
| `internal/planner/` | 六问交易计划 | `Planner.Build` |
| `internal/ledger/` | 成交解析/持仓/权益/审计 | `Ledger.ApplyFill`、`ParseFill` |
| `internal/match/` | 模拟撮合 | `Matcher.Match` |
| `internal/pipeline/` | 七任务编排 + 备份 | `Pipeline.RunDaily/RerunTask` |
| `internal/scheduler/` | 常驻调度 + CatchUp | `Scheduler.Start/CatchUp` |
| `internal/reporter/` | 日报/周报 HTML | `Report`、`Weekly` |
| `internal/gui/` | 本地 HTTP UI | `gui.Run`、`Server.routes` |
| `internal/regime/` | 市场体制标注与报告 | `ClassifyDate`、`ClassifyDateA`、`Run` |
| `internal/regimesweep/` | 参数梯度实验 | `Run` |
| `internal/backtest/` | 历史回放 | `Run`、`Replay`、`Compare` |
| `internal/position/` | 空占位包 | 无代码（腿管理已并入 store/planner） |
| `scripts/` | 构建、一致性、Python 研究脚本 | `build.bat`、`check_consistency.py` |
| `docs/` | SSoT + 实验纲领 + 本 Wiki | `docs/README.md` |

---

## 4. 技术栈与选型

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 语言 | Go 1.27（`go.mod` 声明） | 单文件编译、启动快、跨平台 |
| SQLite 驱动 | `modernc.org/sqlite` | 纯 Go、无 CGO，单 exe 零环境依赖 |
| 存储模式 | WAL + `MaxOpenConns(1)` | 单写者避免 write lock；WAL 允许 GUI 读与调度写并行 |
| GUI | `net/http` + `embed` 单页 | 无 Node 构建链、无外部 JS 库，canvas 自绘 |
| 报告 | `html/template` | 离线 HTML，浏览器直接打开 |
| 调度 | 自研 goroutine + ticker | 只需每日固定时刻 + 补跑，无需外部 cron |
| 数据源 | 通达信 `vipdoc` 日线 + CSV 回流 | 离线、稳定、无需行情 API |

依赖总数极少：`go.mod` 只有 SQLite 与 `golang.org/x/text`（GBK 解码）等少数包。

---

## 5. 关键术语

| 术语 | 含义 |
| --- | --- |
| 箱体（box） | 由确认上轨与确认下轨围成的价格区间；状态 `forming/confirmed/broken_up/broken_down/invalid` |
| 三日法则 | 连续 3 日未创新高确认上轨；连续 3 日未创新低确认下轨（`ConfirmDays` 可参数化） |
| 阶梯（ladder） | 按时间串联的箱体链，`prev_box_id` 指向最近一个 confirmed 箱体 |
| 突破（breakout） | 收盘 > 上轨 × premium，且放量，且非一字板 |
| 首次事件语义 | 信号只在状态迁移当日发一次，条件持续满足不重复发 |
| 跟踪池（watch） | 候选股的六阶段生命周期：watching/pending/holding/exited/suspended/blacklisted |
| 单位（unit） | 仓位计量：1 单位 = 权益 × 2%；A 判据升势日首买 2 单位，其余 1 单位 |
| 六问 | 买什么、何时买、买多少、卖什么、何时卖、卖多少 |
| A 判据 | 上证 60 日低点相邻日比：低点抬升=升势；用于首买单位，不用于择时 |
| scratch 库 | 回测专用目标库，参数覆盖只写 scratch，不污染生产库 |
| 体制（regime） | Bull/Bear/Range；宽度法供报告，A 判据供仓位 |

---

## 6. 规模与状态指标

| 指标 | 数值 |
| --- | --- |
| 版本 | `0.5.0-m8` |
| 迁移版本 | 17（0001–0017） |
| 业务表 | 16 张表（15 业务表 + `schema_version`） |
| 业务包 | 18（另有空 `internal/position`） |
| Go 测试函数 | 178 |
| GUI API | 8 个 GET 端点 |
| 调度任务 | 每日 7 个 + 周六 weekly |
| 回测模式 | prefill / progressive / verify / regime |

> 状态类信息（里程碑完成度、观察期进度）以 `docs/05-task-breakdown.md` 与 `docs/30-system-prediction.md` 为准；本 Wiki 只描述代码事实。

---

## 7. 与 SSoT 文档的关系

```
docs/00 理论宪法        ← 代码注释引用规则条目
docs/01 需求/时刻表/参数 ← scheduler.Schedule + migrations 种子
docs/02 架构             ← import 图与分层
docs/03 ERD              ← migrations/*.sql
docs/04 API 契约         ← contract.go（双向强制同步）
docs/06 一致性机制       ← scripts/check_consistency.py
```

Code Wiki 不复制 SSoT 全文，只补充：

- 代码结构、函数职责、调用链
- 实现细节与历史坑（archive/07 等修复注释）
- 开发者上手路径、构建/测试/排障
