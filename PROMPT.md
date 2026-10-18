# PROMPT · Darvas-box-trading 下一阶段强化指令

> 用途：新开发会话（人或AI）开工前的接力提示词，先读完本文件再动手。
> 版本：v1.1（2026-10-18，P0 托管落地 + B4 动态仓位落地后更新）
> 现状：系统已达成「每日无人值守自动运行」（serve 由 Windows 计划任务 DarvasBoxServe 托管，
> 日志 var/serve.log）；v1.0 候选，观察期 2026-10-19 启动至 2026-11-14（满20交易日）。

## 一、你在接手什么

一个只用达瓦斯箱体理论的个人A股交易系统：Go + SQLite 单 exe、本地 HTTP UI、
固定时刻表调度，每日自动跑 ingest → boxscan → screen → poolsync → signal → plan → report。
从 M0 到 M7.4 共 25 个 commit，前身是 7 个阵亡系统（历史与教训见
docs/retrospective-2026/retrospective-2026.html）。

## 二、铁律（违反任何一条=返工）

1. 只用达瓦斯箱体理论，禁止 MA/均线/立花义正/Sperandeo 等任何其他理论入码。
2. migrations append-only，禁止修改已发布迁移脚本。
3. 修改 internal/contract/contract.go 必须同一 commit 内同步 docs/04。
4. 提交前必须跑 `python scripts/check_consistency.py` 和 `go test ./...`，全绿才可提交。
5. modernc SQLite 驱动会把 DATE 列扫成 RFC3339：所有 SELECT 日期列必须 CAST(x AS TEXT)。
6. UpsertBoxes 禁止删旧重插，按 (top, bottom) 匹配复用 DB ID。
7. var/perf_test.db 是全量测试库，禁止破坏性操作。
8. 信号必须首次事件语义（昨日状态对比），禁止条件持续满足时重复生成。
9. 箱体扫描与信号仅限沪深主板/科创板/创业板 A 股。
10. 前端零外部 JS 库；GUI 只读。

## 三、强化路线（按优先级推进）

### P0 · 活着（观察期内的稳定性）
- [x] serve 改 Windows 计划任务托管：独立于 IDE 进程，开机自启、崩溃自动重启。
      （2026-10-18 落地：任务名 DarvasBoxServe，S4U 运行，启动即拉起；stdout/stderr 重定向
      到 var/serve.log；首次启动自动补跑 10-16 整链并落心跳，重启验证干净启动）
- [x] backup 目录 journal 残留清理。（2026-10-18：删 10-13 journal + 旧库 10-13/14/15，
      保留 10-16 基线备份）
- [x] 观察期连续运行盯守：已建「Darvas 观察期每日盯守」自动化（工作日 16:50 查 serve 存活 /
      日报落盘 / TDX 数据新鲜度 / fills 审核提醒，10-19 至 11-14 有效）
- [ ] M7.1 最终验收（2026-11-14 前后满 20 交易日）。

### P1 · 理论最后一公里
- [x] 动态仓位模式判定：已判定"做"并落地（c8f542a）——试探仓钳制 [200股, 权益5%名义] +
      确认放大 5 倍（原书3-10）+ 单股名义上限 20% 不变；参数 risk.probe_min_shares /
      risk.probe_max_pct / risk.scale（migration 0005）。
- [ ] M0.8 第二本书《我在股市活下来了》OCR → 心理纪律章节入库。

### P2 · 攒实盘纪律（模拟执行练习）
- [x] 日常回路工具链：炒股手 CSV 导入器（c4dd42e）+ 每日盯守 automation 提醒（2026-10-18）。
      【习惯项】每日炒股手 CSV 导出 → `darvas fills <file.csv> --yes` → 执行质量审核。
- [ ] 积累 20 个交易日「计划 vs 执行」偏差记录，复盘手痒/滑点/漏执行模式。
- [ ] 养成权益曲线对照习惯（系统自算 realized_pnl，不信任券商盈亏列）。

### P3 · 卫生债
- [x] README M7.4 状态修正（⏳ → ✅，15b5183 已完成移植演练；d4d39ba）。

## 四、验收口径

强化的目标不是加功能，而是通过一个残酷测试：
**连续 20 个交易日无人值守，每天日报准时落盘，每笔计划有留痕，每笔执行有审核。**
做到了，v1.0 才算转正；做不到，回到 P0 找原因。

## 五、档案索引

| 主题 | 位置 |
|------|------|
| 理论宪法 | docs/00-darvas-theory.md |
| 需求与每日时间表 | docs/01-requirements.md |
| 架构（含降级预案） | docs/02-architecture.md |
| 数据模型 | docs/03-erd.md |
| API 契约（仓储事实源 §3.3） | docs/04-api-contract.md |
| 任务划分与校对实录 | docs/05-task-breakdown.md |
| 一致性机制 | docs/06-consistency.md |
| 历程与阵亡墙 | docs/retrospective-2026/retrospective-2026.html |
