# 07 · M8 回放问题清单

> 来源：M8.1 内置回放器（`darvas backtest`，0.5.0-m8）的 as-of 差分审计。  
> 观察期规则冻结（PROMPT.md M8 附加纪律 #1）：本清单只给证据，不做主。  
> 任何规则层修复一律走周五复盘 + 用户拍板；存储/工程层修复（标 [工程]）不受冻结约束，但仍建议复盘确认。
>
> 审计方法：prefill（全历史K线预灌，未来数据物理在场）与 progressive（逐日灌D日K线，  
> 未来数据物理缺席=地面真值）双模式按同一交易日历重放六任务链，决策输出全等即  
> as-of 纪律成立。逐日箱体哈希定位首个分叉日，决策表语义 dump 定位分叉字段。  
> 静态审计补全运行时无法触发的死路（见 #1）。

## #1 [P0·已修复 2026-10-18] 四类信号在干净 as-of 状态下全部不可达

> **状态：已修复（方案A，2026-10-18，commit 见 git log）**。本条保留为历史记录与  
> 修复依据；下方「根因/证据/影响」描述修复**前**的状态，「修复」段记录方案A落地。

**症状（修复前）**：生产库 `var/darvas.db` 的 `signal` 表 0 行——系统从未发出任何信号；  
box 表 138 条 `(top=0, bottom=0, status='forming')` 残留行（in-pool 股票的当前  
形成中箱体）。10-17 演练的 5 个信号（3 breakout + 2 stop）是引导泄漏（见 #4）  
意外打通的死路，不可作为系统正常工作的证据。

**根因**：突破日引擎输出为 `[box1(broken_up, broken_date=D), box2(0.0, 0.0, forming)]`  
——突破/跌破当日引擎会启动新箱体并 emit，新箱体的 `Top/Bottom` 仍为 0（确认中）。  
`UpsertBoxes` 按 `(top, bottom)` 匹配：box1 复用既有 id（低），box2 `(0,0)` 无  
既有匹配则 INSERT 新 id（高）。`LatestBox(code) ORDER BY id DESC LIMIT 1` 永远  
取到 box2 forming → `signal.evalOne` 的四类判定全部依赖 `LatestBox`：

```
stop:    box.Bottom > 0 && close < box.Bottom            // forming.Bottom=0 → 跳过
failed:  latest.Status == BoxBrokenUp && close < latest.Top  // forming → 跳过
breakout:box.Status == BoxBrokenUp && box.BrokenDate == date  // forming → 跳过
pyramid: 同上                                                  // forming → 跳过
```

→ 四类信号在突破/跌破日**结构性不可达**。同理 `poolsync` 的 `broken_down` 出池  
（#3）也依赖 LatestBox，连带失效。

**证据**：

- 生产 `var/darvas.db`：`SELECT COUNT(*) FROM signal` = 0；`SELECT COUNT(*) FROM box WHERE top=0 AND bottom=0` = 138；`SELECT stage, COUNT(*) FROM watch GROUP BY stage` = `('watching', 125)`（无 exited/holding → 无信号无成交）。
- `var/perf_test.db` 三只演练信号股阶梯末行均为 id 更大的 forming 行：
  - sh600052：`(9505, 3.95, 3.6, broken_up, '2026-09-21', '2026-10-09')` → `(9506, 0, 0, forming, '')`
  - sh603258：`(2440, 15.30, 13.55, broken_up, '2026-09-28', '2026-10-14')` → `(2441, 0, 0, forming, '')`
  - sz300411：`(7711, 9.17, 7.92, broken_up, '2026-09-16', '2026-10-16')` → `(7712, 0, 0, forming, '')`
- M8.1 合成差分测试 `TestSyntheticAsOfDifferential` 锁定此行为：突破日 t15  
  screen 候选数立即跌到 0、整窗 signal=0、plan_order=0（详见 `internal/backtest/backtest_test.go` 注释）。

**影响**：观察期（2026-10-19 起 20 交易日）将永远是「每天日报落盘、零信号、零  
订单、零成交」——验收口径（连续 20 日无人值守、每笔计划有留痕）的「每笔计划」  
项将空载。系统当前仅 `screen → 候选池 → 跟踪池` 链路有效，事件驱动的全部  
交易逻辑（买卖信号、止损上移、金字塔加码、出池）均为死路。

**修复方向（待用户拍板，非回放器做主）**：

- 方案A（最小侵入）：`BoxRepo` 新增 `LatestNonFormingBox(code)`（`status != 'forming'` 的最新箱体）；`signal.evalOne` / `poolsync` 改用之。维持现有 `LatestBox` 供 GUI/报告显示当前形成态。
- 方案B（更彻底）：`UpsertBoxes` 不持久化 forming 箱体（仅 confirmed/broken/invalid），把"当前形成态"作为引擎运行时状态而非 DB 行——避免 (0,0) 行累积与 stale 删除噪音。撞铁律 6（按 (top,bottom) 复用）需重审。

