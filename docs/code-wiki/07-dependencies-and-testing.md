# 07 · 依赖关系、测试与扩展

[← 上一篇：运行方式](06-running.md) | [Wiki 索引](README.md)

---

## 1. 包依赖矩阵

仅列仓库内依赖（省略标准库与第三方包）。箭头语义：`A → B` 表示 A import B。

| 包 | 依赖的内部包 |
| --- | --- |
| `contract` | 无（仅标准库） |
| `datasource/tdx` | `contract` |
| `store` | `contract` |
| `ingest` | `contract`、`store` |
| `box` | `contract`、`store` |
| `screener` | `contract`、`store` |
| `pool` | `contract`、`store` |
| `signal` | `contract`、`store` |
| `ledger` | `contract`、`store` |
| `match` | `contract`、`ledger`、`store` |
| `planner` | `contract`、`ledger`、`regime`、`store` |
| `regime` | `box`、`contract`、`store` |
| `reporter` | `contract`、`ledger`、`store` |
| `scheduler` | `contract`、`store` |
| `gui` | `contract`、`scheduler`、`store` |
| `pipeline` | `box`、`contract`、`ingest`、`planner`、`pool`、`reporter`、`screener`、`signal`、`store` |
| `regimesweep` | `contract`、`regime` |
| `backtest` | `contract`、`match`、`pipeline`、`regime`、`store` |
| `cmd/darvas` | `backtest`、`box`、`contract`、`datasource/tdx`、`gui`、`ingest`、`ledger`、`pipeline`、`pool`、`scheduler`、`store` |

### 1.1 第三方依赖

| 包 | 用途 |
| --- | --- |
| `modernc.org/sqlite` | 纯 Go SQLite 驱动（无 CGO） |
| `golang.org/x/text` | GBK 解码（通达信 `.tnf`、炒股手 CSV） |

### 1.2 分层规则

```
L0 contract
L1 datasource/tdx, store
L2 ingest, box, screener, pool, signal, ledger, regime, match, planner
L3 pipeline, scheduler, reporter, gui, backtest, regimesweep
L4 cmd/darvas
```

| 规则 | 说明 |
| --- | --- |
| 禁止反向依赖 | 下层不得 import 上层 |
| `contract` 纯净 | 不得 import 任何业务包；仓储接口保持最小 |
| `store` 不知业务 | 只做 SQL 与 DTO 映射 |
| `cmd` 不写业务 | 只装配与打印 |
| `pipeline` 唯一总装 | 领域包串行编排只在这里 |

### 1.3 非生产 Go 程序

| 路径 | 用途 |
| --- | --- |
| `scripts/dbg_breadth` | 宽度法调试 |
| `scripts/dbg_regime{2,3,4}` | 体制实验调试 |
| `var/bt/idxbox` | 指数字箱体实验 |

这些是研究工具，不参与 `darvas.exe` 构建。

---

## 2. 测试

### 2.1 运行

```powershell
go test ./...
go test ./internal/box -run TestScan -v
go test ./internal/planner -count=1
```

> **测试环境路径**（原根目录 HANDOFF/PROMPT.md 记录，2026-10-29 沉淀于此）：
> bash 下需 `export GOROOT=/d/go-sdk/go; export PATH=/d/go-sdk/go/bin:$PATH`（`D:\Go` 是坏安装，勿用）。

### 2.2 分布（178 个测试函数）

| 包 | 测试文件 | 测试数 | 重点 |
| --- | --- | ---: | --- |
| `box` | 2 | 44 | 三日法则状态机、阶梯、突破/破位、边界 |
| `store` | 10 | 33 | 仓储、迁移、幂等、外键、日期扫描 |
| `planner` | 1 | 22 | 仓位、止损、观察期、闸门、卖单防重 |
| `ledger` | 2 | 13 | 成交解析、幂等、权益、审计、分级流转 |
| `regime` | 2 | 12 | 宽度法、A 判据、分体制汇总 |
| `match` | 1 | 9 | 停损触发、跳空、滑点、费用、除权 |
| `pool` | 1 | 9 | 入池/出池/生命周期 |
| `scheduler` | 1 | 9 | 触发、补跑、幂等、Stop 竞态 |
| `regimesweep` | 1 | 7 | 网格/邻域/资格判定 |
| `signal` | 1 | 6 | 首次事件、优先级 |
| `datasource/tdx` | 1 | 4 | 二进制解析、日期转换 |
| `screener` | 1 | 3 | 四条件与门槛 |
| `backtest` | 1 | 2 | Bootstrap/回放一致性 |
| `pipeline` | 1 | 2 | 备份轮转/恢复 |
| `reporter` | 1 | 2 | 日报/周报生成 |
| `gui` | 1 | 1 | 路由/JSON |
| **合计** | **19** | **178** | — |

