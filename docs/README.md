# 文档索引

> 文档治理原则：一份问题只有一处权威；文档解释事实，代码、迁移和校验脚本决定可执行事实。历史稿、临时稿和实验原始产物不参与现行口径。

## 必读文档

| 文档 | 唯一职责 |
|---|---|
| [00-darvas-theory.md](00-darvas-theory.md) | 达瓦斯理论、箱体规则和理论边界 |
| [01-requirements.md](01-requirements.md) | 用户需求、每日时刻表、价格口径、功能验收 |
| [02-architecture.md](02-architecture.md) | 分层架构、生产数据流、回放和实验产物结构 |
| [03-erd.md](03-erd.md) | 数据模型、状态机、数据库字段和研究字段 |
| [04-api-contract.md](04-api-contract.md) | 模块接口、CLI、撮合及研究字段契约 |
| [06-consistency.md](06-consistency.md) | SSoT、C1–C7 一致性机制和变更协议 |
| [29-box-gradient-experiment-plan.md](29-box-gradient-experiment-plan.md) | **已裁定结论（append-only 16 条）+ 判定口径**；「执行纲领」定位已移交 `55`/`56`/`60`（头部横幅已标注），历史执行顺序仅供审计 |
| [44-new-baseline-config.md](44-new-baseline-config.md) | **唯一参数事实源**；49 键生成台账，C6 校验 |
| [48-regime-attack-defense-design.md](48-regime-attack-defense-design.md) | 攻防转换设计；当前为待实现/待验证，不是生产能力 |
| [49-baseline-rebuild-charter.md](49-baseline-rebuild-charter.md) | 连续基线重建、身份链、永续化和验收标准；**A–F 已收官**（adj 基线 100%），唯一未完成项 = G 攻防转换（接口桩） |
| [53-data-spec-new-baseline.md](53-data-spec-new-baseline.md) | 后复权数据规格；数据侧已实施验收（`adj_ready=1`），生产 `serve` 未切换 |
| [55-adj-experiment-replay-matrix.md](55-adj-experiment-replay-matrix.md) | **adj 口径全量实验重做矩阵（22+3 项）+ Q1–Q5 裁定 + 执行契约 + §9 新基线参数清单/拦截榜/命名出处核对 + §10 三项新实验预注册设计 + §11 中段实测修订**；决定旧结论如何处置、B 类对照基准、样本外纪律，以及 49 键参数哪些真正影响成交 |
| [56-live-plan.md](56-live-plan.md) | **活计划看板（单页）**：当前任务与状态、判据锚点、参数冻结、已关闭清单、铁律、每日检查；**只做导航与勾选，不引入新结论**，冲突以 `55` 为准 |
| [57-books-coverage-industry.md](57-books-coverage-industry.md) | **三书对照 × 方法覆盖审计 × 口径分层 × 行业层可行性**：每项验证显式标 raw/adj；行业层缺失确认（三书共同组件）、CAN SLIM 逐字母审计、静态行业分组证伪、本地免费行业数据盘点（`tdxhy.cfg` / 880 行业指数）、Step 0 零回测预注册 |
| [58-book-crosslearn-absorption.md](58-book-crosslearn-absorption.md) | **菲阿里/炭谷 + Sperandeo + Chan 三书对现有基线的可吸收清单**：只取检验方法/风控指标/仓位计量口径，不取信号；已算出显著性（t=2.228、bootstrap CI [+214,+2030]）、51 连亏 −156,982、成本占利润 12%；参数预算 49 键 vs 上限 13.5 → T4 改为删键；明确不吸收清单 |
| [59-param-taxonomy.md](59-param-taxonomy.md) | **参数自由度重分类**：49 键 → 常量 10 / 结构开关 2 / 死键 7 / 仅日志 9 / 已锁 5 / **活参 16**；真实自由度不是 49 而是 16（+5 冻结=21），对 Chan 上限 13.5 只超 1.19 倍；给出删死键→降日志键→7 次消融的三步路线与三判据验收 |
| [60-experiment-plan.md](60-experiment-plan.md) | **实验预注册队列（唯一排队表）**：21 条实验 / 25 臂 / 130 机时；A·B（既有）· P（消参工程）· K（三书吸收）四系编号互不撞号；6 道批次闸门，批 1 首位是 **K1 前视截尾检验**（不通过则 14 年结论全部作废）；反过拟合五条硬约束 + 统一判据总则 |
| [61-market-breadth-sensing.md](61-market-breadth-sensing.md) | **大盘感知方案（A5）**：涨跌家数比（A/D 比）替代 ma60 做主开关，涨停家数当温度计、跌停家数当反向阀、百日新高家数当赚钱效应面——四样全部从复权库免费回算；牛/熊/猴映射 + 行业只做过滤不做轮动；预注册进 docs/60 |

## 目录

- `experiments/`：实验框架、报告和数据的唯一产物目录；报告必须携带数据口径、库身份和限制。
- `books/`：理论原著资料。
- `archive/`：历史、临时、被替代和已归档文档；**不作为事实源**。
- `archive/code-wiki/`：旧代码导读快照，基于历史提交生成，不能替代源码或现行契约。

## 当前事实源关系

- 理论规则：`00`。
- 每日时间表与需求：`01`。
- Schema：`internal/store/migrations/`；释义：`03`。
- 接口与类型：`internal/contract/contract.go`；释义：`04`。
- 参数：迁移生效值与代码实读键；生成台账：`44`。
- 一致性检查：`scripts/check_consistency.py`、`scripts/param_ledger.py`、`scripts/apply_caliber_banner.py`；规则：`06`（C1–C7）。
- 新基线流程与验收：`49`（A–F 已收官）；已裁定结论与判定口径：`29`；实验预注册队列：`60`；活任务看板：`56`。
- 后复权设计：`53`。`daily_bar_adj` 已落地（`full_hist_adj.db`，`adj_ready=1`），生产 `serve` 仍走 raw，不能据 `53` 宣称生产已切后复权。
- 实验口径与重做裁定：`55`（旧结论一律加横幅保留，新结果 `_adj` 并存；B 类只与新基线比；2019+ 不解冻）。
- 当前该做什么（活任务看板）：`56`（单页；只导航不结论，冲突以 `55` 为准）。

## 引用规则

1. 新规则先更新对应事实源，再更新释义文档和代码引用；不要新建平行规则。
2. 归档内容只用 `archive/...` 明确引用，且必须说明它是历史材料，不是现状。
3. 参数表禁止手改生成区；改代码默认值或 migration 后运行 `python scripts/param_ledger.py --write`。
4. Schema、接口、时间表或参数发生变化，同一提交同步更新对应文档并运行一致性检查。
5. 实验报告不回写生产规则；只有明确裁定后，才把结论沉淀到 `29`、`44` 或对应 SSoT。

## 归档说明

`archive/` 保留原稿和审计留痕，不再维护其过时数字、旧路径和旧命令。重要内容已折叠到现行文档；若历史原文与现行事实冲突，以现行文档、代码和迁移为准。
