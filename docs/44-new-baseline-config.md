# 44 · 基线参数台账（唯一事实源）

> **定位**：回答两个问题——「基线到底包含哪些参数」「每个参数的生效值是多少、来源是什么」。
> 本文件是**唯一事实源**；`docs/01 §6` 是需求视角的依据说明，两者键集必须一致（`check_consistency` C6 强制）。
>
> **参数表由 `scripts/param_ledger.py` 从「Go 代码实读键 + 迁移演化」自动生成，不要手改生成区。**
> 改参数的唯一合法路径：改代码默认值 → **写 migration**（用户铁律）→ 跑 `param_ledger.py --write`。
> 更新于 2026-11-03（基线显式化 0026 + 参数台账生成器上线）。

---

## 一、结论先行

1. **基线 = 49 键**，定义是「`internal/` 与 `cmd/` 里 `GetFloat/GetInt/GetString` 实际读取的全部 param 键」。
   历史上出现过三个互不一致的口径，均已废弃：

   | 旧口径 | 数值 | 为什么不能用 |
   |---|---|---|
   | 迁移种子键（C3 检的那个） | 26 | 只数 `INSERT OR IGNORE`，漏了 0018/0025/0026 的普通 `INSERT` |
   | docs/44 旧「39 项」 | 38+1 | = baseA 快照 38 + max_growth_days，**漏了 8 个只走代码默认的键** |
   | 活参数表（docs/01） | 人工维护 | 新增键容易漏登记，与代码漂移 |

2. **49 键的构成**：**42 键有种子**（库里能直接读出来）+ **7 键只走代码默认**。
   0026 把「基线结构本身」的 8 键（阶梯四档/回看/档界/MA60 镜子/复利起点）全部种子化——
   从此基线**可从库里直接读出**，不再依赖「代码默认恰好等于实验结论」。

3. **7 个只走默认的键，都是故意的**：`buy.uptrend_only` / `buy.highest_box_mode` /
   `buy.highest_box_window`（P3-8 已否决，设计上「不注入即关」）、`regime.a.*` 三键
   （A 判据已不决定仓位单位，仅作第 10 维归因原料）、`tdx.root`（机器相关路径，**不该入库**）。

4. **「已进基线」≠「已证明最优」**：49 键里 **已验 14 / 维持 3 / 否决 3 / 事实 6 / 地基 2 / 未验 21**。
   只有 14 项有明确实验结论，其余是工程默认值。

5. **重建基线现在只需「跑完迁移」**：0025 + 0026 落地后，全新库跑完全部 migration 即得新基线，
   **不需要任何 `--set`**（旧文档「必须注入 4 项，漏一项退旧基线」已作废）。

## 二、参数台账（49 键，生成区）

<!-- BEGIN GENERATED:params -->
> 本表由 `scripts/param_ledger.py` 从 **代码实读键 + 迁移演化** 自动生成，不要手改。共 **49 键**（种子 42 / 只走代码默认 7）。


**数据/账户**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `equity.base` | `1000000` | float | 迁移 0026_baseline_explicit.sql | `1000000.0` | 事实 | 初始权益 100 万（回放/复利起点） |
| `tdx.root` | `（代码计算 tdxRoot()）` | string | 代码默认（未种子化） | `（代码计算 tdxRoot()）` | 事实 | 机器相关路径，**故意不入库**（0019 删行）；环境变量 DARVAS_TDX > 参数 > 自动探测，必须指到 vipdoc 一级 |

**买入**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `buy.highest_box_mode` | `off` | string | 代码默认（未种子化） | `off` | 否决 | P3-8 2026-11-03 定稿否决（alltime/trend 三档全否） |
| `buy.highest_box_window` | `120` | int | 代码默认（未种子化） | `120` | 否决 | 随 highest_box_mode 否决；trend 模式专用 |
| `buy.premium` | `1.03` | float | 种子 0001_init.sql | `1.03` | 未验 | A 类：3–8.14% 无梯度；低端 0–3% 另案（旧队列 P1-4） |
| `buy.uptrend_only` | `off` | string | 代码默认（未种子化） | `off` | 否决 | P3-8 2026-11-03 定稿否决，仅回测注入，不种子化 |

**箱体**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `box.max_growth_days` | `6` | int | 迁移 0024_growth_days_prod.sql | `6` | 已验 | LB-4：≤6 日最优（+626 超门槛、3/4 年改善）；0024 投产 |
| `box.min_width` | `0.05` | float | 种子 0015_box_min_width.sql | `0.05` | 未验 | 维 1，梯度未验（成本最大） |
| `box.volmult` | `2.0` | float | 种子 0001_init.sql | `2.0` | 已验 | P1-4：牛松熊紧，2.0 在震荡/牛/结构市与 1.5 相当、2018 更优，维持 2.0 |
| `box.volwindow` | `50` | int | 种子 0001_init.sql | `50` | 未验 | 放量参照基准窗口 |