### 2.3 测试风格

- 领域引擎优先纯函数测试（`box.Engine.Scan` 零 IO）。
- 仓储测试用临时 SQLite（`t.TempDir()`），不碰生产库。
- 回测验证用双模式差分（progressive vs prefill），而不是只断言终值。
- 关键 bug 修复通常配回归测试（如 `LatestNonFormingBox`、`pendingSell`）。

---

## 3. 一致性检查

脚本：[check_consistency.py](../../scripts/check_consistency.py)

```powershell
python scripts\check_consistency.py
# 退出码 0=一致，1=漂移
```

| 检查 | 内容 |
| --- | --- |
| **C1 枚举对齐** | `contract.go` 的 `BoxStatus` / `WatchStage` / `SignalType` 常量集合 vs 内存库执行全部迁移后的最终 `CHECK IN (...)` |
| **C2 表覆盖** | DDL `CREATE TABLE` 列表 ⊇ `docs/03-erd.md` Mermaid 实体（忽略 `schema_version`） |
| **C3 参数键** | 迁移中 `INSERT OR IGNORE INTO param` 键集合 ⊆ `docs/01` §6 文档键集合 |
| **C4 时刻表** | `scheduler.Schedule` 与 `WeeklyClock` vs `docs/01` §4.1/§4.3 表格（顺序/时刻/任务名全等） |

**注意**：

- C1 只核对三个显式枚举列；`position.status`、`order.status`、`daily_run.status` 等由 DDL CHECK 守卫。
- C3 只检查「代码有、文档无」；反向不检查。
- `docs/06` 提到的 `check_theory_refs.py` 当前仓库中**不存在**，理论引用靠人工评审。
- 新参数如果只在代码中读取、未种子化（如 `gate.ma60.mode`），不受 C3 约束，但不受机器保护。

### 3.1 三道防线

1. **静态对齐**：`check_consistency.py`。
2. **运行时校验**：`store.migrate()` 写 `schema_version`；`darvas doctor` 检查触发器、数据量、恒等式、过期订单。
3. **流程卡点**：契约变更必须同 commit 附迁移 + 文档 + 一致性通过。

---

## 4. Python 研究脚本

`scripts/` 下约 35 个脚本，均不参与生产运行，按用途分组：

| 组 | 文件（示例） | 用途 |
| --- | --- | --- |
| 一致性 | `check_consistency.py` | C1–C4 |
| 回放编排 | `b12_run_queue.py`、`backfill_window.py`、`run_*.bat` | 批量回测、补窗口 |
| 体制研究 | `regime_sizing_backtest.py`、`regime_c_*`、`regime_double_layer_backtest.py`、`regime_filter_analysis.py`、`regime_lifespan_stats.py` | A/C/D 判据、双层体制 |
| E17 实验 | `e17_*.py` | 52 周新高分档、分年归因、箱高报告 |
| M8 指标 | `m88_metrics.py`、`quant_report.py`、`evo_metrics.py`、`p05_yearly_report.py` | 收益/回撤/胜率 |
| 分级与黑名单 | `blacklist_winner_tradeoff.py`、`winner_trades.py`、`markov_ladder.py` | 战绩分级权衡 |
| 阶梯模拟 | `sim_ladder_empirical.py`、`let_profit_run_analysis.py` | 加码/持有期 |
| 数据维护 | `slim_bt_db.py`、`cleanup_non_a_boxes.py`、`audit_e2.py` | 回测库瘦身、清理非 A 股箱体、审计 |

约定：研究脚本只读源库/scratch，不写生产库；产物放 `var/bt/`。

---

## 5. 扩展指南

### 5.1 新增参数

1. 新建迁移 `00NN_xxx.sql`：

```sql
INSERT OR IGNORE INTO param(key, value) VALUES ('my.new_key', '1.0');
```

