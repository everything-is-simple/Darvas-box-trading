# Darvas-box-trading 文档索引

> 本索引是文档治理的导航枢纽。阅读任何文档前先看这里，知道「现在该读什么、谁是事实源」。
> 治理原则：**最少文件数 × 最大必要内容**——主目录只保留 SSoT + 现役纲领 + 中间文档，使命结束即归档。
> 本索引更新于 2026-10-26（P0-3 完成后）。

---

## 主目录结构（2026-10-26 整理后）

### 第一层：7 个 SSoT（单一事实源，必须一致）

这 7 份是系统的**宪法与工程基准**，由 `scripts/check_consistency.py` 强制校验（C1 枚举 / C2 表覆盖 / C3 参数键 / C4 时刻表）。

| #  | 文档                     | 职责                            | 事实源配对                                     |
| -- | ---------------------- | ----------------------------- | ------------------------------------------ |
| 00 | `00-darvas-theory.md`  | 理论宪法（唯一理论来源；第 0 条不择时；§3.6 十维研究框架） | 代码必须能指回条目                              |
| 01 | `01-requirements.md`   | 需求/每日时间表/参数默认值                | `migrations/*.sql` param 种子 + `scheduler/schedule.go` |
| 02 | `02-architecture.md`   | 架构总览 + 数据流（含专项实验报告链路 §9）      | —                                          |
| 03 | `03-erd.md`            | 数据库 ERD + §6 reason_json 研究字段契约 | `internal/store/migrations/*.sql`       |
| 04 | `04-api-contract.md`   | 模块契约 + 撮合语义 + §9 研究字段产出契约     | `internal/contract/contract.go`            |
| 05 | `05-task-breakdown.md` | 里程碑划分与状态（M1–M10）              | —                                          |
| 06 | `06-consistency.md`    | 一致性维护机制 + 运行时不变量              | `check_consistency.py`                     |

**入口先读 00 → 01 §4（每日时间表）→ 05（里程碑）→ 按任务需要跳读 02-04/06。**

### 第二层：现行纲领与定稿（3 文件 + 1 目录）

