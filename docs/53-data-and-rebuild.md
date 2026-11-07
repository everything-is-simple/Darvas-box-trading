# 53 · 数据规格与基线重建（唯一事实源）

> **性质：数据侧规格 + 基线重建作业指导书 + 验收判据。数据侧已实施并验收（2026-11-05），生产 `serve` 尚未切换。**
> **2026-11-06 合并原 `49`（重建新基线行动纲领）**：§9 重建 SOP / §10 验收判据 / §11 永续化 / §12 红线 为原 49 内容，
> 原文见 `archive/49`。A–F 六阶段已收官（adj 基线 100%），唯一未完成 = G 攻防转换，已移交 `48`：G 未开工，前置是 48 新账本重算 + A 系时序（A 系已降支线，以 `60`/`65` 为准）。
> **一句话分工：本文件管「数据长什么样、基线怎么建、建成什么样才算过」，`44` 管参数值，`29` 管已决裁定，`55` 管实验与账本，`60` 管排队与看板。**
> 表内"下线""切换"等描述是**代码侧已实现的契约**（0032，commit `64083db`）：`daily_bar_adj` 已落地、
> `var/bt/full_hist_adj.db` 已验收（`adj_ready=1`，5,561 只 / 17,962,168 行，判据 7/7）；
> 但**生产 `darvas serve` 仍走 raw `daily_bar`**，不得据此宣称生产已切后复权。
> 实验侧对照基准与重做裁定见 `docs/55`。
> **依据**：`tqcenter` 官方接口 **本机实测**（通达信金融终端 V7.73，2026-11-04），
> 而非文档推断。每条结论后附实测证据编号（见 §1）。
> **上游**：`docs/01` §3 分列当前原始价实现与后复权目标；`docs/44` 参数台账（本文不改参数）。
> **下游**：重建 SOP 与验收判据已并入本文 §9–§10（原 `49`）。

---

## §0 一页结论

| 决策项 | **结论** | 依据 |
|---|---|---|
| 数据客户端 | **tqcenter（依赖客户端）**，跑前必须启动通达信金融终端 | 用户指定；E1 |
| 行情接口 | `tq.get_market_data`，`period='1d'`、`fill_data=False`、`count=-1` | E2/E4 |
| **复权方式** | **后复权（以回测起点归一化）**，不用前复权、不用不复权 | E3 |
| 复权参数 | 拉 **`dividend_type='none'` + `'back'` 两遍**，因子 = `back/none`，再除以起点因子 | E3/E4 |
| 复权字段 | **不用 `ForwardFactor`**（语义非直觉，实测与 front/back 均不成简单比例） | E3 |
| 成交量 | `back` 模式**不调量**（Vol/Amount 原值不变），必须**自己按因子反向调整** | E4 |
| 股票全集 | **lday 清单（5,563 只沪深A）**，不用 `get_stock_list("5")` | E5 |
| 退市股 | 339 只 tqcenter **完全拉不到**（行情+除权记录皆空），保留 lday 原价、标注无复权 | E5/E6 |
| 北交所 | **排除**（`get_stock_list("5")` 返回含 357 只 bj，lday 侧本就无） | E5 |
| 时间范围 | 数据拉 **全量历史（1990-01-01 ~ 今，与 `daily_bar` 同轴）**；回测 **2005-01-01 ~ 2018-12-31** | §5 / §8.0-① |
| 参数 | **49 键冻结、原值不动**，`docs/44` 为准 | §5 |
| 除权日清仓 | **规则下线**（`match.go` 该分支删除），持仓自然穿越除权日 | E3 |

---

## §1 实测证据（2026-11-04，一次性探针）

探针脚本：`scripts/tq_probe.py` ~ `tq_probe7.py`（留档可复跑）。

| 编号 | 测什么 | 结果 |
|---|---|---|
| **E1** | tqcenter 可用性 | `tq.initialize` 成功；本机 `TdxW` V7.73 在跑；`sys.path` 需加 `PYPlugins/sys` + `PYPlugins` 两级 |
| **E2** | 字段与单位 | 返回 `Open/High/Low/Close/Volume/Amount/ForwardFactor/VolInStock`；**Volume 单位 = 股**（22,407,262 × 4.07 ≈ Amount 9186 万元，吻合；非「手」） |
| **E3** | 三态语义（600008.SH，2016-05-23 每10股送10股） | `none`：8.12 → 4.07（腰斩，缺口真实存在）<br>`back`：26.36 → 26.80（**连续**）<br>`front`：2.66 → 2.72（连续，但末日=原价）<br>`ForwardFactor`：0.3387 → 0.6901，末值 = 1.000000（**相对最新日归一化**，会随未来除权改写） |
| **E4** | back 是否调量 | **不调**。除权日 `Volume_none = Volume_back = 22,407,262`，`Amount` 亦不变（比值恒 1.0） |
| **E5** | 清单覆盖 | lday A股 **5,563**；`get_stock_list("5")` **5,581**（含 357 只 bj）<br>只在 lday：**339**（已退市：sh600001 邯郸钢铁、sh600005 武钢股份等）<br>只在 tqcenter：**357**（全是 `bj920xxx`） |
| **E6** | 退市股可拉性 | `600001/600002/600005/600065/600087/000003` 行情 **全空**、`get_divid_factors` **全空** |
| **E7** | 同源性 | 现存股 tqcenter `none` 价 **与 lday 原始价逐只完全相等**（600008/600519/000001/002251 全 ✓） |
| **E8** | 吞吐 | 20 只 × 全历史 = 2.2s → **单只 0.11s，5000 只约 9 分钟** |

