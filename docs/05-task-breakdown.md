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
> `internal/planner/planner.go`（qty=floor(equity×risk%/(trigger-stop)/100)×100、单股上限、腿号、
> 执行日=下一交易日：sh000001日线取NextDate(ADR-2)，实盘无未来K线时跳周末兜底。
> 2026-10-17修复：此前日历+1近似导致周五信号→周六死单，实证sh600052 2026-10-09→exec 10-10 expired）。
> planner单测验证买/卖单数量公式、止损上移、执行日=下一交易日（含跨国庆长假与实盘兜底）。
> **M4收尾（2026-10-17）**：`internal/ledger/ledger.go`（ParseFill三格式/ApplyFills订单状态机/
> Snapshot/AuditEquation）+ migration 0002（trade_fill.side）+ CLI `fills` 子命令 +
> doctor 账本检查项 + planner.raiseStops 止损上移接线（M4.4）。
> 附带修复两个潜伏bug：①DATE列被modernc驱动转RFC3339（全repo CAST(date AS TEXT)统一修复，
> 此前 signal.withinDays 的 failed_breakout 判定实际失效）；
> ②UpsertBoxes 重算替换会撞 position/watch/signal 外键（改为按(top,bottom)匹配复用ID+引用保护删除）。
> **B1 修正（2026-10-18，原书校对）**：首腿（breakout）初始止损由"突破箱体下轨下方"改为
> **被突破箱体上轨（突破点）下方**（stop = box.top × sell.discount，紧止损；riskPerUnit 变小→
> qty 变大，受单股上限约束）。腿2/3（pyramid）止损=新箱体下轨下方不变；止损上移规则不变。
> 存量订单/持仓不追溯。planner_test 断言 stop=23.28/qty=6900 + 新增腿2对照用例。
> **B2 跟踪池淘汰生命周期（2026-10-18，原书校对补全，跨M3/M4）**：migration 0003（watch 表
> 重建 stage 扩六值+resume_trigger 列；position.realized_pnl；4 新参数种子）。规则落地：
> ①太晚规则（screener 条件4：broken_up 后≥screen.latedays 交易日不入候选）②两亏拉黑
> （poolsync 巡检：亏损交易≥pool.maxloss→blacklisted 终态+入池拦截）③条件挂起
> （CLI `pool suspend --resume`：收盘≥激活价自动恢复）④5-8上限（ActiveWatchCount≥
> pool.maxwatch 不入池，suspended 不占名额）⑤3周不获利（planner：开仓≥position.staledays
> 同时补全 watch 状态机接线（此前 pending/holding
> 从未写入）：planner 买单→pending、ledger 成交→holding、清仓→exited、买单过期→
> RelaxPendingWatches 回退 watching。测试：pool_test 五用例+screener 太晚规则用例；
> check_consistency C1 升级运行时验证（python sqlite3 执行迁移后读 sqlite_master）。
> **B3 已交易股票历史库（2026-10-18，原书校对补全：达瓦斯自称最重要的内容）**：
> migration 0004（trade_review 复盘表：position_id UNIQUE 每笔一条、code 冗余索引、
> rule_followed/error_type/lesson 三字段人工录入）。三记录制度闭环：①买入理由
> （reason_json，M4 已有）②卖出理由（close_reason+realized_pnl，0003 已有）③亏损
> 原因/教训（本表，`darvas review add <position_id> --lesson`）。战绩视图：
> `darvas review list [code]`（汇总=每股笔数/胜负/累计盈亏/最新教训；单股=每笔明细，
> 未写复盘给出录入命令提示）。再纳入闭环：poolsync 入池查 TrackRecordByCode，
> 曾交易未拉黑者 → watch.note 留痕"先读教训再纳入"。教训不自动生成（不引入
> 自动模型，人工写）。测试：repo_review_test 三用例（UPSERT/汇总明细/战绩）+
> pool_test 再纳入提示用例（含新股对照）。
> **C 炒股手CSV导入器（2026-10-18，第二数据源：模拟执行回流）**：`internal/ledger/csv.go`
> ParseChaoGushouCsv（GBK解码x/text、13列表头严格校验、日期YYYYMMDD→ISO、
> `"300096"_`引号外下划线代码、数量800.00→整800、盈亏列不信任系统自算、
> 交易理由留原文审计）。复用 ApplyFills 订单匹配+持仓状态机+权益快照。
> FillsAuditReport 执行质量审核四维度：漏执行（范围内pending/placed）/
> 滑点（成交vs限价≥0.01）/数量偏差/计划外成交（含"手痒"警示）；幂等重导
> 全跳过不误报纪律满分。CLI：`darvas fills <file.csv> --yes` 按后缀分流。
> 格式事实源：EXPORTT 四个年代样本（2009/2015/2019/2024）验证一致。
> 端到端实录：2015样本10笔（5买5卖闭环）→ 入库+审核报告+review list 战绩
> 联动（3胜2负）+ 重导幂等全跳过。
> **D 工程还债（2026-10-18，文档对齐实际代码：观察期门禁卫生）**：docs/04 §3.3
> 全面对齐 store 方法面——删幻影方法（LatestDates/ActiveBoxes）；BarRepo 13 方法/
> PoolRepo 9 / SignalRepo 4 / TradeRepo 27（+日期范围4方法+B3五方法）/ RunRepo
> 补 DatesOverview / ParamRepo 补 All；SecRepo 定性为 store 公共 API 不进接口面。
> contract.go L1 区加澄清注释：Repo 接口=构造期最小示意，完整方法面以 docs/04
> §3.3 为事实源（扩展方法返回 store 包类型，补进接口会 import 循环）。docs/03：
> 版本行 v1→v4、事实源行改指 migrations 目录、索引表补 6 缺口（candidate/
> watch(code)/signal(code,date)/position(status)/trade_fill 两索引）。README：
> weekly"待交付"与 M7.2 ⏳ 状态修正（均已实现，M7.4 移植演练仍待）。

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

