# Darvas 移植部署方案（本机 · 2026-10-29）

> 目标机实测结论：**目标目录 `I:\Darvas-box-trading`**（NVMe SSD、NTFS、可用 130.4 GB）
> 通达信源目录：**`D:\new_tdx64`**（数据新鲜，`vipdoc` + `T0002\hq_cache` 齐全）
> 结论：**可以整机移植，7 个分项已全部落地并校验；Go 1.27.2 已装在本机 I 盘（详见 §8）。
> 但历史基线仍必须用包内原二进制跑——装 Go 只为改代码/自测，不是为了重造基线。**

---

## 0. 一句话结论

包里那套东西本质上是「**一个静态 Go 单文件 + 一个 4.1 GB SQLite 源库 + 5 个 4.2 GB 年库 + 47 个纯标准库 Python 脚本**」。
唯一和机器绑定的是**通达信路径**：程序默认写死 `H:/new_tdx64/vipdoc`，本机 `H:\` 下没有这个目录。
改成 `D:\new_tdx64\vipdoc` 就行——**用参数改，不要改代码重编译**（重编译会让二进制哈希变，历史结果失去可比性）。

---

## 1. 环境体检（实测）

| 项 | 本机实况 | 判定 |
|---|---|---|
| 物理磁盘 | Disk1 = X15 SSD 512GB **NVMe**；Disk0 = CY-SSV3 SATA SSD；Disk2 = ACASIS USB 64G | ✅ 目标盘在 NVMe 上，脚本的"机械盘警告"不会触发 |
| 盘符布局 | C/H/I 在 Disk1(NVMe)；D/E/F 在 Disk0(SATA SSD)；G/J = U 盘 | I 盘与 C 盘同物理盘，但独立分区，符合"不装 C/D"要求 |
| `I:` 可用 | 155.4 GB 总 / 已用 25.0 GB（=U 盘包副本）/ **可用 130.4 GB** | ✅ 门槛 70 GB，余量 60 GB |
| Python | `C:\Python315\python.exe` = **3.15.1**；另有 `C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12` | ✅ 47 个脚本只 import 标准库（sqlite3/json/csv/statistics/msvcrt…），无 pandas/numpy，3.15 可跑 |
| Go | **已装 1.27.2（2026-10-29 23:29，`I:\tools\go`，版本与 `go.mod` 的 `go 1.27` 匹配）** | ✅ 工具链自检通过（能编译运行 hello.exe）；⚠️ 外部依赖需联网一次，见 §8 |
| Git | 2.56.0 | ✅ 只是为了能查历史，跑回测不需要 |
| 通达信 | `D:\new_tdx64\vipdoc\{sh,sz,bj}\lday`：sh 6043 / sz 5919 / bj 367 个 .day；`T0002\hq_cache\shs.tnf`(9.99MB)+`szs.tnf`(8.82MB)，`sh000001.day` 更新到 22:38 | ✅ 数据新鲜，名称表在位 |
| 通达信默认路径 | `H:\new_tdx64` **不存在**（H 盘别的内容） | ❌ 必须改 `tdx.root`，否则 ingest 静默导入 0 个文件 |
| 当前会话权限 | 非管理员 | ⚠️ 原脚本 `setup-local.ps1` 第 0 步强制要管理员；但实际写入全在 I 盘，**普通权限足够** |

### 磁盘空间账（单位 GB，十进制）

| 占用项 | 大小 | 说明 |
|---|---|---|
| 拼装后源库 `full_hist_10pct.db` | 4.10 | 3 片合并，精确 4,404,174,848 字节 |
| 5 个历史年库 | 20.72 | 2005/2007/2010/2013/2018，不压缩直拷 |
| 待跑 9 年产出 | ≈ 37.4 | 2006/08/09/11/12/14/15/16/17，每年约 4.15 |
| **回测侧小计** | **≈ 62.2** | 必须 |
| 生产库 `var\darvas.db` + 30 份备份 | ≈ 20 | 可选，只有跑 `serve` 生产链才要 |
| U 盘包本体 | 24.9 | 验证通过后可删，回收 25 GB |

部署完成后 `I:` 预计剩 ≈ 59 GB（不含生产库）/ ≈ 39 GB（含生产库 30 份备份）。

---

## 2. 这套系统到底是什么（两套互不干扰的子系统）

```
                 ┌──────────────────── 生产链（每日跑） ────────────────────┐
