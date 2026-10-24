# 五年回测（P0-5）接力 Prompt —— 2026-10-22

> 把下面 `---` 之间整段复制给下一个 AI 作为第一条消息。
> 当前仓库干净（基线 `7911057`），六轮三方对照校订已全部提交推送完毕。

---

## 五年回测接力 Prompt

你接手「Darvas-box-trading」项目（`I:\Darvas-box-trading`），这是尼古拉斯·达瓦斯箱体理论的纯 Go 实现。上一个会话刚完成**六轮三方对照校订**（A1/A2 → B1/B2/B3 → 0017，最新 commit `7911057`），现在要**重跑 2014–2018 五年回测**。旧回测结果（M8.8 R0 轮）已全部作废，因为仓位体系发生了根本重构。

### 零、本轮唯一硬约束：一年一年跑，一年一报告

用户明确要求（最高优先级，违反=返工）：

1. **必须一年一年单独跑**（2014、2015、2016、2017、2018 各一次独立回放，各一个独立 scratch 库）。
2. **每一笔交易记录都要清晰**：这只票在哪个池待过（普通池 normal / 赢家池 winner / 黑名单 blacklist）、仓位是「试探」还是「主力」（新体系下 = 首买下注档位：普通池 1 单位 vs 赢家池 5 单位）。
3. **每一年都要产出统计报告**，至少含：**夏普率、胜率、盈亏比（profit factor）、收益率、最大回撤**，以及当年的逐笔交易明细表（代码/名称/入池日/出池日/池等级/下注档位/开仓价/平仓价/腿数/盈亏）。
4. **回测完一年，就报告一次**（不要等五年全跑完才一次性给结果）。

### 一、前置动作（必须先做，否则回测用的是旧代码）

- 当前 `darvas.exe`（10-22 04:23）是校订后的新代码，但**回测专用二进制 `darvas_bt.exe` 是 10-17 的旧代码**，早于今天所有改动。**必须先重建**：
  ```bash
  go -C /i/Darvas-box-trading build -o var/bt/darvas_p05.exe ./cmd/darvas
  ```
- 重建后先做**快速冒烟**（用户拍板的前置验证）：跑一小段窗口（如 `--from 2014-01-01 --to 2014-03-31`），确认两条新逻辑真实触发：
  1. **赢家池首买 5 单位**（track_level='winner' 的票，首笔成交应为 5 单位而非 1 单位）；
  2. **亏损→黑名单**（普通池票清仓 realized_pnl<0 → track_level 翻成 blacklist + blacklist_until 设为 12 个月后，冷却期内不入池）。
  - 冒烟通过再启动全量；不通过则停下报告，**不要**硬跑五年。

### 二、回测命令与口径（复用上一轮 R0 已验证的入口）

- 源库（只读）：`var/bt/full_hist_10pct.db`（10% 抽样 + addon，全历史在场）。
- 每年一条命令，`--mode regime`（长窗口 prefill 回放 + 分体制报告，`internal/backtest/backtest.go` 的 `ModeRegime`，已存在，勿新造模式）：
  ```bash
  var/bt/darvas_p05.exe backtest --src var/bt/full_hist_10pct.db \
      --from 2014-01-01 --to 2014-12-31 \
      --mode regime --db var/bt/bt_p05_2014.db
  ```
  （2015/2016/2017/2018 依次替换年份与库名。scratch 库名统一 `bt_p05_YYYY.db`，`Bootstrap` 会自动 `os.Remove` 旧 scratch 库重建，勿手动删。）
- **执行方式**：五年约 10 小时，必须走 Windows 计划任务后台跑（沙箱会回收 shell 长进程）。bash 里 `schtasks` 被黑名单拦，用 PowerShell 的 `Get/Stop/Start-ScheduledTask`（或 `Register-ScheduledTask` 新建）。**一年一个任务**，或一个任务里串行 5 年（参考 `scripts/run_p05.bat` 的串行 + 失败即停结构）。
- **失败即停**：任何一年 `rc != 0` 立即停链，绝不把病年写进统计表（上一轮 R0 教训：2017/2018 曾 rc=1 污染指标）。

### 三、逐年统计报告（本轮新增，旧工具不满足，需补脚本）

现有 `scripts/m88_metrics.py` 只出聚合 CSV 一行，**缺**：夏普率、盈亏比、逐笔交易明细、池子轨迹。本轮必须补一个新脚本（建议 `scripts/p05_yearly_report.py`），对每个 `bt_p05_YYYY.db` 产出**该年的完整统计报告**，至少覆盖：

| 指标 | 数据来源（SQL） |
|------|------|
| 收益率 / 最大回撤 | `equity_snapshot`（total 首末、drawdown 最小） |
| **夏普率** | `equity_snapshot` 的日收益率序列：`mean(daily_ret)/std(daily_ret)*sqrt(252)`（无风险利率取 0） |
| 胜率 | `position` closed 且 `realized_pnl>0` / closed 总数 |
| **盈亏比** | `SUM(realized_pnl WHERE >0)/ABS(SUM(realized_pnl WHERE <0))` |
| 逐笔明细 | `position` JOIN `security`（`track_level`/`blacklist_until`）JOIN `position_leg`（leg_no、qty、avg_cost）；「在哪个池」=`track_level` 字段；「试探/主力」= 下注档位（普通池 1 单位=试探，赢家池 5 单位=主力） |