| 文档 | 定位 | 状态 |
| -- | -- | -- |
| **29** `29-box-gradient-experiment-plan.md` | **box 十维梯度实验总计划（现行纲领）**：P0-1 box 高度 ✅、P0-3 regime 归因 ✅、B-1/B-2 MA60 闸门（进行中）、P1 起需改回测器 | 现役 |
| **23** `23-position-ladder-math.md` | 头寸梯子演算 + **§9 仓位定稿（D0 A M1 / P5 不择时六轮证伪）**——B 类实验反复引用其硬结论 | 现役（§9 最终应并入 00，单独立项） |
| **27** `27-iterative-experiment-prompt.md` | 迭代实验方法论模板（2014–2018 逐年回测纲领，已跑完）；docs/06 §6.5 引用 | 方法论保留 |
| **experiments/** | **试验报告陈列馆（系统最宝贵资产）**：P0-1 双口径三级加密、P0-3 三口径×两时点、M9.0 距新高细分等，规则与结果两层分置 | 持续生长，只进不出 |

### 第三层：中间文档（使命将尽，逐个归档；2026-10-26 定性）

| 文档 | 定性 | 剩余价值 |
| -- | -- | -- |
| 24 | `24-regime-filter-and-position-redesign.md` | 中间 | 仓位管理演进链的一环（被 A M1 取代） |
| 26 | `26-darvas-box-regime.md` | 中间 | A/B/C 三判据的谱系出处（P0-3 三口径溯源用） |
| 28 | `28-e17-backtest-diagnosis.md` | 中间 | 回测时长教训（B 类排期引用） |

### 参考资料目录

| 目录 | 内容 |
| -- | -- |
| `books/` | 原著依据（How I Made $2M / Wall Street: The Other Las Vegas 摘录），00 的理论出处 |

---

## archive/（历史档案，不作为事实源）

`docs/archive/`：退役、过期、被证伪、被替代的文档。每份头部有归档声明，注明核心内容沉淀去向。
**07–22 编号连续；retrospective-2026/ 为一次性 HTML 复盘（2026-10-19）。**

| 归档文件 | 原状态 | 核心内容沉淀去向 |
| --- | --- | --- |
| 07-m8-issue-list.md | 9 个 issue 已修复 6 个 | 06 §3.1 执行层不变量 |
| 08-m8.2-matcher.md | 设计稿 | 04 §7.5 撮合器成交语义 |
| 09-m8.3-regime.md | 已退役 | 方法论教训（动量法失败）→ 00 §10 |
| 10-m8.6-pyramid-addon-proposal.md | 提案待拍板 | 00 §4.1 五条件；拍板时从本档复活 |
| 11-m8.7-regime-position-sizing.md | 已证伪 | 教训 → 00 §10 |
| 12-m8.7-regime-root-cause.md | 方法论保留 | 教训一句 → 00 §10 |
| 13-m8.8-breadth-unit-sizing.md | 设计稿 | 01 §6 宽度法+单位制；宪法依据 00 第 0 条/§10 |
| 14-bt-archive.md | 运维说明 | 回测库归档清单，并入常规运维 |
| 15-next-session-prompt.md | 已过期 | 交接完成 |
| 16-provenance-readiness.md | 被证伪 | 上线判定被 18 推翻 |
| 17-m88-preliminary-readiness-update.md | 双重证伪 | 自身再审 + 18 双重推翻 |
| 18-quant-review-m88-deepseek4.1flash-*.md | 审计底稿（2026-10-26 归档） | 「旧回测盈利不可信、不建议转正」→ 00 第 0 条；R1/A1/A2/C4 已落地（9b1e577） |
| 19-backtest-validation-loop-glm5.3-*.md | 验证纲领（2026-10-26 归档） | E1-E6/S1-S4 验收口径被迁移 0012/0013 引用；修复已完成 |
| 20-full-picture-three-way-audit-kimik3-*.md | 三向对照（2026-10-26 归档） | 增补拼图已固化 → 00 §11.4 |
| 21-three-ai-synthesis-unified-plan.md | 三家统一纲领（2026-10-26 归档） | A1(RS超额)+A2(ST排除)+C4(滑点) 已落地 |
| 22-p05-five-year-backtest-prompt.md | p05 接力 prompt（2026-10-26 归档） | 五年回测已完成，结论 → 23 §9 |
| retrospective-2026/ | 一次性 HTML 复盘（2026-10-26 归档） | 历史快照，无活跃引用 |

---

## 引用规则（新增文档必读）

1. **SSoT 互引**：00-06 之间用 `docs/0X-*.md` 短路径，由 `check_consistency.py` 校验存在性。
2. **引用归档稿**：用 `archive/NN` 路径（如 `archive/13`），核心内容应已沉淀至 SSoT（2026-10-26 起 18–22 亦属此类）。
3. **引用现役纲领**：`docs/29`（十维实验）、`docs/23 §9`（仓位定稿）、`docs/experiments/`（实验报告）。
4. **新工作不新建文档**：先回 SSoT 沉淀；确需新文档时顺号（30、31…）并在文件名标注作者模型与时间，归档时 git mv 至 archive/ 保历史。
5. **实验报告**：一律沉淀 `docs/experiments/`（`_framework.md` 零结果 + `_report.{html,md}` + `_data.json`），不落 `var/bt/`。

## 一句话总结

**00-06 是宪法与工程基准（check_consistency 强制）；29 是现行实验纲领、23 §9 是仓位定稿、experiments/ 是实验报告事实源；24/26/28 为中间文档待归档；archive/（07–22 + retrospective）是历史。当前研究主线 = docs/29 的十维梯度实验，B 类 MA60 闸门验证进行中。**
