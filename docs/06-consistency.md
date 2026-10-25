# 06 · ERD / API 一致性维护机制

> 目的：模块化开发最大的腐烂源=「文档说一套、代码跑另一套」。本机制用**单一事实源+机器校验+流程卡点**三道防线保证一致性。

---

## 1. 单一事实源（SSoT）清单

| 事实 | 唯一权威位置 | 释义文档 |
|------|-------------|---------|
| 数据库Schema | `internal/store/migrations/*.sql` | docs/03-erd.md |
| 模块接口/类型 | `internal/contract/contract.go` | docs/04-api-contract.md |
| 每日时间表/任务ID | `internal/scheduler/scheduler.go`（`Schedule` 常量表 + `WeeklyClock`） | docs/01-requirements.md §4 |
| 领域参数默认值 | `migrations/0001_init.sql` 的param种子 | docs/01-requirements.md §6 |
| 理论规则 | docs/00-darvas-theory.md（理论宪法） | 代码必须能指回条目 |

**规则：文档永远不单独成为事实**——文档是给人看的释义，SQL/Go代码是给机器执行的事实，两者由机制强制同步。

---

## 2. 三道防线

### 防线一：命名强制对齐（静态检查，`go vet`自定义或脚本）

`scripts/check_consistency.py`（CI/提交前跑）：

1. **枚举对齐**：抽取contract.go中的`BoxStatus/WatchStage/SignalType/OrderStatus`常量集合，与DDL的`CHECK(...IN(...))`集合做差集比较 → 不一致即失败
2. **表覆盖**：解析DDL的CREATE TABLE列表 vs 03-erd.md的Mermaid实体列表 → 缺一即失败
3. **任务ID对齐**：scheduler常量表 vs 01文档§4任务ID列表
4. **参数键对齐**：param种子INSERT键集合 vs 01文档§6参数表键集合

### 防线二：Schema漂移检测（运行时）

- store.Open时执行migrations后，做`schema_version`表校验
- `darvas doctor`子命令：dump实际schema与migrations累积结果对比（复用tradebuddy的schema-dump思路）
- 备份恢复演练时必跑doctor

### 防线三：流程卡点（变更协议）

**任何PR/commit必须二选一：**

- ✅ 声明「不涉及契约」（改动仅在模块内部实现）
- ✅ 附带「契约变更」：同一commit内包含
  1. migrations新增`000N_xxx.sql`（**禁止修改已发布迁移**，只允许append）
  2. docs/03或04的对应更新
  3. 若涉及状态枚举：ERD状态机表新增迁移行
  4. 跑`check_consistency.py`通过

commit message约定：`feat(box): ...` / `feat(contract): ...`（contract前缀的commit=契约变更，评审必看文档diff）。

---

## 3. 各一致性面细则

### 3.1 数据一致性（运行时不变量）

| 不变量 | 保障机制 |
|--------|---------|
| daily_bar无重复(code,date) | 复合主键+WITHOUT ROWID |
| stop_price只升不降 | SQLite触发器RAISE(ABORT)+应用层预检查 |
| 快照表append-only | Repo层只暴露Insert，无Update方法（接口面即防线） |
| 账本恒等式 cash+Σmarket=total | ledger.AuditEquation每日跑，>1分误差红色告警 |
| signal.type封闭集合 | DDL CHECK+Go类型系统双重 |
| 箱体阶梯 prev_box_id无环 | Upsert时应用层校验（链长度≤100） |

**M8 复核沉淀的状态不变量**（← 沉淀自 archive/07-M8 回放审计、docs/18 量化复核）：