---

## §2 应使用的数据接口 / 数据集

| # | 接口 | 参数 | 用途 | 频率 |
|---|---|---|---|---|
| 1 | `tq.initialize(__file__)` | — | 建立客户端连接 | 每次任务启动 |
| 2 | `tq.get_market_data` | `dividend_type='none'` | **原始价 + 原始量**（对账基准） | 一次全量 |
| 3 | `tq.get_market_data` | `dividend_type='back'` | **后复权价**（算因子用） | 一次全量 |
| 4 | `tq.get_stock_list` | `market='5'` | 交叉校验「本机是否有此股」（**不作股票池**） | 一次 |
| 5 | `tq.get_divid_factors` | `(code, "", "")` | 除权事件（**仅审计留痕**，不再驱动撮合） | 一次 |
| 6 | `tq.get_trading_dates` | `market='SH'` | 与 `sh000001` 交易日历交叉校验 | 一次 |

**明确不使用的接口**：`get_market_snapshot`（实时快照）、`subscribe_quote`（订阅推送）、
`order_stock`（交易通道）、`tdx_formula`（公式计算）——回测不需要，也不引入第二套交易通道。

**统一调用参数（三处必须完全一致，否则数据不可比）**：

```python
period = '1d'
fill_data = False        # 停牌日必须留空，引擎靠「当日无 bar」判定停牌
count = -1               # 全量，不截断
start_time = '20040101'
end_time = '20991231'
```

> **分批约束**：单次 `stock_list` 建议 ≤ 200 只。20 只 2.2s 是线性区间，
> 5000 只一次性传入可能触发 DLL 超时（`timeout_ms=600000`），按 200 只/批切分。

---

## §3 复权方式：后复权，以回测起点归一化

### 3.1 为什么是后复权

| 方式 | 本机实测行为 | 能否用于 14 年回测 |
|---|---|---|
| `none` 不复权 | 除权日价格腰斩（8.12→4.07），序列断裂 | ❌ 箱体/52周新高/量比全部失真 |
| `front` 前复权 | 末日=原价，历史价随**未来**每次除权被改写 | ❌ 回测带未来函数 |
| **`back` 后复权** | 基准=上市首日，除权日连续，历史**永不改写** | ✅ 唯一可用 |
| `ForwardFactor` | 末值恒为 1.0，随未来除权改变 | ❌ 不能直接当因子用 |

### 3.2 复权序列的构造公式（固化到库，不运行时计算）

```
factor(t)      = Close_back(t) / Close_none(t)          # 官方 back/none 比值
factor_norm(t) = factor(t) / factor(T0)                  # T0 = 2004-01-02（回测前置首日）
adj_close(t)   = Close_none(t) × factor_norm(t)          # 起点 = 原价，之后连续
adj_open/high/low 同理，用同一 factor_norm
adj_volume(t)  = Volume_none(t) / factor_norm(t)         # 量反向调整，保持 P×V 守恒
```

**为什么除以 `factor(T0)`**：后复权原序列基准是上市首日，2004 年的价格可能被放大 10 倍
（600008 放大 9.73 倍），导致「100 万本金买到多少股」不可解释。除以起点因子后，
**起点当天 = 真实价，起点之后按除权连续累积**，金额可解释且回测期内无未来改写
（期后新除权对分子分母同比例影响，比值不变）。

### 3.3 量必须调整的理由

`back` 只改价格（E4）。若不调量，除权日成交量在复权序列里凭空翻倍，
`box.volmult=2.0`（成交量 / 过去 50 日均量）会连续失真数十个交易日。
`adj_volume = Volume / factor` 等价于「按股本标准化」，正是量比想要的口径。

### 3.4 `ForwardFactor` 为什么弃用

官方文档称其为「前复权因子，权息变动当天更新，沿用到下次权息变动」。实测（E3）：
末值 1.000000、随未来除权整体缩放；且 `front/none ÷ F` 在不同日期分别得
0.8087 / 0.9673 / 1.0000，**不是常数**，说明它既不是 `front/none` 也不等于
`back/none` 的规范化。语义无法干净复现 → 不用，改用 `back/none` 直接算，可验证、可审计。

### 3.5 除权日清仓规则下线

`internal/match/match.go` §3.8「真实除权日强制清仓」是**不复权数据的补丁**：
价格序列在除权日断裂，只能清仓逃顶。价格连续后该规则失去依据，且它是
32 笔、**531,767 元假亏**的直接来源（`Qty: pos.Qty` 原始股数 × 除权后价，送转股被丢弃）。

**处置**：删除该分支；`dividend_event` 表保留但不再驱动撮合（降级为审计数据）。