### M6.5 · UI 四视图改进（U1-U6，2026-10-17）✅

> 背景：此前仅 2026-10-16 一天有按日快照数据，无「指定日」维度。本轮按用户要求全面改造。

| # | 任务 | 内容 | 状态 |
|---|------|------|------|
| U1 | 全站日期维度 | `GET /api/dates`（daily_run/candidate/trade_plan/equity_snapshot 四表 UNION 去重降序 + 每日附注心跳数/候选数/有计划）；topbar 全局日期下拉，仪表盘/候选/信号随选日联动；空心跳日提示「`darvas run --date X` 幂等回填」 | ✅ |
| U2 | 时间范围查询 | `/api/trades?from=&to=`（fills/plans/equity 范围过滤）+ 权益曲线近30/90/全部 chips；`/api/stock/{code}?from=&to=` 区间K线 | ✅ |
| U3 | 历史快照 | `GET /api/candidates?date=`（缺省最近候选日）、`GET /api/signals?date=`（缺省最近信号日）；跟踪池视图拆三子页：跟踪池（含 exited + 出池原因）/ 候选快照（分数+四条件+基本面）/ 信号日志 | ✅ |
| U4 | 空状态引导 | 无成交/持仓/计划/权益时显示可操作引导文案（如「darvas fills [file] --yes」），不摆空表 | ✅ |
| U5 | 交互细节 | 日期选择联动清缓存重载；箱体阶梯表标注「最近一次 boxscan 结果，最新确认日」；搜索代码精确匹配优先排序；个股视图 from~to 日期输入 | ✅ |
| U6 | 验收 | `darvas run --date` 回填 2026-10-13/14/15 + 浏览器断言（日期列表/四视图切换/范围过滤） | ✅ |

**M6.5 附带交付**：
- store 扩展：`DatesOverview/FillsRange/EquityRange/PlansRange/BarsRange/LatestCandidateDate/LatestSignalDate`；
  `WatchOverview` 增 exit_reason 列（均带单测）
