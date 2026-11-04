# 54 · 新基线设计与实现审查

> 审查日期：2026-11-04。记录仓库当前事实，不表示新基线已完成。无真实14年回放证据，不对收益或数据质量作结论。行号按本次审查时工作区。

## 一、结论

- `docs/44` 登记49个参数；`python scripts/param_ledger.py --check` 实际通过，提示49键与代码、迁移一致。此结果只证明台账生成口径一致，不证明策略语义正确。
- `python scripts/check_consistency.py` 实际通过 C1/C2/C3/C4/C6；不检查复权表、攻防功能、回放完整性或收益判据。C3/C6 检查范围见 `scripts/check_consistency.py:121-167`。
- 新基线关键设计仍有未落地项：`docs/53` 要求后复权数据和 `daily_bar_adj`，当前读取路径仍查询 `daily_bar`；`docs/44` 中攻防模式为 `off`，且 `on` 乘数实现恒为1。
- 所以参数清单已基本对齐，设计与实现尚未整体一致；不能将当前代码称为符合新基线规格。

## 二、参数核对与幽灵参数

权威参数值及类型、种子来源、代码默认值见 [`44-new-baseline-config.md`](44-new-baseline-config.md) §二生成表。当前共49键：42个迁移种子、7个仅代码默认；键与值不在本报告重复抄写，避免形成第二事实源。执行 `python scripts/param_ledger.py` 可输出全表，`--check` 用于核对。

台账生成器扫描 `internal/`、`cmd/` 非测试 Go 源里的字面量 `GetFloat/GetInt/GetString`，忽略测试文件：`scripts/param_ledger.py:120-163`。因此其“全参数”是该静态模式定义的全集，不含动态键、SQL直读或其他语言中的参数读取。当前已知需单列：

| 项 | 文档 | 实现及判断 |
|---|---|---|
| `box.period` | `docs/04-api-contract.md:603` 标为待加参数 | 代码没有该键；`ConfirmDays` 可由调用方传入，默认3，见 `internal/box/engine.go:39-43,261-269`。不是当前台账漏项，而是旧待办文档未更新；是否改为可配置交易参数需设计裁定。 |
| `pos.regime.attack_mult`、`pos.regime.defend_mult`、`pos.regime.breadth_min`、`pos.regime.breadth_exit`、`pos.regime.dd_max`、`pos.regime.dd_exit`、`pos.regime.confirm_days` | `docs/48-regime-attack-defense-design.md:106-113` 列为建议参数 | 当前实现仅登记 `pos.regime_scale.mode`；代码只读取该开关，`on` 仍返回1.0，见 `internal/planner/planner.go:111-114,373-388`。这是设计未实现项，不应伪装成已生效参数。 |
| `gate.ma60.mode`、`gate.ma60.index` | `docs/44` 默认种子分别 `stop`、`sh000001` | 与代码默认 `off`、`sh000001` 不同；新库迁移值优先，见 `docs/44:122-123` 与 `internal/planner/planner.go:94-108`。台账能区分“生效值”和“代码默认值”，不能把后者误报成幽灵参数。 |
| 其余49键 | `docs/44:37-146` | `param_ledger --check` 通过；未发现该工具口径内的键集或迁移生效值漂移。 |

`docs/01 §6` 与 `docs/44 §二` 是参数资料；逐项值请以44生成表为准。`tdx.root` 为机器路径配置，台账登记但未种子化，44说明属刻意设计。参数读取失败或值不可解析时 `ParamRepo` 回退代码默认，见 `internal/store/repo_param.go:15-50`；因此缺行、错类型可能静默改变实际配置，不能只验键名。

## 三、已确认的设计/实现差异

### 1. 后复权数据规格未实现

`docs/53:15-25,47-69` 要求 tqcenter、`none`/`back` 两遍取数、后复权因子归一、个股复权量调整、退市股无因子保留原价，并固定回测范围。当前 TDX Reader 只读取 `.day` 原始文件并直接返回原始价量：`internal/datasource/tdx/reader.go:1-13,63-103,131-141`。`BarRepo` 查询仍直接指向 `daily_bar`，例如 `BarsWindow`、`CloseOn`、`AvgVolume`：`internal/store/repo_bar.go:97-121,182-201`。`docs/53:199-219` 自己列出 `daily_bar_adj`、分流读表和相关验收，表明这些是待实施项。

影响：当前代码不能满足 docs/53 的新价格口径。没有运行 tqcenter 导入或回测，不报告数据实际偏差数值。