D:\new_tdx64     │  darvas.exe ingest → boxscan → screen → poolsync      │
vipdoc\*.day ────┤  → signal → plan → report → 备份                       │
T0002\hq_cache   │  库：var\darvas.db      出口：var\reports\daily_*.html │
                 └───────────────────────────────────────────────────────┘

                 ┌──────────────────── 回测链（跑历史） ────────────────────┐
full_hist_10pct.db ──→ darvas_base.exe backtest --src ... --db bt_baseA_YYYY.db
  (4.1GB 源库)          ↑ 由 scripts/base_run.py 驱动（纯标准库）
                        出口：var\bt\base\bt_baseA_YYYY.db + CSV/JSON/HTML 统计
                 └───────────────────────────────────────────────────────┘
```

**关键事实：两条链不共享数据。回测链完全不读通达信**，只读 `var\bt\full_hist_10pct.db`。
所以「通达信搬到 D 盘」**不影响 9 年回测**，只影响生产链的每日 ingest。

### 分层结构（Go 部分）

| 层 | 包 | 职责 |
|---|---|---|
| 入口 | `cmd/darvas` | CLI：ingest / run / serve / ui / fills / backtest / param / doctor |
| 数据源 | `internal/datasource/tdx` | 读 `.day` 二进制（32字节/条）+ `.tnf` 名称表 |
| 存储 | `internal/store` | SQLite WAL，19 个 migration 内嵌编译，首次运行自动建库 |
| 理论核心 | `internal/box` | 三日法则箱体状态机（**确定性纯函数**，同输入必同输出） |
| 业务 | `screener / pool / signal / planner / ledger` | 四条件初选 → 池迁移 → 四类信号 → 六问计划 → 权益账本 |
| 编排 | `pipeline / scheduler` | 七任务链 + 时刻表补跑 + 交易日历（以 sh000001 为权威） |
| 出口 | `reporter / gui` | HTML 日报；本地 HTTP GUI `127.0.0.1:8787`（embed 静态资产） |

### 依赖清单（真实）

| 依赖 | 必需？ | 说明 |
|---|---|---|
| Windows x64 | 是 | 二进制是 windows/amd64 静态编译 |
| SQLite | 否 | `modernc.org/sqlite` **纯 Go 实现**，不链接系统 sqlite3 |
| Go 工具链 | **否** | 已编译好；`go.mod` 写 `go 1.27`，本机也没装 |
| Python | 是（仅回测编排） | `base_run.py` 只做 subprocess + sqlite3 查询，零第三方包 |
| 7z.exe | 是（仅部署时） | 包内自带绿色版，不用安装 |
| 通达信客户端 | 是（仅生产链） | 每日收盘后手动点"下载数据"，是数据新鲜度的唯一前提 |

---

## 3. 移植的 4 个断点（这是本次移植真正要处理的）

### 断点 1：通达信默认路径写死 H 盘 —— **必须改**

```go
// cmd/darvas/main.go:172-175
if root := os.Getenv("DARVAS_TDX"); root != "" { return root }
return "H:/new_tdx64/vipdoc"   // ← 本机没有
```

三种改法，**推荐第 2 种**：

| 方式 | 命令 | 评价 |
|---|---|---|
| 环境变量 | `setx DARVAS_TDX "D:\new_tdx64\vipdoc" /M` | 机器级持久，不落在库里 |
| **参数入库（推荐，当前有效）** | `darvas.exe param set tdx.root D:\new_tdx64\vipdoc` | 写在 `var\darvas.db` 的 param 表，随库走 |
| 改代码重编译 | 改 `main.go` 再 `go build` | ❌ **禁止**：二进制哈希一变，与老机结果不可比，违背"同参数同输入必出同结果" |

> 🔴 **本次移植新发现的雷（重要）**：源码树里 `internal/store/migrations/0019_drop_dead_params.sql`（2026-10-28 提交 3b767fd）
> 把 `tdx.root` 列为"死参数"并 **`DELETE FROM param WHERE key='tdx.root'`**，
> 但 `cmd/darvas/main.go:182` 至今仍在读它 —— 这是源码里的一处自相矛盾。
> 实测确认：**包内两个二进制内嵌的迁移只到 0018，没有 0019**（`strings darvas.exe | grep 0019` = 0 命中，
> 首次建库只打印到 `applied migration 0018_equity_budget.sql (v18)`）。
> 所以现在 `param set tdx.root` 完全有效。**但只要哪天重编译一次，0019 就会把这个参数删掉，
> 系统静默回落到 `H:/new_tdx64/vipdoc` → 每日 ingest 导入 0 个文件、不报错、日报全空。**
>
> **防御性配置（推荐两条都做）**：`param set` + 机器级 `DARVAS_TDX` 环境变量。
> 优先级是 param > env > 硬编码默认，两者共存时 param 生效；万一将来 0019 生效删掉 param，env 自动接手。

> ⚠️ 坑：`tdx.root` 要指到 **`...\new_tdx64\vipdoc`**，不是 `...\new_tdx64`。
> 因为名称表逻辑是「取 vipdoc 的**父目录** + `T0002\hq_cache`」（`names.go:42`）。
> 指错一级 → 日线能导进来但股票名称全是空的（不报错，静默降级）。

### 断点 2：包里没有生产库 `var\darvas.db` —— **需要你决策**

包内只有回测库和回测结果，**生产和账本数据库没打进来**。两条路：

- **A. 搬老机的库（推荐，前提是老机 `var\backup\darvas_YYYYMMDD.db` 还在）**
  拷最新那份 VACUUM 备份到 `I:\Darvas-box-trading\var\darvas.db`。
  保留全部持仓 / 成交 / 挂单 / 信号历史，账本不断代。694 MB 左右，几分钟。
- **B. 在新机重建**：`darvas.exe ingest --since full`
  得到的是「干净的历史行情库」，**但成交记录、持仓、权益快照全部归零** ——
  等于从今天重新开户，历史账本丢失。
  ⚠️ 实测校正：README 说"全量约 10 分钟"偏乐观。本机实测 **568 万行耗时 8 分 46 秒
  （≈1.08 万行/秒）**，全量 2937 万行按此外推 **约 45 分钟、库约 3–4 GB**。
  所以 B 路不是"十分钟搞定"，要留一小时。

### 断点 3：6 处脚本硬编码 `I:\Darvas-box-trading` —— **恰好无害**

`regime_*.py` 6 处、`run_*.bat` 10 个文件里写死了 `I:\Darvas-box-trading`。
**本次目标路径正好也是 `I:\Darvas-box-trading`**，所以这些一行都不用改。
这也是选 I 盘而不是 F 盘（虽然 F 盘空 201 GB）的核心理由：路径同名 = 零改动、`.workbuddy` 记忆与 git 历史都对得上。

### 断点 4：原脚本的管理员门禁 —— **绕开即可**

`setup-local.ps1` 第 0 步 `Die "需要管理员权限"`。但实际写入全在 I 盘，普通权限足够；
只有「注册计划任务 / 装常驻服务」才需要提权。当前会话非管理员 → 建议**按脚本的 8 步逐条手工执行**（可逐步验收），
或你另开一个管理员 PowerShell 直接跑原脚本。

---

## 4. 部署方案（6 个阶段，含验收口径）

### 阶段 0 · 预检（1 分钟）
```powershell
Get-PSDrive I                      # 可用 ≥ 70 GB
Test-Path "D:\new_tdx64\vipdoc\sh\lday\sh000001.day"
Test-Path "D:\new_tdx64\T0002\hq_cache\shs.tnf"
```
验收：三项全 True，I 盘可用 > 70 GB。✅ 已通过

### 阶段 1 · 拼装源库（约 1 分钟，写 4.1 GB）
把 `02-数据\Darvas-2-data.db.part00/01/02` 按序拼成 `var\bt\full_hist_10pct.db`。
验收（硬指标，不通过就重拷分片，别往下走）：
- 大小 **正好 4,404,174,848 字节**
- SHA256 = `B7E377023C93BB16955ED6BB59295F0B5941391AECF588B6AB3B3D407CE182BA`
- `SELECT COUNT(*) FROM daily_bar` 能出数

### 阶段 2 · 落地代码与程序（1 分钟）
1. `7z.exe x 01-代码文档\Darvas-1-source.7z -oI:\Darvas-box-trading` → cmd/internal/scripts/docs/.git
2. 拷 `darvas_base.exe` → `var\bt\`；`darvas.exe` → 项目根
3. 哈希校验 `darvas_base.exe` = `D1E8719B...ED44C`（已核过包内文件一致）
4. `Expand-Archive 03-结果\Darvas-3-results.zip → var\bt\base`（5 年 CSV/JSON/HTML 统计）
5. `07-记忆\*` → `I:\Darvas-box-trading\.workbuddy\memory\`（下次开对话自动加载）
6. `git checkout -- .gitignore`（打包时漏了这个文件，`git status` 会显示被删除）

### 阶段 3 · 恢复 5 个历史年库（1–3 分钟，21.7 GB 同盘拷贝）
`06-年库\bt_baseA_{2005,2007,2010,2013,2018}.db` → `var\bt\base\`。
验收：逐个 `SELECT COUNT(*) FROM equity_snapshot, MAX(date)`，最后交易日必须 **≥ 当年 12-25**
（2005=242、2007=242、2010=242、2013=238、2018=243 个交易日；**别用行数当判据**）。
这 5 年 `base_run.py` 会自动跳过（判据是 equity_snapshot 最后一天，不是文件存在）。

### 阶段 4 · 接通达信 + 生产链（约 10 分钟，可选）
```powershell
cd I:\Darvas-box-trading
.\darvas.exe param set tdx.root "D:\new_tdx64\vipdoc"
.\darvas.exe ingest --since full          # 或先从老机搬 var\darvas.db，则跳过
.\darvas.exe doctor                        # schema / 数据新鲜度 / 账本恒等式
```
验收：`ingest` 报 `fresh=true`、当日截面股票数 ≈ 5000+；
`doctor` 全绿；`.\darvas.exe ui` 打开 `http://127.0.0.1:8787` 四视图有数据。