- GUI 扩展：`/api/dates`、`/api/candidates`、`/api/signals` 三端点 + stock/trades 范围参数（docs/04 §6 已同步）
- **修复潜伏bug⑧**：scheduler Start 循环每轮重读 `s.stopCh` 字段，Stop 先置 nil 再 close →
  循环恰在 tick 中错过 close 后永远阻塞在 nil 通道（serve 模式 Ctrl+C 挂死 / TestStartStopSmoke
  偶发 10min 超时）→ 循环改用 Start 捕获的局部通道，`-race -count=5` 通过
- CLI 补齐 docs/04 §7 已规范的单任务补跑子命令：`darvas boxscan|screen|poolsync|signal|plan|report [--date]`
  （共用 pipeline.RerunTask 引擎；回填历史日后可用其重跑 boxscan+poolsync 恢复当前态）
- 回填语义备忘：历史日回填按升序串行；boxscan 为确定性重放（UpsertBoxes 按 top,bottom 复用），
  回填后需对最新交易日 `rerun boxscan + poolsync` 收敛当前态；signal/plan 为 append/追加型，
  对已跑过的日期**勿重复执行**（会产生重复信号/订单）

---

## M7 · 周而复始（稳定性+运维）

| # | 任务 | 内容 | 验收 | 状态 |
|---|------|------|------|------|
| 7.1 | 无人值守运行 | 常驻模式连续20交易日 | 无漏跑 | ✅ serve已交付（调度+CatchUp+日历+GUI）；20交易日观察期进行中 |
| 7.2 | 周报 | weekly任务：阶梯全景/周盈亏/纪律审计 | - | ✅ 2026-10-17 weekly四段周报+周六10:00调度+CLI |
| 7.3 | 备份轮转 | 30份滚动+恢复演练 | 恢复成功 | ✅ 2026-10-17 VACUUM备份+轮转+恢复演练单测 |
| 7.4 | 移植演练 | 拷贝至另一台Win机器 | 改tdx.root即可跑 | ✅ 2026-10-17 异地目录+备份库演练通过 |