### 2. 除权日强制清仓与新规格冲突

`docs/53:26,208,230` 要求下线除权日清仓；但 `internal/match/match.go:43-88` 仍按 `dividend_event` 对持仓生成 `sim:dividend` 卖出成交并撤销未成交订单。规格和实现直接冲突。

### 3. 攻防转换只有接口桩

`docs/48:77-115` 规定宽度与指数距高双判据、滞回、状态乘数，并要求只影响入场数量；实现中只有 `pos.regime_scale.mode`，且 `regimeScaleMult` 无论 `off/on` 均返回1.0：`internal/planner/planner.go:111-114,373-388`。因此参数开 `on` 也不会形成攻防转换。不得将这项写成已经生效的策略能力。

### 4. 箱体确认天数不是参数台账参数

`docs/00:160` 和 `docs/04:603` 描述三日固定规则；引擎接受 `BoxParams.ConfirmDays`，零值归一为3：`internal/box/engine.go:39-43,90-125`；生产扫描参数构造没有填它，使用零值：`internal/box/scanner.go:65-74`。它是引擎接口字段而非当前 `param` 键；若基线要求实验配置可复现，应明确固定值3并把实际参数纳入回放身份，或正式增加参数迁移。不能仅凭 `ConfirmDays` 可传入就声称生产参数化完成。

### 5. 生产流水线任务失败不统一传播

`docs/01` §4.1 要求串行、可重跑；`RunDaily` 注释注明除 ingest 外任务失败不阻断后续，且 plan/report 失败不返回错误，其他任务错误被忽略：`internal/pipeline/pipeline.go:46-102`。回放 `Replay` 会收集失败任务，`backtest.go:627-654`。这意味着生产一次运行可能返回 nil error 但任务心跳失败；调用方需检查每个 TaskResult，不能只检查总函数 error。若设计要求“失败即整日未完成”，需以任务状态判据验收。

### 6. 连续回放断点判据不保证无缺日

`LastCompleteDate` 查询六类任务 `status='ok'` 且 `COUNT(*)=6` 的最大日期：`internal/store/repo_run.go:95-109`。它未验证窗口起点到该最大日期之间每个交易日均完整，也未把撮合成功纳入六任务计数。`Replay` 在 report 前单独执行 Matcher，撮合失败仅写入 fails，不登记为独立 daily_run 任务：`internal/backtest/backtest.go:627-644`。续跑从最大完整日之后继续：`internal/backtest/backtest.go:303-319`。因此中间缺日或撮合失败后出现更晚完整日时，最大日期断点本身不能证明账本连续；新基线验收需逐日检查。

### 7. 数据导入状态可能掩盖单文件/批次错误

全量导入将单文件读取错误作为继续条件，并最终置 `Status="ok"`：`internal/ingest/ingest.go:101-119,141-156`。flush 错误被记录日志后仍继续，最终也置 ok：`internal/ingest/ingest.go:120-139`。增量导入同样忽略读取与 flush 错误后置 ok：`internal/ingest/ingest.go:181-215`。如果“ok”被用作新基线数据完整性证据，这个状态不足以证明所有文件成功导入；需独立核对代码数、日期覆盖和导入失败数。

### 8. CLI 覆盖参数的续跑冲突检查漏掉未入库键

`resumeRegime` 仅当覆盖键已存在于参数表且值不同时拒绝：`internal/backtest/backtest.go:267-285`。而七个 default-only 键没有种子行，见 `docs/44:27-29`。对这类键传入不同 `--set` 会被跳过冲突判断，且续跑不会调用 `applyParamOverrides`（续跑路径见 `backtest.go:214-232`）。结果可能是命令参数与实际参数不同而静默沿用原库值。此前测试名 `TestResumeRejectsParamConflict` 只涵盖有库内参数冲突的情况：`internal/backtest/backtest_test.go:343-385`。建议新基线运行器拒绝任何续跑 `--set`，或把完整“有效参数（含默认值）”比较后拒绝不一致。

### 9. 身份记录失败被降级为非阻断

新建回放在 `recordBaseline` 失败时仅打印“ 不阻断”并继续：`internal/backtest/backtest.go:223-232`；身份数据本身包括参数快照、参数指纹和二进制哈希：`internal/store/repo_baseline.go:43-88`。续跑则要求先有身份：`backtest.go:287-300`。这会产生已运行但无法按设计续跑/无法证明身份的账本。若 docs/49 阶段B要求身份为硬标准，记录失败必须使该次基线失败，验收需断言身份行存在且与运行参数一致。