**卖出**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `sell.discount` | `0.97` | float | 种子 0001_init.sql | `0.97` | 维持 | T3 三档在噪声内，维持 0.97，不宣称最优 |

**持仓**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `pos.equity_gate` | `off` | string | 迁移 0025_new_baseline_inject.sql | `on` | 已验 | B-4 ③ 否决权益预算闸门；0025 种子化 off |
| `pos.equity_limit` | `0.5` | float | 种子 0018_equity_budget.sql | `0.5` | 地基 | 闸门关时不生效，留作「牛冲熊守」动态仓地基 |
| `pos.newhigh.cutoff` | `40` | float | 迁移 0026_baseline_explicit.sql | `40` | 已验 | >40% 归 t4（十四年净亏档） |
| `pos.newhigh.lookback` | `252` | int | 迁移 0026_baseline_explicit.sql | `252` | 已验 | 52 周（交易日），随阶梯一并定案 |
| `pos.newhigh.units.t1` | `3` | float | 迁移 0026_baseline_explicit.sql | `3` | 已验 | M9.0：t1/t2 唯一稳定正贡献 |
| `pos.newhigh.units.t2` | `2` | float | 迁移 0026_baseline_explicit.sql | `2` | 已验 | M9.0：t1/t2 唯一稳定正贡献 |
| `pos.newhigh.units.t3` | `1` | float | 迁移 0026_baseline_explicit.sql | `1` | 已验 | M9.0 落地档位，未单独 A/B |
| `pos.newhigh.units.t4` | `0.5` | float | 迁移 0026_baseline_explicit.sql | `0.5` | 已验 | M9.0：>40% 净亏档，给 0.5 单位 |
| `pos.newhigh_ladder` | `on` | string | 迁移 0025_new_baseline_inject.sql | `off` | 已验 | M9.0 第 5 维：距 52 周高点四档加码；0025 种子化 on |
| `pos.regime_scale.mode` | `off` | string | 迁移 0030_regime_scale_stub.sql | `off` | 未验 | 攻防转换传导开关（0030 接口桩）：off 恒等；on 读 L1 状态机，留待后复权 + 新基线账本重算（docs/48 特征二） |
| `pos.single_limit` | `0.08` | float | 种子 0018_equity_budget.sql | `0.08` | 地基 | 同上，单票 ≤8% 权益 |
| `pos.unit` | `0.02` | float | 种子 0010_breadth_unit_position.sql | `0.02` | 已验 | 1 单位 = 2% 权益（A M1 单位制定案） |
| `position.staledays` | `2` | int | 迁移 0016_staledays_2.sql | `2` | 维持 | P2-6a 五档全在噪声内，维持 2（非「2 最优」） |

**跟踪池**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `pool.blacklist_months` | `12` | int | 迁移 0031_blacklist_months_reseed.sql | `12` | 维持 | 亏损清仓后黑名单冷却月数；0017 种子=12，0019 误删，0031 重新种子化=12（代码默认对齐） |
| `pool.flatboxmax` | `4` | int | 种子 0001_init.sql | `4` | 未验 | 连续 4 箱不涨出池 |
| `pool.max_hold` | `14` | int | 迁移 0025_new_baseline_inject.sql | `0` | 已验 | B-4：14 只胜 7 只与 50% 权益闸门；0025 种子化。20 档待验 |
| `pool.maxwatch` | `8` | int | 种子 0003_pool_lifecycle.sql | `8` | 未验 | 软参考：不拒入池，仅日志告警 |
| `pool.min_watch_days` | `3` | int | 种子 0012_min_watch_days.sql | `3` | 未验 | G1：入池后 3 日内不产生首买 |

**筛选**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `screen.latedays` | `4` | int | 种子 0003_pool_lifecycle.sql | `4` | 未验 | 原书：突破后第 4 日没买=太晚 |
| `screen.rs_excess_min` | `0.20` | float | 种子 0014_rs_excess_slippage_st.sql | `0.20` | 未验 | A1：20 日超额 ≥20pp 得 RS 分，梯度未验 |
| `screen.rswindow` | `20` | int | 种子 0001_init.sql | `20` | 未验 | 相对强度窗口 |
| `screen.turnover` | `50000000` | float | 种子 0001_init.sql | `50000000` | 未验 | A 股流动性门槛 5000 万 |

**撮合**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `match.max_entry_gap` | `1.05` | float | 种子 0013_max_entry_gap.sql | `1.05` | 未验 | P0-2 定 1.05，梯度未验 |
| `match.slippage` | `0.002` | float | 种子 0014_rs_excess_slippage_st.sql | `0.002` | 未验 | 模拟成交买贵卖贱 0.2% |

