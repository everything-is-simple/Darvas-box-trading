# Darvas-box-trading

> **达瓦斯箱体交易系统** —— 把尼古拉斯·达瓦斯的自律变成软件
> Go + SQLite + 本地HTTP GUI · 单文件 · 可移植 · 每天收盘后30分钟

---

## 这是什么

复刻《我如何在股市赚了200万》的完整交易体系，只用这一套理论：

```
每个交易日在明确划分的时间点做固定的事情：

15:30 数据同步 → 16:00 全市场箱体重算 → 16:10 初选四条件筛选
→ 16:20 跟踪池状态迁移 → 16:30 信号判定 → 16:40 交易计划生成
→ 16:45 决策日报 + 备份 → 次日09:25 人工挂条件单 → 收盘后录成交 → 周而复始
```

系统每天用书面计划回答**六问**：买什么？何时买？买多少？卖什么？何时卖？卖多少？

- 买什么：跟踪池中放量突破箱体上轨的股票
- 何时买：次日停损买单挂在箱体上轨×1.03
- 买多少：风险仓位公式（1%/2%/2%三腿金字塔）
- 卖什么：收盘跌破最新箱体下轨的持仓
- 何时卖：次日止损卖单挂在下轨×0.97
- 卖多少：全部，一次清仓（达瓦斯纪律）

**单一理论原则**：本系统只实现达瓦斯箱体理论（见`docs/00-darvas-theory.md`），不引入MALF/立花/均线/MACD等任何其他理论的任何规则。

---

## 安装

```bash
# 要求：Go 1.22+（纯Go SQLite驱动，无CGO）；Windows x64
go build -o darvas.exe ./cmd/darvas
```

运行时只需要两个文件（同平台拷走即用，NFR-2）：

- `darvas.exe`
- `var/darvas.db`（首次运行自动创建）

数据源：通达信 vipdoc 日线（默认 `H:\new_tdx64\vipdoc`，可用 `darvas param set tdx.root <路径>` 或环境变量 `DARVAS_TDX` 覆盖）。**每个交易日 15:05 左右在通达信客户端点一次「下载数据」**，这是系统唯一的数据新鲜度前提。

---

## 快速开始

```bash
darvas.exe ingest --since full      # ① 首次全量导入（~10分钟，幂等可重跑）
darvas.exe run --date 2026-10-16    # ② 完整盘后流水线（七任务+日报+备份）
darvas.exe ui                       # ③ 浏览器打开GUI四视图（自动弹窗）
darvas.exe serve                    # ④ 或者：常驻模式（调度+GUI+自动备份）
```

产出：

- `var/reports/daily_YYYYMMDD.html` —— 决策日报（含明日交易计划）
- `var/backup/darvas_YYYYMMDD.db` —— 每日VACUUM备份（保留30份滚动）
- `var/darvas.db` —— SQLite数据库

---

## 每日流程（无人值守推荐 serve）

| 时刻 | 谁 | 做什么 |
|------|----|--------|
| 15:05 | 人 | 通达信客户端「下载数据」 |
| 15:30-16:45 | darvas serve | 按时刻表自动跑七任务链（错过/失败自动补跑重试） |
| 17:00 | 人 | `darvas ui` 看仪表盘/《明日交易计划》，或直接开日报HTML |
| 次日 09:25 | 人 | 按计划在券商APP挂条件单（触发价/委托价/数量系统已算好） |
| 成交后 | 人 | `darvas fills` 粘贴券商成交文本（半自动回录，幂等去重） |

serve 调度语义（M7.1）：

- 每个触发点到达时，把「时刻已过且尚未ok」的任务按依赖链**一次性补齐**（中途开机/电脑关过机都不怕）
- ingest 失败 → 中止本链，下个触发点自动重试；plan 失败 → 标红继续日报
- 交易日历以 sh000001 日线日期为权威（ADR-2）：周末/节假日/TDX未下载自动跳过
- 启动时 CatchUp：前一交易日完全没跑 → 整日补跑
- 每日 report 成功后自动 VACUUM 备份 + 30份滚动轮转（NFR-3）

---

## CLI 一览