| 不变量 | 保障机制 | 来源 |
|--------|---------|------|
| 跟踪池 active ≤ maxwatch | `check_consistency.py` 新增断言 `watch where stage in ('watching','pending','holding')` 数量 ≤ param `pool.maxwatch` | archive/07 #3 + docs/18 P0-4 |
| 订单 trigger 与现价偏离上限 | `planner` 生成订单前校验 `abs(trigger/close - 1) < gap上限违反直接拒单 + `match` 成交后校验 `abs(fill/trigger - 1) ≤ max_entry_gap`（默认 1.05） | docs/18 P0-2 |
| 同仓位同批次不产两张卖单 | `planner.pendingSell` 生成卖单后立即回写 `true`（修复后） | docs/18 P0-3/P0-5 |

### 3.2 接口一致性（编译期）

- 模块构造函数签名统一：`NewXxx(deps...) Xxx`，deps全部是contract接口类型 → **mock天然可行，测试不碰真DB**
- 领域引擎（BoxEngine.Scan等）零IO依赖 → 纯函数测试
- GUI绑定层方法表（04文档§6）与Wails bindings生成物对照（6.x任务中建立脚本比对）

### 3.3 行为一致性（理论与代码）

每个领域判定规则在代码注释中**必须引用理论条目号**，例如：

```go
// BreakoutValid implements docs/00-darvas-theory.md §4.2：
// 突破日成交量 ≥ 50日均量 × VolMultiplier，无量突破=假信号
```

`scripts/check_theory_refs.py`：抽取代码中的`§`引用，核对00文档章节存在性（防文档重构后悬空引用）。

---

## 4. 文档版本纪律

- 每份docs文档头部带版本行，契约变更commit同步bump
- 文档间引用用相对链接，禁止复制粘贴内容（复制=腐烂的开始）
- 每月月末「文档对账」：跑三个脚本+通读一次，30分钟内完成

---

## 5. 一致性检查脚本（随M1交付）

```
scripts/
├── check_consistency.py   # 枚举/表/任务/参数四项对齐检查
├── check_theory_refs.py   # 理论条目引用核对
└── doctor_schema.py       # 实际库schema vs migrations漂移检测
```

三个脚本全部纳入：①pre-commit（本地）②`darvas doctor`（运行时）③发版前检查清单。

---

## 6. 研究数据一致性规则（2026-10-25 立项）

> box 十维迭代实验（理论框架见 docs/00 §3.6）在一致性侧的额外规则。研究层叠加在生产层之上，不得破坏生产层的一致性防线（§1-§5）。

### 6.1 研究事实源（SSoT）补充

| 事实 | 事实源 | 派生禁止 |
|------|--------|---------|
| 已完成年份清单 | `var/bt/e17_metrics.csv` | 不要从 db 文件存在性推断（db 可能存在但未跑完） |
| 已汇报年份清单 | `var/bt/e17_reported_years.txt` | 自动汇报成功后才 append，避免重复汇报 |
| 队列状态 | `var/bt/e17_years_status.json` | state=waiting/running/done/failed + year + detail |
| 单年回测结果 | `var/bt/bt_e17_<year>.db` 的 position/plan_order 表 | 离线分析只读，不反向修改 |
| reason_json 字段语义 | docs/03 §6 + docs/04 §9 | 字段键名与语义冻结，不得改名或改义 |

### 6.2 研究铁律（与 docs/01 §10.5 一致，此处为一致性侧执行规则）

- **参数冻结**：回测期间不得修改 `box.*`、`risk.*`、`buy.*`、`sell.*`、`match.*` 任何参数——所有年度必须用同一组参数跑，跨年可比性依赖于此。参数改动 = 新轮回，必须用独立回测库（如 `bt_e17_<year>_p3.db`）。
- **串行不并发**：每年串行，文件锁 `e17_years.lock` 防并发；任一年失败即停队列。
- **库隔离**：只写 `var/bt/bt_e17_<year>.db`；源库 `full_hist_10pct.db` 与生产库 `darvas.db` mode=ro 打开，不得写入。
- **只读分析**：离线脚本（`e17_watch.py`、`e17_annual_tier_report.py`）用 `mode=ro` URI 连接回测库，不得反向修改任何表或字段。
- **不编造字段**：分析脚本对未产出的字段（如当前无 `volume_ratio`）必须返回 None 并在报告诚实标注"该维度需扩展回测器"，不留空也不填 0。

### 6.3 跨年可比性检查

每年回测完成后，`e17_watch.py` 必须校验：

- 该年 `plan_order` 表的 `reason_json` 字段键名集合与往年一致（已落地字段不得缺）。
- 该年 `sizing_basis` 取值集合 ⊆ {newhigh_t1, newhigh_t2, newhigh_t3, newhigh_t4}（不得出现新档位）。
- 该年 `units` 取值集合 ⊆ {3, 2, 1, 0.5}（与 E17 阶梯定义一致）。
- 任一不一致即在该年报告顶部红色告警，并停止队列。

### 6.4 研究文档对账（追加到月末对账清单）

月末「文档对账」追加以下检查：

- docs/00 §3.6 十维总表的"当前实现状态"列与实际代码一致（`internal/box` 是否加了 box.period 参数化、`reason_json` 是否加了 volume_ratio 等）。
- docs/01 §10.3 档位定义与 `internal/planner` 的 sizing_basis 分档逻辑一致。
- docs/03 §6.1 已落地字段清单与 `internal/contract` 的 SignalContext 字段一致。
- docs/05 M9/M10 各子里程碑状态与 `var/bt/e17_years_status.json` + `e17_metrics.csv` 一致。

### 6.5 研究产物的版本纪律

- `var/bt/e17_annual_tier_report.html` 每年跑完自动重建（覆盖旧版），不保留历史版本——研究结论是迭代的，最新版即权威版。
- `var/bt/e17_metrics.csv` append-only，不得删除或修改已有行（已汇报年份的事实源）。
- `var/bt/e17_reported_years.txt` append-only，已汇报年份不得移除（避免重复汇报）。
- 跨年结论定型后，写入 `docs/27-iterative-experiment-prompt.md` 或新 docs 文档（如 docs/29+），不直接改 docs/00-06 的研究框架结构（框架是宪法，结论是案例）。

