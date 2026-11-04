# 54 · 新基线设计与实现审查

> 历史审计快照（2026-11-04），不再维护为当前缺口清单；行号、结论与已修项均属于审计当时。当前执行顺序见 [29](../29-box-gradient-experiment-plan.md)，验收见 [49](../49-baseline-rebuild-charter.md)。本稿无真实 14 年回放证据，不对收益或数据质量作结论。

- `docs/44` 登记49个参数；`python scripts/param_ledger.py --check` 曾在本轮实际运行并通过，提示49键与代码、迁移一致。该结果只证明台账生成口径一致，不证明策略语义正确。
- `python scripts/check_consistency.py` 曾在本轮实际运行并通过 C1/C2/C3/C4/C6；不检查复权表、攻防功能、回放完整性或收益判据。C3/C6 检查范围见 `scripts/check_consistency.py:121-167`。
- 新基线关键设计仍有未落地项：`docs/53` 要求后复权数据和 `daily_bar_adj`，当前行情读取仍查询 `daily_bar`；`docs/44` 中攻防模式为 `off`，且 `on` 乘数实现恒为1。
- 因此参数台账的静态键集检查通过，但设计与实现尚未整体一致；不能将当前代码称为符合新基线规格。

## 二、参数核对与幽灵参数

权威参数值及类型、种子来源、代码默认值见 [44](../44-new-baseline-config.md) §二生成表。审计时共 49 键：42 个迁移种子、7 个仅代码默认；本报告不重复抄值。

台账生成器扫描 `internal/`、`cmd/` 非测试 Go 源里的字面量 `GetFloat/GetInt/GetString`，忽略测试文件：`scripts/param_ledger.py:120-163`。因此其“全参数”是该静态模式定义的全集，不含动态键、SQL直读或其他语言中的参数读取。当前已知需单列：

| 项 | 文档 | 实现及判断 |
|---|---|---|
| `box.period` | `docs/04-api-contract.md:603` 标为待加参数 | 代码没有该键；`ConfirmDays` 固定为3，见 `internal/box/engine.go:39-43,261-269`。`docs/49:116-120` 已裁定冻结为引擎常量，故此条待办已过期，不是当前缺参。 |
| `pos.regime.attack_mult`、`pos.regime.defend_mult`、`pos.regime.breadth_min`、`pos.regime.breadth_exit`、`pos.regime.dd_max`、`pos.regime.dd_exit`、`pos.regime.confirm_days` | `docs/48-regime-attack-defense-design.md:106-113` 列为建议参数 | 当前实现仅登记 `pos.regime_scale.mode`；代码只读取该开关，`on` 仍返回1.0，见 `internal/planner/planner.go:111-114,373-388`。这是设计未实现项，不应伪装成已生效参数。 |
| `gate.ma60.mode`、`gate.ma60.index` | `docs/44` 默认种子分别 `stop`、`sh000001` | 代码默认分别为 `off`、`sh000001`，见 `internal/planner/planner.go:94-108`；新库迁移值优先。台账能区分生效值与代码默认值，不能将差异误报成幽灵参数。 |
| 其余49键 | `docs/44:37-146` | 本轮 `param_ledger --check` 显示键集与迁移生效值未漂移；该静态扫描的盲区仍须结合源码审查。 |

`docs/01 §6` 与 `docs/44 §二` 是参数资料；逐项值以44生成表为准。`tdx.root` 为机器路径配置，台账登记但未种子化，44说明属刻意设计。参数读取失败或值不可解析时 `ParamRepo` 回退代码默认，见 `internal/store/repo_param.go:15-50`；缺行、错类型可能改变实际配置，不能只验键名。

## 三、已确认的设计/实现差异


### 1. 后复权数据规格未实现

`docs/53:15-25,47-69` 要求 tqcenter、`none`/`back` 两遍取数、后复权因子归一、个股复权量调整、退市股无因子保留原价，并固定回测范围。当前 TDX Reader 只读取 `.day` 原始文件并直接返回原始价量：`internal/datasource/tdx/reader.go:1-13,63-103,131-141`。`BarRepo` 查询仍直接指向 `daily_bar`，例如 `BarsWindow`、`CloseOn`、`AvgVolume`：`internal/store/repo_bar.go:97-121,182-201`。`docs/53:199-219` 自己列出 `daily_bar_adj`、分流读表和相关验收，表明这些是待实施项。

影响：当前代码不能满足 docs/53 的新价格口径。没有运行 tqcenter 导入或回测，不报告数据实际偏差数值。

### 2. 除权日强制清仓与新规格冲突

`docs/53:26,208,230` 要求下线除权日清仓；但 `internal/match/match.go:43-88` 仍按 `dividend_event` 对持仓生成 `sim:dividend` 卖出成交并撤销未成交订单。规格和实现直接冲突。

