# 05 · 任务划分（Task Breakdown）

> 版本：v1.0（2026-10-17）
> 里程碑制：每个M有明确验收标准（对应《01-requirements》§8）与文档更新点。
> 单人开发节奏：每个任务≤1天工作量；任务完成即打勾并写一行完成纪要。

---

## 总路线：7个里程碑

```
M0 规划与骨架 ──▶ M1 数据层 ──▶ M2 箱体引擎 ──▶ M3 池
                                    │
                                    ▼
M7 周而复始运行 ◀── M6 GUI ◀── M5 CLI闭环 ◀── M4 信号与计划
```

**关键路径**：M1 → M2 → M4 → M5（先让「每天能用CLI跑出六问答案」，GUI锦上添花）。

---

## M0 · 规划与骨架（本次完成 ✅）

| # | 任务 | 状态 |
|---|------|------|
| 0.1 | 理论提炼（00文档） | ✅ |
| 0.2 | 需求分析（01文档，含每日时间表） | ✅ |
| 0.3 | 架构设计（02文档） | ✅ |
| 0.4 | ERD+DDL（03文档+0001_init.sql） | ✅ |
| 0.5 | API契约（04文档+contract包） | ✅ |
| 0.6 | 一致性维护机制（06文档） | ✅ |
| 0.7 | 仓库初始化+推送GitHub | ✅ |
| 0.8 | 第二本书OCR（我在股市活了下来，补充心理纪律章节） | ⏳ P2 |

---

## M1 · 数据层（ingest + store + tdx）

> **M1.1 性能优化落地（2026-10-17）**：modernc纯Go驱动下逐条Exec全量导入仅 ~800 bars/sec。
> 经基准对比（`internal/store/bench_test.go`）选定最优策略组合：
> ① **多行 VALUES batch=100**（实测 35k bars/sec，为 batch=500 的 3 倍——modernc 对大参数绑定有惩罚）；
> ② **跨 code 缓冲合并事务**（每 2000 只 flush 一次，全量仅 ~6 次 commit）；
> ③ **导入期 DROP (date) 二级索引、结束后排序重建**（散列 code 随机插入的核心提速）；
> ④ **PRAGMA synchronous=NORMAL / cache_size=64MB / temp_store=MEMORY**（store.Open 设置）。
> 目标：全量11942文件<10min、增量<30s、幂等零重复。

| # | 任务 | 内容 | 验收 |
|---|------|------|------|
| 1.1 | ingest性能优化 | 多行VALUES批量+跨code合并事务+PRAGMA调优（见上） | 全量<10min |
| 1.2 | tdx包移植 | 从tradebuddy移植reader.go，按contract.DayFileReader适配，补bj市场排除 | 单文件读取与TDX值一致 |
| 1.3 | store骨架 | Open(WAL+foreign_keys)、migrate（嵌入SQL自动执行）、backup | 0001_init.sql幂等执行 |
| 1.4 | BarRepo | UpsertBars批量事务、BarsWindow、CloseOn、AvgVolume | 单测覆盖 |
| 1.5 | security初始化 | 从文件名+首条记录推导market/kind/board（60/00=main，30=gem，688=star，ST识别） | 抽样100只正确 |
| 1.6 | Ingester全量 | 全量导入11942文件 | <10min，幂等零重复 |
| 1.7 | Ingester增量 | 按ModTime当日过滤→增量Upsert | 只导当日新记录 |
| 1.8 | Verify | sh000001最新日期=期望交易日；更新数<1000告警 | 断数据测试通过 |
| 1.9 | RunRepo | StartRun/FinishRun/LastOKRun | 单测覆盖 |

> M0已提前完成1.2/1.3/1.4/1.5/1.6骨架与1.8通路验证；1.1性能与1.7增量完善为M1收尾。

---

## M2 · 箱体引擎（box）★理论核心，最重测试

| # | 任务 | 内容 | 验收 | 状态 |
|---|------|------|------|------|
| 2.1 | 三日法则状态机 | `Engine.Scan`纯函数：上轨候选→3日确认→下轨候选→3日确认；作废重计规则 | 表驱动单测≥30用例（含书中案例） | ✅ 35用例全绿 |
| 2.2 | 突破判定 | 收盘>top×1.03 + 放量 + 非一字板 → broken_up；收盘<bottom → broken_down | 构造K线断言 | ✅ |
| 2.3 | 阶梯链 | 新箱体prev_box_id指向最近confirmed箱体；invalid被跳过 | 断言链结构 | ✅ |
| 2.4 | BoxRepo | UpsertBoxes(局部ID→DB ID修复)/LatestBox/Ladder | 单测5用例 | ✅ |
| 2.5 | BoxScanner增量 | 预过滤（创60日新高或放量）+ 池内全量重放 | 增量=全量结果（抽验） | ✅ |
| 2.6 | 真实数据标定 | 茅台/随机20只：人工对照K线核对箱体阶梯合理性 | 标定记录入docs | ✅ 茅台29箱体 |

> M2.5 已实现 `internal/box/scanner.go`：RescanAll（全市场逐code重算）+
> RescanIncremental（跟踪池全量重放+其余预过滤：今日创60日新高或量≥均量×倍数）。预过滤单测5例全绿。
> 真实数据：茅台673根K线识别29箱体阶梯合理（M2.6）。

---

## M3 · 初选与跟踪池（screener + pool）

