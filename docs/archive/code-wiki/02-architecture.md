# 02 · 架构

[← 上一篇：项目总览](01-overview.md) | [Wiki 索引](README.md) | [下一篇：数据模型 →](03-data-model.md)

---

## 1. 分层架构

```
┌──────────────────────────────────────────────────────────────────────┐
│ L4 入口层   cmd/darvas/main.go                                       │
│             CLI 解析 / serve 装配 / 报告打开                          │
├──────────────────────────────────────────────────────────────────────┤
│ L3 编排与适配层                                                      │
│   pipeline（七任务链 + 备份）   scheduler（时刻表 + CatchUp）          │
│   reporter（日报/周报）         gui（HTTP + embed 前端）               │
│   backtest（回放）              regimesweep（参数实验）                │
│   regime（体制标注与报告）                                            │
├──────────────────────────────────────────────────────────────────────┤
│ L2 领域层                                                            │
│   ingest   box   screener   pool   signal   planner   ledger   match  │
├──────────────────────────────────────────────────────────────────────┤
│ L1 基础层                                                            │
│   datasource/tdx（只依赖 contract）    store（SQLite 唯一入口）        │
├──────────────────────────────────────────────────────────────────────┤
│ L0 契约层   contract（纯类型 + 接口，只依赖标准库）                    │
└──────────────────────────────────────────────────────────────────────┘
                          ▼
        darvas.db（SQLite WAL） / var/reports / var/backup
                          ▲
   外部数据：TDX vipdoc *.day / 炒股手 CSV / 券商成交文本
```

**依赖铁律**：箭头只能向下。

- `contract` 不 import 任何业务包。
- `datasource/tdx` 不依赖 `store`，可独立测试。
- 领域包只依赖 `contract` + `store`（`planner` 额外依赖 `ledger`、`regime`；`match` 依赖 `ledger`；`regime` 依赖 `box`）。
- `store` 只依赖 `contract`，不知道任何业务语义。
- `pipeline` 是唯一把领域包串起来的编排层。
- `cmd` 是最上层装配者，不做业务计算。

