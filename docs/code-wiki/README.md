# Code Wiki · Darvas-box-trading

> 面向开发者的代码导读。基于 `bd3028d` 工作树生成，覆盖架构、模块、数据模型、运行方式、依赖与测试。
> 与 `docs/00`–`docs/06`（项目宪法/SSoT）不同：本 Wiki 解释 **代码怎么实现**，SSoT 解释 **需求与理论是什么**。两者冲突时以 SSoT 为准，并回改代码或文档。

---

## 1. 项目一句话

达瓦斯箱体理论的单机自动化实现：每天收盘后从通达信 `vipdoc` 导入日线，重算全市场箱体，按四条件初选、跟踪池状态机、首次事件信号、六问交易计划依次推进，产出次日停损单计划与 HTML 日报；提供只读本地 GUI、常驻调度、模拟撮合与历史回放实验能力。

- 语言/存储：Go（纯 Go SQLite 驱动 `modernc.org/sqlite`，无 CGO）+ SQLite（WAL）
- 形态：单文件 `darvas.exe` + 单个 `var/darvas.db`
- 理论边界：**只实现达瓦斯箱体理论**，禁止混入均线/MACD/立花等其他体系
- 当前版本：`0.5.0-m8`（[main.go](../../cmd/darvas/main.go)）

---

## 2. 阅读路径

| 读者 | 建议顺序 |
| --- | --- |
| 第一次接触项目 | 本页 → [01 项目总览](01-overview.md) → [02 架构](02-architecture.md) → [06 运行方式](06-running.md) |
| 要改业务规则 | [03 数据模型](03-data-model.md) → [04 核心模块](04-core-modules.md) → [07 依赖与测试](07-dependencies-and-testing.md) |
| 要改调度/GUI/回测 | [05 编排层](05-orchestration.md) → [06 运行方式](06-running.md) |
| 要查某个函数在哪 | 各模块文档的「关键函数」表，或直接检索 `internal/` |

---

## 3. 文档目录

| 文件 | 内容 |
| --- | --- |
| [01-overview.md](01-overview.md) | 项目定位、设计原则、仓库地图、技术栈、术语表、规模指标 |
| [02-architecture.md](02-architecture.md) | 分层架构、依赖方向、盘后数据流、调度补跑、GUI/回测通路、横切机制 |
| [03-data-model.md](03-data-model.md) | 16 张表、ER 关系、状态机枚举、17 个迁移、参数体系、运行时不变量 |
| [04-core-modules.md](04-core-modules.md) | contract、tdx、ingest、box、screener、pool、signal、planner、ledger、match |
| [05-orchestration.md](05-orchestration.md) | pipeline、scheduler、reporter（日报/周报）、gui、backtest、regime、regimesweep、CLI 入口 |
| [06-running.md](06-running.md) | 构建、配置、首次运行、日常运维、CLI 全表、成交录入、备份恢复、排障 |
| [07-dependencies-and-testing.md](07-dependencies-and-testing.md) | 包依赖矩阵、178 个测试分布、一致性检查 C1–C4、Python 研究脚本、扩展指南 |

---

## 4. 仓库速览

```
cmd/darvas/              CLI 入口（子命令分发）
internal/
  contract/              跨模块类型与接口（单一事实源）
  datasource/tdx/        通达信 .day 二进制读取器
  ingest/                全量/增量导入 + 质量校验
  store/                 SQLite 唯一入口 + 8 个 Repository + 17 个迁移
  box/                   三日法则状态机（纯函数）+ 每日批处理
  screener/              初选四条件
  pool/                  跟踪池状态机（六阶段）
  signal/                首次事件信号判定
  planner/               六问 → 交易计划（仓位/止损/卖单）
  ledger/                成交解析、持仓状态机、权益快照、恒等式审计
  match/                 模拟撮合（停损单 + 复权清仓 + 滑点）
  pipeline/              盘后七任务编排 + 备份轮转
  scheduler/             时刻表调度 + CatchUp 补跑
  reporter/              HTML 日报/周报
  gui/                   本地 HTTP UI（embed 单页 + JSON API）
  regime/                市场体制标注（宽度法 + A 判据 + MA60）
  regimesweep/           箱体参数梯度扫描实验器
  backtest/              历史回放器（prefill/progressive/verify/regime）
scripts/                 build.bat、一致性检查、Python 研究/实验脚本
docs/                    00–06 SSoT + 实验纲领 + 本 Code Wiki
var/                     运行时产物（db/backup/reports，gitignored）
```

**规模**（`bd3028d`）：

- `internal/` 约 15,500 行（非测试代码约 9,300 行，测试约 6,100 行）
- 18 个可编译业务包（`internal/position` 为空占位包）
- 178 个 Go 测试函数
- 17 个 append-only 迁移、16 张表（15 业务表 + `schema_version`）
- 8 个 GUI JSON API + 1 个内嵌单页前端

---

## 5. 快速开始（最短路径）

```powershell
# 构建（Go 1.27，go.mod 要求；无需 CGO）
go build -o darvas.exe .\cmd\darvas

# 首次全量导入（幂等）
.\darvas.exe ingest --since full

# 跑完整盘后流水线
.\darvas.exe run --date 2026-10-27

# 常驻模式：调度 + GUI + 自动备份
.\darvas.exe serve
```

细节、参数与排障见 [06-running.md](06-running.md)。

---

## 6. 不可违反的工程约定

来自 SSoT 与项目历史教训，改代码前必读：

1. **单一理论**：只实现达瓦斯箱体规则，不引入其他交易理论的规则。
2. **迁移 append-only**：已发布的 `internal/store/migrations/*.sql` 禁止修改，只能新增 `00NN_*.sql`。
3. **契约同步**：改 `internal/contract/contract.go` 必须在同一 commit 内同步 `docs/04-api-contract.md`。
4. **提交门禁**：提交前运行 `python scripts/check_consistency.py` 与 `go test ./...`，全部通过。
5. **仓储事实源**：`docs/04 §3.3` 是完整仓储接口事实源；`contract.go` 中的 Repo 接口只是构造期最小示意，不要为避免 import 循环而强行补齐。
6. **日期列扫描**：`modernc.org/sqlite` 会把 `DATE` 列扫描为 RFC3339，所有 SELECT 日期列必须 `CAST(xxx AS TEXT)`。
7. **箱体幂等**：`UpsertBoxes` 按 `(top,bottom)` 复用既有 DB ID，禁止「删旧重插」，以免破坏 `watch.current_box_id` / `position.entry_box_id` 外键。
8. **GUI 只读**：日期切换只是查询参数，不写后端；状态修改（暂停/移除/参数）走 CLI。
9. **股票池边界**：箱体扫描与信号生成只针对沪深主板、科创板、创业板 A 股；排除 B 股、指数、其他板块与 ST。

---

## 7. 文档事实源对照

| 主题 | 代码事实源 | 文档事实源 |
| --- | --- | --- |
| 理论规则 | `internal/box/engine.go` 注释 | `docs/00-darvas-theory.md` |
| 时刻表/参数默认值 | `internal/scheduler/scheduler.go`、迁移种子 | `docs/01-requirements.md` §4/§6 |
| 架构分层 | `internal/*` import 图 | `docs/02-architecture.md` |
| 数据库 Schema | `internal/store/migrations/*.sql` | `docs/03-erd.md` |
| 模块契约 | `internal/contract/contract.go` | `docs/04-api-contract.md` |
| 一致性机制 | `scripts/check_consistency.py` | `docs/06-consistency.md` |