> **M7.4 移植演练实录（2026-10-17）**：以「新机器=异地全新目录」模拟——
> 拷贝单文件 `darvas.exe`（20MB，静态编译+embed资产，无Go/Node依赖）+
> 最新 VACUUM 备份 `darvas_20261015.db`（694MB）至 `C:\Users\Administrator\darvas-migrate\`
> （顺带验证 M7.3 备份可异地恢复）：
> ① `doctor` 默认路径（exe同目录var）通过：schema v2、11942证券、日历至10-16、
>    止损触发器在位、恒等式审计通过；
> ② 故意设错 `tdx.root` → 系统优雅降级（ingest 0 files 仅告警不崩溃）；
> ③ `param set tdx.root H:/new_tdx64/vipdoc` 一改即恢复：ingest fresh=true
>    （9150 files）、ladder 正常输出茅台29箱体；
> ④ `ui` 异地起服务冒烟：/api/dashboard 7心跳 + /api/dates 4日期正常返回。
> 结论：**新机器只需 ① 拷贝exe+var/备份库 ② 改 tdx.root 一个参数** 即可跑。

> **M7.2 已实现（2026-10-17）**：`internal/reporter/weekly.go`（周报四段：
> ① 持仓箱体阶梯全景 ② 周盈亏=权益首尾增减+当周成交+当周平仓 ③ 权益曲线
> （快照表+SVG sparkline） ④ 纪律审计=未止损（收盘<止损仍持有）/止损·假突破
> 信号无卖出成交/过期挂单/账本恒等式（复用ledger.AuditEquation））。
> - 覆盖区间：锚定日往前7天（from=date-6），输出 `var/reports/weekly_YYYYMMDD.html`。
> - 调度：scheduler.fireWeekly 周六本地≥10:00 触发一次（独立于交易日历；
>   CatchUp 启动时补查当日）；错过不自动回放，`darvas weekly --date X` 手动补。
> - CLI：`darvas weekly [--date]`，缺省锚定最近周六，产出后自动打开浏览器。
> - store 扩展：`SignalRepo.SignalsRange(from,to)`（带单测）；
>   scheduler 测试更新周六时点预期 + TestWeeklySaturdayOnce（触发一次/整点/周五不触发）。

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

## M8 · 内置回放器（独立 scratch 库，规则零重写）

> 立项：原 PROMPT.md P4/M8（2026-10-18；该文件 2026-10-29 已删，git 历史可溯）；范围修订：docs/01 §9（外部回测框架全部否决，
> 改为 Go 内置 `darvas backtest` 在 scratch 测试库重放生产流水线本身）。
> 纪律：观察期内规则冻结——回放只产出「问题清单」（已归档至 archive/07-m8-issue-list.md），
> 改规则走周五复盘+用户拍板；scratch 库独立不污染实时库心跳；源库只读（铁律 7）。

| # | 任务 | 内容 | 验收 | 状态 |
|---|------|------|------|------|
| 8.1 | 回放骨架+as-of审计 | scratch引导+交易日历循环+差分审计 | 六任务as-of纪律成立 | ✅ 2026-10-18 |
| 8.2 | 模拟撮合器 | D+1 OHLC成交+T+1/涨跌停/整手/印花税，接ledger | - | ⏳ |
| 8.3 | 体制运行器 | 事先标注牛/熊/猴窗口，分体制报告 | - | ⏳ |
| 8.4 | 参数扫描 | box.volmult/buy.premium/risk.scale敏感性矩阵 | - | ⏳（可选） |

> **M8.1 已实现（2026-10-18）**：`internal/backtest/backtest.go` + CLI `darvas backtest`。
> - **引导**：整库拷贝源库 → 清空全部事务态表（含 box，10-17 演练遗漏教训 archive/07 #4）→
>   progressive 模式额外裁掉窗口后的非指数K线；sh000001 全历史保留（日历权威，ADR-2）。
> - **回放循环**：按 sh000001 日历逐日重放六任务链（boxscan→screen→poolsync→signal→
>   plan→report，走生产路径 `pipeline.RerunTask`，规则零重写）；ingest 由引导替代。
> - **差分审计**：prefill（未来在场）vs progressive（未来缺席=地面真值）双模式
>   重放同窗口 → 逐日箱体哈希 + 决策表语义 dump 比较；全等 = as-of 纪律成立的运行时证明。
> - **护栏**：拒用 darvas.db/perf_test.db 作 scratch；源库 mode=ro 打开（fallback immutable=1）；
>   日报重定向到 `<scratch>_reports/`（绝不写生产 var/reports，archive/07 #5）。
> - **测试**：`TestSyntheticAsOfDifferential` 合成全链条差分+机制断言；
>   `TestBootstrapGuards` 护栏单测；go test ./... 全绿；C1-C4 一致性全绿。
> - **审计结论**：六任务链未读到任何未来K线（#9 日历读为 ADR-2 豁免）；as-of 纪律成立。
>   审计同时发现事件驱动链路三条死路（archive/07 #1-#3，规则冻结期不改，待用户拍板）。

---

## M9/M10 · 迭代实验纲领（2026-10-25 立项，同日升级为十维）

> box 十维迭代实验的里程碑划分（理论框架见 docs/00 §3.6，施工图见 docs/29）。M8 验证了回测引擎与 as-of 纪律，M9/M10 在其之上叠加**围绕 box 十维的梯度分布研究**——不是新引擎，是新研究层。
> 编号口径：维度编号 1-10 与 docs/00 §3.6 十维总表、docs/29 §2.1 完全一致；实验 ID（P0-1/P0-3/P1-4/P2-5/P2-6/P3-7/P3-8）与里程碑（M9.0 + M10.0-M10.3）与 docs/29 §四/§七 完全一致。

### M9.0 · 第 5 维（box 位置）全历史段验证（✅ 已完成）

| 项 | 内容 |
|----|------|
| 维度 | box 位置（距 52 周新高）—— E17 阶梯四档 t1/t2/t3/t4（十维 #5） |
| 年份 | 2005-2018（14 年基本历史段） |
| 状态 | ✅ 14 年全部完成，总净利 **+842,801** |
| 执行器 | `scripts/e17_run_years.py` + `scripts/e17_run_2008.py`（串行） |
| 产物 | `var/bt/bt_e17_<year>.db` + `e17_metrics.csv` + `e17_annual_tier_report.html` |
| 结论 | t1（0-5%）为唯一稳定 edge（跨 13 年 +745,593，PF 2.31）；t2 负贡献；t3 盈亏平衡；t4（20-40%）13 年合计 -2,633，需显著反弹/反转才有 edge |

### M9.1 · 样本外校验（**暂停使用**，用户 2026-10-26 拍板：2019+ 数据暂不使用）

| 项 | 内容 |
|----|------|
| 维度 | 仍为第 5 维（box 位置），换样本外年份 |
| 年份 | 2019 起切两段（用户待定具体切点） |
| 状态 | 未启动 |
| 约束 | 红线（docs/29 §八.6）：样本外在规则定稿前不参与任何参数选择，只做最终校验 |

### M10.0 · P0（第 2/10 维，A 类零成本）—— P0-1 ✅ / P0-3 ✅ / B-1·B-2 ✅（全 14 年）

| 实验 | 维度 | 类型 | 内容 | 产出 | 状态 |
|------|------|------|------|------|------|
| P0-1 | 第 2 维 box 高度 | A 类后验 | 双口径（主 `/top` 与闸门同尺、对照 `/bottom`）+ 三级加密（5%→2%→1%），每带统计四项口径 + PF + 缺口 + 剔除前三赢家 | `docs/experiments/e17_box_height_report.{html,md}` + `_data.json` + 框架 `e17_box_height_framework.md` | ✅ 2026-10-26 完成，HARD 校准 10/10 |
| P0-3 | 第 10 维 头寸 regime | A 类归因 | 三口径（MA20 / MA60 / A 判据）× 两时点（建仓日 / 平仓日）；每桶四项口径 + PF + 缺口 + 剔除前三，**n 与 m（regime 段数）并列**；距 52 周高点回撤为操作口径 + 账户回撤描述性 | `docs/experiments/e17_regime_attribution_report.{html,md}` + `_data.json` + 框架 `e17_regime_attribution_framework.md` | ✅ 2026-10-26 完成，HARD 校准 22/22 |HARD 校准 22/22 |
| **B-1 / B-2** | 第 10 维 MA60 闸门（P0-3 下游） | B 类验证 | 代表年 4 年 × 3 变体（Baseline/half/stop）+ off 等价验证年 + 全 14 年补跑 10 年；三变体唯一差别 `gate.ma60.mode` | `docs/experiments/e17_regime_gate_report.md` §七 + `bt_b12_*` 库 + `b12_metrics_*.csv` | ✅ 2026-10-27 全 14 年收官：Σ已实现 +842,801 → +974,937（+132,136），亏损年减亏 66.2%，三判据全过 → 宪法第 0 条第四次修正已落笔 |

> P0-1 口径变更登记（详见 docs/29 §五 P0-1）：原设计单用 `(top−bottom)/bottom` 且以「0-5% 窄箱中位为正」为证伪判据；实测 0-5% 带在两口径下恒为 0 笔（样本全过 5% 闸门），该判据永不触发，已改为 H1/H2 两条可触发判据，并把「5% 阈值是否过严」明确划归 B 类。

> P0-3 口径变更登记（详见 docs/29 §五 P0-3、框架 §10）：
> ① 原设计只列 MA20 / MA60 / 权益回撤三桶 → 增列 **A 判据**（生产仓位规则与 M9.0 报告同口径），三口径并列；
> ② 原写「按交易当日」未定义是哪天 → 明确**建仓日为主 + 平仓日为对照**，`reason_json.regime` 因 plan/execute 1 日错位仅作审计轨；
> ③ 原「权益回撤」未分账户/指数 → **拆成双口径**：距 52 周高点回撤（外生，操作口径）+ 账户权益回撤（内生，描述性）；「2005 基准峰累计回撤」实测退化（单档独占 77.8%）降为审计对照；账户回撤 `>10%` 档 3,402 个交易日 **0 天**，登记「不可检验」；
> ④ 原方法漏「利润集中度」→ 补齐四项固定口径 + 剔除前三，并新增 **有效样本 m（regime 段数）** 与 `m≥20` 硬门槛（MA60 仅 147 段）；
> ⑤ 原产出 `var/bt/*.html` 且含「建议闸门阈值」→ 迁至 `docs/experiments/` 三件套 + 框架，**删除阈值**（`docs/00` §5.6.7 已否决清单硬否决熊市闸门）；
> ⑥ 原判据「某桶为负则闸门假设成立，进入 P2-6」→ 改为 H1–H6 + 独立 B 类清单（P2-6 实为止损维度，原指向错位）。

### M10.1 · P1（第 6/7 维，加字段+参数化，1 轮 14h）

| 实验 | 维度 | 类型 | 内容 | 产出 |
|------|------|------|------|------|
| P1-4 | 第 6/7 维 强度+量比 | 加字段+参数化 | reason_json 加 `volume_ratio`/`break_strength_pct`；`buy.premium` 参数化（默认 3% 保基准可比） | 强度 6 档 + 量比 6 带分布 |

### M10.2 · P2（第 8/9 维，出场归因+止损规则）

| 实验 | 维度 | 类型 | 内容 | 产出 |
|------|------|------|------|------|
| P2-5 ✅ | 第 8/9 维 出场归因 | A 类后验（MFE） | 浮盈回吐分布（MFE/回吐率），为移动止损立项提供证据 | `docs/experiments/e17_giveback_report.html`（框架 `e17_giveback_framework.md`）· **2026-10-27 完成**：H1 不成立 / H2 成立但薄 / H3 复现；结构发现 = 63% 出口由 `time_stop_noprofit` 制造 |
| P2-6a | 第 9 维 时间止损 | B 类验证 | `position.staledays` 梯度 **0 / 2 / 5 / 10 / off(100000)**（原 `3` 已裁；**`0` 不是关闭而是最激进档**，见 docs/29 P2-6a） | 链接复利终值 + MDD + 短持块净利 + 大赢家保留率；框架 `e17_staledays_framework.md` |
| P2-6b | 第 8/9 维 止损规则 | B 类验证（**证据已削弱**） | 初始止损 5/8/10/15% + trailing 5/8/10/15%（代表年粗筛） | 仅在 P2-6a 无解时立项 |

### M10.3 · P3（第 1/3/4 维，宽度+升势/最高box）

| 实验 | 维度 | 类型 | 内容 | 产出 |
|------|------|------|------|------|
| P3-7 | 第 1 维 box 宽度 | B 类验证 | window 3/5/10/20/60 参数化（代表年粗筛，最贵一维） | 宽度 5 档四项口径 |
| P3-8 | 第 3/4 维 升势/最高box | A 类定义+事件研究 | 升势/最高 box 口径确认（须先与用户确认再实验） | 是/否对照 |


### M10.4 · 滚雪球复利与统计口径（✅ 2026-10-27）

| 项 | 内容 |
|---|---|
| 问题 | 14 年逐年从 100 万重启，Σ年净利是加法账；真实资金是乘法（滚雪球） |
| 算法 | `scripts/e17_snowball.py`：只读 28 库 `equity_snapshot`，`level *= total_d/total_{d-1}`，跨年直接衔接 |
| 产物 | `docs/experiments/e17_snowball_report.md` + `e17_snowball_curve.svg` + `var/bt/snowball_stats.json` |
| 结论 | baseline 1,000,000 → **2,610,550**（CAGR 7.09%，MDD −21.10%，水下 1657 天/49% 样本）；baseline+ → **2,961,561**（CAGR 8.06%，MDD −8.03%，水下 868 天）→ 复利口径把闸门价值放大（Calmar 0.336 → 1.005） |
| 口径 | 定义与擂台规则写入 docs/01 §10.6；metrics 列语义写入 docs/04 §9.5；一致性规则见 docs/06 §7 |

### M10.5 · 实验方法论与队列纪律（2026-10-27 汇总，原 docs/27 折叠）

1. **A 先 B 后**：先零成本分带统计（A 类），显著才做规则验证（B 类）；跳过 A 直接跑 B 是最大浪费。
2. **单因子轮换（OFAT）**：每轮只动一个因子，其余与基线逐字相同；禁止全组合遍历。
3. **一年一年跑、串行不并发**：逐年独立库 `bt_<prefix>_<year>.db`，前序失败即停队列；跨年可比依赖同一组参数。
4. **等价验证先行**：任何改代码的实验，先用 off/对照模式跑一个代表年，与历史基线做**四表逐单 diff**（plan_order / position / trade_fill / equity_snapshot），通过才启动正式实验。
5. **代表年粗筛**：牛/熊/震荡/结构市各选代表年（如 2007/2008/2010/2013），显著才补全 14 年。
6. **长任务分段执行**：单任务 ≤ 约 2 小时，逐段接续（整套队列常驻后台会被环境回收，2026-10-26 已踩坑）。
7. **四项口径缺一不报**：胜率 / 每单位净利 / 保本胜率 / 利润集中度；另加 PF、缺口、剔除前三赢家。
8. **结论分层**：跨年结论写入 `docs/29`+ 或 `docs/experiments/`，不直接改 docs/00-06 的框架结构（框架是宪法，结论是案例）。

### M10.6 · 下一步（2026-10-29 四分类；权威视图见 docs/29 §五「试验清单总览」，以 29 为准）

**2.1 必须做（按序）**：

| 优先级 | 事项 | 类型 | 状态 |
|---|---|---|---|
| 0 | **Box 轨迹工程验证（A0/A1）**——确定性契约 + 生命周期可观测性，零策略变更 | 工程测试 | 待做（是止损上移研究的前置） |
| 1 | **新基线 14 年 · 双镜对照**（A=上证 B=中证500，`max_hold=14`+`equity_gate=off`） | B 类 | 🔄 阶段1 冒烟 ✅；阶段2 代表 4 年 ✅ 判决「无稳定结论」；阶段2.5 AND 门推演 ✅（否决，改 OR）；阶段3 全量待跑 |
| 2 | **止损随 box 上移 · 事件研究**（依赖第 0 项） | A 类零成本 | 待做 |
| 3 | **P2-6a `position.staledays` 梯度** | B 类 | 待做 |
| 4 | **双镜 OR 门（双强共振）** ⚠️原 AND 门已被推演否决 | 先 A 类推演✅ 后 B 类 | 推演：OR 1.969× > 单镜最佳 1.869×；AND 1.700× 最差 |
| 5 | **P1-4 突破强度+量比字段化** | 工程+A/B | 未启动 |
| 6 | **入场/池管理旋钮扫描**（min_watch_days/latedays/flatboxmax/max_entry_gap/A判据） | A 类打包 | 未启动 |
| 7 | P3-8 升势/最高 box · P3-7 box 宽度 | A 定义+B | 未启动 |
| 8 | box.min_width 5% 参数化 | B 类 | 未立项 |

**2.2 可选（有前置条件）**：大赢家加仓放大（A 类先证 edge）、牛冲熊守动态仓位（待 14 年结果）、P2-6b trail%（staledays 无解才做）。

> **B-4 已收官（2026-10-28）**：②（14 只上限）胜 ③（50% 预算闸门），预算闸门否决、`max_hold=14` 转正。
> **死参数清点（2026-10-28）**：62 键中 29 键代码不读，已从 docs/01 §6 移除并登记。
> 已完成不再列：双指数分桶（弃用）；50% 权益闸门（B-4 否决，保留不启用）；P2-5（docs/29 §四第 9 条）。
### M9/M10 路线图

```
M9.0 (第5维位置, ✅完成) ──→ M9.1 (样本外, 规则定稿后)
                  ──→ M10.0 (P0: 第2/10维, A类零成本) ──→ M10.1 (P1: 第6/7维)
                  ──→ M10.2 (P2: 第8/9维) ──→ M10.3 (P3: 第1/3/4维)
```

- M9.0（第 5 维位置）是唯一已完成的维度；其余九维按 docs/29 的 P0-P3 迫切度排期。
- P0 为 A 类零成本（无需改回测器），先做；P1 起需改回测器（加字段/参数化），每轮约 14 小时。
- M10.0-M10.3 之间无强依赖，可并行立项，但回测仍须串行（不并发铁律）。
- 各实验的详细设计、判定标准、红线见 docs/29 §五/§六/§八。

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