输出建议：每个年份一个 `bt_p05_YYYY_report.md`（人类可读）+ 追加一行到 `var/bt/p05_metrics.csv`（机器可读，含新列 sharpe、profit_factor）。

**注意新体系语义（勿再用旧字段名误导）：**
- 新体系**没有**「腿号金字塔」「试探 5 单位/主力 30 单位预算」了——`leg_no` 仍存在，但「试探/主力」的含义已改为**首买下注档位**：普通池 1 单位、赢家池 5 单位，**一律不加码**（一条腿，只发生在第一次信号）。
- 「在哪个池待过」直接读 `security.track_level`（normal/winner/blacklist）及其时间线，必要时回查 `position`/`ledger` 的开平仓时点来重建「该票在哪个阶段是哪个等级」。

### 四、必读文档（按顺序）

1. `docs/00-darvas-theory.md` —— 理论宪法（第 0 条最高原则、§5.6.5 战绩分级制、§6 池）。
2. `docs/21-three-ai-synthesis-unified-plan.md` —— 三家 AI 综合统一计划，回测的验证判据 E1–E6 在这里。
3. `docs/18/19/20` —— 三家 AI 原始审计（回测要回答的问题都源于此）。
4. `docs/13-m8.8-breadth-unit-sizing.md`（已归档到 archive/）—— 上一轮 M8.8 回测的历史口径，作对照。
5. `PROMPT.md` / `HANDOFF.md` —— 项目接力事实源与交接状态。

### 五、本轮验证判据（回测要回答的核心问题）

来自 docs/21 第三段，五年回测的**决定性实验**是：

- **H1/S1：加跳空上限后是否为正期望**（上一轮 M8.8 五年净利 251,085 中 171% 来自 2 笔跳空追高成交，剔除后 -178,908，**不得作为转正依据**）。
- **E1–E6**（docs/21 列出的验证项，跑完逐年对照）。

每一年报告里，**重点看**：正期望是否成立、跳空成交占比是否仍异常、赢家池 5 单位是否真实吃到利润、黑名单是否有效隔离亏损票。

### 六、硬约束（违反任何一条=返工）

1. **单一理论**（docs/00 §9）：只实现达瓦斯箱体理论，禁止引入 MALF/立花/均线/MACD/动量等任何其他理论规则。
2. **零未来信息**：回放不得偷看未来价格/未来箱体。
3. **观察期冻结**（10-19~11-14）：回测用独立 scratch 库名 `bt_p05_*`，禁止污染生产 `var/darvas.db` 和 `perf_test.db`。
4. **生产 serve 不要碰**：它跑的是用户实盘系统（当前仍在跑），PID 见 HANDOFF。
5. **路径约定**：唯一本地库 `I:\Darvas-box-trading`；bash 工具会剥离 `cd` 前缀，用 `go -C /i/Darvas-box-trading ...` 和 `git -C /i/Darvas-box-trading ...`；通达信数据源 `H:\new_tdx64`（与项目无关）。
6. **不要 pop stash**：`git stash list` 里的 M8.8 WIP 有 nextDate 死循环 bug，从零重写更干净（本轮已无需它，忽略即可）。

### 七、完成标准

- 五年各出一个独立 `bt_p05_YYYY.db` + `bt_p05_YYYY_report.md` + `p05_metrics.csv` 一行。
- 每一年跑完即向用户报告一次（含夏普率/胜率/盈亏比/收益/回撤 + 逐笔明细）。
- 五年跑完汇总一份 `bt_p05_summary.md`，回答 H1/S1 + E1–E6 的核心问题。
- 若某年 rc!=0 或指标异常，停下报告，勿硬跑。

### 关键路径速查

| 用途 | 路径 |
|------|------|
| 项目根 | `I:\Darvas-box-trading` |
| 理论宪法 | `docs/00-darvas-theory.md` |
| 统一计划（验证判据） | `docs/21-three-ai-synthesis-unified-plan.md` |
| 回测入口 | `cmd/darvas/main.go`（`runBacktest`）、`internal/backtest/backtest.go` |
| 源库 | `var/bt/full_hist_10pct.db` |
| 旧指标脚本（需补夏普/盈亏比/逐笔） | `scripts/m88_metrics.py` |
| 串行回测模板 | `scripts/run_p05.bat`（注意：里面写的 `darvas_p05.exe` 尚未构建，需先走第一步） |
| 生产 serve 日志 | `var/serve.log` |
| 记忆目录 | `I:\Darvas-box-trading\.workbuddy\memory\`（.gitignore 已排除） |

---

**开始吧。先做第一步（重建 exe + 冒烟），冒烟通过再启动五年全量，一年一报告。**
