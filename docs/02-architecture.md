# 02 · 项目设计（ARCHITECTURE）

> 版本：v1.1（2026-10-17）
> 技术栈：**Go + SQLite（纯Go驱动） + 本地HTTP UI（net/http + embed，无Node构建链）**，单文件桌面程序

---

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                     darvas.exe（单可执行文件）                     │
│                                                                 │
│  ┌───────────────┐   ┌──────────────────────────────────────┐  │
│  │   scheduler    │   │      GUI（本地HTTP UI :8787）          │  │
│  │  每日时间点调度 │→ │  仪表盘│跟踪池│个股箱体│交易记录 四视图  │  │
│  └───────┬───────┘   └──────────────┬───────────────────────┘  │
│          │                          │ JSON API（只读）          │
│          ▼                          ▼                          │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                  pipeline（盘后流水线编排）                │   │
│  │  ingest → boxscan → screen → poolsync → signal →         │   │
│  │  plan → report        每步幂等、可独立重跑、留痕           │   │
│  └───────┬─────────────────────────────────────────────────┘   │
│          ▼                                                      │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                     领域引擎层（纯Go包）                   │   │
│  │  box（箱体状态机） screener（初选） pool（跟踪池）          │   │
│  │  signal（信号） planner（计划） position（持仓）            │   │
│  │  ledger（账本）                                            │   │
│  └───────┬─────────────────────────────────────────────────┘   │
│          ▼                                                      │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  store（SQLite访问层·唯一DB入口·Repository模式）           │   │
│  └───────┬─────────────────────────────────────────────────┘   │
│          ▼                                                      │
│  ┌──────────────┐        ┌──────────────────────────┐          │
│  │ datasource/tdx │─读取→ │ ingest（快照/增量/校验）  │          │
│  │ .day二进制读取 │        └──────────────────────────┘          │
│  └──────────────┘                                              │
│                     darvas.db（SQLite WAL，同目录）              │
└─────────────────────────────────────────────────────────────────┘
                          ▲
        H:\new_tdx64\vipdoc\{sh,sz}\lday\*.day（外部，通达信维护）
```

**分层铁律**：依赖方向只能自上而下；领域引擎不import GUI/调度；`store`是被依赖的底座；TDX读取器不依赖store（可独立测试）。

---

## 2. 技术选型与理由

| 决策 | 选择 | 理由 | 放弃的备选 |
|------|------|------|-----------|
| 语言 | Go 1.22+ | 单文件编译、跨平台、启动<100ms | Python（部署重） |
| SQLite驱动 | `modernc.org/sqlite` | **纯Go无CGO** → 单exe、构建零环境依赖 | `mattn/go-sqlite3`（要CGO） |
| GUI | **本地HTTP UI（生效）** | `net/http` + embed静态资产，零外部依赖、零构建链，系统默认浏览器打开 | Wails v2（要Node构建链）、Fyne（图表弱）、Lorca（依赖Chrome） |
| 图表 | 前端canvas自绘（无JS库） | K线+箱体矩形+成交量+权益曲线，离线单exe不引CDN/Node包 | lightweight-charts（外部依赖） |
| 报告 | HTML模板 + excelize(Excel) | 每日照着挂单，双格式 | PDF |
| 调度 | 自研轻量调度（goroutine+time） | 只需每日7个时刻+补跑逻辑 | 外部cron（破坏可移植性） |

> GUI决策记录（ADR）：原方案Wails v2，因其构建链需要Node而按§2预案降级为本地HTTP UI，M6已按此交付
> （`darvas ui`起127.0.0.1:8787 + 系统默认浏览器打开，静态页embed进exe，API只读）。
> **领域层设计不受GUI选型影响**；若未来切回Wails，仅替换 `internal/gui` 绑定层。

---

## 3. 目录结构（仓库即工程）

```
Darvas-box-trading/
├── README.md                     # 项目总览+快速开始
├── docs/                         # 00理论 01需求 02设计 03ERD 04API 05任务 06一致性
├── cmd/darvas/main.go            # 入口：CLI解析+ui/serve子命令
├── internal/
│   ├── contract/                 # ★ 模块间接口定义（单一事实源，见04文档）
│   │   └── contract.go
│   ├── datasource/tdx/           # .day读取器（从tradebuddy移植+增强）
│   │   └── reader.go
│   ├── ingest/                   # 全量/增量导入+质量校验
│   ├── store/                    # SQLite唯一入口
│   │   ├── store.go              # Open/Close/migrate/backup
│   │   ├── repo_bar.go           # daily_bar仓储
│   │   ├── repo_box.go           # box仓储
│   │   ├── repo_pool.go          # candidate/watch仓储
│   │   ├── repo_trade.go         # position/plan/fill/ledger仓储
│   │   ├── repo_run.go           # daily_run/param仓储
│   │   └── migrations/0001_init.sql  # ★ DDL单一事实源
│   ├── box/                      # 箱体状态机（纯函数核心+扫描器）
│   ├── screener/                 # 四条件初选
│   ├── pool/                     # 跟踪池状态机
│   ├── signal/                   # 四类信号判定
│   ├── planner/                  # 六问→订单计划
│   ├── position/                 # 腿管理/止损上移/仓位计算
│   ├── ledger/                   # 权益快照/恒等式审计
│   ├── scheduler/                # 时间表调度+补跑+周六weekly触发
│   ├── reporter/                 # HTML/Excel日报周报
│   └── gui/                      # 本地HTTP UI（net/http + embed）
│       ├── gui.go                # Server/路由/JSON API（dashboard/dates/watch/candidates/signals/stock/trades）
│       └── static/index.html     # 单文件前端（四视图+全站日期下拉+池子页，canvas自绘K线/箱体/权益曲线）
├── scripts/                      # build.bat / backup.bat
├── var/                          # 运行时目录（gitignore）
│   ├── darvas.db                 # SQLite（程序同目录或此处，可配置）
│   ├── backup/                   # 每日db备份×30
│   └── reports/                  # daily_YYYYMMDD.html
└── .gitignore
```

---

## 4. 运行形态

### 4.1 部署形态（满足"随便移植到同平台机器"）

```
目标机器（Windows x64）仅需：
  darvas.exe      ← 单文件（~15MB，含前端资产embed）
  darvas.db       ← 数据库（可从旧机拷贝=完整迁移）
