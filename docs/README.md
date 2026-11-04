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
| [29-box-gradient-experiment-plan.md](29-box-gradient-experiment-plan.md) | **当前实验与新基线执行纲领**；下一步、依赖顺序、验收、已知未修缺陷挂账、**已裁定结论（只追加）与判定口径** |
| [44-new-baseline-config.md](44-new-baseline-config.md) | **唯一参数事实源**；49 键生成台账，C6 校验 |
| [48-regime-attack-defense-design.md](48-regime-attack-defense-design.md) | 攻防转换设计；当前为待实现/待验证，不是生产能力 |
| [49-baseline-rebuild-charter.md](49-baseline-rebuild-charter.md) | 连续基线重建、身份链、永续化和验收标准 |
| [53-data-spec-new-baseline.md](53-data-spec-new-baseline.md) | 后复权数据规格；数据侧已实施验收（`adj_ready=1`），生产 `serve` 未切换 |
| [55-adj-experiment-replay-matrix.md](55-adj-experiment-replay-matrix.md) | **adj 口径全量实验重做矩阵（22 项）+ Q1–Q5 裁定 + 执行契约 + §9 新基线参数清单/关键参数判定**；决定旧结论如何处置、B 类对照基准、样本外纪律，以及 49 键参数哪些真正影响成交 |

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
- 新基线流程与验收：`49`；当前执行队列：`29`。
- 后复权设计：`53`。`daily_bar_adj` 已落地（`full_hist_adj.db`，`adj_ready=1`），生产 `serve` 仍走 raw，不能据 `53` 宣称生产已切后复权。
- 实验口径与重做裁定：`55`（旧结论一律加横幅保留，新结果 `_adj` 并存；B 类只与新基线比；2019+ 不解冻）。

## 引用规则

1. 新规则先更新对应事实源，再更新释义文档和代码引用；不要新建平行规则。
2. 归档内容只用 `archive/...` 明确引用，且必须说明它是历史材料，不是现状。
3. 参数表禁止手改生成区；改代码默认值或 migration 后运行 `python scripts/param_ledger.py --write`。
4. Schema、接口、时间表或参数发生变化，同一提交同步更新对应文档并运行一致性检查。
5. 实验报告不回写生产规则；只有明确裁定后，才把结论沉淀到 `29`、`44` 或对应 SSoT。

## 归档说明

`archive/` 保留原稿和审计留痕，不再维护其过时数字、旧路径和旧命令。重要内容已折叠到现行文档；若历史原文与现行事实冲突，以现行文档、代码和迁移为准。