```
darvas serve  [--addr] [--db]        常驻模式：调度+GUI+备份（M7，日常用它）
darvas ui     [--addr] [--db]        本地GUI四视图（浏览器，只读）
darvas run    [--date]               完整盘后流水线（一次性全链路）
darvas ingest [--date] [--since full]  数据同步（增量/全量）
darvas boxscan|screen|poolsync|signal|plan|report [--date]   单任务补跑
darvas weekly [--date]               周报（M7.2）
darvas fills  [file] [--yes]         成交粘贴录入（同花顺/通达信/CSV三格式）
darvas ladder <code>                 个股箱体阶梯（终端打印）
darvas param  get|set                参数读写（风险系数/箱体宽度/tdx.root等，热调）
darvas doctor [--db]                 体检：schema/数据新鲜度/账本恒等式审计
darvas version
```

通用约定：`--db` 缺省 exe同目录/var/darvas.db（或环境变量 DARVAS_DB）；所有任务幂等可重跑，留痕 daily_run（GUI仪表盘心跳灯）。

---

## 文档体系（按此顺序读）

| 文档 | 内容 |
|------|------|
| [docs/00-darvas-theory.md](docs/00-darvas-theory.md) | **理论宪法**：箱体理论全部规则+A股适配映射表 |
| [docs/01-requirements.md](docs/01-requirements.md) | 需求分析：每日时间表、功能清单、验收标准 |
| [docs/02-architecture.md](docs/02-architecture.md) | 项目设计：分层架构、技术选型、数据流 |
| [docs/03-erd.md](docs/03-erd.md) | 数据库ERD（事实源：`internal/store/migrations/`） |
| [docs/04-api-contract.md](docs/04-api-contract.md) | 模块API契约（事实源：`internal/contract/contract.go`） |
| [docs/05-task-breakdown.md](docs/05-task-breakdown.md) | 任务划分：M0-M7里程碑 |
| [docs/06-consistency.md](docs/06-consistency.md) | ERD/API一致性维护机制（三道防线） |

---

## 技术栈

| 项 | 选择 |
|----|------|
| 语言 | Go（纯Go SQLite驱动`modernc.org/sqlite`，无CGO，单exe） |
| 数据库 | SQLite WAL（调度写+GUI读双连接）+ 每日VACUUM备份30份滚动 |
| GUI | 本地HTTP UI（`net/http`+embed静态资产，浏览器访问，零外部依赖；docs/02 §2 ADR降级预案生效版） |
| 数据源 | 通达信`vipdoc/{sh,sz}/lday/*.day`（1.2万文件）+ `T0002/hq_cache/*.tnf`（代码→名称） |
| 部署 | `darvas.exe` + `darvas.db` 拷走即用（同平台零安装） |

---

## 项目结构

```
cmd/darvas/            # CLI入口（run/ui/serve/fills/...）
internal/
  contract/            # ★ 模块间接口（单一事实源）
  datasource/tdx/      # .day二进制读取器 + tnf名称表
  ingest/  store/      # 数据导入与SQLite仓储
  box/                 # 箱体引擎（三日法则状态机）
  screener/  pool/     # 初选四条件 + 跟踪池状态机
  signal/  planner/    # 四类信号 + 交易计划（六问出口）
  position/  ledger/   # 金字塔腿 + 账本（权益快照/恒等式审计）
  scheduler/           # 常驻调度：时刻表触发+CatchUp补跑+交易日历
  pipeline/            # 盘后流水线编排 + 备份轮转
  reporter/            # HTML日报
  gui/                 # 本地HTTP UI（embed静态+JSON API）
docs/                  # 00-06七份文档
scripts/               # 一致性检查脚本
var/                   # 运行时（db/备份/报告）
```

---

## 当前状态

- ✅ M0 规划与骨架 · M1 数据层（全量<10min）· M2 箱体引擎（35用例）· M3 池 · M4 信号与计划（含账本）· M5 CLI闭环 · M6 GUI四视图
- 🚧 M7 周而复始：✅ 7.1 常驻调度（serve）· ✅ 7.2 周报 · ✅ 7.3 备份轮转+恢复演练 · ⏳ 7.4 移植演练 · 观察期：连续20交易日无人值守

数据源已验证：TDX vipdoc 更新至 2026-10-16 收盘；真实库全链路跑通（142候选入池、茅台29箱体）。

---

## 数据与仓库

- **唯一本地库**：`I:\Darvas-box-trading`（代码、文档、运行时数据全在此目录）
- 远端：https://github.com/everything-is-simple/Darvas-box-trading
- `var/`、`*.db`、`.workbuddy/` 不入库（.gitignore）
- 理论原著电子版在仓库外：`H:\《交易之no.14.year》\2011.usa.达瓦斯.我如何在股市赚了200万\`；`docs/books/` 仅存扫描件页面标记（OCR待做，M0.8）
