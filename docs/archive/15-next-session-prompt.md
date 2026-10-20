# 给下一个 AI 的接力 Prompt（2026-10-20 08:30）

> ⚠️ **已归档（2026-10-21）**：本文件核心内容已沉淀回 7 个 SSoT，仅作历史记录保留。
> 归档原因见各文件末尾的『归档说明』。如需查阅，本文件不再代表当前事实源。

> 把下面整段复制给新 AI 作为第一条消息。当前仓库已干净（基线 `f8e0085`），文档完整，可直接执行。

---

## 接力 Prompt

你接手「Darvas-box-trading」项目（I:\Darvas-box-trading），这是尼古拉斯·达瓦斯箱体理论的纯 Go 实现。上一个 AI 会话完成了 M8.8 的"文档层 + 参数层"落地（commit `f8e0085` 已推送），**代码层交给你执行**。

### 第一步：必读文档（按顺序）

1. `docs/00-darvas-theory.md` —— 理论宪法，特别注意**第 0 条 · 最高原则「众人恐慌我更恐慌，众人贪婪我更贪婪」**，这是 M8.8 的宪法依据，高于任何单一交易规则。
2. `docs/13-m8.8-breadth-unit-sizing.md` —— M8.8 完整设计，**重点读 §7「当前状态与交接」**，里面有 9 步执行清单。
3. `docs/12-m8.7-regime-root-cause.md` —— M8.7 证伪记录（箱体法无法标指数），避免重蹈覆辙。
4. `PROMPT.md` —— 项目接力事实源，M8.8 条目在 §"里程碑进展"。
5. `HANDOFF.md` —— 交接状态，10-20 段有当前仓库健康度。
6. `docs/14-bt-archive.md` —— 回测库归档说明。

### 第二步：当前仓库状态

- 基线 `f8e0085`，工作区干净（只剩 2 个 exe~ 备份，无碍）。
- `go build ./...` 通过；`go test ./...` 16 包全绿；`check_consistency.py` 全绿（47 键）。
- 生产 serve（`darvas serve`，PID 见 HANDOFF）跑 M8.6 固定仓位版，**不要碰它**。
- 有一个 `git stash`（message: "M8.8 WIP 半完成代码（regime 测试 panic 未修）"），**不要 pop**——里面 `classify_test.go` 的 `nextDate` 有死循环 bug（day>28 重置不进位月份 → 栈溢出）。从零重写更干净；stash 里 `BreadthAgg`/`OpenExposureUnits` 的实现思路可参考。
- migration 0010 已入库（参数已落地），但 `regime.ClassifyDate` 和 `planner.buyOrder` 还是 M8.7 旧实现（读指数），**需要重写**。

### 第三步：执行清单（docs/13 §7.3，按顺序）

1. **重写 `regime.ClassifyDate`** 为宽度法：
   - 给 `BoxRepo` 加 `BreadthAgg(from, to string, minHistory int) (upByDate, downByDate map[string]int, err error)`——SQL 聚合 box 表 `broken_up/broken_down + broken_date`，CTE 先算每只股票 first_date 过滤次新股（JULIANDAY 差 ≥ minHistory×1.5）。
   - 3 日确认：查 `[d-window-confirm+2, d]` 共 `(window+confirm-1)` 日事件，按日聚合，Go 内滚动窗口算每日 ratio，末 `confirm` 日连续 ≥ `bull_thresh` 判牛；否则熊猴合并（熊仅日志区分）。
   - 签名向后兼容：`ClassifyDate(st, date, params, indices) Regime`，内部忽略 params/indices（保留只为不破坏调用方）。

2. **重写 `planner.buyOrder`** 为单位制：
   - 单位股数 = `floor(equity × pos.unit / price / 100) × 100`；
   - 腿1（breakout）qty = 单位股数 × `per_stock_probe_bear`(熊猴) 或 ×2(牛)；
   - 腿2/3（pyramid）qty **仅当 Bull**：递进至 `per_stock_main_max`，单次加码 ≥ `per_stock_main_step`；
   - 熊猴市 pyramid 信号 → 不生成订单（硬规则："试探后不放大"）。

3. **加组合级预算检查**：给 `TradeRepo` 加 `OpenExposureUnits(unitValue float64) (probeUnits, mainUnits float64, err error)`——持仓只有 leg1 = 试探仓；有 leg2+ = 主力仓（全部腿计入 main）。planner.Build 在构造订单前查预算，试探满 5 不再加新试探、主力满 30 不再加码。

4. **重写测试**：
   - `classify_test.go`：宽度法语义（造 box 事件驱动 ratio；注意 `nextDate` 要支持跨月，别重蹈死循环覆辙）。
   - `planner_test.go`：单位制语义（腿1=1单位股、牛市加码、组合顶预算）；旧 risk 公式测试预期值全废，重写。

