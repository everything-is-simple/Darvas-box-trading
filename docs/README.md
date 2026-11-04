# Darvas-box-trading 文档索引

> 本索引是文档治理的导航枢纽。阅读任何文档前先看这里，知道「现在该读什么、谁是事实源」。
> 治理原则：**一份文档只回答一个问题；一份问题只有一处权威**。判断留存的标准是「新会话/改代码时会不会用到」，用不到就归档。
> 本索引更新于 **2026-11-04**（治理后 15 份：7 SSoT + 29/44/48/49/50/51 + 36 + 46；35/42/43/45/47 归档，结论已折叠）。

---

## 主目录结构（15 份 + 4 个目录）

### 第一层：7 个 SSoT（宪法与工程基准，`scripts/check_consistency.py` 强制校验）

| #  | 文档 | 职责 | 机器校验 |
| -- | -- | -- | -- |
| 00 | `00-darvas-theory.md` | **理论宪法**：唯一理论来源；第 0 条（四次修正）；§3.6 十一维框架；§3.7 生命体五属性；§5.6.7 仓位定稿与已否决清单；§5.6.9 仓位目标（牛 70/熊 10） | 代码须能指回条目 |
| 01 | `01-requirements.md` | **需求**：数据源与价格口径 §3 / 每日时间表 §4（唯一时刻源）/ 参数依据 §6 | `scheduler.go`（C4）、migrations（C3） |
| 02 | `02-architecture.md` | **架构**：数据流、回放链路、专项实验报告链路 | — |
| 03 | `03-erd.md` | **ERD**：表结构、`reason_json` 契约、`equity_snapshot` 复利前提 | `migrations/*.sql`（C2） |
| 04 | `04-api-contract.md` | **契约**：模块接口、撮合语义、研究字段产出 | `contract.go`（C1） |
| 05 | `05-task-breakdown.md` | **里程碑**：M1–M10 状态与排期 | — |
| 06 | `06-consistency.md` | **一致性机制**：三道防线、C1–C6 校验口径、统计口径 C5 | `check_consistency.py` |

### 第二层：实验与基线（4 份，每次都用）

| 文档 | 定位 | 什么时候读 |
| -- | -- | -- |
| **29** `29-box-gradient-experiment-plan.md` | **唯一实验纲领**：基线四层、覆盖度、结论登记表、待做队列、判定标准、预言对账、红线 | 立项/查结论/排队列 |
| **44** `44-new-baseline-config.md` | **基线参数台账（唯一参数事实源）**：47 键逐键生效值+来源+验证状态，由 `scripts/param_ledger.py` 生成，C6 强制校验 | 问「基线有哪些参数、值是多少」 |
| **49** `49-baseline-rebuild-charter.md` | **重建新基线行动纲领**：重建 SOP、验收四条、基线身份、永续化设计、落地顺序 | 重建基线/问「怎么重建、怎么验收、怎么活着」 |
| **50** `50-continuous-ledger-design.md` | **连续单库结转语义（阶段 C 实施规格）**：真连续复利 vs 事后链式、三件交付物、验收 V5 | 做连续单库/问「怎么把 14 单年库升级成连续账本」 |
| **51** `51-resume-design.md` | **断点续跑 `--resume`（阶段 D 实施规格）**：断点判据、身份校验、续跑分流、验收 V6 | 做断点续跑/问「中断了怎么续、怎么防止串基线」 |
| **48** `48-regime-attack-defense-design.md` | **攻防转换立项**（当前唯一在跑实验的宪法）：宽度信号、三层设计、8 个参数键、5 条预注册判据 | 做攻防转换实验 |

> `交易场景与参数全景.html`：参数全景的**可视化视图**（六图 + 38 键表）。**值一律以 44 台账为准**，
> 该 HTML 尚未适配 0026 的 8 个新增种子键，属待更新资产（不是事实源）。

### 第三层：算法法令

