# Darvas-box-trading

> **达瓦斯箱体交易系统** —— 把尼古拉斯·达瓦斯的自律变成软件
> Go + SQLite + 本地HTTP GUI · 单文件 · 可移植 · 每天收盘后30分钟

---

## 这是什么

复刻《我如何在股市赚了200万》的完整交易体系，只用这一套理论：

```
每个交易日在明确划分的时间点做固定的事情：

数据同步 → 全市场箱体重算 → 初选四条件筛选
→ 跟踪池状态迁移 → 信号判定 → 交易计划生成
→ 决策日报 + 备份 → 次日盘前人工挂单 → 午间录成交 → 周而复始

> 各环节触发时刻集中管理于 `docs/01-requirements.md` §4（唯一事实源；调度器/CLI/GUI/测试/C4 全部派生）。
```

系统每天用书面计划回答**六问**：买什么？何时买？买多少？卖什么？何时卖？卖多少？

- 买什么：跟踪池中放量突破箱体上轨的股票
- 何时买：次日停损买单挂在箱体上轨×1.03
- 买多少：**阶梯制**——按「距 52 周高点」分四档，档1(0–5%) 3 单位 / 档2(5–10%) 2 单位 / 档3(10–20%) 1 单位 / 档4(20–40%) 0.5 单位、>40% 拒买（1 单位 = 2% 权益；`pos.newhigh_ladder=on`。**已退役的 A M1「升势 2 单位/其余 1 单位」不再使用**）
- 卖什么：收盘跌破最新箱体下轨的持仓
- 何时卖：次日止损卖单挂在下轨×0.97
- 卖多少：全部，一次清仓（达瓦斯纪律）

**单一理论原则**：本系统只实现达瓦斯箱体理论（见`docs/00-darvas-theory.md`），不引入MALF/立花/均线/MACD等任何其他理论的任何规则。

---

## 安装

```bash
# 要求：Go 1.22+（纯Go SQLite驱动，无CGO）；Windows x64
go build -o darvas.exe ./cmd/darvas
```

运行时只需要两个文件（同平台拷走即用，NFR-2）：

- `darvas.exe`
- `var/darvas.db`（首次运行自动创建）

数据源：通达信 vipdoc 日线（自动探测 `D:/H:/I:/new_tdx64/vipdoc`，可用 `darvas param set tdx.root <路径>` 或环境变量 `DARVAS_TDX` 覆盖；**必须指到 vipdoc 一级**，否则名称全空且不报错）。**每个交易日收盘后在通达信客户端点一次「下载数据」（当日数据收盘后才可下载，时点见 docs/01 §4.2）**，这是系统唯一的数据新鲜度前提。

---

## 快速开始

```bash
darvas.exe ingest --since full      # ① 首次全量导入（~10分钟，幂等可重跑）
darvas.exe run --date 2026-10-16    # ② 完整盘后流水线（七任务+日报+备份）
darvas.exe ui                       # ③ 浏览器打开GUI四视图（自动弹窗）
darvas.exe serve                    # ④ 或者：常驻模式（调度+GUI+自动备份）
```

产出：

- `var/reports/daily_YYYYMMDD.html` —— 决策日报（含明日交易计划）
- `var/backup/darvas_YYYYMMDD.db` —— 每日VACUUM备份（保留30份滚动）
- `var/darvas.db` —— SQLite数据库

---

## 每日流程（无人值守推荐 serve）

| 环节 | 谁 | 做什么 |
|------|----|--------|
| 数据下载 | 人 | 通达信客户端「下载数据」（当日数据收盘后才可下载） |
| 盘后流水线 | darvas serve | 按时刻表自动跑七任务链（错过/失败自动补跑重试；时点见 docs/01 §4.1） |
| 浏览复核 | 人 | 浏览候选池/跟踪池，可选基本面复核 |
| 看计划 | 人 | `darvas ui` 看仪表盘/《明日交易计划》，或直接开日报HTML |
| 次日盘前 | 人 | 按计划在券商APP挂集合竞价/条件单（触发价/委托价/数量系统已算好；时点见 docs/01 §4.2） |
| 午间 | 人 | `darvas fills` 粘贴券商成交文本或炒股手CSV（午间录入，幂等去重） |

> 各环节具体时刻集中管理于 `docs/01-requirements.md` §4（唯一事实源，调度器/CLI/GUI/测试/C4 全部派生）。