### 3. 攻防转换只有接口桩

`docs/48:77-115` 规定宽度与指数距高双判据、滞回、状态乘数，并要求只影响入场数量；实现中只有 `pos.regime_scale.mode`，且 `regimeScaleMult` 无论 `off/on` 均返回1.0：`internal/planner/planner.go:111-114,373-388`。因此参数开 `on` 也不会形成攻防转换。不得将这项写成已经生效的策略能力。

### 4. 箱体确认天数为冻结引擎常量

`docs/49:116-120` 已裁定 `ConfirmDays=3` 冻结为引擎常量，不设参数键。**（本仓复核补注 2026-11-04：`a8a7d7a` 后三处生产构造——`scanner.boxParams`、`regime` 标定、`cmd/darvas`——已统一从 `box.DefaultParams()` 起手再覆盖可调字段，不再依赖「零值进入引擎归一为3」这条隐式路径；`docs/04:603` 的 `box.period` 待办已标废。）**

### 5. 生产流水线任务失败不统一传播

`docs/01` §4.1 要求串行、可重跑；`RunDaily` 注释注明除 ingest 外任务失败不阻断后续，且任务错误按实现收集为 `TaskResult`：`internal/pipeline/pipeline.go:46-102`。调用方不能只看整体 error，必须检查每个任务状态。回放 `Replay` 会收集失败任务，`backtest.go:627-654`。

### 6. 连续回放断点判据不保证无缺日

`LastCompleteDate` 查询六类任务 `status='ok'` 且 `COUNT(*)=6` 的最大日期：`internal/store/repo_run.go:95-109`。它未验证窗口起点到该最大日期之间每个交易日均完整，也未把撮合成功纳入六任务计数。`Replay` 在 report 前单独执行 Matcher，撮合失败仅写入 fails，不登记为独立 daily_run 任务：`internal/backtest/backtest.go:627-644`。续跑从最大完整日之后继续：`internal/backtest/backtest.go:303-319`。因此中间缺日或撮合失败后出现更晚完整日时，最大日期断点本身不能证明账本连续；新基线验收需逐日检查。

### 7. 数据导入状态可能掩盖单文件/批次错误

全量导入将单文件读取错误作为继续条件，并最终置 `Status="ok"`：`internal/ingest/ingest.go:101-119,141-156`。flush 错误被记录日志后仍继续，最终也置 ok：`internal/ingest/ingest.go:120-139`。增量导入同样忽略读取与 flush 错误后置 ok：`internal/ingest/ingest.go:181-215`。如果“ok”被用作新基线数据完整性证据，这个状态不足以证明所有文件成功导入；需独立核对代码数、日期覆盖和导入失败数。

### 8. 年库“完整”判据仍不能证明全程完整

`scripts/base_run.py:73-84,91-99` 现在检查权益快照非空、最大日期达到终点且六任务链不存在失败/缺失日期；相比旧的“任意非空快照”判据已有加固。但它没有将库中日期与权威交易日历逐日比对，也没有把回放撮合单独纳入 `daily_run` 判据，因此稀疏快照若伴随任务行满足检查，仍不能仅凭 `db_complete` 证明账本覆盖完整。设计终点条件见 `docs/53:210`，仍须逐日审计。

### 9. 成交写入缺少跨表原子性
`internal/ledger/ledger.go:335-365` 先将订单置 filled，再写 trade_fill，最后执行持仓更新；`applySell` 到 `:447-457` 才检查是否有持仓、数量是否超卖。仓储 `AppendFill` 使用独立 `db.Exec`（`internal/store/repo_trade.go:112-116`），这些步骤没有共用事务。因而持仓校验失败时，前面的订单/成交写入已经生效；再次输入同笔成交又会被 `FillExists` 跳过（`ledger.go:307-315`）。这与 `docs/01` NFR-4 的事务恢复要求冲突。此项为源码路径确认，尚未注入故障实测。

### 10. 参数消费者硬编码分叉（**已修，本条为行号漂移误读**）

**（本仓复核补注 2026-11-04：`30dd82b` 已修。`scanner.prefilter` 均量窗口改读 `box.volwindow`、
`regime` 的 `MinBoxWidth` 改读 `box.min_width`；原文引用的 `scanner.go:184-185` / `regime.go:410-416`
在修复后已指向其他代码，属按旧行号复核导致的误报。2005 全年冒烟对 `verify_0029_2005.db`
12 项聚合逐分一致 + 成交/权益轨迹 md5 全同，可证行为中性。）**

原文（保留备查）：`internal/box/scanner.go:184-185` 的增量预过滤均量窗口固定50，而引擎窗口读取 `box.volwindow`（`:69-70`）；`internal/regime/regime.go:410-416` 将 `MinBoxWidth` 固定为0.05，未读取 `box.min_width`。默认时相同，改参时两条路径不一致；具体行情影响未实测。