完整 import 矩阵见 [07-dependencies-and-testing.md](07-dependencies-and-testing.md#1-包依赖矩阵)。

---

## 2. 进程形态

### 2.1 `darvas run`（一次性）

```
store.Open → tdx.NewReader → ingest.New → pipeline.New
  → pipeline.RunDaily(date)
      ingest → boxscan → screen → poolsync → signal → plan → report → BackupDaily
  → 打开 var/reports/daily_YYYYMMDD.html
```

### 2.2 `darvas serve`（常驻）

```
store.Open ×2（写连接 stW / 只读连接 stR，WAL 双连接）
  → pipeline.New(stW, ingester)
  → scheduler.New(stW, pipeline, tdxReader)
      OnDayDone = pipeline.BackupDaily
  → scheduler.CatchUp()   // 前一交易日整日补跑 + 今日已过点补跑 + 周六周报
  → scheduler.Start()     // 每 30s tick
  → gui.Run(stR, addr, autoOpen=false)
```

调度器只通过 `Runner` 接口调用 `pipeline.RerunTask`，不直接持有领域引擎。

### 2.3 `darvas ui`（只读）

```
store.Open → gui.Run(st, addr, autoOpen=true)
```

GUI 只注册 GET 路由，不提供写操作。

---

## 3. 盘后数据流（核心链路）

```
TDX .day / CSV / 券商文本
      │
      ▼
[1] ingest        全量/增量写 daily_bar + security；校验新鲜度
      │
      ▼
[2] boxscan       对股票池逐只读 800 根 K 线 → box.Engine.Scan → UpsertBoxes
      │           增量：跟踪池全量 + 全市场预过滤（新高/放量/破位）
      ▼
[3] screen        四条件：流动性 / RS 超额 / 放量比 / 箱体形态门槛
      │           → candidate（append-only 快照）
      ▼
[4] poolsync      candidate → watch(watching)；更新当前箱体；
      │           跌破下轨 / 连箱不涨 → exited；黑名单冷却 / 挂起激活
      ▼
[5] signal        对 watching/pending/holding 判定首次事件：
      │           stop / failed_breakout / breakout
      ▼
[6] plan          信号 + 持仓 + 权益 + 参数 → trade_plan + plan_order
      │           含首买、止损卖、初始止损、不获利离场、止损上移
      ▼
[7] report        任务心跳 + 候选 + 信号 + 明日计划 + 持仓 → HTML
      │
      ▼
[8] BackupDaily   VACUUM INTO var/backup/darvas_YYYYMMDD.db，保留 30 份
```

### 3.1 各阶段读写表

| 阶段 | 读 | 写 |
| --- | --- | --- |
| ingest | TDX 文件 / CSV | `security`、`daily_bar` |
| boxscan | `daily_bar`、`security`、`watch`、`box` | `box` |
| screen | `daily_bar`、`security`、`box`、`param` | `candidate` |
| poolsync | `candidate`、`watch`、`box`、`security`、`daily_bar` | `watch`、`security.track_level` |
| signal | `watch`、`box`、`daily_bar`、`param` | `signal` |
| plan | `signal`、`position`、`box`、`equity_snapshot`、`param`、`watch` | `trade_plan`、`plan_order`、`watch.stage`、`position.stop_price`、`equity_snapshot` |
| report | `daily_run`、`candidate`、`signal`、`trade_plan`、`plan_order`、`position` | 文件（不写库） |
| 人工 fills | 文件/stdin | `trade_fill`、`position`、`position_leg`、`plan_order`、`equity_snapshot`、`security.track_level` |
| match（仅回测） | `plan_order`、`daily_bar`、`position` | 同 fills |

---

## 4. 调度与补跑

### 4.1 时刻表（唯一事实源：`scheduler.Schedule`）

| 时刻 | 任务 |
| --- | --- |
| 16:00 | ingest |
| 16:30 | boxscan |
| 17:00 | screen |
| 17:30 | poolsync |
| 18:00 | signal |
| 18:30 | plan |
| 19:00 | report |
| 周六 08:00 | weekly（`WeeklyClock`，独立于每日链） |

> 代码注释的历史叙述中偶见 10:00 的旧描述；**当前常量是 `08:00`**，以常量和 `docs/01 §4.3` 为准。

### 4.2 触发语义

`Scheduler.tick()` 每 30 秒运行一次：

1. `fireDue`：找出「当天已到点、且该任务今天尚未消费触发点」的条目 → 消费并执行 `runOverdue`。
2. `runOverdue`：
   - 判定交易日（`sh000001` 是否在 DB 或 TDX 文件中有当日 K 线；没有则视为非交易日/未下载，跳过）。
   - 按 `Schedule` 顺序遍历：未到点则停止；`daily_run` 已 ok 则跳过；否则 `RerunTask`。
   - `ingest` 失败 → 中止本链，等下一个触发点；其他失败 → 记录后继续。
   - `report` ok → `fireDayDone` → `OnDayDone`（生产环境接备份）。
3. `fireWeekly`：周六到点且当天未 ok → 跑 weekly（不查交易日历）。

### 4.3 CatchUp

启动时（`serve` 调用 `CatchUp` 后 `Start`）：

- 前一交易日（`PrevDate("sh000001", today)`）若**没有任何 ok 心跳** → 整日链式补跑。
- 今天已过点但未 ok → 链式补跑。
- 更早的历史缺口不自动回放，用 `darvas run --date X` 或单任务补跑。

### 4.4 并发与停止

- `running` 标志防止 tick 重入。
- `Start` 捕获局部 `stopCh`，`Stop` 置 nil 并 close；循环使用局部通道，避免在 nil 通道上永久阻塞。
- `Stop` 取消 ctx（长任务尽快退出），等待 `stopped` 关闭。

---

## 5. GUI 数据流

```
浏览器  GET /api/*?date=YYYY-MM-DD
   │
   ▼
gui.Server（只读 *store.Store）
   │  SQL / Repository 查询
   ▼
JSON（nil 切片序列化为 []，禁止 null）
   │
   ▼
static/index.html（单文件，canvas 自绘 K 线/箱体/权益曲线，无外部 JS 库）
```

| 路由 | 作用 |
| --- | --- |
| `GET /api/dashboard` | 当日概览：任务心跳、候选/信号计数、持仓、计划等 |
| `GET /api/dates` | 有数据的日期列表（全站日期下拉） |
| `GET /api/watch` | 跟踪池看板 |
| `GET /api/candidates` | 按日候选快照 |
| `GET /api/signals` | 按日信号 |
| `GET /api/stock/{code}` | 个股 K 线/箱体/区间查询 |
| `GET /api/trades` | 交易记录四合一（区间） |
| `GET /api/schedule` | 调度时刻表（由 scheduler 派生，避免硬编码） |

---

## 6. 回测与实验通路

### 6.1 内置回放器（`internal/backtest`）

```
源库（只读）──Bootstrap──▶ scratch 库（var/bt/bt_<mode>.db）
                              │
                              ▼
                    按交易日逐日 Replay：
                    boxscan → screen → poolsync → signal → plan
                         → match（模拟成交）
                         → report
                              │
                              ▼
              prefill/progressive 哈希对比（verify）
              或 regime 长窗口 + 体制报告
```

- `prefill`：全历史 K 线预灌，任务靠查询边界自律。
- `progressive`：逐日灌 K 线（未来价格物理缺席），作为差分审计的地面真值。
- `verify`：双模式重放 + `boxStateHash` / `Compare` 差分。
- `regime`：长窗口 prefill + 体制报告。

`match` 只用于回测（生产是人工录入成交）。

### 6.2 参数扫描（`internal/regimesweep`）

对箱体五参数（premium / min width / volume multiplier / volume window / confirm days）做全组合 + 邻域稳定性 + 分年度报告，读回放数据，不写生产库。

### 6.3 体制标注（`internal/regime`）

- `ClassifyDate`：宽度法（全市场 broken_up/broken_down 事件流，20 日滚动），供报告。
- `ClassifyDateA`：A 判据（上证 60 日低点相邻日比），供 planner 仓位。
- `CloseVsMA`：上证收盘 vs MA60，供 `gate.ma60.mode` 实验开关（off/half/stop）。

---

## 7. 横切机制

| 机制 | 实现 |
| --- | --- |
| 任务心跳 | 每次任务前 `StartRun`，结束后 `FinishRun` 写 `daily_run`；调度据此幂等跳过已 ok |
| 幂等 | 导入 upsert；箱体按 `(top,bottom)` 复用；信号同日去重；计划头 `ON CONFLICT(plan_date)` |
| 事务 | ingest 按 2000 code 批量提交；box Upsert 单 code 单事务；fills 逐笔幂等 |
| 连接 | `SetMaxOpenConns(1)`，WAL；serve 开两个 `*Store`（读写/只读） |
| 备份 | `VACUUM INTO`，文件名含日期，按字典序保留最近 30 份 |
| 审计 | `equity_snapshot` 全量重算；`AuditEquation` 校验权益恒等式与数量闭合 |
| 日期 | 所有查询日期列 `CAST(... AS TEXT)`，规避 modernc 驱动 RFC3339 扫描 |
| 参数 | 全部经 `param` 表热调；迁移种子 + `ParamRepo.Get*` 兜底默认 |
| 日志 | 每个包带 `[module] ` 前缀的 `log.Logger` |

---

## 8. 失败策略

| 场景 | 行为 |
| --- | --- |
| `ingest` 失败 | `RunDaily`/调度链中止；等下一触发点重试 |
| `boxscan/screen/poolsync/signal` 失败 | 记录 failed，继续后续任务；可单任务补跑 |
| `plan` 失败 | 标红，已完成任务保留，日报仍生成；需人工介入 |
| `report` 失败 | 不影响数据链；日报缺失 |
| 备份失败 | 仅告警，不阻断 |
| `weekly` 失败 | 记录；可 `darvas weekly --date X` 补跑 |
| 撮合/成交异常 | `ApplyFill` 返回错误并拒绝错账（宁可拒绝不可错账） |

---

## 9. 扩展点

| 想做什么 | 改哪里 |
| --- | --- |
| 新增策略参数 | 新迁移 `INSERT OR IGNORE INTO param` + 读取处 `Get*` 兜底 |
| 新增表/列 | 新迁移文件（append-only）+ 更新 `docs/03` |
| 新增模块契约 | `contract.go` + 同步 `docs/04` |
| 新增 GUI 视图 | `internal/gui/gui.go` 路由 + `static/index.html` |
| 新增定时任务 | `scheduler.Schedule` + `docs/01 §4.1` + `pipeline.RerunTask` 分支 |
| 新增回测模式 | `backtest.Mode` + `backtest.Run` switch |