serve 调度语义（M7.1）：

- 每个触发点到达时，把「时刻已过且尚未ok」的任务按依赖链**一次性补齐**（中途开机/电脑关过机都不怕）
- ingest 失败 → 中止本链，下个触发点自动重试；plan 失败 → 标红继续日报
- 交易日历以 sh000001 日线日期为权威（ADR-2）：周末/节假日/TDX未下载自动跳过
- 启动时 CatchUp：前一交易日完全没跑 → 整日补跑
- 每日 report 成功后自动 VACUUM 备份 + 30份滚动轮转（NFR-3）

---

## CLI 一览

```
darvas serve  [--addr] [--db]        常驻模式：调度+GUI+备份（M7，日常用它）
darvas ui     [--addr] [--db]        本地GUI四视图（浏览器，只读）
darvas run    [--date]               完整盘后流水线（一次性全链路）
darvas ingest [--date] [--since full]  数据同步（增量/全量）
darvas boxscan|screen|poolsync|signal|plan|report [--date]   单任务补跑
darvas weekly [--date]               周报（M7.2）
darvas fills  [file] [--yes]         成交粘贴录入（同花顺/通达信/CSV三格式）
darvas ladder <code>                 个股箱体阶梯（终端打印）
darvas param  get|set                参数读写（风险系数/箱体宽度/tdx.root等，热调）
darvas doctor [--db]                 体检：schema/数据新鲜度/账本恒等式审计
darvas version
```

通用约定：`--db` 缺省 exe同目录/var/darvas.db（或环境变量 DARVAS_DB）；所有任务幂等可重跑，留痕 daily_run（GUI仪表盘心跳灯）。

---

## 文档体系

> **完整索引、治理规则与"已合并文档去向"见 [docs/README.md](docs/README.md)。**
> 顶层保留 **13 份**必要文档（2026-11-06 两轮治理：20 → 14 → 13）。**一个内容方向只有一份**；被合并的原文整份留在 `docs/archive/`。

| 想做什么 | 读哪份 |
|---|---|
| 判断某条规则能不能加 | `docs/00-darvas-theory.md`（理论宪法） |
| 今天该做什么 / 下一个实验是什么 | `docs/60-plan-and-board.md`（**唯一计划与看板**） |
| 查参数的值 / 自由度 / 能不能动 | `docs/44-new-baseline-config.md`（§二 值、§六 分类、§4.3 冻结） |
| 查账本数字与教训 | `docs/55-ledger-and-lessons.md`（§11 收官、§14 教训 R19–R34） |
| 灌数据 / 重建基线 | `docs/53-data-and-rebuild.md` |
| 做牛熊识别 / 攻防转换 | `docs/48-market-regime.md` |
| 工程侧（需求/架构/表/接口/一致性） | `docs/01` / `02` / `03` / `04` / `06` |
| 已裁定的结论 / 已关闭别重跑的 | `docs/29-rulings-registry.md` |
| 想从别的书搬东西进来 | `docs/57-external-methods.md` |

---

## 技术栈

| 项 | 选择 |
|----|------|
| 语言 | Go（纯Go SQLite驱动`modernc.org/sqlite`，无CGO，单exe） |
| 数据库 | SQLite WAL（调度写+GUI读双连接）+ 每日VACUUM备份30份滚动 |
| GUI | 本地HTTP UI（`net/http`+embed静态资产，浏览器访问，零外部依赖；docs/02 §2 ADR降级预案生效版） |
| 数据源 | 通达信`vipdoc/{sh,sz}/lday/*.day`（1.2万文件）+ `T0002/hq_cache/*.tnf`（代码→名称） |
| 部署 | `darvas.exe` + `darvas.db` 拷走即用（同平台零安装） |

---

## 项目结构

