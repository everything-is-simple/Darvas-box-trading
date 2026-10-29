# E17-A0/A1 · Box 轨迹工程验证框架（确定性契约 + 生命周期可观测性）

> 状态：**框架定稿（2026-10-29，先于结果）**。本文件只写规则：怎么问、怎么取数、怎么判定。
> 结果一律进 `e17_a0_determinism_report.md` 与 `e17_a0_determinism_data.json`，本文件不含结果数字。
> 立项依据：docs/29 §五之二 第 0 项 + §七「止损随 box 上移」前置段；docs/00 §3.7 第 1.1 点
> 「算法心脏活了但生命史证据未落地」；docs/06 §3.1 生命周期不变量表（本框架即其 A0 验证清单的执行版）。

---

## 〇、目的与红线

**要回答的问题**：Box 的轨迹（身份 + 事件日期）在系统的三种重建路径下是否一致、可信、可追溯？
只有答案为「是」，第 2 项（止损随 box 上移事件研究）才有资格把重建结果当作事实使用。

**红线（零策略变更）**：
- 不改任何策略参数、不新增维度、不触碰 2019+ 样本（本实验只用 2005-2013 数据段）；
- 源库只读（`mode=ro`），全程**不写任何数据库**（对照在内存中完成）；
- 差异必须**逐条归类**（窗口截断 / 预过滤滞后 / 真不一致），禁止默认忽略，禁止混类统计；
- 失败即升级 A3 schema 设计（migration 方案 + docs/03/04/06 同 commit），不得带病放行第 2 项。

---

## 一、三路重建路径定义（A0④ 的被测对象）

生产系统对 Box 轨迹有三种重建路径（`internal/box/scanner.go`）：

| 路 | 名称 | 定义 | 生产对应 |
|---|---|---|---|
| **A** | 全量真值 | 对个股全部历史 K 线（本实验取 ≥2005-01-01）逐日 `Engine.Scan`，逐日快照 | 数学参照（ADR-1 确定性纯函数的极限语义） |
| **B** | 800 窗口 | 只取最近 `rescanWindow=800` 根 K 线再 `Engine.Scan`（scanner.go:22） | `rescanOne`——`RescanAll` 与 `RescanIncremental` 共用的实际计算路径 |
| **C** | 增量预过滤 | 逐日只对「预过滤命中」的票重算（60 日新高 / 量≥均量×volmult / 最新 confirmed 下轨被收盘跌破），未命中票保留昨日状态 | `RescanIncremental` 的池外路径；**本实验取池=∅ 的最严苛上界**（生产中池内票每日全量重放，只会更好不会更差） |

- 参数：`DefaultParams()`（= 0001_init.sql 种子：volmult 2.0 / volwindow 50 / premium 1.03 / min_width 0.05 / confirm_days 3）。
- 样本：活跃股（kind=stock，非 ST，main/gem/star）中 2013-01-01 前已有 ≥200 根日线者，按代码排序等步长抽样至 ≤800 只；
  日历 = sh000001 在 [2013-01-01, 2013-12-31] 的 238 个交易日。
- 选取 2013 的理由：脱钩结构牛、突破/作废/破位事件密度高，三类状态迁移都能采到足量样本。

## 二、A0①②③ 判定标准（引擎级，Go 单测，`internal/box/a0_test.go`）

| # | 契约 | 判定规则（写死，不得事后放宽） | 门槛 |
|---|---|---|---|
| ① | **终态不可逆 + 序列前缀稳定** | 对任意前缀 p：`Scan(bars[:p])` 除最后一个（可变尾箱）外，必须与全量 `Scan(bars)` 的对应前缀**逐字段相等**（含 status / 确认日 / broken_date / prev 链）。broken_up / broken_down / invalid 三种终态各有专测。 | 违反数 = 0 |
| ② | **一根 K 线一箱 + 同根只开一箱** | (a) 逐前缀推进时箱体序列长度日增量 ∈ {0,1}（单日至多一个终止事件）；(b) 输出 ID 严格连续 1..N；(c) broken 箱体的 brokenDate 当日，前缀扫描已含 ID+1 的新 tail（终止当根即开新箱）；(d) invalid 同理（下一个实例首现日 = 作废日）；(e) 身份相位演化固定为 (0,0)→(top,0)→(top,bottom)。 | 违反数 = 0 |
| ③ | **重复 Scan 幂等** | 同输入 5 次重复 + 双引擎交替扫描，输出 `reflect.DeepEqual`；且 Scan 不得变异输入切片（深拷贝比对）。 | 违反数 = 0 |