2. 代码读取时给默认值：

```go
v := st.ParamRepo.GetFloat("my.new_key", 1.0)
```

3. 同步 `docs/01` §6 参数表（否则 C3 失败）。
4. 跑一致性与测试。

### 5.2 新增表/列

1. 新建迁移文件（禁止改已发布文件）。
2. 同步 `docs/03-erd.md`（否则 C2 失败）。
3. 仓储层加 DTO/方法。
4. 若新表被回测比较，加入 `dumpDecisionState`。

### 5.3 修改契约

1. 改 `internal/contract/contract.go`。
2. 同 commit 更新 `docs/04-api-contract.md`。
3. 更新所有实现与调用方（编译器会提示）。
4. 若涉及枚举，确保 DDL CHECK 同步（C1）。

### 5.4 新增定时任务

1. `scheduler.Schedule` 增加 `TaskTime`。
2. `docs/01` §4.1 表格同步（C4 严格比较顺序）。
3. `pipeline.RerunTask` 增加分支。
4. 如需日报心跳，`reporter` 会自动读取 `daily_run`。

### 5.5 新增 GUI 视图

1. `gui.Server.Handler()` 注册 GET 路由。
2. `static/index.html` 加视图（原生 JS，无外部库）。
3. 保证 nil 切片输出 `[]`、只读、无写操作。

### 5.6 新增回测模式

1. `backtest.Mode` 增加常量。
2. `backtest.Run` 增加分支。
3. 若产生新 scratch 库，更新 `.gitignore` 与文档。

### 5.7 新增研究结论

放 `var/bt/` 或 `docs/experiments/`，不要混入生产参数；生产参数变更必须走迁移 + docs/01。

---

## 6. 历史坑与硬约束（改代码前必读）

| 坑 | 事实 | 防御 |
| --- | --- | --- |
| `LatestBox` 取到 `forming(0,0)` | 突破日引擎同时输出 broken_up 与 forming，`id DESC` 必取 forming | 一律用 `LatestNonFormingBox` |
| 日期扫描 | modernc 把 DATE 扫成 RFC3339 | SELECT 日期列 `CAST(x AS TEXT)` |
| 箱体重插破坏外键 | 删旧重插导致 watch/position FK 断裂 | `UpsertBoxes` 按 `(top,bottom)` 复用 ID |
| 止损被下调 | 应用层遗漏即出错 | DB 触发器 `RAISE(ABORT)` + 应用层预检 |
| 单写者 | WAL 下多写连接会锁冲突 | `MaxOpenConns(1)`；serve 读写分开 |
| 同批次重复卖单 | time_stop 与信号循环重复 | `pendingSell` 即时回写 |
| 追高成交 | 回测跳空按开盘价成交致严重追高 | `match.max_entry_gap` 双向闸门 |
| 调度 Stop 竞态 | 循环读被置 nil 的 `stopCh` | Start 捕获局部通道 |
| 未来数据泄漏 | prefill 模式查询无边界 | 所有查询以 `date` 为上限；progressive 做地面真值 |
| 非 A 股混入 | B 股/指数/北交所参与箱体 | `ActiveStockCodes` 只返回沪深主板/科创/创业 A 股，ST 按名称排除 |
| 研究脚本写生产库 | 参数调优污染主库 | 只写 scratch；`backtest --set` 仅覆盖 scratch |
| GUI 写库 | 只读原则被破坏 | 路由只注册 GET |
| schtasks 黑名单 | agent 沙箱 bash 里 schtasks 被拦 | 用 PowerShell `Get/Stop/Start-ScheduledTask` |
| VACUUM 备份耗时 | 生产库 VACUUM 需 10-15 分钟 | 低峰期执行，勿占交易时段 |
| CatchUp 回放污染心跳 | 缺心跳会整链补跑 | 回放/测试一律用 scratch 库，勿指生产库 |

---

## 7. 关键质量指标

| 指标 | 当前值 |
| --- | --- |
| 测试函数 | 178 |
| 测试覆盖包 | 16/18 业务包；`contract` 为纯类型（编译期覆盖），`ingest` 靠端到端导入验证 |
| 迁移版本 | 17 |
| 业务表 | 15（另加 `schema_version`，共 16 张表） |
| 内部包依赖环 | 0 |
| 一致性检查项 | 4（C1–C4） |
