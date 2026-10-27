# 03 · 数据模型

[← 上一篇：架构](02-architecture.md) | [Wiki 索引](README.md) | [下一篇：核心模块 →](04-core-modules.md)

> 事实源：`internal/store/migrations/*.sql` + `docs/03-erd.md`。改 Schema 必须新增迁移（append-only）并同步 ERD。

---

## 1. 数据库总览

| 项 | 值 |
| --- | --- |
| 引擎 | SQLite（`modernc.org/sqlite`，纯 Go） |
| 默认路径 | `var/darvas.db`（可用 `--db` / 环境变量覆盖） |
| 日志模式 | WAL |
| 外键 | `PRAGMA foreign_keys = ON` |
| 连接策略 | `SetMaxOpenConns(1)`；serve 开写/读两个 Store |
| 迁移 | 17 个，`internal/store/migrations/*.sql`，go:embed |
| 表合计 | 16（15 张业务表 + `schema_version`） |

### 1.1 表分组

| 域 | 表 |
| --- | --- |
| 主数据 | `security`、`daily_bar` |
| 箱体与选股 | `box`、`candidate`、`watch`、`signal` |
| 交易 | `position`、`position_leg`、`trade_plan`、`plan_order`、`trade_fill`、`trade_review` |
| 账本与运行 | `equity_snapshot`、`daily_run` |
| 配置与版本 | `param`、`schema_version` |

---

## 2. 表结构

### 2.1 `security` — 证券基础

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `code` PK | TEXT | `sh600519` / `sz000001` / `sh000001`（指数） |
| `market` | TEXT | `sh` / `sz` |
| `name` | TEXT | 名称（空=未知，ST 过滤时保守保留） |
| `kind` | TEXT | `stock` / `index` |
| `board` | TEXT | `main` / `gem` / `star` / `st` |
| `industry` | TEXT | 行业（可空） |
| `list_date` | DATE | 上市日 |
| `status` | TEXT | `active` / `delisted` |
| `track_level` | TEXT | `normal` / `winner` / `blacklist`（0017） |
| `blacklist_until` | DATE | 黑名单冷却截止日（0017） |
| `updated_at` | DATETIME | 本地时间 |

`track_level` 是 2026-10-22 改版后的「战绩分级」：`normal` / `winner` / `blacklist`；亏损降级、黑名单冷却。0017 曾定义「普通 1 单位、赢家 5 单位」，但 2026-10-23（A M1）已**取消 winner 5x**，planner 改按 A 判据「升势 2 单位 / 其余 1 单位」；`track_level` 仍记录并影响黑名单与跟踪池状态，不再直接决定下注档位。

### 2.2 `daily_bar` — 日线

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `code` PK/FK | TEXT | → `security.code` |
| `date` PK | DATE | `YYYY-MM-DD` |
| `open/high/low/close` | REAL | 价格 |
| `volume` | INTEGER | 手 |
| `amount` | REAL | 元 |
| `adj_factor` | REAL | 预留复权因子，固定 1.0 |
| `ingested_at` | DATETIME | 导入时间 |

- `PRIMARY KEY (code, date) WITHOUT ROWID`，重复导入天然幂等。
- 索引：`idx_daily_bar_date(date)`、`idx_daily_bar_code_date(code,date)`（0011 性能修复，避免 2900 万行全扫）。

### 2.3 `box` — 箱体（阶梯链）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` PK | INTEGER | 自增 |
| `code` FK | TEXT | → `security` |
| `top` / `bottom` | REAL | 上下轨；下轨未确立为 NULL |
| `top_confirmed` / `bottom_confirmed` | DATE | 三日确认日 |
| `status` | TEXT | `forming` / `confirmed` / `broken_up` / `broken_down` / `invalid` |
| `prev_box_id` | INTEGER | 自引用，阶梯前驱 |
| `broken_date` | DATE | 突破/跌破日 |
| `created_at` | DATETIME | — |

索引：`(code,status)`、`(code,id)`。

**写入策略（关键）**：`BoxRepo.UpsertBoxes` 按 `(top,bottom)`（6 位小数）匹配既有箱体并**复用 DB ID**；未匹配旧箱删除，但被 `watch.current_box_id`、`position.entry_box_id`、`signal.box_id` 或作为他箱前驱引用的箱体保留。禁止「删旧重插」，否则外键断裂并可能整事务回滚。