| 文档 | 定位 |
| -- | -- |
| **36** `36-algorithm-brain-charter.md` | 算法层权威总纲：七法（状态机 / 四对象分离 / 数学边界 / 五属性 / 5.83% 口径 / 生命周期边界 / 缺口 L1-L8） |

### 第四层：定调复盘

| 文档 | 定位 |
| -- | -- |
| **46** `46-alpha-and-experiment-value.md`（+ `46-alpha-structure.html`） | 回答「有没有 alpha / 从哪来 / 实验值不值」；实验价值总表（采纳 6 / 维持 11 / 否决 6） |

### 目录

| 目录 | 定位 |
| -- | -- |
| `experiments/` | **实验报告陈列馆（最宝贵资产）**：`_framework.md`（零结果规则）+ `_report.{html,md}` + `_data.json`；索引见其 README |
| `books/` | 原著依据，00 的理论出处 |
| `code-wiki/` | 代码导读（实现层），与 SSoT 冲突以 SSoT 为准 |
| `archive/` | 历史档案，**不作为事实源**（见下） |

### 2026-11-03 归档（结论已折叠，原稿仅作审计留痕）

| 归档文件 | 原定位 | 折叠去向 |
| -- | -- | -- |
| `archive/35-box-second-buy-point-and-volume-divergence.md` | 研究前瞻（pinbar 第二买点、「量=分歧」） | 29 前言（未立项、不插队）+ 00 §3.6 |
| `archive/42-tdxquant-integration.md` | TdxQuant 数据源与除权校准 | **01 §3 价格口径**（原始价格 + 只认 `dividend_event`）+ 03 |
| `archive/43-sell-exit-reconciliation.md` | T4/T5 卖出出口对账与六分类改造 | 29 §四 序 22 + 03 `close_reason` 枚举 |
| `archive/45-verification-backlog.md` | 待验证项横向台账 | 29 §五之二（队列）+ §八之二（待验证参数总表） |
| `archive/47-next-two-experiments-plan.md` | 两条正面线索闭环记录 | 44 §4.1（复核结论）+ 46（价值总表） |

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
| 18-quant-review-m88-deepseek4.1flash-*.md | 审计底稿（2026-10-26 归档） | 「旧回测盈利不可信、不建议转正」→ 00 第 0 条；R1/A1/A2/C4 已落地 |
| 19-backtest-validation-loop-glm5.3-*.md | 验证纲领（2026-10-26 归档） | E1-E6/S1-S4 验收口径被迁移 0012/0013 引用；修复已完成 |
| 20-full-picture-three-way-audit-kimik3-*.md | 三向对照（2026-10-26 归档） | 增补拼图已固化 → 00 §11.4 |
| 21-three-ai-synthesis-unified-plan.md | 三家统一纲领（2026-10-26 归档） | A1(RS超额)+A2(ST排除)+C4(滑点) 已落地 |
| 22-p05-five-year-backtest-prompt.md | p05 接力 prompt（2026-10-26 归档） | 五年回测已完成，结论 → 00 §5.6.7 |
| 34-box-lifecycle-audit-gpt-20261029.md（+原始 zip） | 外部生命周期审计原稿（2026-10-29 归档；断言已逐条对照代码复核属实） | 五属性/侧面乙/5.83% 口径 → 00 §3.7；A0/A1 → 29 §七；缺口注记 → 01/03/04/06 |
| ~~HANDOFF.md~~（根目录，2026-10-29 删，git 历史可溯） | 根目录新会话交接 prompt（10-19 起多轮滚动，已过时） | 测试路径/运维坑 → code-wiki 07 §2.1/§6；事实源指引 → 本 README 与 docs/29 |
| ~~PROMPT.md~~（根目录，2026-10-29 删，git 历史可溯） | 根目录接力事实源 v1.6（已过时） | 测试路径/运维坑 → code-wiki 07 §2.1/§6；M8 铁律 → code-wiki 07 §3.1 与 docs/06；队列纪律 → 05 M10.5 |
| retrospective-2026/ | 一次性 HTML 复盘（2026-10-26 归档） | 历史快照，无活跃引用 |
| `A:\Darvas-archive\docs\39-41\` | 阶段性交接材料（39/40/41，2026-11-02 已归档） | 历史执行顺序与启动 prompt；当前事实以 29、36、42 和 experiments/ 为准；A 盘台账见归档仓 README |

---

## 2026-10-27 删除记录（内容已折叠，git 历史可回溯）

| 删除文件 | 折叠去向 |
| --- | --- |
| 23-position-ladder-math.md | 00 §5.6.7（D0–D3 决策表 + 已否决清单 + L=0.5R 引擎洞察） |
| 24-regime-filter-and-position-redesign.md | 00 §5.6.5（0017 战绩分级）/ §5.6.7（演进结果） |
| 26-darvas-box-regime.md | 00 §5.6.8（A 判据定义 + 判别力实测 + 三元陷阱） |
| 27-iterative-experiment-prompt.md | 05 M10.5（实验方法论与队列纪律 8 条） |
| 28-e17-backtest-diagnosis.md | 02 运维铁律（不并发、耗时异常诊断要点） |
| 30-system-prediction.md（+svg） | 29 §九 预言对账（原文冻结于删除前 commit） |
| 31-regime-gate-redesign-draft.md（+2 svg） | 29 §六 B-4 设计 + 00 §3.6/§5.6.8（双指数已弃、单镜中证500 定案）；四态表退化结论在 e17_market_index_report |
| 32-constitutional-amendment-proposal.md | 00 第 0 条第四次修正注记（提案全文 git 历史） |
| 33-max-hold-revision-draft.md | 29 §六 B-4 设计 + 00 §5.6.9 落地注记 + 01 §10.5.x 参数映射（A′ 已落地） |

---

## 引用规则（新增文档必读）

1. **SSoT 互引**：00-06 之间用 `docs/0X-*.md` 短路径，由 `check_consistency.py` 校验存在性。
2. **引用归档稿**：用 `docs/archive/NN-*.md` 全路径（如 `docs/archive/42-tdxquant-integration.md`），
   核心内容应已沉淀至 SSoT。**已在 SSoT 有折叠位置的，一律引折叠处，不引归档稿。**
3. **引用现役纲领**：`docs/29`（唯一实验纲领：基线、队列、结论登记）、`docs/00 §5.6.7`（仓位定稿）、`docs/experiments/`（实验报告）。
4. **新工作不新建文档**：先回 00-06/29 沉淀；确需新文档时顺号（34…）并在文件名标注作者模型与时间。
5. **实验报告**：一律沉淀 `docs/experiments/`（`_framework.md` 零结果 + `_report.{html,md}` + `_data.json`），不落 `var/bt/`；**每份必须有明确结论**。
6. **已删除文档不得复活**：若需引用其结论，引 SSoT 折叠处；若确需原稿，用 git 历史（`git log --all -- <path>`）。
   2026-10-27 晚起 30-33 亦属此类（实验报告等历史文件中残留的 `docs/3X` 引用一律指向 git 历史）。

## 一句话总结

**00-06 是宪法与工程基准；29 是唯一实验纲领；44 是唯一参数事实源（47 键，机器校验）；48 是当前在跑实验的宪法；49 是重建纲领；50 是连续单库实施规格；36 是算法实现法令；46 是定调复盘；`experiments/` 是实验报告事实源。**
**当前状态**：入场端四候选（高度 P0-1 / 体制 P0-3 / 量比 P1-4 / 升势最高 Box P3-8）全部验证完毕，唯一存活 MA60 闸门（防守）+ 阶梯制（仓位）；下一步按 29 队列推进（攻防转换 B 类、`raiseStops` 事件研究、Box 宽度）。
**基线已显式化**：migration 0025 + 0026 之后，全新库跑完迁移即得新基线，不需要任何 `--set`。