## 三、A0④ 判定标准（数据级，`cmd/a0check` 生成器，内存三路对照）

### 3.1 比对对象（逐日、逐票）

1. **尾箱状态**（当前 forming/confirmed 状态投影：top / bottom / top_confirmed / bottom_confirmed / status）——signal/poolsync 的直接输入；
2. **终态事件集**：broken_up / broken_down 事件（top, bottom, broken_date）——止损上移研究的直接原料。

语义比对忽略引擎局部 ID 与 DB 自增 ID（A/B 路起点不同则局部 ID 必然不同；DB ID 复用语义单列 A1 §四.4）。

### 3.2 洁净区与差异归类（三类，互斥穷尽）

**关键设计：洁净区 = 共同后缀（commonSuffix）**。800 窗口重启后，B 路的箱体家谱与 A 路**定义性分叉**
（B 从窗口起点重开箱体，分段与 A 不同），分叉点之后（更近端）两家谱必然再同步——因此洁净区取
**从尾往前语义键逐箱相同的段**：该段内两路输入上下文完备（同一批 K 线、同一确定性引擎），
任何差异都是真不一致；分叉区（共同后缀之前）的差异属窗口语义。

语义键 = `(top, bottom, top_confirmed, bottom_confirmed, status, broken_date)`（浮点 'g' 格式化拼接，
忽略引擎局部 ID 与 DB 自增 ID）。

> **勘误（2026-10-29，首跑证伪 → 判据修订；docs/06 §7.3 两阶段修订留痕）**
> 初版判据：「洁净箱体 = top_confirmed ≥ 窗口起点+10 根；洁净事件 = 事件日 ≥ 窗口起点+50 根且箱体洁净」。
> 首跑实测 missing_clean=165 / spurious_clean=376 / c_loss=115，硬失败。
> 根因：top_margin 只保证箱体输入落在窗口内，**不保证 A/B 家谱「同箱同史」**——B 重启箱可整窗口
> 分叉后再同步，A 的洁净箱在 B 中可能以不同分段路径存在（或不存在），初版判据把它们误判为
> 缺失/多出。修订：洁净区改**共同后缀**；C 路删「洁净事件永久丢失」判据，改**恒等式抽查**
> （C 状态 == B 在其上次触发日的 800 窗口重扫，确定性抽 50 票全对）。锚比对保留 top_margin=10
> 作为「洁净缺失」的辅助判定。

| 类 | 定义 | 处置 |
|---|---|---|
| **窗口语义** | 共同后缀之外的 A/B 差异：分叉区事件（trunc_zone_A / trunc_zone_B）、尾箱/锚的家谱相位差（交叉存在、单方超前） | 允许，必须计数并给分布（共同后缀均长/最短） |
| **预过滤滞后**（仅 C 路） | C 状态落后于 B，但在该票下一次预过滤触发日**收敛**；恒等式：C 状态 == B 在其上次触发日的 800 窗口重扫 | 允许，必须统计滞后天数分布与触发率；恒等式抽查失败 > 0 即硬失败 |
| **真不一致** | (a) 共同后缀内 A/B 事件键集合差（suffix_violation）；(b) 一路的最新 confirmed 箱为洁净箱体（top_confirmed ≥ 窗口起点+10 根）却完全不存在于另一路序列 | **硬失败**，逐例登记，数量 > 0 即本实验失败 |

**硬门槛（放行第 2 项的先决条件）**：
1. A0①②③ 违反数 = 0；
2. A vs B 最新 confirmed 锚「洁净缺失」型真不一致 = 0（尾箱差异全部归窗口语义诊断桶，不设独立硬闸门）；
3. A vs B 共同后缀事件差异（suffix_violation）= 0；
4. C 路恒等式抽查失败 = 0（确定性抽 50 票）；
5. 差异总账：窗口语义 / 预过滤滞后两类必须给出**数量与分布**，缺一不得引用本实验结论。

### 3.3 Signal / PlanOrder 层（④ 的下游覆盖，两件套）

1. **经验锚**：`darvas backtest --mode verify`（M8.1 差分审计）在本框架定稿后于有界窗口
   （2013-07-01 ~ 2013-09-30）复验一次：prefill vs progressive 六任务链（boxscan→screen→poolsync→
   signal→plan→report+match）决策输出 dump（含 signal / trade_plan / plan_order / position /
   equity_snapshot）必须全等；
2. **归纳论证**（登记，不再单独实验）：box 逐日状态三路全等（3.2 硬门槛过）+ 引擎与各任务
   均为确定性纯函数（A0③ + pipeline 任务无内部随机源）⇒ 相同 box 输入下 signal/plan_order
   必然等值。box 层是唯一的历史重建自由度，其余输入（K 线、参数、日历）三路共享同一份。