### 阶段 5 · 基准跑（60–90 分钟，决定要不要现在跑）
```powershell
cd I:\Darvas-box-trading
python scripts\base_run.py --years 2011 --mirrors A --force
```
原机 0.344 分/交易日、242 日 ≈ 83 分钟。跑完把这三行发回：
```
交易日 243 日   耗时 xx.x 分钟   → 0.0xx 分/交易日
原机 0.34 分/交易日     加速比 x.xx×
剩余 9 年：串行约 x.x 小时；4 路并行约 x.x 小时
```
> 注：2011 年的库不在 5 个历史年库里，所以这次跑出来的 `bt_baseA_2011.db` 是**新增的第 6 年**，可直接入账。

### 阶段 6 · 接管剩余 9 年（可选，串行约 12–13 小时）
`python scripts\base_run.py --years 2006,2008,2009,2011,2012,2014,2015,2016,2017 --mirrors A`
纪律：**一律串行，勿并发**；中断后重跑会自动识别半成品库并删掉重来；
2006/2008 老库是坏的，本机全新目录不存在污染。

---

## 5. 风险与注意

| # | 风险 | 处置 |
|---|---|---|
| 1 | `tdx.root` 指到 `new_tdx64` 而非 `vipdoc` | 名称全空但不报错。按阶段 4 的写法，指到 vipdoc |
| 2 | 重编译导致二进制哈希变化 | 不装 Go、不改代码；用 `param set` 覆盖路径 |
| 3 | 生产库没搬 → 账本断代 | 部署前先确认老机 `var\backup\darvas_YYYYMMDD.db` 是否还在 |
| 4 | 分片被截断 | 阶段 1 用"大小 + SHA256"双判据，不看进度条 |
| 5 | 年库拷坏的假完成 | 只看 `equity_snapshot` 最后交易日 ≥ 12-25 |
| 6 | 中断产生 440 字节畸形残片 | `base_run.py` 会检测到 equity_snapshot 为空并删除重跑 |
| 7 | 30 份生产备份吃 20 GB | 首次跑 serve 前先确认 I 盘余量；必要时调备份份数 |
| 8 | 04-对话 566 个会话不是可迁移会话 | 已归档，项目路径变了接不上；只作检索用，别指望"接上历史" |