**大盘闸门**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `gate.ma60.index` | `sh000001` | string | 迁移 0026_baseline_explicit.sql | `sh000001` | 已验 | 单镜上证定案；中证500 换镜未采纳（baseB 是历史对照锚） |
| `gate.ma60.mode` | `stop` | string | 迁移 0025_new_baseline_inject.sql | `off` | 已验 | B-2 全 14 年：指数收盘 < MA60 停买，擂主；0025 种子化 |

**宽度/A 判据**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `regime.a.down_bias` | `0.998` | float | 代码默认（未种子化） | `0.998` | 未验 | A 判据降势阈值，从未调过 |
| `regime.a.lookback` | `60` | int | 代码默认（未种子化） | `60` | 未验 | A 判据回看；**不再决定仓位单位**（阶梯制已接管） |
| `regime.a.up_bias` | `1.002` | float | 代码默认（未种子化） | `1.002` | 未验 | A 判据升势阈值，从未调过 |
| `regime.breadth.bear_thresh` | `0.40` | float | 种子 0010_breadth_unit_position.sql | `0.40` | 未验 | 熊标记 0.40（仅日志） |
| `regime.breadth.bull_thresh` | `0.55` | float | 种子 0010_breadth_unit_position.sql | `0.55` | 未验 | 牛触发 0.55（仅日志/归因） |
| `regime.breadth.confirm` | `3` | int | 种子 0010_breadth_unit_position.sql | `3` | 未验 | 连续确认 3 日 |
| `regime.breadth.min_history` | `60` | int | 种子 0010_breadth_unit_position.sql | `60` | 未验 | 次新过滤 60 根 |
| `regime.breadth.window` | `20` | int | 种子 0010_breadth_unit_position.sql | `20` | 未验 | 宽度窗口（第 10 维归因原料） |

**费用**

| 键 | 生效值 | 类型 | 来源 | 代码默认 | 验证状态 | 依据 |
|---|---|---|---|---|---|---|
| `fee.commission_min` | `5` | float | 种子 0006_fees.sql | `5` | 事实 | 券商最低收费 |
| `fee.commission_rate` | `0.00025` | float | 种子 0006_fees.sql | `0.00025` | 事实 | 券商费率，非策略参数 |
| `fee.stamp_tax` | `0.001` | float | 种子 0006_fees.sql | `0.001` | 事实 | 法定印花税 |
| `fee.transfer_rate` | `0.00001` | float | 种子 0006_fees.sql | `0.00001` | 事实 | 过户费 |
<!-- END GENERATED:params -->

## 三、14 项「已验」逐条说清（防止过度解读）

| 项 | 结论 | 是否「最优」 |
|---|---|---|
| `gate.ma60.mode=stop` | 14 年验证通过（B-2 擂主）：指数收盘 < MA60 停买 | 是（相对换镜/无闸门） |
| `gate.ma60.index=sh000001` | 单镜上证定案；中证500 换镜未采纳 | 是（双镜已裁定终止，baseB 仅历史对照锚） |
| `pool.max_hold=14` | B-4：14 只胜 7 只，也胜 50% 权益闸门 | 是（相对 7 与闸门）。**20 档未验**，见 §4.2 |
| `pos.equity_gate=off` | B-4 ③ 否决权益预算闸门 | 是 |
| `pos.newhigh_ladder=on` | M9.0 甜区落地四档阶梯，现行生产仓位语义 | 是（相对已退役 A M1） |
| `pos.newhigh.units.t1~t4=3/2/1/0.5` | 档位结构已验证（t1/t2 唯一稳定正贡献，t4>40% 净亏） | 结构是；**每档单位数未单独 A/B** |
| `pos.newhigh.lookback/cutoff=252/40` | 随阶梯一并定案 | 是（结构参数） |
| `box.max_growth_days=6` | LB-4：≤6 日最优（+626 超门槛、3/4 年改善） | 是。**「提升器」不是「翻正药」** |
| `box.volmult=2.0` | P1-4：牛松熊紧——1.5 在震荡/牛/结构市最优，**2018 熊市 2.0 更优** | 是（当前市况下）。门槛最优值是市况的函数 |
| `pos.unit=0.02` | 1 单位 = 2% 权益（A M1 单位制定案） | 是 |
| `position.staledays=2` | P2-6a 五档全在噪声内，维持 2 | **否**（维持，不宣称 2 最优） |
| `sell.discount=0.97` | T3 三档在噪声内，维持 0.97 | **否**（维持默认） |

## 四、待复核与待办

### 4.1 待新基线复核的「正面参数」

> 原则：**未复核的正面参数，只作假设先验，不作生产结论。**