### 2.4 `candidate` — 初选快照

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` PK | INTEGER | — |
| `screen_date` | DATE | 初选日 |
| `code` FK | TEXT | → `security` |
| `score` | REAL | 四条件得分 |
| `reasons_json` | TEXT | 各条件实测值与阈值 |
| `fundamental_flag` | TEXT | `yes/no/unknown`（人工复核） |
| `created_at` | DATETIME | — |

append-only 快照：`AppendCandidates` 只插入；同日人为重跑 `screen` 会追加重复行，`CandidatesOn` 按 `screen_date` 返回全部（正常调度因 `daily_run` 心跳跳过已 ok 任务，不会重复）。

### 2.5 `watch` — 跟踪池状态机

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` PK | INTEGER | — |
| `code` FK | TEXT | → `security` |
| `candidate_id` FK | INTEGER | 入池来源 |
| `added_date` | DATE | 入池日（观察期起点） |
| `stage` | TEXT | `watching/pending/holding/exited/suspended/blacklisted`（0003 重建） |
| `current_box_id` FK | INTEGER | 当前箱体 |
| `box_count` | INTEGER | 已走完箱体数（`pool.flatboxmax` 判定） |
| `exit_reason` | TEXT | 淘汰原因 |
| `resume_trigger` | REAL | 条件挂起激活价（`suspended`） |
| `note` | TEXT | 人工备注/历史战绩提示 |
| `updated_at` | DATETIME | — |

### 2.6 `signal` — 信号日志

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` PK | INTEGER | — |
| `date` | DATE | 信号日 |
| `code` FK | TEXT | — |
| `type` | TEXT | `breakout/pyramid/stop/failed_breakout`（`pyramid` 已退役，保留枚举） |
| `box_id` FK | INTEGER | 参照箱体 |
| `close_px` | REAL | 当日收盘 |
| `box_level` | REAL | 触发轨价格 |
| `payload_json` | TEXT | 首次事件上下文/昨日状态 |
| `created_at` | DATETIME | — |

索引：`(date,type)`、`(code,date)`。`AppendSignals` 按 `(date, code, type, COALESCE(box_id,0))` 去重，重复插入直接跳过。

### 2.7 `position` / `position_leg` — 持仓与腿

`position`：

| 列 | 说明 |
| --- | --- |
| `id` PK | — |
| `code` FK | 股票 |
| `opened_date` | 开仓日 |
| `entry_box_id` FK | 入场箱体 |
| `qty` | 当前数量 |
| `avg_cost` | 含费成本 |
| `stop_price` | 止损位，**只升不降**（应用层 + 触发器 `trg_position_stop_no_down`） |
| `status` | `open/closed` |
| `closed_date` / `close_reason` | 平仓信息 |
| `realized_pnl` | 已实现盈亏（0003 新增） |

`position_leg`：

| 列 | 说明 |
| --- | --- |
| `position_id` FK | 持仓 |
| `leg_no` | 1/2/3（CHECK） |
| `date` / `qty` / `price` | 腿成交 |
| `stop_after` | 本腿成交后止损位 |
| `plan_order_id` | 关联订单 |

`UNIQUE(position_id, leg_no)`；超过 3 腿的手动加码不记腿。

### 2.8 `trade_plan` / `plan_order` — 书面计划

`trade_plan`：`plan_date`（唯一）、`execute_date`（下一交易日）、`equity_base`、`open_position_count`。

`plan_order`：

| 列 | 说明 |
| --- | --- |
| `trade_plan_id` FK | 计划 |
| `code` | 股票 |
| `side` | `buy/sell` |
| `order_kind` | `stop_buy/stop_sell` |
| `trigger_price` / `limit_price` | 触发价 / 委托价 |
| `qty` | 数量 |
| `leg_no` | 腿号（可空） |
| `reason_json` | 六问答案与推导（可审计） |
| `status` | `pending/placed/filled/cancelled/expired` |

### 2.9 `trade_fill` — 成交

| 列 | 说明 |
| --- | --- |
| `plan_order_id` | 可空（计划外成交也入账） |
| `code` / `date` / `price` / `qty` | 成交事实 |
| `fee` | 费用（佣金+印花税+过户费） |
| `raw_text` | 原始文本，审计 |
| `side` | `buy/sell`（0002 新增，回填自订单） |

索引：`(code,date)`、`(date)`。

### 2.10 `equity_snapshot` — 每日权益

| 列 | 说明 |
| --- | --- |
| `date` PK | 交易日 |
| `cash` | 现金 |
| `market_value` | 持仓市值 |
| `total` | 总权益 |
| `peak_total` | 历史峰值 |
| `drawdown` | `(peak-total)/peak`，负数 |

### 2.11 `daily_run` — 任务心跳

`UNIQUE(date, task)`；`status ∈ running/ok/failed/skipped`，另有 `started_at/finished_at/error/affected_rows`。调度器与 `RerunTask` 用它实现幂等跳过。

### 2.12 `trade_review` — 已交易股票历史库

`position_id` 唯一（每笔交易一条复盘），`code`、`rule_followed`、`error_type`、`lesson`、`created_at`。B3 原书校对产物，人工录入。

### 2.13 `param` — 参数

`key` PK、`value`、`old_value`、`changed_at`。所有业务参数热调；读取一律 `GetFloat/GetInt/GetString(key, 默认值)`，默认值写在代码中。

### 2.14 `schema_version`

由 `store.migrate()` 维护；迁移文件内禁止自行 INSERT 版本号。

---

## 3. ER 关系

```
security 1──N daily_bar
security 1──N box ──self──▶ box.prev_box_id
security 1──N candidate ──1──N watch
box 1──N watch.current_box_id
box 1──N signal.box_id
box 1──N position.entry_box_id