---

## §4 数据缺口与对策（必须写进结论，不许含糊）

| 缺口 | 量级 | 对策 | 残留风险 |
|---|---|---|---|
| **退市股无数据** | 339 只（占沪深A股 6.1%）；tqcenter 行情与除权记录**皆空** | 保留 lday 原价，**不复权**，在库内打标 `adj_status='no_factor'` | 这 339 只在除权日仍有缺口；但它们多数 2005–2010 即退市，且现有基线本就无其除权记录 |
| **不能用 `get_stock_list` 当股票池** | 用它会把 339 只退市股整个删掉 | 股票池以 **lday 清单**为准（5,563 只），tqcenter 只提供因子 | 无（这是修正，不是缺口） |
| **量能未复权** | 全部股票 | `adj_volume = Volume / factor` | 无 |
| **北交所混入** | 357 只 bj | 按 `docs/00` §7 排除（lday 侧本就无 bj） | 无 |
| **`fill_data=False` 的停牌空洞** | 视个股 | 保持空洞（引擎以「当日无 bar」判定停牌），**不得填值** | 若误用 `True` 会把停牌日填成前收，产生假成交 |

> **诚实标注**：即便如此，股票池仍是「当前 lday 里能看到的代码」，2005 年真实上市
> 但连 lday 都没保留的股票依然缺失。这与 `docs/01` §10.1 已有的幸存者偏差声明同级，
> 不因换数据源而消除，也不因换数据源而加重。

---

## §5 重跑前必须固定的全部配置项（逐项 checklist）

> 注：本节的 A1–A7 / B1–B9 / C1–C4 / D1–D4 / E1–E5 是**重建 SOP 配置组编号**，与 `60` 的实验臂（A1/A3/A5/A6、D3/D4 等）、机检 C1–C9 **同名不同物**。

### A. 数据源

| # | 项 | **固定值** |
|---|---|---|
| A1 | 客户端 | 通达信金融终端 V7.73（须启动，tqcenter 依赖） |
| A2 | 接口 | `tqcenter.tq.get_market_data` |
| A3 | 原始价基准 | lday（`D:\new_tdx64\vipdoc\{sh,sz}\lday`），已验证与 tqcenter `none` 逐只相等 |
| A4 | 复权因子来源 | tqcenter `dividend_type='back'` ÷ `'none'` |
| A5 | 股票池 | lday 沪深 A 股 **5,563** 只：`sh60*` / `sh68*` / `sz00*` / `sz30*`；排除 `sh000*` 指数、`sz399*` 指数、基金、B股、bj |
| A6 | 落库表 | 新增 `daily_bar_adj`（`code,date,adj_open,adj_high,adj_low,adj_close,adj_volume,factor_norm,adj_status`） |
| A7 | 原表 | `daily_bar` **保持原价不动**，作为对账基准 |

### B. 复权方式

| # | 项 | **固定值** |
|---|---|---|
| B1 | 复权类型 | **后复权** |
| B2 | 归一化基准日 T0 | **2004-01-02**（该股当日无 bar 则取其后首个有 bar 日；2004 后完全无数据则走 no_factor，见 §8.0-②） |
| B3 | 因子公式 | `factor_norm = (back/none) / (back(T0)/none(T0))` |
| B4 | 价格 | `adj_* = none_* × factor_norm` |
| B5 | 成交量 | `adj_volume = none_volume / factor_norm` |
| B6 | 成交额 | `amount` **不调整**（元，绝对量，用于 `screen.turnover`） |
| B7 | 停牌 | `fill_data=False`，保留空洞 |
| B8 | 除权日清仓 | **下线** |
| B9 | 退市股 | 原价直通（`factor_norm=1`，`adj_status='no_factor'`） |

### C. 参数（49 键，`docs/44` 为准）

| # | 项 | **固定值** |
|---|---|---|
| C1 | 键数 | **49**（42 库内种子 + 7 代码默认），生成器 `scripts/param_ledger.py` 校验 |
| C2 | 变更 | **本轮零变更**。「换口径」与「调参数」必须分开，否则收益变化无法归因 |
| C3 | 仓位制 | 阶梯制 `pos.newhigh_ladder=on`，`档1/档2/档3/档4 = 3/2/1/0.5`，`pos.unit=0.02` |
| C4 | 校验 | 重跑前 `python scripts/check_consistency.py` 必须全绿 |

### D. 时间范围

| # | 项 | **固定值** |
|---|---|---|
| D1 | 数据拉取 | **1990-01-01 ~ 2026-11-04（全量历史，与 `daily_bar` 同轴）**；**不是** 2004 起（会砍断 800 根窗口，见 §8.0-①） |
| D2 | 回测区间 | **2005-01-01 ~ 2018-12-31**（14 年连续单库） |
| D3 | 起始权益 | `equity.base = 1000000` |
| D4 | 交易日历 | 权威 = `sh000001`（ADR-2），用 `get_trading_dates` 交叉校验 |

### E. 撮合与账户口径