---

## 6. 需要你拍板的 3 件事

1. **生产账本怎么办**：搬老机的 `var\backup\darvas_YYYYMMDD.db`（保留成交/持仓），还是新机 `ingest --since full` 重建（干净但账本归零）？
2. **现在就跑基准 2011 吗**（占 60–90 分钟，跑完才知道新机加速比）？
3. **U 盘包 24.9 GB 是否保留在 I 盘**：留着可随时复核 SHA256，删了回收 25 GB。建议验收通过后再删。

---

## 7. 已执行记录（2026-10-29 22:40 – 23:05 实测）

阶段 1–3 已在本机跑完，全部硬指标通过：

| 阶段 | 结果 |
|---|---|
| 1 拼源库 | 4,404,174,848 字节 ✅ 精确相符；SHA256 `B7E37702…182BA` ✅ 匹配（13.0 秒）；`daily_bar` **29,371,270 行**，区间 1990-12-19 ~ 2026-10-16 |
| 2 代码与程序 | 源码解包 28 MB（cmd/internal/scripts/docs/.git）；`darvas_base.exe` 大小 21,110,272 + SHA256 `D1E8719B…ED44C` ✅ 与原机同一程序；`darvas.exe` 就位；结果包 19 个文件解到 `var\bt\base`；记忆 13 份日记 + MEMORY.md 回填 `.workbuddy\memory`；`git checkout -- .gitignore` 后工作树干净 |
| 3 五年年库 | 21.7 GB 拷贝 **55 秒**（≈400 MB/s）；逐库校验全部 PASS |

