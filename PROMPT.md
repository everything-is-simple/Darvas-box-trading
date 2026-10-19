# PROMPT · Darvas-box-trading 下一阶段强化指令

> 用途：新开发会话（人或AI）开工前的接力提示词，先读完本文件再动手。
> 版本：v1.7（2026-10-19，M8 修复链全部清零 + M8.2 模拟撮合器立项 + 确立「历史数据
> 并行校准」方向——观察期只验人与机器，规则参数用 36 年历史回放提前校）
> 现状：系统已达成「每日无人值守自动运行」（serve 由 Windows 计划任务 DarvasBoxServe 托管，
> 日志 var/serve.log，运行版本与代码版本均已 0.5.0-m8——M8 新增 backtest 子命令 +
> #1/#6/#2 完整修复链）；全部 commit 已推送 GitHub（everything-is-simple/Darvas-box-trading）；
> v1.0 候选，观察期 2026-10-19 启动至 2026-11-14（满20交易日），期间规则冻结。
> M8 修复链（2026-10-19 全部清零）：docs/07 #1 四类信号死路（LatestNonFormingBox，91bdd1e）、
> #6 UpsertBoxes prev_box_id FK 自引用（107dded+d55492d）、#2 watch.BoxCount 换箱累加 +
> 金字塔钳制改 LegCountByCode 真实腿数（0ce1a34+58e0e83，用户拍板例外授权）；#3 随 #1 修。
> 三轮 verify 差分全等；sz300411 无信号根因=全程未入池（池8/8满员）属规则层，非 bug。
> 数据厚度（历史校准依据）：通达信 H:\new_tdx64\vipdoc 日线覆盖 1990-12-19 ~ 今，约 36 年。

## 一、你在接手什么

一个只用达瓦斯箱体理论的个人A股交易系统：Go + SQLite 单 exe、本地 HTTP UI、
固定时刻表调度，每日自动跑 ingest → boxscan → screen → poolsync → signal → plan → report。
从 M0 到 M7.4 共 34 个 commit（已全部推送 GitHub），前身是 7 个阵亡系统（历史与教训见
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
- [x] 观察期连续运行盯守：「Darvas 观察期每日盯守」自动化（工作日 **20:00** 查 serve 存活 /
      日报落盘 / TDX 数据新鲜度 / fills 审核提醒，10-19 至 11-14 有效）。
      （2026-10-18 时刻集中一处管理：§4 唯一事实源，调度器/CLI/GUI/测试/C4 派生；
      流水线 16:00→19:00、盯守 20:00、盘前 08:00/08:20、周六周报 08:00）
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

### P4 · M8 内置回放引擎（2026-10-18 立项；观察期内只做骨架，规则冻结）

背景：系统规则已机械化，观察期只验「人」（执行链路）；系统在牛/熊/猴体制下的历史
表现交给回放加速。外部回测框架经 2026-10-18 调研全部否决（backtrader 2023-04 冻结、
vectorbt 开源版仅维护/PRO 付费、backtesting.py 单标的、Nautilus/LEAN 过重；且规则需
Python 重写=双重事实源、A股摩擦不送、破坏单 exe 分发）。判决与范围修订见
docs/01 v1.1 §9。

**2026-10-19 方向升级（历史数据并行校准，walk-forward）**：观察期的规则冻结只冻
「生产库里正在跑的规则」，不冻「scratch 库的历史校准」。两条线并行——
①生产系统继续无人值守收集真实执行数据（验人+验运维）；②用 36 年历史回放提前校
规则参数（箱体 volmult / buy.premium / risk.scale / 池容量 / 大盘闸门证据）。方法论
= walk-forward 滚动验证：历史切成若干段（按牛/熊/猴体制 + 制度变迁 1996涨跌停/2005股改/
2019注册制），前段调参、后段验证，参数稳定才算真参数。唯一前置依赖 = M8.2 撮合器。

方案：Go 内置 `darvas backtest`——scratch 测试库灌全量历史 K 线（perf_test.db 模式，
独立库），按交易日历逐日重放 as-of 六任务链（生产代码路径、规则零重写），新增模拟
撮合器（D+1 OHLC、T+1/涨跌停/整手/印花税）接入现成 ledger（订单匹配/止损上移/
金字塔/恒等式审计全复用），分体制输出报告。

- [x] M8.1 回放骨架：scratch DB 引导 + 交易日历循环。首任务=审计六任务 as-of 纪律
      （任何任务读到「未来」K线即返工）。
      （2026-10-18 落地：`internal/backtest` + CLI `darvas backtest --mode verify`；
      prefill/progressive 双模式差分审计通过——六任务链未读到任何未来K线；
      审计同时发现事件驱动链路三条死路 docs/07 #1-#3，规则冻结期不改待用户拍板）
- [ ] M8.2 模拟撮合器：D+1 OHLC 成交语义 + A股摩擦，接 ledger / B4 仓位金字塔。
      （2026-10-19 立项开工：设计文档 docs/08-m8.2-matcher.md，待用户确认后进代码。
      核心=「人工贴成交单 → 当日 OHLC 模拟触发」，撮合结果喂 ledger.ApplyFill，
      持仓状态机/金字塔/止损上移/恒等式审计全复用，规则零重写）
- [x] M8.3 体制运行器：用客观规则**事先**标注牛/熊/猴窗口（禁摘樱桃），分体制
      权益曲线/回撤/胜率/暴露时间 HTML 报告。建议首测：2014-07~2015-06 牛、
      2015-06~2016-02 崩、2018 熊、2019-2020 结构牛、2021-2024 猴、2024-09 至今。
      （2026-10-19 落地：internal/regime 包 + `--mode regime`；体制标注用 sh000001
      过去 N 日动量（默认 120 日 ±15%，param 可配），设计见 docs/09。跑全历史需先
      从 TDX 灌多年数据）
- [ ] M8.4（可选）参数扫描：box.volmult / buy.premium / risk.scale 敏感性矩阵。

M8 附加纪律（违反任何一条=返工）：
1. 观察期（10-19~11-14）内规则冻结：回放只产出「问题清单」，改规则一律走周五
   复盘 + 用户拍板，禁止热改。
2. 回放必须跑独立测试库，禁止污染实时库心跳（CatchUp 缺心跳教训）。
3. 若回放显示熊市流血 →「大盘闸门」属新规则，撞铁律 §9 单一理论约束，由用户
   拍板定夺（达瓦斯「只在上涨市场买入」是否原书纪律）；回放只给证据，不做主。

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
| M8 回放问题清单（as-of 审计证据 + 死路 #1-#3） | docs/07-m8-issue-list.md |
| M8.2 模拟撮合器设计（成交语义 + 复权日处理 + 数据源调研） | docs/08-m8.2-matcher.md |
| M8.3 体制运行器设计（牛熊猴标注 + 分体制报告） | docs/09-m8.3-regime.md |
| 历程与阵亡墙 | docs/retrospective-2026/retrospective-2026.html |