## 四、A1 生命周期可观测性口径（同一次运行产出，不另起实验）

### 4.1 实例定义

- **实例** = A 路全量扫描输出序列中的一个槽位（occurrence index，全史下标稳定，前缀性质由 A0① 保证）。
- **身份真空期**：`top==0` 的 forming 阶段（上轨未确认）——DB 身份键为 (0,0)，跨重启不可区分；
  统计其天数占比，如实登记为「DB 身份不可观测期」。
- **身份相位**：`(0,0) → (top,0) → (top,bottom)`（A0②(e) 实测锚定）——DB 身份键随子阶段演化，
  这是 `UpsertBoxes` 按 (top,bottom) 复用 ID 的直接推论（docs/03 §2.1）。

### 4.2 事件视图（每实例一张账）

事件列：`first_seen`（实例首现于逐日快照的交易日）/ `top_confirmed` / `bottom_confirmed` /
`terminal_status` / `terminal_date`（broken_up、broken_down 取 broken_date；**invalid 无原生日期**，
取「下一实例首现日」= 作废重启日，并在数据中标注 `_derived`）。

### 4.3 删失标注（不伪造生死日期）

| 标注 | 操作定义（cmd/a0check 实现口径） |
|---|---|
| **左删失（样本边界）** | `top_confirmed < 2005-01-01`（A 路取数起点）：出生日期不完整 |
| **左删失（B 窗）** | `top_confirmed <` 该票 B 路 800 窗口起点日：对 B 路语义而言出生不可观测 |
| **左删失（观测边界）** | 实例出生早于逐日观测窗（2013-01-01）：`first_seen` 被钳制为窗内首现日，出生日不可信。终态在窗内者，其存续为**下界**（自首现起算）；终态日早于首现（窗内首现即终结，`obsCensored`）不入存续分布 |
| **观测窗前已终结**（`pre_window_terminated`） | 终态日期早于逐日观测窗——A 路全量扫描可见、逐日账本从未观测到它存活的全史实例。**单列显式计数**：不冒充删失、不入存续分布（勘误：初版静默跳过，2026-10-29 补账） |
| **右删失** | 窗口末日仍 forming / confirmed（未见终态） |

**铁律**：存续统计只对「有身份且已终结、且终态日晚于观测窗内首现日」的实例计算；各删失类
单独列示占比，**严禁**把删失实例混入生死分布冒充完整个案。对账恒等式：
已终结实例总数 = 存续入样 + obsCensored + pre_window_terminated。

### 4.4 必报指标（缺一不得引用）

1. 终态占比：broken_up / broken_down / invalid / 右删失（forming、confirmed 分列）；
2. 存续日数（交易日）：分位（p25/p50/p75/p90）——仅已终结实例；
3. 身份真空期：`(0,0)` 天数占比（实现口径：`(0,0)` 尾箱天数 ÷ 全部票日，含无有效箱体的票日）；
4. **身份复用计数**：同码内语义键 (top,bottom) 出现 ≥2 个不同实例的次数（UpsertBoxes 复用把
   两个生命周期折叠进同一 DB 行的潜在次数）；(top,0) 中间身份实例数；
5. 左/右删失实例数与占比。

## 五、产物与复现

| 产物 | 生成器 |
|---|---|
| A0①②③ 证据 | `go test ./internal/box -run TestA0`（a0_test.go，随 `go test ./...` 常态回归） |
| A0④+A1 数据 | `go run ./cmd/a0check -src var/bt/full_hist_10pct.db -from 2013-01-01 -to 2013-12-31 -maxcodes 800 -out docs/experiments/e17_a0_determinism_data.json`（只读源库，内存对照，不写库） |
| Signal/PlanOrder 锚 | `go run ./cmd/darvas backtest --mode verify --src var/bt/full_hist_10pct.db --from 2013-07-01 --to 2013-09-30`（scratch 库，用后即弃） |
| 报告 | `e17_a0_determinism_report.md`（人读结论）+ `_data.json`（机器可复算） |

## 六、失败升级路径

任一硬门槛不过 → **不得**启动第 2 项止损上移统计；升级 A3 schema 设计
（`box` 表补 `start_date` / `invalidated_date` / `invalid_reason` / append-only 迁移事件账）：
先写 migration 方案，**同 commit** 更新 docs/03 §2.1、docs/04 §4.1、docs/06 §3.1（append-only，禁改已发布迁移）。