### 10. 年库“完整”判据过弱

`scripts/base_run.py:73-91` 将任意非空 `equity_snapshot` 判为完整；`run_one` 因而跳过该年，见 `:94-100`。它不检查应有交易日数、起止日期、每日任务或逐日完整性。与 `docs/53:210` 的 `max(date) ≥ 2018-12-01` 也不等价：后者仅作为终点条件，同样不能独立证明中间无洞。须以指定交易日历和完整任务链逐日对账。

### 11. 成交写入缺少跨表原子性

`internal/ledger/ledger.go:335-365` 先将订单置 filled，再写 trade_fill，最后执行持仓更新；`applySell` 到 `:447-457` 才检查是否有持仓、数量是否超卖。仓储 `AppendFill` 使用独立 `db.Exec`（`internal/store/repo_trade.go:112-116`），这些步骤没有共用事务。因而持仓校验失败时，前面的订单/成交写入已经生效；再次输入同笔成交又会被 `FillExists` 跳过（`ledger.go:307-315`）。这与 `docs/01` NFR-4 的事务恢复要求冲突。此项为源码路径确认，尚未注入故障实测。

### 12. 首次计划本金未读取 equity.base

`internal/planner/planner.go:65-75` 在无权益快照时直接使用常量1000000并写默认快照；账本本金却读取 `equity.base`（`internal/ledger/ledger.go:558-560`）。当前默认100万时一致，修改该参数且没有快照时，两条路径不一致。不是缺参，而是消费方遗漏。

### 13. 参数数据库核对结果不控制检查退出码

`scripts/param_ledger.py:343-367` 的 `cmd_check` 只比较 docs/44 与代码/迁移；`main` 在 `:399-400` 遇到 `--check` 就返回，不再执行 `:407-413` 的真实库差异提示。真实库核对应使用 `--db <库>` 查看实际值或读取结构化导出中的 `db_mismatch/db_extra/db_missing`，不能把 `--db ... --check` 的退出码当成数据库验收。七个刻意未种子化键需要按有效默认值补齐比较，不应一概要求库中存在49行。

### 14. 参数消费者仍有硬编码分叉

`internal/box/scanner.go:184-185` 的增量预过滤均量窗口固定50，而引擎窗口读取 `box.volwindow`（`:69-70`）；`internal/regime/regime.go:410-416` 声称复用 boxscan 参数，却将 `MinBoxWidth` 固定为0.05，未读取 `box.min_width`。默认值时相同，改参时两条路径不再采用同一配置。尚未用行情序列验证其具体输出差异。

## 四、其他需对齐的文档/边界

- `docs/04:603` 仍将 `box.period` 写成待加参数；`docs/44` 的49键不含该键，`docs/00:160` 仍记固定3日。应明确该行是历史实验待办，或更新成实际生产契约。
`docs/53:227-232` 明确要求同步修订 `docs/01` 价格口径、`docs/03` ERD、`docs/archive/42`，并修改实现；本次审查确认这些条目仍需按规格落实。切表验收应逐个覆盖 `repo_bar.go` 中所有按代码取数的方法（至少 `BarsWindow`、`BarCount`、`BarCountClosed`、`BarsRange`、`CloseOn`、`AvgVolume`、`AvgAmount`、`LatestDate`、`PrevDate`、`NextDate`，并以当前完整源码核实余项），不能只验证箱体主路径。指数路径也要用 `sh000*`/`sz399*` 样例实测仍读原始 `daily_bar`，覆盖 regime、MA60 与交易日历消费者。`scanner.prefilter` 当前只在 `ActiveStockCodes` 返回的股票集合中调用（`internal/box/scanner.go:121-135`；该查询过滤 `kind='stock'`，`internal/store/repo_sec.go:107-111`），所以不把“它会扫描指数”列为现状问题；若未来调用范围改变，再按新范围补验。这里不把规格主张当作已验证的市场事实，探针证据仅按53文档所述引用。
- `docs/06:70-76` 将权益恒等式、仓位闭合、filled订单有fill列为运行时不变量；`internal/ledger/ledger.go:640-646` 定义审计接口，CLI 在 `cmd/darvas/main.go:246-250` 调用。需要将该审计实际纳入新基线逐日验收，而非只运行静态一致性脚本。
- `repo_param.go` 对缺参、解析失败回退默认值（`internal/store/repo_param.go:15-50`）；参数台账检查默认值不等于迁移值时会显示差异，但真实回测库必须另外传入 `--db` 做数据库对账。