| 年 | 交易日 | equity_snapshot 最后交易日 | 判据 ≥12-25 |
|---|---|---|---|
| 2005 | 242 | 2005-12-30 | PASS |
| 2007 | 242 | 2007-12-28 | PASS |
| 2010 | 242 | 2010-12-31 | PASS |
| 2013 | 238 | 2013-12-31 | PASS |
| 2018 | 243 | 2018-12-28 | PASS |

`base_run.py` 实测自检：`ROOT=I:\Darvas-box-trading`、EXE/SRC 均存在、
2005/2007/2010/2013/2018 五年的 `db_complete=True`（会被自动跳过），2011 为 `False`（待跑）。

### 通达信连通性实测（独立临时目录，未污染正式库）

```
param set tdx.root D:\new_tdx64\vipdoc  → ok
ingest --date 2026-10-29（首次）  → 9182 files, 5,683,123 bars, 8m46s
                                   库落到 682 个交易日 / 850 MB（约 2.8 年窗口）
                                   名称同步 52,269 条，当日截面 9,166 只
ingest --date 2026-10-29（稳态）  → 9182 files, 9,182 bars, 2m43s  ← 这才是每日真实成本
```

**结论：`D:\new_tdx64\vipdoc` 这条链接完全跑通**（日线 + `shs.tnf/szs.tnf` 名称表都对），
每日盘后 ingest 稳态约 **2 分 43 秒**，落在 16:00 时点后的窗口内，够用。

> 23:29 在**正式生产库**上又跑了一次同样的 ingest：9182 files / 5,683,123 bars / 名称 52,269 /
> 当日截面 9,166 / 库 850 MB / 682 交易日（2024-01-02 ~ 2026-10-29）/ `doctor` OK，
> 但耗时 **18m44s** —— 因为当时 robocopy 正在同盘复制 25 GB，I/O 抢了 13 分钟。
> **教训：长任务不要并发跑；16:00 的 ingest 窗口里别再安排别的磁盘重活。**

### 遗留（2026-10-29 23:45 更新）
- 生产库已在本机**新建**（schema v18），`tdx.root` 已指向 `D:\new_tdx64\vipdoc`。
- 若要接回老机账本：把老机 `var\backup\darvas_YYYYMMDD.db` 覆盖到 `var\darvas.db`，
  再重设一次 `param set tdx.root`（老库里的值可能还是 H 盘）。

---

## 8. Go 工具链落地（2026-10-29 23:20–23:45）

### 8.1 装在哪

| 项 | 值 | 说明 |
|---|---|---|
| 版本 | **1.27.2 windows/amd64** | 本机 `D:\go` 已有官方 zip，与 `go.mod` 的 `go 1.27` 精确匹配，无需下载 |
| 安装位置 | `I:\tools\go`（264 MB） | 非 C 非 D，与项目同盘 |
| GOPATH | `I:\tools\gopath` | module 缓存 432 MB |
| 编译缓存 | `I:\tools\gocache` | **原默认在 C 盘** `AppData\Local\go-build`，已挪走 |
| PATH | 用户级追加 `I:\tools\go\bin`、`I:\tools\gopath\bin` | 需重开终端生效 |
| 持久配置 | `C:\Users\Administrator\AppData\Roaming\go\env` | 由 `go env -w` 写入 |
| GOPROXY | `https://goproxy.cn,https://proxy.golang.org,direct` | 国内可用 |
| GOTOOLCHAIN | `local` | 禁止自动去下载新工具链（本机网络有坑，见 8.2） |

### 8.2 网络排查的坑：`http_proxy` 指向一个死端口