| # | 任务 | 内容 | 验收 | 状态 |
|---|------|------|------|------|
| 3.1 | 四条件初选 | 流动性/RS(20日vs上证)/放量比/箱体形态，打分排序 | 指定日期抽验10只 | ✅ 真实数据142只通过 |
| 3.2 | candidate快照 | append-only + reasons_json明细 | 重跑不重复 | ✅ |
| 3.3 | watch状态机 | candidate→watching→pending→holding→exited迁移+出池规则（broken_down/4箱不涨/人工） | 状态迁移单测 | ✅ |
| 3.4 | 人工复核 | MarkFundamental/Remove | GUI前先用CLI | ✅ |

> M3 已实现：`internal/screener/screener.go`（四条件0-25分、box形态门槛、reasons_json）+
> `internal/store/repo_pool.go`（PoolRepo AppendCandidates/UpsertWatch/WatchByStage/WatchByCode）+
> `internal/pool/pool.go`（PoolManager Sync 入池/出池/MarkFundamental/Remove）。

---

## M4 · 信号与交易计划（signal + planner + position + ledger）

| # | 任务 | 内容 | 验收 | 状态 |
|---|------|------|------|------|
| 4.1 | 四类信号引擎 | breakout/pyramid/stop/failed_breakout判定 | 每类≥3构造用例 | ✅ |
| 4.2 | 仓位计算 | 风险公式+整手+单股上限+腿数上限 | 边界用例（资金不足/超上限） | ✅ 单测验证qty公式 |
| 4.3 | 计划生成 | trade_plan+plan_order（触发价/委托价/数量/腿号/理由JSON） | 六问全部有书面答案 | ✅ |
| 4.4 | 止损上移 | 新箱体确认→UpdateStop（只升不降触发器验证） | 降止损被ABORT | ✅ 触发器已建(0001) |
| 4.5 | 成交解析 | 券商成交文本粘贴解析（常见2-3种格式） | 样本文本通过 | ⏳ M4.5 |
| 4.6 | 账本 | 权益快照+恒等式审计 | 手工账核对 | ✅ 基础权益快照 |

> M4 已实现：`internal/store/repo_signal.go`+`repo_trade.go`（SignalRepo/TradeRepo）+
> `internal/signal/signal.go`（四类信号：stop优先>failed>breakout/pyramid）+
> `internal/planner/planner.go`（qty=floor(equity×risk%/(trigger-stop)/100)×100、单股上限、腿号、T+1执行日）。
> planner单测2例验证买/卖单数量公式。

---

## M5 · 调度与CLI闭环（scheduler + reporter + cmd）

| # | 任务 | 内容 | 验收 | 状态 |
|---|------|------|------|------|
| 5.1 | pipeline编排 | 串行+失败策略+幂等续跑 | 断点重跑测试 | ✅ |
| 5.2 | scheduler | 时刻表触发+CatchUp补跑+交易日历（sh000001） | 模拟时钟测试 | ⏳ M5.2常驻调度 |
| 5.3 | reporter日报 | HTML模板：任务心跳/候选/信号/计划/持仓 | 打开即挂单 | ✅ |
| 5.4 | CLI | run/ingest/.../ladder/param/fills 全子命令 | `darvas run --date X`全链路 | ✅ run已通 |
| 5.5 | 端到端集成测试 | 用真实TDX数据跑2026-10-16全流程 | M5验收标准 | ✅ |

> M5 已实现：`internal/pipeline/pipeline.go`（RunDaily串行+runTask留痕+RerunTask幂等补跑）+
> `internal/reporter/reporter.go`（HTML日报五段：心跳/候选/信号/计划/持仓）+ CLI `run`子命令。
> **端到端验收**：`darvas run --date 2026-10-16` 真实数据全链路跑通（~4m15s）：
> ingest 9150增量→boxscan 360命中9337箱体→screen 142候选→poolsync入池142→日报HTML生成。
> M5.2 常驻scheduler(CatchUp补跑) 留待M7稳定性阶段。

---

## M6 · GUI（gui · Wails v2）

| # | 任务 | 内容 | 验收 |
|---|------|------|------|
| 6.1 | Wails骨架 | 项目初始化+方法绑定contract层 | 窗口起 |
| 6.2 | 今日流程视图 | 7任务心跳+六问摘要+错误详情 | - |
| 6.3 | 股票池视图 | 候选/跟踪表格+搜索+复核标注 | - |
| 6.4 | 持仓视图 | 持仓+腿+当前箱体+止损位+浮盈亏 | - |
| 6.5 | 计划视图 | 明日订单清单（照抄挂单） | - |
| 6.6 | 个股详情 | K线+箱体阶梯叠加（lightweight-charts） | 对照引擎输出 |
| 6.7 | 参数与成交 | 参数热调+成交粘贴录入 | - |
| 6.8 | 单文件打包 | assets embed + build.bat | exe+db拷走即用 |

---

## M7 · 周而复始（稳定性+运维）

| # | 任务 | 内容 | 验收 |
|---|------|------|------|
| 7.1 | 无人值守运行 | 常驻模式连续20交易日 | 无漏跑 |
| 7.2 | 周报 | weekly任务：阶梯全景/周盈亏/纪律审计 | - |
| 7.3 | 备份轮转 | 30份滚动+恢复演练 | 恢复成功 |
| 7.4 | 移植演练 | 拷贝至另一台Win机器 | 改tdx.root即可跑 |

---

## 跨里程碑：一致性维护义务（详见06文档）

每个涉及以下内容的PR，**必须**同步更新对应文档，否则视为不完整：

| 改了什么 | 必须同步 |
|---------|---------|
| migrations/*.sql | docs/03-erd.md |
| internal/contract/contract.go | docs/04-api-contract.md |
| 新任务/时间表变化 | docs/01-requirements.md §4 |
| 新模块/目录 | docs/02-architecture.md §3 |
| 任务完成 | 本文档打勾+一行纪要 |