## 五、新基线验收建议

**A. 参数冻结**

1. 固定 `docs/44` 49键清单、迁移版本、所有代码默认值；输出参数快照和指纹。真实库执行 `python scripts/param_ledger.py --db <库>` 检查实际值，并核对结构化结果 `db_mismatch/db_extra/db_missing`；七个刻意未种子化键按代码默认值补齐后比较。`--check` 只验文档，不验数据库差异。
2. 明确 `ConfirmDays=3` 是固定引擎配置还是纳入参数；处理 `docs/04` 的 `box.period` 旧条目；把攻防7个建议值标成待实现/未纳入，直到真实接线。
3. 对续跑参数覆盖做负向验收：已种子参数和 default-only 参数，只要运行值与账本身份不同均须拒绝。

**B. 数据口径**

4. 逐规格验收 `docs/53:199-219`：股票覆盖5563、无因子339、抽样除权连续性、量额守恒、原始价同源；逐个核实 `repo_bar.go` 所有行情读取方法及其他直接读表SQL按代码分流，而非只改 `BarsWindow`；用 `sh000*`/`sz399*` 验证 regime、MA60、交易日历仍读取原始指数数据。
5. 验收除权日无 `sim:dividend` 成交，并证明持仓跨除权日、股数/成本口径与复权数据一致。

**C. 连续账本与身份**

6. 2005-01-01 至 2018-12-31 交易日历逐日连续；首日权益、末日范围、回放每日六任务及撮合状态、权益快照均逐日可查；生产每日七任务另行核对。任何缺日、失败、无快照都不得被判完整或越过续跑。
7. 断点故障注入：中间日失败、撮合失败、进程中断、末日成功但更早缺日，恢复后必须从最早未完整交易日补齐；重复执行不得重复成交或改写历史事实。
8. 基线身份必须存在，含参数快照/指纹、引擎二进制hash、源库hash；任何记录失败都应导致运行失败。身份不一致时续跑必须拒绝。
9. 每个交易日执行账本审计：现金+市值=总权益、成交数量闭合、filled订单有fill；费用、卖出实现盈亏及持仓市值按新价格口径一致。异常要有日/代码/订单定位。

**D. 行为和输出**

10. `mode=off` 与冻结的新基线逐单一致；攻防 `on` 的状态切换、连续确认、立即降级、乘数传导及不影响卖出行为均需有可复现序列与全14年结果。`docs/49:148-151` 将其列为建成硬标准；在设计正式调整前，桩实现不能算通过。
11. 结果验收使用 `docs/48` 预注册判据并先基于新账本重算锚点；保存逐年指标、交易明细、现金权益轨迹、状态序列及参数身份。未执行的指标不写“通过”。
12. `python scripts/check_consistency.py` 仍作为结构检查，但不能替代以上数据、行为、完整性验收。

13. 保留 `docs/49:86-96` 的原有硬判据：V1结构一致性、V2真实库参数对账、V3剔除前三赢家后净利仍为正、V4年度方向一致性。V3不能被总盈利或攻防改善替代；不满足时须记录失败，不能改阈值让结果通过。

**本报告覆盖边界**：这是已确认发现的清单，不是“所有实现逐行审查、全部差异已穷尽”的证明。已读取关键入口、参数访问、回放/断点、箱体、数据仓储和成交路径；未完成GUI全部交互、所有离线实验脚本及历史存档每条结论的逐项闭环核验。后台分组审查因模型访问权限失败，没有可采用的分组结果。后续要声称“无遗漏”，必须建立文档规则→实现符号→调用方→行为证据的逐条覆盖矩阵；当前材料不支持该声明。

## 六、本次已执行检查

- `python scripts/param_ledger.py --check`：通过，49键台账与代码+迁移对齐。
- `python scripts/check_consistency.py`：通过，C1/C2/C3/C4/C6 全绿。
- 合成临时库仅插入2005-01-04一条权益快照，实际调用 `scripts/base_run.py` 的 `db_complete`：返回 `True`，复现半成品被判完整。
- 内存库写入2005-01-05失败任务及2005-01-06六任务成功，执行 `LastCompleteDate` 同型SQL：返回 `2005-01-06`，确认最大日期判据会跨过更早失败日。此为SQL判据复现，未执行完整Go续跑场景。
- 未执行：Go测试、真实数据导入、tqcenter探针、后复权灌库、基线回放与收益判据。故这些结果没有通过结论。