- DNS 正常、TCP 443 直连可达（`goproxy.cn:443`、`223.5.5.5:443` 均 OPEN）。
- 但 shell 环境里有 `http_proxy=http://127.0.0.1:57669`（宿主注入），**该端口已无响应**。
  于是所有 `curl` 都卡满超时，输出 `000`，看起来像"整机没网"。
- **教训：本机排查网络不能用 curl**（会继承死代理）。用 PowerShell
  `Invoke-WebRequest -NoProxy`，或先 `$env:HTTP_PROXY=''` 再发请求，才测得真实连通性。

### 8.3 编译验证（全部通过）

| 检查 | 结果 |
|---|---|
| `go version` | go1.27.2 windows/amd64 |
| 标准库自检 | hello.exe（2.49 MB）编译并运行正常 |
| `go mod download` | 11 个模块 / 432 MB / **0.7 分钟** |
| `go build ./...` | exit=0 |
| `go build ./cmd/darvas` | 产出 21,108,224 字节（基线 21,106,176，差 **2,048 字节**） |
| 哈希 | 新二进制 `1B6FE79D…` ≠ 基线 `2D2D076C…`（符合预期，不可混用） |
| 离线编译 | 清空下载通道（`GOPROXY=off`）后仍能 9.7 秒编译成功 → 依赖已落本地缓存 |
| 可复现性 | **同一份源码连编两次，字节数相同（21,108,224）但哈希不同**（`1B6FE79D…` vs `9B311B72…`） |

### 8.4 ⚠️ 重编译会删掉 `tdx.root` —— 已实测确证

用**生产库副本**做的对照实验（不碰正式库）：

1. 基线 exe 建库 → `schema v18`，`param get tdx.root` = `D:\new_tdx64\vipdoc`
2. 换成新编译的 exe 再跑一次 → 自动执行 `0019_drop_dead_params.sql` → `schema v19`
3. 再查 `param get tdx.root` → **返回空**

后果链：参数被删 → 回落到环境变量 → 环境变量也没有则回落硬编码 `H:/new_tdx64/vipdoc`
（本机不存在）→ **每日 ingest 静默导入 0 个文件、不报错、日报全空**。

### 8.5 三层兜底（已落地，且实测有效）

`cmd/darvas/main.go:171-182` 的真实逻辑：

```go
func tdxRoot() string {
    if root := os.Getenv("DARVAS_TDX"); root != "" { return root }
    return "H:/new_tdx64/vipdoc"            // 最后兜底
}
root := st.ParamRepo.GetString("tdx.root", tdxRoot())  // 查不到 → 返回默认
```

即优先级 **`param tdx.root` > 环境变量 `DARVAS_TDX` > 硬编码 H 盘**。
`GetString` 在无行时确实返回默认值（`internal/store/repo_param.go:41`，`sql.ErrNoRows → return def`）。

已设置：

| 层 | 值 | 生效范围 |
|---|---|---|
| param | `tdx.root = D:\new_tdx64\vipdoc` | 当前库，随库走（`darvas.exe` 已回读确认） |
| 环境变量 | `DARVAS_TDX = D:\new_tdx64\vipdoc` | **用户级**（机器级需管理员，当前会话没有） |

实证（在 schema 已是 v19、param 已被删空的库上）：

| DARVAS_TDX | 结果 |
|---|---|
| `Z:\no_such_dir\vipdoc` | `warn: read tnf names: tnf not found under Z:\no_such_dir\T0002\hq_cache`、`0 files, 0 bars`、警告「数据不新鲜」 |
| `D:\new_tdx64\vipdoc` | 正常开始扫描（无警告） |

→ 证明环境变量确实被读到，`0019` 之后不会静默失效。

### 8.6 使用纪律

- **历史基线与老机对比的实验，只能用 `var\bt\darvas_base.exe`**（哈希 `D1E8719B…`，与老机同一份）。
- **重编译产物天然不可复现**：实测同一源码连编两次，字节数一致但哈希不同（Go 默认写入
  buildid / VCS 信息）。所以"用重编译的 exe 复现老机结果"这条路根本不成立 —— 想吃回头草，
  只能把老机的 exe 拿回来备份（现在就在 `var\bt\darvas_base.exe`，**别覆盖、别改名**）。