5. **验证**：`go build ./...` + `go test ./...` 16 包全绿 + `check_consistency.py` 全绿。

6. **提交**：`feat(planner): M8.8 市场宽度标定 + 单位制仓位——宪法第0条落地`。

7. **重编** `darvas_v2.exe`：`go build -o darvas_v2.exe ./cmd/darvas`。

8. **跑回测 2014-2018**（一年一年跑、一年一年记录、一年一年分析）：
   - 用 Windows 计划任务（`Register-ScheduledTask`）避免沙箱回收；bash 里 `schtasks` 被黑名单拦，用 PowerShell 的 `Get/Stop/Start-ScheduledTask`。
   - 源库 `var/bt/full_hist_10pct.db`（probe 10% + addon），库名 `bt_m88_YYYY.db`。
   - `Bootstrap` 会自动 `os.Remove` 旧 scratch 库重建，不需要手动删旧库。
   - 每年记录到 `var/bt/m88_yearly_record.md`：收益、最大回撤、试探笔数、主力笔数（应仅牛市有）、单位占用峰值。

9. **对比 M8.6 进化版**（`bt_evo_2014~2016.db` 保留作对照）：M8.8 是否"熊市几乎空仓 + 牛市吃到主力利润 + 永远留 30% 现金"。

### 第四步：硬约束（违反任何一条=返工）

1. **单一理论**（docs/00 §9）：只实现达瓦斯箱体理论，禁止引入 MALF/立花/均线/MACD/动量等任何其他理论的任何规则。M8.8 用系统自身 `box.Scan` 扫出的箱体事件标牛熊，**不算引入新理论**——它就是箱体引擎的合法输出。
2. **零未来信息**：`ClassifyDate` 只查 `[d-window+1, d]` 完成事件；窗口只取已完成的箱体（broken_date ≤ d），不偷看未来箱体。
3. **观察期冻结**（10-19~11-14）：规则变更走 scratch 测试库，禁止污染实时库心跳；回测用独立库名。
4. **不要 pop stash**：`git stash list` 会看到一个 M8.8 WIP，里面有死循环 bug，从零重写更干净。
5. **生产 serve 不要碰**：PID 见 HANDOFF，它跑 M8.6 固定仓位版，是当前用户的实盘系统。
6. **路径约定**：唯一本地库 `I:\Darvas-box-trading`；bash 工具会剥离 `cd` 前缀，用 `go -C /i/Darvas-box-trading ...` 和 `git -C /i/Darvas-box-trading ...`；通达信数据源 `H:\new_tdx64`（与项目目录无关）。

### 第五步：上线判据（docs/13 §7.4）

- 2008/2018 大熊 → 几乎空仓（混合系数 ≤0.3）；
- 2015 股灾 → 转熊及时；
- 2017 分化蓝筹牛 → 诚实轻仓（接受不启主力，这是宽度法的诚实表达）；
- 牛市年（2006/2009/2014/2020 等）→ 主力仓吃到利润；
- 永远留 30% 现金（15 预备 + 未满顶的主力）。

任一硬指标不达标 → 回滚 M8.6 固定仓位版（当前生产 serve），M8.7/M8.8 整体退为 P1。

### 老回测数据处理

`docs/14-bt-archive.md` 有完整说明。简版：
- **保留**：`full_hist*.db`（源库）、`bt_evo_2014~2016.db`（M8.8 对照基准）；
- **可手动删**（约 18GB）：`bt_2017A/B.db`、`bt_evo_2017.db`、`bt_evo_2018.db`、`bt_prefill.db`、`bt_progressive.db`、`bt_regime.db`；
- **不需要手动删**：M8.8 跑回测时 `Bootstrap` 会自动覆盖 `bt_evo_2017/2018.db`。

### 关键路径速查

| 用途 | 路径 |
|------|------|
| 项目根 | `I:\Darvas-box-trading` |
| 理论宪法 | `docs/00-darvas-theory.md`（第 0 条最高原则） |
| M8.8 设计 | `docs/13-m8.8-breadth-unit-sizing.md`（§7 交接清单） |
| M8.7 证伪 | `docs/12-m8.7-regime-root-cause.md` |
| 接力事实源 | `PROMPT.md` |
| 交接状态 | `HANDOFF.md` |
| 宽度诊断工具 | `scripts/dbg_breadth/main.go`（只读源库，可复现数据依据） |
| 源库 | `var/bt/full_hist_10pct.db` |
| 生产 serve 日志 | `var/serve.log` |
| 记忆目录 | `I:\Darvas-box-trading\.workbuddy\memory\`（.gitignore 已排除） |

---

**开始吧。先读 docs/13 §7，再动手。**