### 修复（方案A，2026-10-18 落地）

用户拍板方案A（最小爆炸半径 + 可逆 + 让生产对齐单元测试已假设的语义），同日实施：

- `internal/store/repo_box.go`：新增 `LatestNonFormingBox(code)`——  
  `status IN ('confirmed','broken_up','broken_down') ORDER BY id DESC LIMIT 1`  
  （排除 forming 暂态 + invalid 作废，避免 invalid 的 bottom spuriously 触发 stop）。  
  `contract.BoxRepo` 接口虽声明但全代码无人作为类型使用，故只加到具体 struct，不碰契约、不碰 docs/04。
- 4 处决策调用点 `LatestBox → LatestNonFormingBox`：
  - `internal/signal/signal.go` evalOne（#1 核心）
  - `internal/pool/pool.go` watching 循环（#3 出池判定 + current_box_id）
  - `internal/planner/planner.go` 买卖箱体取值（box.Top/Bottom=0 致 buyOrder 直接 return nil 的连带死路）
  - `internal/planner/planner.go` raiseStops（forming bottom=0 致止损上移永不触发的连带死路）
- 保留 `LatestBox`：`screener.go`（box_pass，突破即从 watching 候选毕业，符合语义）、  
  `pool.go` 入池取 boxID（候选已过 screen 的 LatestBox=confirmed）、GUI（展示当前形成态）。

**修复后验证**（2026-10-18，`darvas backtest --src var/perf_test.db --from 2026-09-11 --to 2026-10-16 --mode verify`，14m17s）：

- 真实 20 日 prefill ≡ progressive 决策输出**全等**（as-of 未回归 ✓）。
- 信号不再全 0：09-17、09-21 两日各 1 个 breakout 信号 + 1 笔买单（两模式一致）。
- #3 连带复活：poolsync exited=1 出现在 09-16/09-28/10-08（跌破出池死路通）；  
  连带 pool 开始演化（旧股出池→新股入池，entered=1 多日）。
- 合成差分测试 `TestSyntheticAsOfDifferential` 更新期望并 PASS：t15 出 breakout 信号 +  
  买单（leg1, qty 4400, exec t16, t17 过期，watch watching→pending→watching）。
- 现有 signal/pool/planner/store 单测全绿（修复对齐了单元测试已假设的语义）。

**残留（未修，观察期内排期）**：#2（watch.BoxCount 不递增 → flat_too_long 出池规则死，  
独立逻辑，方案A 不碰）。#6（UpsertBoxes prev_box_id FK 自引用保护缺失）已于  
2026-10-19 修复（见 #6 修复段）。

## #2 [P1] watch.BoxCount 从不递增

**症状**：`watch.BoxCount` 仅在 `pool.Sync` 入池时置 0（pool.go:90），全代码无  
任何递增点。`grep BoxCount internal/ --exclude-dirs=_test.go` 确认。

**影响**：

- FR-303「连续 N 箱不涨出池」失效（`if w.BoxCount >= flatMax` 恒假）；
- 金字塔腿数上限 `if w.BoxCount < maxLegs` 恒真——本应钳制加码，但因 #1 pyramid  
  信号本就不可达，影响暂未暴露。

**修复方向**：在 boxscan 状态迁移确认时（box1 confirmed→broken_up/down），按  
关联 watch 累加 BoxCount。需关联当前形成态箱体与 watch.current_box_id 的迁移  
判定（与 #1 方案A 配套）。

## #3 [P1·已修复 2026-10-18] poolsync 自动出池两条路径均不可达

> **状态：已修复**（随 #1 方案A 一并——poolsync watching 循环改用 LatestNonFormingBox）。

**症状（修复前）**：`pool.Sync` 的 `broken_down` 出池（pool.go:111）与 `flat_too_long`  
出池（pool.go:119）均依赖 `LatestBox` 的状态——前者撞 #1（forming 不破），  
后者撞 #2（BoxCount 不增）。生产 watch 表 0 exited 佐证。

**修复**：poolsync watching 循环的 `LatestBox` 改用 `LatestNonFormingBox` → broken_down  
出池判定可达。修复后真实 20 日回放 exited=1 出现在 09-16/09-28/10-08 三日（两模式一致）。  
`flat_too_long` 出池仍死（依赖 #2 BoxCount，未修）。

## #4 [P2·已修正] 演练引导未清 box 表 → 候选池被未来箱体污染