| # | 项 | **固定值** |
|---|---|---|
| E1 | 成本价 | `match.slippage = 0.002`、`buy.premium = 1.03`、`match.max_entry_gap = 1.05` |
| E2 | 费用 | 佣金 `0.00025`（最低 5 元）、印花税 `0.001`（仅卖）、过户费 `0.00001` |
| E3 | 成本计算 | 必须 `Σ(position_leg.qty × price)`，**禁用** `avg_cost × qty` |
| E4 | 权益口径 | 账户层用**权益变动**（含浮盈）；单笔/出场讨论用**已实现** |
| E5 | 涨跌停 | 按板块实际规则判定，沿用现有 `isLimitBoard` |

---

## §6 执行步骤与验收判据

| 步 | 动作 | 验收判据（可触发） |
|---|---|---|
| 1 | 补灌 `daily_bar_adj`（5224 只 tqcenter + 339 只原价直通） | `COUNT(DISTINCT code) = 5563`；`adj_status='no_factor'` 恰 339 |
| 2 | 因子合理性 | 抽样 20 只在除权日 `|adj 涨跌幅| < 11%`（连续），同一日 `none` 涨跌幅可超 20% |
| 3 | 守恒校验 | `adj_close × adj_volume` 与 `amount` 相对误差 **< 5%**（VWAP≠收盘，故留余量） |
| 4 | 同源性 | 抽样 50 只 `daily_bar.close` ≡ tqcenter `none`（差分 0） |
| 5 | 引擎切表 | `BarsWindow` 读 `daily_bar_adj`；用 1 年小样本跑通，成交笔数 > 0 |
| 6 | 除权清仓下线 | 全库检索 `sim:dividend` 成交数 = **0** |
| 7 | 一致性 | `check_consistency.py` 机检全绿（C1–C4、C6–C9，无 C5） |
| 8 | 14 年重跑 | 串行、白天跑；完整性判据 `max(date) ≥ 2018-12-01` |

> **⚠️ 切表实施要点（2026-11-04 补，防静默失效）**：
> ① `daily_bar_adj` **只灌个股**（5224+339），**不含指数**（sh000001/sz399* 不在股票池清单里）。
> 引擎切表后，凡读指数的地方（MA60 闸门 `CloseVsMA`、交易日历 `NextDate`、regime 判体制）
> **必须回退读 `daily_bar`**——否则指数读空，闸门静默失效（与 0019 误杀 `tdx.root` 同型：
> 能跑、结果错、不报错）。实施时按 `code` 前缀判定：`sh000*`/`sz399*` 走原表，其余走 adj 表。
> ② `repo_bar.go` 读 `daily_bar` 的 SQL 有 **10+ 处**（BarsWindow/BarCount/BarExists/CloseOn/
> VolumeWindow/AmountWindow/LastDate/PrevDate），不是只改 `BarsWindow` 一处——
> 漏改任何一处都可能静默读旧价。建议抽一个统一的取数入口（按 code 分流），一处改、处处对。

---

## §7 切换时的同步验收

| 位置 | 验收要求 |
|---|---|
| `docs/01` §3、§10.1 | 当前实现与目标已分列；实际切换后同步更新实施状态 |
| `docs/03` ERD | 实施时新增 `daily_bar_adj` 实际表结构，禁止提前写成现有表 |
| `internal/match/match.go` | 后复权切换时删除除权日强制清仓分支，并验证持仓穿越除权日 |
| `internal/store/repo_bar.go` | 所有读行情路径统一按股票/指数分流，避免混读原价与后复权价 |
| `docs/archive/42` | 仅为旧口径历史证据，不再修改历史结论；数据目标由本文承接 |

---

## §8 实施规格（2026-11-04 草案，待确认后转正）

### 8.0 先纠正三处规格错误（不改这三处，灌完就白跑）

**① D1「数据从 2004-01-01 起」会砍断箱体窗口 —— 必须灌全量历史。**

`internal/box/scanner.go:22` 的 `rescanWindow = 800`（约 3 年历史），`planner` 的 52 周新高 `lookback=252`。
2005-01-04 当天要凑够 800 根，必须回溯到 2001 年中。本机实测（`full_hist_10pct.db`）：

| 若 adj 表从…起 | 2005-01-04 能凑够 800 根的股票数 |
|---|---|
| 2002-01-01 | **0** |
| 2004-01-01 | **0** |
| 全量历史 | **1,346** |

若按原 D1 灌 2004 起：回放能跑、不报错，但 2005–2007 每天只有 240 根，箱体识别被左截断 ——
**典型的「能跑、结果错、不报错」**，与 0019 误杀 `tdx.root` 同型。故 D1 改为全量历史。

**② 「数据起点」与「归一化基点 T0」是两件事，原文混在一起。**

- 数据起点 = 灌多少年（要全量，供窗口）。
- T0 = 把后复权序列除以哪个因子（`B2 = 2004-01-02`），只影响价格刻度，不影响收益率。

T0 兜底三档（原文缺失）：

| 情形 | T0 取法 |
|---|---|
| 该股 2004-01-02 有 bar | 2004-01-02 |
| 当日停牌 / 2004 后才上市 | 该股 2004-01-02 **之后**首个有 bar 日 |
| 2004 之后完全无数据（早期退市股） | 不归一化，`factor_norm=1`、`adj_status='no_factor'` |

