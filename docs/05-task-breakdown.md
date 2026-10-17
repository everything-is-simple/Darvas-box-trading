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
| 4.4 | 止损上移 | 新箱体确认→UpdateStop（只升不降触发器验证） | 降止损被ABORT | ✅ planner.raiseStops接线+单测3断言 |
| 4.5 | 成交解析 | 券商成交文本粘贴解析（常见2-3种格式） | 样本文本通过 | ✅ 2026-10-17 三格式解析+ApplyFills订单流转 |
| 4.6 | 账本 | 权益快照+恒等式审计 | 手工账核对 | ✅ 2026-10-17 Snapshot重算+AuditEquation三重校验+doctor接线 |

> M4 已实现：`internal/store/repo_signal.go`+`repo_trade.go`（SignalRepo/TradeRepo）+
> `internal/signal/signal.go`（四类信号：stop优先>failed>breakout/pyramid）+
> `internal/planner/planner.go`（qty=floor(equity×risk%/(trigger-stop)/100)×100、单股上限、腿号、T+1执行日）。
> planner单测2例验证买/卖单数量公式。
> **M4收尾（2026-10-17）**：`internal/ledger/ledger.go`（ParseFill三格式/ApplyFills订单状态机/
> Snapshot/AuditEquation）+ migration 0002（trade_fill.side）+ CLI `fills` 子命令 +
> doctor 账本检查项 + planner.raiseStops 止损上移接线（M4.4）。
> 附带修复两个潜伏bug：①DATE列被modernc驱动转RFC3339（全repo CAST(date AS TEXT)统一修复，
> 此前 signal.withinDays 的 failed_breakout 判定实际失效）；
> ②UpsertBoxes 重算替换会撞 position/watch/signal 外键（改为按(top,bottom)匹配复用ID+引用保护删除）。

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

## M6 · GUI（internal/gui · 本地HTTP UI）✅（2026-10-17）

> **方案变更（ADR）**：原计划 Wails v2，因构建链需 Node 按预案降级为本地HTTP UI
> （docs/02 §2）：`net/http` + embed 静态单文件前端 + 4 只读 JSON 端点，零外部依赖。
> 验收实录：`darvas ui --db var/perf_test.db` 起服务，浏览器 DOM 断言四视图全 PASS
> （仪表盘 7 心跳全绿 / 跟踪池 142 行含中文名 / sh600519 K线+13 箱体矩形渲染 / 交易记录权益曲线+计划）。

| # | 任务 | 内容 | 状态 |
|---|------|------|------|
| 6.1 | ~~Wails骨架~~ → HTTP UI骨架 | net/http + embed static + `darvas ui` 子命令（--addr :8787 + 自动开浏览器） | ✅ |
| 6.2 | 仪表盘视图 | 7任务心跳灯（ok/failed/running/未运行 + 耗时/行数/error明细）+ 权益卡 + 当日计划订单 | ✅ |
| 6.3 | 跟踪池视图 | WatchOverview 表格（代码/中文名/阶段chips/阶梯数/当前箱体/最新收盘）+ 搜索过滤 + 点击跳个股 | ✅ |
| 6.4 | 持仓视图 | 交易记录视图内：持仓子表（open 算 LastClose 浮盈亏 pnl/pct）+ closed 全历史 | ✅ |
| 6.5 | 计划视图 | 交易记录视图内：PlansRecent(10) 计划头+订单+状态badge；仪表盘含当日计划 | ✅ |
| 6.6 | 个股详情 | canvas 自绘 K线（涨红跌绿）+ 箱体矩形叠加（confirmed蓝/broken红绿/forming黄虚线）+ 止损线 + 十字光标tooltip + 成交量 + 箱体阶梯表（不引外部JS库） | ✅ |
| 6.7 | 权益曲线 | 交易记录视图内：EquityAll sparkline（面积图+峰值虚线+最新值标注） | ✅ |
| 6.8 | 单文件打包 | 静态资产 embed 进 exe（`go build` 即含前端，无Node） | ✅ |

**M6 附带交付**：
- tnx 名称解析 `tdx/names.go`（T0002/hq_cache/*.tnf 二进制表：360B/条，code@+0，name@+31 GBK）
  + `SecRepo.UpdateNames/Names` + ingest 尾部自动同步（真实库回填 9575 名称）
- store 扩展：`PlansRecent/WatchOverview/LastClose/RunsOn/LatestRunDate`（均带单测）
- **修复潜伏bug**：① WatchByCode NULL scan（exit_reason/note 缺 COALESCE，真实库必触发）；
  ② PlansRecent 无订单计划 Orders=nil 序列化 JSON null（前端 .map 崩）→ 初始化空切片；
  ③ 前端 tbl() 四处调用点误传已 join 的字符串（rows.join 崩）→ 统一传数组；
  ④ 静态服务加 Cache-Control: no-cache（embed 无 mtime，防浏览器缓存旧页面）
- 写操作（fills/param/run/pool 人工操作）仍走 CLI，GUI 只读（docs/04 §6）

---

## M7 · 周而复始（稳定性+运维）

| # | 任务 | 内容 | 验收 | 状态 |
|---|------|------|------|------|
| 7.1 | 无人值守运行 | 常驻模式连续20交易日 | 无漏跑 | ✅ serve已交付（调度+CatchUp+日历+GUI）；20交易日观察期进行中 |
| 7.2 | 周报 | weekly任务：阶梯全景/周盈亏/纪律审计 | - | ⏳ |
| 7.3 | 备份轮转 | 30份滚动+恢复演练 | 恢复成功 | ✅ 2026-10-17 VACUUM备份+轮转+恢复演练单测 |
| 7.4 | 移植演练 | 拷贝至另一台Win机器 | 改tdx.root即可跑 | ⏳ |

> **M7.1/M7.3 已实现（2026-10-17）**：`internal/scheduler`（常驻调度器）+
> `internal/pipeline/backup.go`（备份轮转）+ CLI `serve` 子命令。要点：
> - 调度语义：每任务一个触发点，到达时把「时刻已过且未ok」的任务按依赖链一次补齐；
>   ingest 失败中止本链、下个触发点重试；每触发点每日只消费一次（不空转重试）。
> - CatchUp：前一交易日（sh000001日历）完全没跑→整日补跑；今天已过点未ok→链补。
>   更早历史缺口用 `darvas run --date X` 手动补。
> - 交易日历（ADR-2）：DB `HasBar(sh000001)` + TDX sh000001.day 直读（未入库时判定当日）。
> - 备份：`RunDaily` 尾部 + scheduler OnDayDone 钩子双接线（`var/backup/darvas_YYYYMMDD.db`，
>   VACUUM INTO + 保留30份滚动删除，同日重跑覆盖）；恢复演练=单测里 store.Open 备份读数。
> - serve：读写分离双连接（WAL：调度写+GUI读，ADR-5），默认不弹浏览器。
> - 时钟/Runner可注入，scheduler 8 用例（触发链序/失败中止重试/非交易日跳过/TDX日历/
>   CatchUp前日/今日链补/已ok跳过/启停）+ backup 2 用例全绿。

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