- 新编译的 `darvas.exe` 仅用于改代码自测，**不得用于产出可对比结果**。
- 若真要让源码自洽：新增 `0020` 迁移把参数插回去（migrations 是 append-only，**不能改 0019**）。
- 编译命令（PowerShell，注意清掉死代理）：
  ```powershell
  cd I:\Darvas-box-trading
  $env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:NO_PROXY='*'
  go build -o var\bt\..\..\build\darvas.exe .\cmd\darvas   # 别覆盖项目根的基线 exe
  ```
- 依赖已全量缓存到 `I:\tools\gopath\pkg\mod`，**之后断网也能编译**（无需再下载）；
  换机时可整目录拷走，或执行一次 `go mod vendor` 生成 `vendor/` 随源码走。

---

## 9. U 盘包归档到 F 盘（2026-10-29 23:50 – 00:55）

### 9.1 7 个分项的恢复状态（全部完成）

| 包内 | 大小 | 恢复到 | 验证依据 |
|---|---|---|---|
| `01-代码文档/Darvas-1-source.7z` | 19.6 MB | `I:\Darvas-box-trading` | 287 文件；`git log` 正常（main, 7f3b292） |
| `02-数据/*.part00-02` | 4.2 GB | `var\bt\full_hist_10pct.db` | 4,404,174,848 字节；SHA256 `B7E37702…182BA` **匹配** |
| `03-结果/Darvas-3-results.zip` | 48 KB | `var\bt\base` | 24 文件（5 db + 5 csv + 5 json + 5 html + 4 md） |
| `04-对话/Darvas-0-conversations.7z` | 19.5 MB | `var\conversations` | 474 jsonl + 68 正本 + INDEX.md |
| `05-程序/darvas.exe` | 21.1 MB | 项目根 | SHA256 `2D2D076C…` **匹配** |
| `05-程序/darvas_base.exe` | 21.1 MB | `var\bt` | SHA256 `D1E8719B…ED44C` **与老机一致** |
| `05-程序/7z.exe + 7z.dll` | 2.4 MB | 未复制（留在归档） | 只在解包时用 |
| `06-年库/bt_baseA_2018.db` 等 5 个 | 20.7 GB | `var\bt\base` | 5 个 SHA256 **逐个匹配**（见 §7） |
| `07-记忆/*.md` | 240 KB | `.workbuddy\memory` | 19 文件（13 日记 + MEMORY + 5 automation memory） |

**唯一未恢复**：5 个 automation 的**任务定义**（在老机 `workbuddy.db` 里，包里只带了各自的
`memory.md` 执行记录）。判断：**不恢复** —— 它们是老机运行态的监视器（盯 serve 存活、盯回测日志
出新年份），新机现在没有对应进程，恢复后只会空转或误报。

### 9.2 搬迁踩到的两个坑

**坑 1：`robocopy` 参数被 Git Bash 改坏**
在 Bash 里执行 `robocopy ... /E ...`，MSYS 把 `/E` 转成路径 `E:/`，robocopy 直接报"无效参数 #3"。
→ 用 PowerShell 工具跑，或加 `MSYS2_ARG_CONV_EXCL='*'`。

**坑 2（严重）：`robocopy /MT:8` 与 `Copy-Item` 都会把大文件写坏**

| 轮次 | 方式 | 结果 |
|---|---|---|
| 1 | `robocopy /E /MT:8`（13 分钟时被我强杀） | 8 个大文件 FAILED |
| 2 | `robocopy` 重拷这 8 个（跑满 42 分钟） | `bt_baseA_2013.db` 仍 FAILED |
| 3 | `Copy-Item` 单拷该文件（6.8 分钟） | 仍 FAILED，首处差异固定在 **byte 50,593,793** |
| 4 | **`dd bs=4M conv=fsync`**（7.5 分钟） | **MATCH，`cmp` 零差异** |

结论：**不是盘坏，是复制路径的问题**。往这块盘写 4.5 GB 单文件，必须用块级复制并强制落盘：
```bash
dd if=/i/源路径 of=/f/目标路径 bs=4M conv=fsync status=progress
```

**顺带测出的盘速**：块大小 4 MB 顺序写，前 2 GB 有缓存（1 GB/s），之后稳定掉到 **10 MB/s** ——
25 GB 要 40 分钟。这块 CY-SSV3 盘写入速度异常低，**不适合再当热备份盘**，建议：
- 以后只当冷归档（写完校验一次，之后少动）；
- 重要数据别只放这里，至少两处。