**症状**：10-17 演练的 reset 清空了 `signal/plan_order/trade_plan/trade_fill/
position_leg/position/equity_snapshot/watch/candidate/daily_run` 共 10 张  
事务态表，**遗漏了 box**——回放从「未来箱体状态」（perf_test 截至 10-16 的  
全量阶梯）上启动。screen 的 `LatestBox(code)` 对全市场 5561 只股票中的多数  
返回 10-16 的未来箱体 → candidate 列表被污染（如 09-11 当日 390 候选远超  
干净回放的真实值）。

**已修正**：M8.1 `Bootstrap.wipeTables` 把 `box` 列入清空序列（子先于父 FK  
顺序：trade_review → trade_fill → position_leg → position → plan_order →  
trade_plan → signal → equity_snapshot → candidate → watch → box → daily_run）。  
箱体现在必须由回放逐日重算累积（as-of by construction）。

**教训**：状态表清单必须显式枚举并复核——任何遗漏的"状态"表都会让回放从  
未来状态上启动，差分测试会暴露（prefill 残留 vs progressive 清空的对比会  
立即分叉）。本项由 M8.1 差分测试守卫。

## #5 [P2·已修正] 演练 report 写入生产 var/reports

**症状**：10-17 演练的 daily report 直接写入 `var/reports/daily_2026-09-11.html`  
…`daily_2026-10-16.html`，覆盖了生产日报（10-15/10-16 后来被真实 serve 重写  
掩盖，但中间日期仍残留演练产物）。

**已修正**：M8.1 `pipeline.SetReportDir(dir)` 重定向报告输出；`Replay` 按  
scratch 库名派生目录（`<scratch去.db>_reports/`）。生产路径 `run/serve/rerun`  
仍用默认 `var/reports` 不受影响。

## #6 [P2·工程·已修复 2026-10-19] UpsertBoxes stale 删除漏 box.prev_box_id 自引用保护

**症状**：`UpsertBoxes` 末尾删除本次未复用的 stale 箱体时，`NOT IN` 子句只  
保护了 `position.entry_box_id` / `watch.current_box_id` / `signal.box_id` 三处  
外键，遗漏了 `box.prev_box_id` 自引用。被幸存箱体引用为前驱的 stale 箱体删除  
触发 FK 787（FOREIGN KEY constraint failed），整个 UpsertBoxes 事务回滚，该股票  
当日箱体更新全部丢失。10-17 演练 `sz300324` 从 09-14 起每日复现此错误。

**影响**：被引用为前驱的箱体无法清理，长期累积可能拖慢 boxscan；该股票的  
箱体阶梯滞后于真实状态。非交易规则，属存储 bug，建议观察期后修复。

**修复方向（待复盘确认）**：DELETE WHERE 子句追加 `AND id NOT IN (SELECT
prev_box_id FROM box WHERE code=? AND prev_box_id IS NOT NULL)`。撞铁律 6  
（按 (top,bottom) 复用）需重审是否影响阶梯链重建。

### 修复（2026-10-19 落地）

按上述方向实施（工程 bug，不撞规则冻结）：

- `internal/store/repo_box.go` UpsertBoxes stale DELETE 追加第四处 NOT IN 保护：
  `SELECT prev_box_id FROM box WHERE code = ? AND prev_box_id IS NOT NULL`，
  与既有 position/watch/signal 三处保护同款写法。
- 触发路径还原：靠 signal/watch/position FK 保护幸存的箱体（不在重算批次内，
  prev 不被 UPDATE 重写），其 prev_box_id 指向的 stale 箱体被 DELETE 时触发
  FK 787 → 整个事务回滚、该股当日箱体更新全丢。
- 回归测试 `TestUpsertBoxesStaleDeleteKeepsPrevRef`（repo_box_test.go）：
  修复前以完全相同的 FK 787 报错失败，修复后通过；阶梯语义 = 前驱被引用的
  stale 箱体保留（与 position/watch/signal 引用保留一致的历史事实语义）。
- 铁律 6 不受影响：(top,bottom) 复用策略未动，保护只影响 DELETE 的筛选集。
- 修后验证（2026-10-19 重跑 `darvas backtest --src var/perf_test.db --from
  2026-09-11 --to 2026-10-16 --mode verify`，12m17s）：
  - `go test ./...` 全绿 + `check_consistency.py` 全部一致；
  - 差分全等 ✓（20 交易日 prefill ≡ progressive）；
  - 信号与上轮（修复前 #1 方案A 验证）一致：09-17 sz300739、09-21 sz300473
    各 1 个 breakout（两模式一致）——#6 修复未改变本窗口决策输出；
  - sz300411 期望落空的原因查清：其 (9.17,7.92) 箱体已正确持久化
    （broken_up 2026-10-16，即回放末日），但它在整个回放窗口从未入跟踪池
    （池 8/8 满员按 screen 排名竞争）→ 信号不发属规则层结果，非存储 bug。

## #7 [P3] weekly 周报 LastClose 无日期界

