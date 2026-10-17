# Darvas-box-trading

> **达瓦斯箱体交易系统** —— 把尼古拉斯·达瓦斯的自律变成软件
> Go + SQLite + 桌面GUI · 单文件 · 可移植 · 每天收盘后30分钟

---

## 这是什么

复刻《我如何在股市赚了200万》的完整交易体系，只用这一套理论：

```
每个交易日在明确划分的时间点做固定的事情：

15:30 数据同步 → 16:00 全市场箱体重算 → 16:10 初选四条件筛选
→ 16:20 跟踪池状态迁移 → 16:30 信号判定 → 16:40 交易计划生成
→ 次日09:25 人工挂条件单 → 收盘后录成交 → 周而复始
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

## 快速开始（开发期）

```bash
# 要求：Go 1.22+；数据源：通达信 vipdoc（H:\new_tdx64\vipdoc，每日盘后在通达信点"下载数据"）

go build -o darvas.exe ./cmd/darvas

darvas.exe ingest                      # 首次全量导入（~10分钟）
darvas.exe run --date 2026-10-16       # 完整盘后流水线
darvas.exe ladder sh600519             # 查看个股箱体阶梯
darvas.exe gui                         # 桌面界面（M6里程碑交付）
```

产出：
- `var/reports/daily_YYYYMMDD.html` —— 决策日报（含明日交易计划）
- `var/darvas.db` —— SQLite数据库（拷走这两个文件=完整迁移到新机器）

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
| 数据库 | SQLite WAL + 每日30份滚动备份 |
| GUI | Wails v2（WebView2，Win10/11自带；降级预案见02文档§2） |
| 数据源 | 通达信`vipdoc/{sh,sz}/lday/*.day`（已验证1.2万文件、每日更新） |
| 部署 | `darvas.exe` + `darvas.db` 拷走即用（同平台零安装） |

---

## 项目结构

```
cmd/darvas/            # CLI+GUI入口
internal/
  contract/            # ★ 模块间接口（单一事实源）
  datasource/tdx/      # .day二进制读取器
  ingest/  store/      # 数据导入与SQLite仓储
  box/                 # 箱体引擎（三日法则状态机）
  screener/  pool/     # 初选四条件 + 跟踪池状态机
  signal/  planner/    # 四类信号 + 交易计划（六问出口）
  position/  ledger/   # 金字塔腿 + 账本
  scheduler/ reporter/ # 每日时间表调度 + 日报
  gui/                 # Wails桌面界面
docs/                  # 00-06七份文档
scripts/               # 一致性检查脚本
var/                   # 运行时（db/备份/报告/日志）
```

---

## 当前状态

- ✅ M0 规划与骨架（2026-10-17）
- ⏳ M1 数据层 → M2 箱体引擎 → M3-M7（详见任务划分）

数据源已验证：TDX vipdoc 更新至 2026-10-16 收盘（茅台 1329.00 / 上证 3716.28）。

---

## 数据与仓库

- 本地工作目录：`I:\Darvas-box-trading`
- 远端：https://github.com/everything-is-simple/Darvas-box-trading
- `var/` 与 `*.db` 不入库（.gitignore）
