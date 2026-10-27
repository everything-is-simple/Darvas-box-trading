# 02 · 项目设计（ARCHITECTURE）

> 版本：v1.1（2026-10-17）
> 技术栈：**Go + SQLite（纯Go驱动） + 本地HTTP UI（net/http + embed，无Node构建链）**，单文件桌面程序

---

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                     darvas.exe（单可执行文件）                     │
│                                                                 │
│  ┌───────────────┐   ┌──────────────────────────────────────┐  │
│  │   scheduler    │   │      GUI（本地HTTP UI :8787）          │  │
│  │  每日时间点调度 │→ │  仪表盘│跟踪池│个股箱体│交易记录 四视图  │  │
│  └───────┬───────┘   └──────────────┬───────────────────────┘  │
│          │                          │ JSON API（只读）          │
│          ▼                          ▼                          │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                  pipeline（盘后流水线编排）                │   │
│  │  ingest → boxscan → screen → poolsync → signal →         │   │
│  │  plan → report        每步幂等、可独立重跑、留痕           │   │
│  └───────┬─────────────────────────────────────────────────┘   │
│          ▼                                                      │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                     领域引擎层（纯Go包）                   │   │
│  │  box（箱体状态机） screener（初选） pool（跟踪池）          │   │
│  │  signal（信号） planner（计划） position（持仓）            │   │
│  │  ledger（账本）                                            │   │
│  └───────┬─────────────────────────────────────────────────┘   │
│          ▼                                                      │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  store（SQLite访问层·唯一DB入口·Repository模式）           │   │
│  └───────┬─────────────────────────────────────────────────┘   │
│          ▼                                                      │
│  ┌──────────────┐        ┌──────────────────────────┐          │
│  │ datasource/tdx │─读取→ │ ingest（快照/增量/校验）  │          │
│  │ .day二进制读取 │        └──────────────────────────┘          │
│  └──────────────┘                                              │
│                     darvas.db（SQLite WAL，同目录）              │
└─────────────────────────────────────────────────────────────────┘
                          ▲
        H:\new_tdx64\vipdoc\{sh,sz}\lday\*.day（外部，通达信维护）
        H:\2025炒股手训练软件\EXPORTT\*.csv（外部第二数据源：模拟成交回流，GBK，
        `darvas fills <file.csv>` 导入，见 docs/04 §7）
```

**分层铁律**：依赖方向只能自上而下；领域引擎不import GUI/调度；`store`是被依赖的底座；TDX读取器不依赖store（可独立测试）。

---

## 2. 技术选型与理由

| 决策 | 选择 | 理由 | 放弃的备选 |
|------|------|------|-----------|
| 语言 | Go 1.22+ | 单文件编译、跨平台、启动<100ms | Python（部署重） |
| SQLite驱动 | `modernc.org/sqlite` | **纯Go无CGO** → 单exe、构建零环境依赖 | `mattn/go-sqlite3`（要CGO） |
| GUI | **本地HTTP UI（生效）** | `net/http` + embed静态资产，零外部依赖、零构建链，系统默认浏览器打开 | Wails v2（要Node构建链）、Fyne（图表弱）、Lorca（依赖Chrome） |
| 图表 | 前端canvas自绘（无JS库） | K线+箱体矩形+成交量+权益曲线，离线单exe不引CDN/Node包 | lightweight-charts（外部依赖） |
| 报告 | HTML模板 + excelize(Excel) | 每日照着挂单，双格式 | PDF |
| 调度 | 自研轻量调度（goroutine+time） | 只需每日7个时刻+补跑逻辑 | 外部cron（破坏可移植性） |

> GUI决策记录（ADR）：原方案Wails v2，因其构建链需要Node而按§2预案降级为本地HTTP UI，M6已按此交付
> （`darvas ui`起127.0.0.1:8787 + 系统默认浏览器打开，静态页embed进exe，API只读）。
> **领域层设计不受GUI选型影响**；若未来切回Wails，仅替换 `internal/gui` 绑定层。

---

## 3. 目录结构（仓库即工程）

```
Darvas-box-trading/
├── README.md                     # 项目总览+快速开始
├── docs/                         # 00理论 01需求 02设计 03ERD 04API 05任务 06一致性
├── cmd/darvas/main.go            # 入口：CLI解析+ui/serve子命令
├── internal/
│   ├── contract/                 # ★ 模块间接口定义（单一事实源，见04文档）
│   │   └── contract.go
│   ├── datasource/tdx/           # .day读取器（从tradebuddy移植+增强）
│   │   └── reader.go
│   ├── ingest/                   # 全量/增量导入+质量校验
│   ├── store/                    # SQLite唯一入口
│   │   ├── store.go              # Open/Close/migrate/backup
│   │   ├── repo_bar.go           # daily_bar仓储
│   │   ├── repo_box.go           # box仓储
│   │   ├── repo_pool.go          # candidate/watch仓储
│   │   ├── repo_trade.go         # position/plan/fill/ledger仓储
│   │   ├── repo_run.go           # daily_run/param仓储
│   │   └── migrations/0001_init.sql  # ★ DDL单一事实源
│   ├── box/                      # 箱体状态机（纯函数核心+扫描器）
│   ├── screener/                 # 四条件初选
│   ├── pool/                     # 跟踪池状态机
│   ├── signal/                   # 四类信号判定
│   ├── planner/                  # 六问→订单计划（含止损上移 raiseStops + 仓位 unitShares）
│   ├── position/                 # 占位空包（腿管理在 store/repo_trade.AddLeg；止损上移/仓位在 planner）
│   ├── ledger/                   # 权益快照/恒等式审计
│   ├── scheduler/                # 时间表调度+补跑+周六weekly触发
│   ├── reporter/                 # HTML/Excel日报周报
│   └── gui/                      # 本地HTTP UI（net/http + embed）
│       ├── gui.go                # Server/路由/JSON API（dashboard/dates/watch/candidates/signals/stock/trades）
│       └── static/index.html     # 单文件前端（四视图+全站日期下拉+池子页，canvas自绘K线/箱体/权益曲线）
├── scripts/                      # build.bat / backup.bat
├── var/                          # 运行时目录（gitignore）
│   ├── darvas.db                 # SQLite（程序同目录或此处，可配置）
│   ├── backup/                   # 每日db备份×30
│   └── reports/                  # daily_YYYYMMDD.html
└── .gitignore
```

---

## 4. 运行形态

### 4.1 部署形态（满足"随便移植到同平台机器"）

```
目标机器（Windows x64）仅需：
  darvas.exe      ← 单文件（~15MB，含前端资产embed）
  darvas.db       ← 数据库（可从旧机拷贝=完整迁移）