可选环境变量/配置文件 darvas.toml：
  tdx_root = "H:\\new_tdx64\\vipdoc"   # 新机器上TDX路径不同时改这里
```

- db查找顺序：`--db`参数 > `DARVAS_DB`环境变量 > exe同目录`darvas.db`
- TDX路径同理可配置 → 移植只改一个toml

### 4.2 进程形态

- **常驻模式**（推荐）：开机自启/手动启动，托盘驻留，调度器按时间表自动触发，GUI随时打开
- **手动模式**：`darvas run --date 2026-10-16` 单次跑全流程（补跑/调试）

---

## 5. 关键设计决策（ADR摘要）

### ADR-1 箱体引擎=确定性纯函数
箱体识别做成「K线序列 → 箱体序列」的**确定性纯函数**（同输入必同输出），DB只是结果的持久化。好处：①单元测试即文档；②重跑幂等天然成立；③未来任何GUI渲染与信号判定都基于同一事实。

### ADR-2 交易日历来自数据本身
用sh000001的日期序列判断交易日与「数据新鲜度」，不内置日历库（节假日规则变化零维护）。

### ADR-3 信号当日生成、订单次日执行
达瓦斯无盘中逻辑 → 系统的所有决策都是**收盘后**基于收盘价产生，产出的是**次日**条件单计划。数据模型上`trade_plan.plan_date`（生成日）与`execute_date`（执行日）分离。

### ADR-4 人工成交录入而非对接券商
自动下单明确不做（需求§9）。成交录入提供「粘贴解析」（券商APP成交明细文本→结构化），把人工成本压到每笔30秒。

### ADR-5 SQLite WAL + 每日备份
单机单用户，WAL模式足够并发（GUI读+调度写）；每日16:45流水线尾部做`VACUUM INTO`备份。

---

## 6. 数据流（一次完整交易日）

```
15:05 通达信客户端 ──下载数据──▶ vipdoc/*.day（外部更新）
15:30 ingest      ──增量读取──▶ daily_bar（今日T行）
16:00 boxscan     ──K线窗口──▶ box（重算跟踪池全量+候选池增量+全市场滚动N日）
16:10 screen      ──四条件──▶ candidate_pool（当日快照）
16:20 poolsync    ──状态机──▶ watch（入池/升阶/出池）
16:30 signal      ──收盘价vs箱体──▶ signal（breakout/pyramid/stop/failed）
16:40 plan        ──信号+持仓+param──▶ trade_plan+plan_order（六问答案）
16:45 report      ──汇总──▶ daily_20261017.html + db备份
     (次日09:25 人工按plan_order挂券商条件单；成交后paste回录 → trade_fill → ledger)
```

### boxscan增量策略（性能关键，NFR-1）

- **全量**：仅首次与参数变更时（约1.2万只×窗口重放，目标<10min）
- **每日增量**：
  - 跟踪池+候选池股票：全K线窗口重放（正确性优先，量小）
  - 其余全市场（docs/00 §7 股票池：沪深主板+创业板+科创板；排除指数/B股/北交所）：只对「今日创60日新高 或 今日量≥均量×2」的股票重放（预过滤）
  - 依据：不满足预过滤的股票不可能新产生符合四条件的箱体事件

---

## 7. 错误处理与可观测性

| 层 | 策略 |
|----|------|
| pipeline | 每任务事务包裹；失败写`daily_run`（status=failed+error）；`plan`失败=红色告警 |
| ingest | 坏记录跳过计数；更新数<阈值→阻断（数据未下载完整） |
| GUI | 任务心跳灯（7绿=健康）；失败任务点击看日志 |
| 日志 | 结构化写入`var/log/darvas.log`，按日轮转，保留90天 |

---

## 8. 安全与备份

- db每日备份30份滚动（ADR-5）
- 参数修改写入`param`并留`changed_at`（审计）
- 所有时间使用本地时区（Asia/Shanghai，单机单用户无歧义）