**症状**：`internal/reporter/weekly.go:52` 用 `BarRepo.LastClose(code)` 读  
"最新收盘"——该方法 SQL 为 `ORDER BY date DESC LIMIT 1`，无日期上界。回放  
场景若对历史窗口跑 weekly，会读到 DB 中 D 之后的未来收盘价。

**影响**：M8.3 分体制报告若复用 weekly 路径会引入未来数据。当前每日链不  
含 weekly，无即时风险。

**修复方向**：weekly 周报改为接受 anchor date 参数，全部价格读走  
`CloseOn(code, date)` 或 `lastCloseBefore(code, date)`。

## #8 [P3] security.status='active' 是"今日快照" → 幸存者偏差方向

**症状**：`ActiveStockCodes` 按 `kind='stock' AND board IN ('main','gem','star')
AND status='active'` 过滤——`status` 是源库当前快照（perf_test 的 active 名单  
= 截至某日的上市状态）。回放历史窗口时：

- 尚未上市的股票：无 ≤D 的 K线，自然被 boxscan/screen 淘汰 ✓；
- 当时已上市、现已退市的股票：`status` 可能非 'active' → 被排除 → 幸存者偏差  
  方向（漏掉了后来退市的样本）。

**影响**：M8.3 分体制回测的样本完整性需评估；M8.1 as-of 审计不涉此（两模式  
宇宙相同 → 不分叉）。

**修复方向**：security 表若记录 `list_date`/`delist_date`（视数据源），  
`ActiveStockCodes` 改为按回放日过滤；或回放期接受少量幸存者偏差并标注。

## #9 [信息·豁免] planner NextDate 读 sh000001 未来日历

**症状**：`planner.nextTradingDate` 通过 `BarRepo.NextDate('sh000001', planDate)`  
读取 D 之后的最近交易日——未来日历信息。

**豁免**：ADR-2（docs/02 §6）明文豁免——交易日历是公开先验信息（节假日历  
提前发布），非价格泄漏。回放场景使用精确日历（实盘当日无未来数据时降级为  
"跳过周末"近似，由订单过期机制兜底假日偏差）。M8.1 progressive 模式预灌  
sh000001 全历史，保持两模式 NextDate 行为一致，差分测试不被日历差异干扰。

**结论**：非违规，记录在案以供未来审计复核。

---

## 审计结论（as-of 差分）

**已通过**——两轮差分均 prefill ≡ progressive 全等：

1. **合成数据**（`TestSyntheticAsOfDifferential`，CI 常驻）：构造能走完「箱体确认→入池→  
   放量突破」全链条的合成序列 + 未来才物化的泄漏陷阱股，17 个回放日决策输出全等。
2. **真实数据**（`darvas backtest --src var/perf_test.db --from 2026-09-11 --to 2026-10-16 --mode verify`，  
   2026-10-18 运行，耗时 14m17s，#1 方案A 修复后）：20 个交易日双模式重放，逐日 boxscan hit 数 /  
   screen 候选数 / poolsync / signal / plan / equity 全部一致。修复后信号不再全 0：  
   09-17、09-21 两日各出 1 个 breakout 信号 + 1 笔买单（两模式一致）；#3 连带复活，  
   exited=1 出现在 09-16/09-28/10-08 三日。

**as-of 纪律成立**：六任务链未读到任何未来K线（#9 豁免的 sh000001 日历读除外）。  
差分方法论的守卫边界：若未来任何任务偷读未来K线，prefill（未来在场）与  
progressive（未来缺席）的决策输出必然分叉——首个分叉日由逐日箱体哈希定位，  
分叉字段由决策表语义 dump 定位。

**#1 方案A 修复后**：事件驱动链路恢复——breakout 信号会出、跌破出池会触发、  
pool 会演化。观察期（2026-10-19 启动）可在修复后的系统上运行。#6（UpsertBoxes  
FK 自引用保护）已于 2026-10-19 修复；残留 #2（flat_too_long 出池死）不阻塞观察期，  
留观至周五复盘。

**附：真实回放与 10-17 演练的对照**（佐证 #4 引导泄漏）：

| 日期                                              | 演练 screen（box 表带未来状态） | 干净回放 screen（box 全清重算） |
| ----------------------------------------------- | --------------------- | --------------------- |
| 09-11                                           | 390                   | 130                   |
| 09-14                                           | 391                   | 191                   |
| 09-15                                           | 427                   | 232                   |
| 09-16                                           | 469                   | 272                   |
| 10-09                                           | 1013                  | 885                   |
| 10-16                                           | 875                   | 1043                  |
| 演练的候选数被未来箱体污染（不可作 as-of 地面真值）；干净回放才是 as-of 正确值。 |                       |                       |
| 两模式全等证明：干净回放的结果与未来K线是否存在无关 → 任务链自律。             |                       |                       |