> **T0 取 2004-01-02 而非「该股上市首日」的理由**：回测起点是 2005 年，T0 贴近起点 → 价格刻度≈真实价，
> 「100 万买到多少股」肉眼可校。取上市首日则老股价格被放大 5–10 倍，读库对账时无法目视判对错。
>
> 两者**收益率等价**，可自行验证：按 T0=A 归一得到序列 `P_A(t) = none(t)·f(t)/f(A)`，
> 取 T0=B 得 `P_B(t) = P_A(t)·f(A)/f(B)`，全期相差一个常数 `k`。等金额建仓时持股数 ∝ 1/P，
> 单笔盈亏 `= 权益·(P_exit/P_entry − 1)`，`k` 在分子分母同时约掉。**只改刻度，不改收益**。

**③ `daily_bar.volume` 单位是「股」不是「手」，注释写错（不影响数据）。**

实测 `sh600519` 2016-05-23：`volume=1,870,618 × close 254.2 = 4.75 亿 ≈ amount`，比值 1.001。
若单位是「手」，乘积只有 475 万，差 100 倍。故 `0001_init.sql:36`、`contract.go:22` 的 `-- 手` 是错误注释。
**订正范围**：`contract.go`、`repo_bar.go`、`docs/03` 三处改为「股」；**`0001_init.sql` 不回改**
（迁移 append-only 铁律，见 docs/06 §2 防线三），旧注释仅作历史留痕。
**tqcenter 返回的 Volume 同为股（E2），两边直接可比，`adj_volume` 无需乘除 100。**

### 8.1 表结构（migration 0032）

```sql
-- 后复权日线：与 daily_bar 同轴（同 code/date 全集），只存个股，不含指数
CREATE TABLE IF NOT EXISTS daily_bar_adj (
    code        TEXT NOT NULL,
    date        DATE NOT NULL,
    adj_open    REAL NOT NULL,
    adj_high    REAL NOT NULL,
    adj_low     REAL NOT NULL,
    adj_close   REAL NOT NULL,
    adj_volume  INTEGER NOT NULL,          -- 股（= round(none_volume / factor_norm)）
    amount      REAL NOT NULL,             -- 元，原值拷贝**不调整**（B6）
    factor_norm REAL NOT NULL,             -- (back/none) / (back(T0)/none(T0))
    adj_status  TEXT NOT NULL,             -- 'ok' | 'no_factor'（原价直通）
    PRIMARY KEY (code, date)
) WITHOUT ROWID;

-- 数据资产状态（不是参数，不进 param 台账）
CREATE TABLE IF NOT EXISTS data_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- 基线身份补数据口径维度（0032 内 ALTER，不新开迁移）
ALTER TABLE baseline ADD COLUMN data_mode TEXT NOT NULL DEFAULT 'raw';
```

设计取舍（逐条给理由，便于反驳）：

| 决策 | 取值 | 理由 |
|---|---|---|
| 建独立表而非给 `daily_bar` 加列 | **独立表** | `daily_bar` 走 `INSERT OR REPLACE`（`repo_bar.go:52`），加列会被**每次 ingest 整行覆盖清空**；且原表必须保持原价作对账基准（A7） |
| 存 `amount` | **存（原值拷贝）** | 免 join，且 §6-3 守恒校验可在同一行内自证（`adj_close×adj_volume ≈ amount`）；它是拷贝不是事实源，重灌即刷新 |
| `WITHOUT ROWID` | **是** | 与 `daily_bar` 一致，PK `(code,date)` 即聚簇，窗口查询走覆盖索引 |
| `adj_status` | `ok` / `no_factor` | 退市股原价直通必须可识别，否则「339 只没复权」会被当成已复权 |
| 不存 `security` 外键 | **不设 FK** | 装载器会遇到 `security` 里没有的退市代码；校验交给覆盖率断言（8.7） |

### 8.2 `data_meta` 契约（决定引擎走哪个模式）

| key | 值 | 写入时机 |
|---|---|---|
| `adj_ready` | `0` / `1` | **只有 §6 全部验收通过才写 1** |
| `adj_t0` | `2004-01-02` | 装载开始 |
| `adj_loader_ver` | 如 `v1` | 装载开始 |
| `adj_loaded_at` | 时间戳 | 通过后 |
| `adj_codes` / `adj_rows` / `adj_no_factor` | 计数 | 通过后 |

**三态语义（这是防静默失效的核心）：**

| 状态 | 判定 | 引擎行为 |
|---|---|---|
| `raw` | 表不存在或无 `adj_ready` 行 | 全部读 `daily_bar`（旧库、`verify_0029_2005.db`、生产照旧可用） |
| `adj` | `adj_ready='1'` | 个股读 `daily_bar_adj`，指数读 `daily_bar` |
| **半成品** | 表有行但 `adj_ready≠1` | **启动即拒绝**，提示「后复权数据未通过验收，禁止回放」 |