**校验纪律（这次的教训）**：`du`/文件大小完全一致 **不代表内容一致**（8 个坏文件大小全部对得上）。
必须 `sha256sum -c SHA256SUMS.txt` 逐文件比哈希。

### 9.3 归档最终状态

用 `dd` 补好 `bt_baseA_2013.db` 后，全量重跑 `sha256sum -c SHA256SUMS.txt` → **36/36 全 OK**，
F 盘归档完整（26,733,628,075 字节 / 37 文件）。

> 00:54 用户指示：**"别管 F 盘了，只要主 I 盘是好的就行"**。故 F 盘仅作冷归档，不再投时间；
> 重点是下面第 10 节的 I 盘验收。

---

## 10. 移植完成度验收（2026-10-30 01:00 实测）

### 10.1 已在 I 盘验收通过（12 项）

| # | 项目 | 实测证据 | 结论 |
|---:|---|---|---|
| 1 | 源码 | 67 个 .go / 48 个 .py / 9 篇 docs；`git log` = `7f3b292`（main 分支干净） | ✅ |
| 2 | 源库 | `full_hist_10pct.db` 4,404,174,848 字节；`daily_bar` **29,371,270 行**；1990-12-19 ~ 2026-10-16 | ✅ |
| 3 | 回测程序 | `var\bt\darvas_base.exe` 21,110,272 字节，SHA256 `D1E8719B…` 与老机**同一份**；`version` = 0.5.0-m8 | ✅ |
| 4 | 生产程序 | `darvas.exe` 21,106,176 字节，SHA256 `2D2D076C…`；`version` = 0.5.0-m8 | ✅ |
| 5 | 5 个年库 | 逐个 SHA256 与清单匹配；`equity_snapshot` 交易日 242/242/242/238/243，最后交易日全部 ≥ 12-25 | ✅ 5/5 |
| 6 | 结果包 | 24 文件（5 db + 5 csv + 5 json + 5 html + 4 md） | ✅ |
| 7 | 编排器 | `base_run.py` 自检：ROOT/EXE/SRC 全部就位；2005/2007/2010/2013/2018 `db_complete=True`，2011 `False` | ✅ |
| 8 | 一致性 | `check_consistency.py` → C2 表覆盖 16 表 / C3 参数键 26 键 / C4 时刻表 7 任务，**全部一致** | ✅ |
| 9 | 生产库 | 新建 schema v18；`daily_bar` 5,683,123 行 / 682 交易日 / 2024-01-02~2026-10-29；`security` 9,182 只；`doctor` OK | ✅ |
| 10 | 通达信 | `param tdx.root` = `D:\new_tdx64\vipdoc`（已回读）；用户级 `DARVAS_TDX` 同值兜底；实测 ingest 9182 文件 / fresh=true | ✅ |
| 11 | Go 工具链 | 1.27.2 @ `I:\tools\go`；`go build ./...` exit=0；离线编译 9.7 s；依赖缓存 432 MB | ✅ |
| 12 | 记忆 + 对话 | `.workbuddy\memory` 19 文件；`var\conversations` 474 jsonl → 68 正本 + INDEX | ✅ |

### 10.2 尚未完成的 4 项（都不是"移植"缺陷）

| # | 项 | 现状 | 性质 |
|---:|---|---|---|
| 1 | 9 个回测年份 | `2006/2008/2009/2011/2012/2014/2015/2016/2017` 未跑（5/14 完成） | **运行**，非移植；串行约 12–13 小时 |
| 2 | E17 的 14 个年库 | **未随包带来**（14×4 GB ≈ 56 GB，包里只有 base 链的 5 个） | 需要时重跑；结论已存在 `docs/experiments/`（18 份资产） |
| 3 | 生产链自动化 | **无任何 Darvas 计划任务**、`darvas serve` 未运行 | 需用户决定是否接管；老机靠 `DarvasBoxServe` 计划任务 |
| 4 | 老机生产账本 | `var\darvas.db` 是本机新建的空账本（无历史成交/持仓） | 如要接回，从老机拷 `var\backup\darvas_YYYYMMDD.db` 覆盖 |

### 10.3 结论

**移植本身已经全部完成**：代码、数据、程序、配置、记忆五项齐备，且每项都有可复核的硬证据
（哈希 / 行数 / 交易日 / 一致性检查）。

剩下 4 项里，第 1、2 项是"花机器时间跑数据"，第 3、4 项是"要不要接管实盘运行"——都属于**决策**，
不属于移植动作。