```
cmd/darvas/            # CLI入口（run/ui/serve/fills/...）
internal/
  contract/            # ★ 模块间接口（单一事实源）
  datasource/tdx/      # .day二进制读取器 + tnf名称表
  ingest/  store/      # 数据导入与SQLite仓储
  box/                 # 箱体引擎（三日法则状态机）
  screener/  pool/     # 初选四条件 + 跟踪池状态机
  signal/  planner/    # 四类信号 + 交易计划（六问出口，含止损上移/仓位）
  position/  ledger/   # position 为空占位（腿管理在 store）；账本（权益快照/恒等式审计）
  scheduler/           # 常驻调度：时刻表触发+CatchUp补跑+交易日历
  pipeline/            # 盘后流水线编排 + 备份轮转
  reporter/            # HTML日报
  gui/                 # 本地HTTP UI（embed静态+JSON API）
docs/                  # 13 份必要文档 + README 索引（00-04、06、29、44、48、53、55、57、60）
docs/archive/          # 历史档案（非事实源，原稿留痕）
scripts/               # 一致性检查脚本
var/                   # 运行时（db/备份/报告）
```

---

## 当前状态

- ✅ M0 规划与骨架 · M1 数据层（全量<10min）· M2 箱体引擎（35用例）· M3 池 · M4 信号与计划（含账本）· M5 CLI闭环 · M6 GUI四视图
- ✅ M7 周而复始：✅ 7.1 常驻调度（serve）· ✅ 7.2 周报 · ✅ 7.3 备份轮转+恢复演练 · ✅ 7.4 移植演练（15b5183）
- ✅ M8 内置回放器：✅ 8.1 回放骨架 + as-of 差分审计（`darvas backtest`）· ✅ 8.2 模拟撮合器（`internal/match`）· ✅ 8.3 体制运行器（`--mode regime`）· ✅ 8.4a 体制参数敏感性（结论：基线已处理论前沿，单参数无解）
- ✅ **仓位现行 = 阶梯制**（`pos.newhigh_ladder=on`，距 52 周高点四档 3/2/1/0.5 单位）；已退役的 **A M1**（升势 2 单位/其余 1 单位）仅存在于实验史，定稿见 `docs/00 §5.6.7`，事实见 `docs/29`
- ✅ **新基线已收官**（`docs/53`）：A–F 完成、后复权已上线，adj 连续基线 `bt_baseA_cont_adj.db` 100% 跑完（+1,213,822 / ×2.214 / MDD −32.50%，`docs/55 §11`）；唯一未完成项 = G（攻防转换）仍是接口桩（`mode=off`）
- ⏳ **正在跑 A1 `units.t4=0` 全窗消融**（`var/bt/arms/bt_a1_t4off.db`）：已到 **2017-04（87.7%）**，总账领先基线，但**主判据（剔最好年）已翻负**（`docs/55 §13`）
- 🔧 **2026-11-06 主线变更为 D 系「原著复刻」**：把达瓦斯原著 15 条规则逐条对照代码，判出 **14 条有缺口**——加仓从未接线（1,142 笔全单腿）、2 日强平违反「没有理由卖掉上涨的股票」、亏损拉黑违反「止损后更高价买回」。13 个 D 臂已预注册，主干 9 臂 / 52 机时（`docs/60 §2.5`、`docs/00 §11.5`）
- 📌 **今天做什么 / 下一个实验 → [docs/60 计划与看板](docs/60-plan-and-board.md)**；历史审计见 [归档快照](docs/archive/54-new-baseline-audit.md)。
- 📖 **系统全貌 → [docs/64 专家总典](docs/64-darvas-master.md)**（2026-11-06 立）：13 份文档重构合并成的单一专家文档，原著 15 条 × 系统对照 + 六大总成诊断 + D 系修复路线；原稿备份于 [docs/backup](docs/backup/2026-11-06-pre-merge/)。
- 📚 **文档已治理（2026-11-06，两轮）**：顶层 20 → 14 → **13 份**。第一轮合并 `49/58/59/61/62/63`；第二轮 **`56` 并入 `60`**（计划只留一份）、`55 §3/§4` 归档。去向见 [docs/README.md §墓碑](docs/README.md)。

数据源状态：TDX vipdoc 增量更新可用；回测源库 `var/bt/full_hist_adj.db`（2005–2026，5,561 只 / `adj_ready=1`）。

---

## 数据与仓库

- **唯一本地库**：`I:\Darvas-box-trading`（代码、文档、运行时数据全在此目录）
- 远端：https://github.com/everything-is-simple/Darvas-box-trading
- `var/`、`*.db`、`.workbuddy/` 不入库（.gitignore）
- 理论原著电子版在仓库外：`H:\《交易之no.14.year》\2011.usa.达瓦斯.我如何在股市赚了200万\`；`docs/books/` 仅存扫描件页面标记（OCR待做，M0.8）