可选环境变量/配置文件 darvas.toml：
  tdx_root = "H:\\new_tdx64\\vipdoc"   # 新机器上TDX路径不同时改这里
```

- db查找顺序：`--db`参数 > `DARVAS_DB`环境变量 > exe同目录`darvas.db`
- TDX路径同理可配置 → 移植只改一个toml

### 4.2 进程形态

- **常驻模式**（推荐）：开机自启/手动启动，托盘驻留，调度器按时间表自动触发，GUI随时打开
- **手动模式**：`darvas run --date 2026-10-16` 单次跑全流程（补跑/调试）

---

## 5. 关键设计决策（ADR摘要）

### ADR-1 箱体引擎=确定性纯函数
箱体识别做成「K线序列 → 箱体序列」的**确定性纯函数**（同输入必同输出），DB只是结果的持久化。好处：①单元测试即文档；②重跑幂等天然成立；③未来任何GUI渲染与信号判定都基于同一事实。

### ADR-2 交易日历来自数据本身
用sh000001的日期序列判断交易日与「数据新鲜度」，不内置日历库（节假日规则变化零维护）。

### ADR-3 信号当日生成、订单次交易日执行
达瓦斯无盘中逻辑 → 系统的所有决策都是**收盘后**基于收盘价产生，产出的是**下一交易日**条件单计划。数据模型上`trade_plan.plan_date`（生成日）与`execute_date`（执行日）分离；执行日按ADR-2取sh000001日线中plan_date之后的最近交易日（回放/补跑场景节假日精确），实盘当日无未来K线时兜底为跳过周末的下一工作日（假日偏差由订单过期机制兜底，不产生错误成交）。

### ADR-4 人工成交录入而非对接券商
自动下单明确不做（需求§9）。成交录入提供「粘贴解析」（券商APP成交明细文本→结构化），把人工成本压到每笔30秒。

### ADR-5 SQLite WAL + 每日备份
单机单用户，WAL模式足够并发（GUI读+调度写）；每日流水线尾部（report任务后）做`VACUUM INTO`备份（时点见 docs/01 §4.1）。

---

## 6. 数据流（一次完整交易日）

```
盘后 通达信客户端 ──下载数据──▶ vipdoc/*.day（外部更新，时点见 docs/01 §4.2）
ingest      ──增量读取──▶ daily_bar（今日T行）
boxscan     ──K线窗口──▶ box（重算跟踪池全量+候选池增量+全市场滚动N日）
screen      ──四条件──▶ candidate_pool（当日快照）
poolsync    ──状态机──▶ watch（入池/升阶/出池）
signal      ──收盘价vs箱体（首次事件语义：状态迁移当日发一次，同日重跑幂等）──▶ signal（breakout/pyramid/stop/failed）
plan        ──信号+持仓+param──▶ trade_plan+plan_order（六问答案）
report      ──汇总──▶ daily_YYYYMMDD.html + db备份
（各任务触发时刻见 docs/01 §4.1 唯一事实源；scheduler.Schedule 派生 CLI/GUI/测试）
     (次日09:25 人工按plan_order挂券商条件单；成交后paste回录 → trade_fill → ledger)
```

### boxscan增量策略（性能关键，NFR-1）

- **全量**：仅首次与参数变更时（约1.2万只×窗口重放，目标<10min）
- **每日增量**：
  - 跟踪池+候选池股票：全K线窗口重放（正确性优先，量小）
  - 其余全市场（docs/00 §7 股票池：沪深主板+创业板+科创板；排除指数/B股/北交所）：只对「今日创60日新高 或 今日量≥均量×2」的股票重放（预过滤）
  - 依据：不满足预过滤的股票不可能新产生符合四条件的箱体事件

---

## 7. 错误处理与可观测性

| 层 | 策略 |
|----|------|
| pipeline | 每任务事务包裹；失败写`daily_run`（status=failed+error）；`plan`失败=红色告警 |
| ingest | 坏记录跳过计数；更新数<阈值→阻断（数据未下载完整） |
| GUI | 任务心跳灯（7绿=健康）；失败任务点击看日志 |
| 日志 | 结构化写入`var/log/darvas.log`，按日轮转，保留90天 |

---

## 8. 安全与备份

- db每日备份30份滚动（ADR-5）
- 参数修改写入`param`并留`changed_at`（审计）
- 所有时间使用本地时区（Asia/Shanghai，单机单用户无歧义）

---

## 9. 研究数据流与回测产物（2026-10-25 立项）

> 本节定义 box 十维迭代实验（理论框架见 docs/00 §3.6，需求口径见 docs/01 §10）在架构侧的数据流与产物。生产侧架构（§1-§8）不变，研究是叠加在回放器之上的离线分析层。

### 9.1 研究数据流

```
TDX 日线 (H:\new_tdx64)
   │  ingest (复用生产代码路径)
   ▼
源库 full_hist_10pct.db (只读，禁止改动)
   │  darvas_e17.exe backtest --from <year> --to <year> --set scratch
   ▼
年度独立回测库 bt_e17_<year>.db (var/bt/, 每年一个, 串行不并发)
   │  position_leg.leg_no=1 JOIN plan_order.reason_json  (含 box_bottom/box_top/trigger/
   │      high_52w/newhigh_distance_pct/sizing_basis/units)
   ▼
e17_watch.py + e17_annual_tier_report.py  (Python 离线分析, 只读查 db)
   │  按 t1/t2/t3/t4 + box 高度梯度分带统计四项口径
   ▼
var/bt/e17_metrics.csv             (年度汇总, append)
var/bt/e17_watch_report.md         (摘要, 每次重建)
var/bt/e17_annual_tier_report.html (逐年分层详细报告, 每年跑完自动补章节)var/bt/e17_annual_tier_report.html (逐年分层详细报告, 每年跑完自动补章节)

e17_snowball.py                    (滚雪球复利, 只读 28 库 equity_snapshot)
   │  逐年链式复利 level *= total_d/total_{d-1}（跨年直接衔接）
   ▼
var/bt/snowball_stats.json         (复利统计: 终值/CAGR/MDD/夏普/Calmar)
docs/experiments/e17_snowball_report.md + e17_snowball_curve.svg

专项实验报告（独立链路，产物一律进 docs/experiments/，不落 var/bt/）
   │  只读 var/bt/bt_e17_<year>.db × 14 + e17_metrics.csv（mode=ro）
   ▼
e17_box_height_report.py / e17_newhigh_distance_report.py / e17_regime_attribution_report.py
   │  先 --check-only 跑 HARD/WARN/INFO 校准（失败即 exit 2，不写文件），再正式生成三件套
   ▼
docs/experiments/<实验>_framework.md (零结果纯规则) + _report.{html,md} + _data.json
```

### 9.2 回测产物清单

| 产物 | 路径 | 写入时机 | 说明 |
|------|------|---------|------|
| 年度回测库 | `var/bt/bt_e17_<year>.db` | darvas_e17.exe 跑完该年 | 独立 SQLite，含 position/position_leg/plan_order/equity_snapshot 等完整表 |
| 年度报告 | `var/bt/bt_e17_<year>_report.md` | `p05_yearly_report.py` 后处理 | 单年 Markdown 报告 |
| metrics 汇总 | `var/bt/e17_metrics.csv` | 每年完成后 append 一行 | 跨年汇总，是"已完成年份"的事实源 |
| 监控摘要 | `var/bt/e17_watch_report.md` | `e17_watch.py` 每次运行重建 | 含已完成年份四项口径 + 进行中年份进度 + 2008 队列状态 |
| 逐年分层报告 | `var/bt/e17_annual_tier_report.html` | `e17_annual_tier_report.py` 每年完成后重建 | 每年一章，含 t1-t4 四项口径 + box 高度梯度分布 + 诚实标注 || 复利统计 | `var/bt/snowball_stats.json` + `docs/experiments/e17_snowball_report.md` | `e17_snowball.py` 手动重建 | baseline / baseline+ 的链式复利终值、CAGR、日频最大回撤、夏普、Calmar（口径见 docs/01 §10.6） |
| 已汇报年份 | `var/bt/e17_reported_years.txt` | 自动汇报成功后 append | 避免重复汇报同一年份 |
| 队列状态 | `var/bt/e17_years_status.json` | 队列脚本每次状态变化时写 | state=waiting/running/done/failed + 当前 year + detail |

### 9.3 串行执行与并发防护

- **唯一执行器**：`scripts/e17_run_years.py`（2005-2013 补跑）+ `scripts/e17_run_2008.py`（2008 样本外）。
- **文件锁**：`e17_years.lock` 防止队列并发启动；`MultipleInstances=IgnoreNew` 计划任务设置防止重复触发。
- **跳过已完成**：每年启动前查 `e17_metrics.csv`，已有该年则跳过；重复启动不会重跑。
- **失败即停**：任一年失败（rc≠0 或后处理异常）即停止队列，不带着错误往后跑；失败 traceback 写入 `e17_years_status.json` 的 detail 字段。
- **不并发铁律**：2014 年曾因环境 I/O 争抢出现单日 boxscan 4 小时异常（历史诊断见 git 历史：2026-10-25 E17 耗时诊断），所有回测必须串行。

### 9.4 自动监控与汇报

- **监控计划任务**：`DarvasE17Watch`（每 20 分钟刷新 `e17_watch_report.md`）。
- **自动汇报 automation**：`c931a120-06fd-406f-bcd3-7ced77a6c6e6`（每小时检查 metrics 新增年份，发现即汇报，成功后 append `e17_reported_years.txt`）。
- **汇报内容**：年度净利/收益率/最大回撤/胜率/利润因子 + t1-t4 四项口径（胜率、每单位净利、保本胜率、利润集中度）+ 重点 t4 深度回调区风险收益；2008 注明"样本外验证"；2005-2013 无 p05 基线，不编造对照。

### 9.5 研究字段的后验可行性（2026-10-25 诊断结论）

| 维度 | reason_json 字段 | 后验可行性 |
|------|-----------------|-----------|
| 第 2 维 box 高度 | `box_bottom` + `box_top` | ✅ **已完成（P0-1，M10.0）**。⚠️ 两套分母：闸门用 `/top`（`engine.go:127`），历史统计多用 `/bottom`，换算 `b=t/(1−t)`——报告须双口径并列，详见 `docs/experiments/e17_box_height_framework.md` §4 |
| 第 5 维 box 位置 | `newhigh_distance_pct` + `sizing_basis` | ✅ 已完成（E17 阶梯，M9.0） |
| 第 6 维 突破强度 | `trigger`（但全部=box_top×1.03） | ❌ 当前回测器写死 3%，需参数化 `buy.premium` 多轮跑 |
| 第 7 维 量比 | 无 | ❌ 需改回测器加 `volume_ratio` 字段 |
| 第 4 维 最高 box | `box_top` + `high_52w`（但代理口径 0 命中） | ❌ 需用 box 序列历史重定义，非单点比 52 周高 |
| 第 1 维 box 宽度 | 无（3 日固定） | ❌ 需参数化 box 周期长度多轮跑 |
| 第 10 维 头寸 regime | `reason_json.regime`（= C3@plan_date）+ 上证 MA20/MA60 + 指数 52 周高点回撤 | ✅ **A 类已完成（P0-3，M10.0）**。⚠️ `regime` 是 plan_date 标签，与建仓日错 1 日（2014 年一致 86/100），归因须按建仓日重算；账户回撤逐年重置且 `>10%` 档 0 天，登记「不可检验」。详见 `docs/experiments/e17_regime_attribution_framework.md` |

> 解锁第 1/4/6/7 维的代码改动清单见 docs/05 M9/M10。