没有「读不到就退回原价」这条路径 —— 那正是 0019 的失败形态。

### 8.3 切表规则

**按读取性质分两类，绝不混：**

| 类别 | 方法（`repo_bar.go`） | 读哪张表 |
|---|---|---|
| **价格类**（引擎要连续价） | `BarsWindow` `BarsRange` `CloseOn` `AvgVolume` `AvgAmount` `LastClose` | `priceTable(code)` |
| **日期类**（只要日期轴） | `LatestDate` `PrevDate` `NextDate` `HasBar` `CountBars` `BarCount` `BarCountClosed` | **永远 `daily_bar`** |

日期类不切表的理由：`daily_bar_adj` 与 `daily_bar` 日期轴本应同集，一旦不同集（装载漏股/起止不同），
`MIN(date)`（次新股过滤 `repo_box.go:295`）、`COUNT(*)`（B2 太晚规则）会**静默换判据**。日期轴只认一个源。

`priceTable(code)` 判定：`security.kind='index'`（799 只，`Open` 时预载为 `map[string]bool`）→ `daily_bar`；
其余 → `daily_bar_adj`。**不用前缀猜测**（`sh000*` 里混着指数/其他，`sz399*` 也不全是），用库内 `kind` 事实源。

**`repo_bar.go` 之外的价格读取点（会漏，必须一起切）：**

| 位置 | 内容 | 处置 |
|---|---|---|
| `repo_pool.go:218` | 跟踪池最新收盘（子查询） | **切**（浮盈亏要用连续价） |
| `repo_trade.go:213` | 权益快照最新收盘 | **切** |
| `repo_box.go:295` | `MIN(date)` 次新股过滤 | 不切（日期类） |
| `backtest.go:425/444` | 日期列表 | 不切 |
| `regime.go:212`、`regimesweep.go:290` | 指数 MA60 | 不切（本就是指数，读原表） |

**护栏测试** `TestNoDirectDailyBarPriceRead`：扫全仓非测试 `.go`，凡出现 `FROM daily_bar` 且 SELECT 含
价格列且不在白名单 → 测试失败。把「以后有人新加一条读价 SQL」也挡住。

### 8.4 Bootstrap 必须同步裁剪

`backtest.go:533` 的 progressive 裁剪 `DELETE FROM daily_bar WHERE date >= ? AND code != 'sh000001'`
必须**同步** `DELETE FROM daily_bar_adj ...`。否则 `LastClose`（无日期上界）会读到窗口外的未来价，
progressive 隔离被击穿。

### 8.5 身份：把数据口径写进 baseline

`baseline.data_mode ∈ {raw, adj}`，`RecordBaseline` 时落库。
HARD① 对账要求 `data_mode` + `param_fingerprint` + `engine_sha256` 三者同时相同 ——
raw 库和 adj 库即使参数完全一致也**不可横比**（价格序列本身不同）。

### 8.6 装载器 `scripts/load_bar_adj.py`

| 项 | 规格 |
|---|---|
| 股票池 | `daily_bar` 里按 A5 规则（`sh60*/sh68*/sz00*/sz30*`）实测 **5,561 只**（原文 5,563 是 lday 目录口径，差 2 只以库内为准） |
| 拉取 | 分批 ≤200 只；每股拉 `none` + `back` 两遍；`period='1d'`、`fill_data=False`、`count=-1`、`start_time='19900101'` |
| 因子 | `factor = back_close/none_close`；`factor_norm = factor / factor(T0)` |
| 价格 | `adj_* = none_* × factor_norm` |
| 量 | `adj_volume = round(none_volume / factor_norm)`，下限 1（除权日 0 量不产生 0 股） |
| 额 | 原值拷贝 |
| 无因子股 | tqcenter 返回空 → 从 `daily_bar` 直通，`factor_norm=1`、`adj_status='no_factor'` |
| 幂等 | `INSERT OR REPLACE`，单股一事务；中断后 `--resume` 跳过已完整股票 |
| 失败处理 | 单股失败**不得中止全场**，记 `load_fail.tsv`，最终汇入验收报告；失败股计入未覆盖清单 |
| 两阶段 | 先灌全部行 → 跑 §8.7 验收 → 通过才写 `adj_ready=1` |
| 预估 | 17.96M 行；拉数约 10 分钟（E8: 0.11s/只），写库 10–20 分钟 |

### 8.7 验收判据（补齐 §6，全部可脚本触发）