## 四、其他需对齐的文档/边界
- `docs/53:227-232` 要求同步修订价格口径、ERD、除权撮合及行情读表；当前仍按原始 `daily_bar` 路径实现。切表验收需覆盖 `repo_bar.go` 所有读方法及其他 SQL 直读，并验证指数分流。`scanner.prefilter` 只处理股票集合，见 `internal/box/scanner.go:121-135`、`internal/store/repo_sec.go:107-111`。
- `docs/06:70-76` 定义权益恒等式、持仓闭合、filled订单有fill；审计接口见 `internal/ledger/ledger.go:640-646`，CLI 调用见 `cmd/darvas/main.go:246-250`。新基线应逐日运行审计。
- `internal/store/repo_param.go:15-50` 对缺参、解析失败回退默认值；真实回测库须做参数值对账。

## 五、新基线验收建议

**A. 参数冻结**

1. 固定 `docs/44` 49键清单、迁移版本、所有代码默认值；输出参数快照和指纹。真实库执行 `python scripts/param_ledger.py --check --db <库>` 并核对 `db_mismatch/db_extra/db_missing`；default-only 键按代码默认值补齐比较。退出码只反映文档与代码/迁移一致，必须检查库差异输出。
2. 保持 `ConfirmDays=3` 与 `docs/49` 冻结决定一致；将 `docs/04` 的 `box.period` 过时待办标废；把攻防7个建议值标为待实现/未纳入，直到真实接线。
3. 续跑的任何 `--set` 均应拒绝；此规则已实现于 `internal/backtest/backtest.go:270-282`，并已有 default-only 参数负向回归覆盖（`TestResumeRejectsParamConflict` 含一致值与 default-only 两例）。**（本仓复核补注：该项已满足。）**

**B. 数据口径**

1. 按 `docs/53:199-219` 验收股票覆盖5563、无因子339、抽样除权连续性、量额守恒、原始价同源；覆盖 `repo_bar.go` 所有行情读取方法及其他直接SQL，并验证指数走原始数据。
2. 验收除权日无 `sim:dividend` 成交，并证明持仓跨除权日时数量、成本与复权口径一致。

**C. 连续账本与身份**

1. 2005-01-01 至 2018-12-31 交易日历逐日连续；回放每日六任务、撮合状态、权益快照逐日可查；生产每日七任务另行核对。任何缺日、失败、无快照均不得判完整或越过续跑。
2. 故障注入中间日失败、撮合失败、进程中断及末日成功但更早缺日；恢复后从最早未完整交易日补齐，重复执行不得重复成交或改写历史事实。
3. 基线身份含参数快照/指纹、引擎二进制hash、源库hash；任何身份记录失败均使运行失败，不一致时拒绝续跑。
4. 每日审计现金+市值=总权益、成交数量闭合、filled订单有fill；费用、实现盈亏和持仓市值按新价格口径一致。

**D. 行为和输出**

1. `mode=off` 与冻结基线逐单一致；攻防 `on` 的状态切换、确认、降级、乘数传导及不影响卖出行为需有可复现序列和全14年结果。
2. 按 `docs/48` 预注册判据，在新账本重算锚点；留存逐年指标、交易明细、权益轨迹、状态序列及身份。未执行不得标为通过。
3. `check_consistency.py` 只作结构检查，不能替代数据、行为、完整性验收。
4. 保留 `docs/49:86-96` 的 V1-V4 硬判据；不满足须记录失败，不改阈值。

**本报告覆盖边界**：这是源码与文档交叉核实的已确认发现，不是“所有实现逐行审查、全部差异已穷尽”的证明。已读取关键入口、参数访问、回放/断点、箱体、数据仓储和成交路径；未逐项审查GUI全部交互、所有离线实验脚本及历史存档。后台分组审查未产出可采用结果。要声称“无遗漏”，还须建立文档规则→实现符号→调用方→行为证据的覆盖矩阵；当前材料不支持该声明。

本轮源码复核确认：续跑现在拒绝任何 `--set`（`internal/backtest/backtest.go:270-282`），身份记录失败会中止新回放（`:230-234`），planner 首次权益读取 `equity.base` 已修复（`internal/planner/planner.go:65-75`）。旧版报告中的这些问题不再列为当前差异。

## 六、本次检查

- `python scripts/param_ledger.py --check`：本轮通过，49键台账与代码、迁移一致。
- `python scripts/check_consistency.py`：本轮通过，C1/C2/C3/C4/C6 全绿。
- 继承此前已记录的核验：稀疏库曾使 `db_complete` 返回 `True`；`LastCompleteDate` 同型SQL曾跨过更早失败日。本轮未重跑这两项。
- 未执行：Go测试、真实数据导入、tqcenter探针、后复权灌库、基线回放与收益判据；没有相关通过结论。