| 参数 | 历史「最佳」 | 口径来源 | 复核结论 |
|---|---|---|---|
| 距 52 周新高甜点 | 2–4% 净利 +640,260、胜率 39.3%、PF 4.02 | 旧 E17 库 | **✅ 已复核（2026-11-03）：edge 已散，不采纳。** 详见下 |
| `box.volmult` 1.5 vs 2.0 | 1.5 在震荡/牛/结构市最优，2018 熊市 2.0 优 | 已在新基线（`e17_volmult_run.py` COMMON 与 baseA 100% 一致） | **✅ 维持 2.0**；1.5 是市况函数，非固定档 |
| 量比 ≥1.5 × 甜点 2–4% 组合 | 曾疑为双重确认 | baseA 自带 volmult=2.0 | **伪命题**：成交单量比天然 ≥2.0（预检 1560/1560 = 100%），组合退化为甜点单参数 |

**甜点复核结论**（`scripts/e17_newhigh_sweetspot_baseA.py` → `docs/experiments/e17_newhigh_sweetspot_baseA_report.md`）：

- 表面更强：净利 +702,685（旧 +640,260）、每单位 +1,830、PF 3.62。
- **逐年拆开 edge 已散**：2–4% 的 +702,685 里 **2007 单年占 +526,851（75%）**，除 2007 外 12 年剔除前三全负；
  每单位中位 −465（右尾驱动）。8–10% 的 +302,283 由 2015 单年撑起。
- **判定：不采纳为加仓参数**，与「均等首买 + 不加码」一致，不产生新注入键。
- 真信号：牛市年份利润集中在距高 0–10%，那是**右尾进攻**叙事（docs/48），不是可微调旋钮。

### 4.2 待办

1. ~~生产机对账 + 4 注入项落地~~ **✅ 2026-11-03 完成**：migration 0025 种子化
   `pos.newhigh_ladder=on` / `gate.ma60.mode=stop` / `pool.max_hold=14` / `pos.equity_gate=off`。
2. ~~`box.max_growth_days` 投产~~ **✅ 2026-11-02 完成**：0024 改默认 0→6。
3. **基线显式化** **✅ 2026-11-03 完成**：0026 种子化 8 个结构键（阶梯 6 + MA60 镜子 + 复利起点）。
4. **`pool.max_hold` 14→20（待验证）**：20 档 B-4 未验，且逼近被否决的 50% 权益闸门
   （2.6%×20≈52%）。必须先跑「14 vs 20」单参数 B 类回测。
5. **C6 已接管本表校验**：`check_consistency.py` 的 C6 = 本文件台账 ↔ 代码+迁移，漂移即失败。

## 五、参数层踩过的坑（可复用教训）

1. **0017：种子给了 `max_hold=7`，代码默认 0 变死代码**——沿用 7 个月的「事实基线」其实是种子值，
   代码默认早就没人走过。教训：**种子即事实**，改默认必须写 migration。
2. **0019 误杀 `tdx.root`**：0019 声称清理「29 个死参数」，实际 **28 个真死 + 1 个活参数**
   （`tdx.root` 代码仍在读）。若照旧库直接跑新 exe，迁移会删掉本机 `D:\new_tdx64` 的设置，
   默认值又写死老机 `H:/new_tdx64` → **每日 ingest 静默失效**。
   已修：`tdxRoot()` 改为「环境变量 > 参数 > 自动探测 D:/H:/I:」，本机不再依赖参数行。
3. **三个口径打架**（26 / 38 / 39 / 47 / 48 / 49）：根因是「谁在数种子、谁在数快照、谁在数代码」。
   教训：**参数清单必须由代码反推**，人力维护的表一定会漂。0030 加 `pos.regime_scale.mode` 后
   代码实读口径 47→48；0031 把 `pool.blacklist_months` 从直读 SQL 改走 `GetInt` 三件套口径后 48→49。
4. **0019 第二个误杀 `pool.blacklist_months`**：跟 `tdx.root` 同型——0019 声称清理死参数，
   实际 `repo_trade.go:applyTrackTransition` 仍在读它（定亏损清仓后的黑名单冷却月数）。
   0019 删行后代码走默认 12，恰等于 0017 种子值，**行为不变是纯运气**。
   更隐蔽的是：`param_ledger.py` 只扫 `Get*` 字面量，**直读 SQL 是盲区** → 台账漏登、
   `ParamRepo.Set` 对不存在的 key 静默成功、0027 指纹含/不含此行的库指纹不同（对账噪声）。
   已修：0031 重新种子化 `=12`，代码改走 `GetInt` 三件套口径，`Set` 加固 RowsAffected=0 报错。
5. **迁移写法不统一**：0001/0003/0010 用 `INSERT OR IGNORE`，0018 用裸 `INSERT`，0025/0026 用
   `ON CONFLICT DO UPDATE`。解析脚本必须区分三种语义，否则会漏掉 0018 那批种子。