| # | 判据 | 脚本 |
|---|---|---|
| 1 | `COUNT(DISTINCT code) = 股票池数`（5,561，或装载器实际清单数） | `verify_bar_adj.py` |
| 2 | `adj_status='no_factor'` 的股票数 = 拉取失败数（实测 **339**，与 §5-A5「只在 lday」口径一致） | 同上 |
| 3 | **因子-行动一致性**（2026-11-05 灌库后修订，原「除权日 \|adj 涨跌幅\| < 11%」作废）：抽样 20 只、除权日 ≤15 自然日 Gap，`fn(除权日)/fn(前日)` 与交易所除权除息参考价公式的期望缩放（送股+配股+派息一体：`ex_ref=(10×前收+配×配价−派息)/(10+送+配)`，比值=前收/ex_ref）偏差 ≤5%。原判据不可判定：科创/创业 ±20% 涨跌幅合法，除权前停牌使 adj 累计停牌期大盘真实涨幅（如 sh600352 2005-08-05 股改停牌 12 日，adj +26.7% 为真实总回报） | 同上 |
| 4 | **调整增量误差**（2026-11-05 灌库后修订，原「\|adj换手−amount\|/amount < 5%」作废）：抽 1000 行 `\|adj_close×adj_volume − close×volume\| / (close×volume) ≤ 1%`。原判据误判 raw 固有偏差（收盘≠VWAP，暴涨日 raw 自身偏 5-6%，实测 sz002033 2015-06-04 raw 偏差 6.10% 与 adj 完全相同） | 同上 |
| 5 | 抽样 50 只 `adj_close` 在 T0 当日 ≡ `daily_bar.close`（factor_norm=1） | 同上 |
| 6 | **窗口完整性**（2026-11-05 灌库后修订）：≥800 根（截至 2005-01-04）股数 **adj ≡ daily_bar 同口径查询结果**（实测 1,123）。原「≈1,346」系灌库前另一口径 Universe 的估算，作废 | 同上 |
| 7 | **三态拒绝**：造 `adj_ready=0` 的库 → 回放必须报错退出 | Go 测试（`data_view_test.go`） |
| 8 | `sim:dividend` 成交数 = 0（除权清仓已按 `DataMode()=="raw"` 下线，`match.go` 0032 豁免） | `smoke_0032.py` adj 冒烟 |

> **2026-11-05 实测终值**：`adj_ready=1`，5,561 只（ok 5,222 / no_factor 339），17,962,168 行，
> T0=2004-01-02；验收 7/7 过（判据 7=Go 测试、判据 8=adj 冒烟另验）。23 只「2026 年仍有交易
> 但 TQ 无数据」的 no_factor 股已逐只单股直查复核（券商合并 2026-09 退市潮 + 退市整理股
> 如 sz301139 元道退），全部真退市/合并，零误判。

### 8.8 本轮明确不做

| 项 | 原因 |
|---|---|
| 生产 `serve` 切后复权 | 生产要挂**真实价**止损单；切后复权需 factor 反算回真实价，是独立工程。挂账，回测验收后再议 |
| 前复权 / 不复权 | 8.0 已论证 |
| 调整 `amount` | B6：成交额是绝对金额，`screen.turnover` 要的就是真实成交额 |
| 退市股补因子 | tqcenter 完全没有（E6），只能原价直通 + 打标 |

---

## §9 基线重建 SOP（原 49 §二，2026-11-06 按现行库名/二进制改写）

> 重建只认这一份命令。**旧的 `full_hist_10pct.db` / `darvas_base.exe` 已退役**（见 `archive/49`，勿据此操作）。
> 现行：源库 `var/bt/full_hist_adj.db`｜二进制 `bin/darvas-dev.exe`｜续跑 `--resume`。

```bash
# 冒烟：单年（快速验证方向；跨年不结转，只供对账，不作正式基线）
python scripts/base_run.py --years 2007 --mirrors A

# 代表年（看方向对不对；同样只作分年对账）
python scripts/base_run.py --years 2007,2010,2013,2018 --mirrors A

# 正式重建：连续单库（持仓跨年结转、真实复利，这才是基线本体）
python scripts/base_run.py --cont --from 2005-01-01 --to 2018-12-31 --mirrors A

# 中断后续跑（resume 分支会校验参数冲突；改了参数必须删库重建）
python scripts/base_run.py --cont --from 2005-01-01 --to 2018-12-31 --mirrors A --resume
```

**关键点**

- 单年模式**跑出的库不是基线**，不能拿来做 §10 的判据——判据必须打在**连续单库**上。
- `base_run.py` 已把 4 个实验结论注入项（`newhigh_ladder=on / ma60=stop / max_hold=14 / equity_gate=off`）写死在 `COMMON` 里，与 migration 0025 一致 → **重建不需要 `--set`**。
- 执行器**幂等**：`db_complete()` 判 `equity_snapshot` 非空才跳过，强杀留下的半成品会被识别并删掉重跑。
- **串行，勿并发**（回测并发会污染 scratch 心跳）。
- L0 lifespan 建库（`66`）用现有 adj 数据、零机时（不跑回测），不新增数据规格需求。

**耗时**：实测约 **28 min/年 × 14 年 ≈ 6.6 小时**；臂实验约 5.2h/臂。**只在白天跑**（夜间长进程必被环境回收）。

---

## §10 验收判据（原 49 §三/§四/§六）

> 现在 `check_consistency.py` 只查文档一致性，**不证明账本对**。本节是账本侧的验收。