position 1──N position_leg
position 1──1 trade_review

trade_plan 1──N plan_order 1──N trade_fill
security 1──N trade_fill / trade_review
```

均为弱级联：代码层负责一致性，DB 用外键阻止悬空引用；历史事实（成交/箱体）不允许物理删除。

---

## 4. 状态机

| 实体 | 状态迁移 |
| --- | --- |
| `box` | forming → confirmed → broken_up / broken_down；结构破坏 → invalid |
| `watch` | watching → pending → holding → exited；watching ⇄ suspended；任意 → blacklisted（冷却后回 normal 池） |
| `position` | open → closed（一次性，不可复活） |
| `plan_order` | pending → placed → filled；pending/placed → cancelled/expired |
| `daily_run` | running → ok / failed / skipped |
| `security.track_level` | normal → winner → normal（亏损降级）；normal → blacklist（冷却 12 月）→ normal |

---

## 5. 迁移历史（append-only）

| 版本 | 文件 | 内容 |
| --- | --- | --- |
| 0001 | `0001_init.sql` | 全量初始 Schema + 14 个种子参数 + 止损触发器 |
| 0002 | `0002_fill_side.sql` | `trade_fill.side` + 回填 + `idx_fill_date` |
| 0003 | `0003_pool_lifecycle.sql` | `watch` 重建为六状态 + `resume_trigger`；`position.realized_pnl`；池生命周期参数 |
| 0004 | `0004_trade_review.sql` | `trade_review` 表 |
| 0005 | `0005_dynamic_position.sql` | 试探/放大参数（后被单位制取代） |
| 0006 | `0006_fees.sql` | 佣金/最低佣金/印花税/过户费参数 |
| 0007 | `0007_regime.sql` | 指数动量体制参数（已退役对照） |
| 0008 | `0008_pyramid_addon.sql` | 同箱加码上限（加码已退役，参数保留） |
| 0009 | `0009_regime_position.sql` | 体制仓位倍数（已退役） |
| 0010 | `0010_breadth_unit_position.sql` | 宽度法参数 + 单位制参数 |
| 0011 | `0011_daily_bar_code_date_index.sql` | `(code,date)` 复合索引（性能） |
| 0012 | `0012_min_watch_days.sql` | `pool.min_watch_days=3` |
| 0013 | `0013_max_entry_gap.sql` | `match.max_entry_gap=1.05` |
| 0014 | `0014_rs_excess_slippage_st.sql` | `screen.rs_excess_min=.20`、`match.slippage=.002`（ST 排除是代码改动） |
| 0015 | `0015_box_min_width.sql` | `box.min_width=0.05` |
| 0016 | `0016_staledays_2.sql` | `position.staledays` 15 → 2 |
| 0017 | `0017_track_level.sql` | `security.track_level/blacklist_until` + 战绩分级参数 |

迁移机制：文件名前缀数字为版本；`store.migrate()` 在事务内执行未应用脚本并写 `schema_version`。**禁止修改已发布脚本**；参数修正用新迁移 `UPDATE`。

---

## 6. 参数体系

### 6.1 当前生效参数（代码确实读取）

| Key | 默认 | 读者 | 作用 |
| --- | --- | --- | --- |
| `tdx.root` | `H:/new_tdx64/vipdoc` | ingest / cmd | 通达信数据根 |
| `equity.base` | `1000000` | ledger | 首次权益基准（未种子化） |
| `buy.premium` | `1.03` | box/signal/planner | 突破触发溢价 |
| `sell.discount` | `0.97` | planner | 止损触发折价 |
| `box.volmult` / `box.volwindow` | `2.0` / `50` | box/regime | 突破放量 |
| `box.min_width` | `0.05` | box | 箱体最小宽度（工程假设，原书无） |
| `screen.turnover` | `50000000` | screener | 5 日均额下限 |
| `screen.rswindow` | `20` | screener | RS 回看 |
| `screen.rs_excess_min` | `0.20` | screener | 个股−上证超额收益（pp） |
| `screen.latedays` | `4` | screener | 太晚规则 |
| `pool.flatboxmax` | `4` | pool | 连箱不涨淘汰 |
| `pool.maxwatch` | `8` | pool | 软参考，仅告警 |
| `pool.min_watch_days` | `3` | planner | 入池后最少观察交易日 |
| `pool.max_hold` | `7` | planner | 同时持仓上限 |
| `pool.blacklist_months` | `12` | `store/repo_trade` | 黑名单冷却 |
| `position.staledays` | `2`（0016） | planner | 不获利离场 |
| `pos.unit` | `0.02` | planner | 1 单位权益比例 |
| `match.max_entry_gap` | `1.05` | planner/match | 追高上限 |
| `match.slippage` | `0.002` | match | 回测滑点 |
| `fee.*` | 万2.5 / 5 / 千1 / 万0.1 | match | 回测摩擦成本 |
| `regime.breadth.*` | 20/0.55/0.40/3/60 | regime | 宽度法 |
| `regime.a.lookback/up_bias/down_bias` | 60/1.002/0.998 | regime | A 判据（未种子化） |
| `gate.ma60.mode` | `off` | planner | MA60 闸门实验（未种子化） |
| `pos.newhigh_ladder` + `pos.newhigh.*` | `off` | planner | 52 周新高分档实验（未种子化） |

### 6.2 历史/未读取参数（保留作对照或审计）

| Key | 状态 |
| --- | --- |
| `risk.leg1/leg23/maxpos/maxlegs` | 腿号风险体系退役（单位制取代） |
| `risk.probe_min_shares/probe_max_pct/scale` | 动态仓位试探体系退役 |
| `risk.addon_max_premium` | 同箱加码退役（2026-10-23） |
| `regime.window/bull_thresh/bear_thresh`、`regime.mult.*`、`regime.indices` | 指数体制版退役 |
| `screen.rsthreshold` | 比值法退役（超额收益取代） |
| `pool.maxloss` | 两亏永久拉黑退役（track_level 取代） |
| `pos.units.winner` | 分级阶段曾用；当前 planner 按 A 判据 2/1 单位，未读取该键 |
| `pos.units.probe/main/reserve/per_stock_*` | 单位上限细分未接线 |
| `screen.maxboxage` | 种子保留，当前无读取 |

> 判断依据是 `GetFloat/GetInt/GetString` 的 grep 结果（`bd3028d`）。新增参数时，务必在迁移里种子化并在代码中给默认值。

---

## 7. 运行时不变量

1. **止损只升不降**：DB 触发器 + `ClosePosition/raiseStops` 双层防护。
2. **成交是账本唯一事实源**：持仓、现金、权益均由 `trade_fill` 重算；计划数量不参与对账。
3. **日期扫描**：所有 SELECT 的 DATE 列必须 `CAST(x AS TEXT)`，否则 modernc 驱动返回 RFC3339。
4. **幂等键**：`daily_bar(code,date)`、`daily_run(date,task)`、`trade_plan(plan_date)`、`position_leg(position_id,leg_no)`、`trade_review(position_id)`、`equity_snapshot(date)`。
5. **append-only 语义**：`candidate`、`signal`、`trade_fill` 不更新历史；`box` 允许幂等重算；`watch` 由状态机迁移。
6. **FK 安全**：删除箱体/证券前必须确认无引用；历史成交与箱体不可删。
7. **单写者**：`MaxOpenConns(1)`；长任务不要嵌套开启第二个写连接。