| 判据 | 内容 | 怎么跑 | 通过标准 |
|---|---|---|---|
| **V1 一致性** | 机检 8 项（C1–C4、C6–C9，无 C5） | `python scripts/check_consistency.py` | 全绿 |
| **V2 账本对账** | 库可读、`equity_snapshot` 非空、参数键 = 基线 | `python scripts/param_ledger.py --db <库>` | 与 `44` 台账逐键一致 |
| **V3 剔除前三不翻负** | 总账剔除前三赢家后仍为正 | 复用 `e17_regime_scale_a.py` 的 `summarize` 口径 | 剔除前三净利 > 0（**铁律级判据**） |
| **V4 年度一致性** | 逐年方向与 `44 §四` 登记一致 | `base_year_summary.py` | 方向一致 |

**V3 口径适配说明**：该脚本只读①引擎产物表（`position`/`position_leg`/`equity_snapshot`，换 adj 后天然复权口径）
②`daily_bar` 里的 `sh000001`（指数不除权，序列天然连续）→ **对后复权天然适配**，只需打在连续单库上。

**阶段判据（A–F 已全部实测通过）**

| 阶段 | 判据 | 状态 |
|---|---|---|
| C 连续单库 | **V5-1 结转**：跨年 open 持仓数 > 0｜**V5-2 复利**：连续库终值 ≠ 链式终值｜**V5-3 逐年**：与单年库逐年收益率差 < 1% | ✅ |
| D 断点续跑 | **V6-1 幂等**：二跑识别「已是最新」0 日续跑｜**V6-2 断点续**：kill 后从 `MAX(ok date)+1` 续、`daily_run` 无重复｜**V6-3 参数拒续**：任何 `--set` 一律拒绝｜**V6-4 换二进制拒续**：引擎 hash 不一致拒绝｜**V6-5 六任务全 ok** | ✅ |
| E 日追加/补跑 | **V7-1 多日补跑**｜**V7-2 空库兜底**｜**V7-3 幂等** | ✅ |
| **G 攻防转换** | 见 `48 §五` 五条判据 | ⚠️ **接口桩 `mode=off`，G 未开工**：前置是 48 新账本重算 + A 系时序（A 系已降支线，以 `60`/`65` 为准） |

> 阶段 A–F 的实施细节见 `archive/50`、`archive/51`、`archive/52`；**代码是事实源**（`base_run.py --cont` / `--resume` / `scheduler.CatchUp`）。

### 10.1 基线身份（F9 的补法，已落地 0027）

| 身份 | 落点 | 示例 |
|---|---|---|
| 参数指纹 `param_fingerprint` | `baseline.param_fingerprint` | param 表 `(key,value)` 排序序列 sha256 |
| `baseline_id` | `baseline.baseline_id` | `2026-11-04-newhigh-ladder-v1` |
| 引擎 hash `engine_sha256` | `baseline.engine_sha256` | `os.Executable()` sha256 |

**两库是不是同一基线，比指纹就知道**（HARD①：跨二进制不可横比，必须先有身份才谈对账）。
**注意**：库内 `engine_sha256` 必须等于你手上那个 exe——`var/bt/darvas_base.exe` 会被 HARD① 直接拒绝。

**引擎常量的归属（2026-11-04 定案）**：`box` 箱体确认 `ConfirmDays=3` **冻结为引擎常量，不设 param 键**——
它已被 `engine_sha256` 覆盖（改这个数必须改代码，二进制 hash 必变，续跑立刻拒绝）。
→ 后果：`04 §9.3` 的 `box.period` 待办**作废**（不再参数化）。

---

## §11 永续化（原 49 §五，已实现，此处只留承诺）

连续单库 = 一条 2005→今 的账本，持仓跨年结转，复利是真实资金约束，中断能续跑。三个改造（① 连续单库 ② 断点续跑 ③ 日追加）全部落地后承诺：

- **幂等**：同一窗口重跑产物全等（`daily_run` UNIQUE + `equity_snapshot` 非空判据兜底）。
- **可续**：中断后从断点续，不等同"从头重跑"，也不丢已跑结果。
- **可对账**：断点前后账本连得上（靠 §10.1 的身份 + 参数指纹）。

**生产端 `CatchUp` 也已改为「从 `max(ok date)+1` 连续补到今日」**，停机一周补 6 天不留洞。

---

## §12 重建红线（原 49 §七，违反必返工）

1. **同一二进制锚**：跨机/跨二进制不可横比。换二进制必须重跑全部账本并换 `baseline_id`。
2. **参数只走 migration**：改默认值必须写 migration（0017 教训），禁止 `--set` 手改当事实源。
3. **回测串行、用 scratch 库**：禁并发，禁污染实时库心跳。
4. **除权只认 `dividend_event`**：价格反推会把跌停误判成除权（0022 已修，别再走回头路）。
5. **`tdx.root` 故意不入库**：机器相关路径，自动探测 D:/H:/I:，换机不静默失效。
6. **旧基线被淘汰直接扔**：不留当对照组（用户铁律）。

---

## 附 · 探针复跑方式

```bash
# 前提：通达信金融终端已启动
python scripts/tq_probe3.py   # 三态语义 + 因子端点
python scripts/tq_probe4.py   # 量能是否复权 + 吞吐
python scripts/tq_probe6.py   # 退市股可拉性
```

> 探针只读、不改库、不写业务表，可随时复跑对账。
