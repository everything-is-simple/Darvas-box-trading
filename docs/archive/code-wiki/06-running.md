# 06 · 运行方式与运维

[← 上一篇：编排层](05-orchestration.md) | [Wiki 索引](README.md) | [下一篇：依赖与测试 →](07-dependencies-and-testing.md)

---

## 1. 前置条件

| 项 | 要求 |
| --- | --- |
| Go | 1.27（`go.mod` 声明；纯 Go SQLite，无需 CGO/GCC） |
| 操作系统 | Windows（开发/生产验证环境）；代码本身跨平台 |
| 数据源 | 通达信客户端，已下载日线到 `vipdoc` |
| 磁盘 | 全历史库约数 GB；默认库 2024 起约数百 MB |

通达信目录约定：

```
H:/new_tdx64/vipdoc/sh/lday/*.day
H:/new_tdx64/vipdoc/sz/lday/*.day
H:/new_tdx64/vipdoc/../T0002/hq_cache/*.tnf
```

TDX 根优先读 `param tdx.root`（0001 种子默认 `H:/new_tdx64/vipdoc`）；仅当该参数缺失时回退 `DARVAS_TDX` 环境变量或硬编码默认。切换真实路径用 `darvas param set tdx.root <路径>`。

---

## 2. 构建

```powershell
# 方式一：脚本
scripts\build.bat

# 方式二：直接
go build -o darvas.exe .\cmd\darvas
```

产物为单文件 `darvas.exe`，拷到同平台即可运行（数据库随之拷贝）。

---

## 3. 数据库与运行目录

| 路径 | 内容 | 是否入库 |
| --- | --- | --- |
| `var/darvas.db` | 主库（WAL） | 否 |
| `var/darvas.db-wal/-shm` | WAL 临时文件 | 否 |
| `var/backup/darvas_YYYYMMDD.db` | 每日 VACUUM 备份（保留 30） | 否 |
| `var/reports/daily_YYYYMMDD.html` | 日报 | 否 |
| `var/reports/weekly_*.html` | 周报 | 否 |
| `var/bt/*.db` | 回测 scratch 库 | 否 |

数据库路径优先级：`--db` > `DARVAS_DB` > `exe目录/var/darvas.db`。

---

## 4. 首次运行

```powershell
# 1. 体检（会创建空库并自动迁移）
.\darvas.exe doctor

# 2. 全量导入（2024 起；首次建议）
.\darvas.exe ingest --since full

# 3. 全历史（仅回测/体制校准需要，耗时长）
.\darvas.exe ingest --since all

# 4. 跑一次完整盘后链
.\darvas.exe run --date 2026-10-27

# 5. 常驻
.\darvas.exe serve --addr 127.0.0.1:8787
```

`ingest` 结束会打印 Verify：最新交易日、是否 fresh、当日截面股票数。若 `fresh=false`，先在通达信「下载数据」再重跑。

---

## 5. 日常运行模式

### 5.1 推荐：`serve` 常驻

启动顺序：

1. `CatchUp()`：前一交易日无任何 ok → 整日补跑；今天已过点 → 链式补跑；周六 → weekly。
2. `Start()`：每 30 秒巡检，按时刻表触发。
3. GUI 只读展示。
4. 每天 `report` 成功后自动备份。

Windows 建议用「任务计划程序」托管 `serve`，避免 IDE/终端关闭导致进程退出。

### 5.2 时刻表

| 时刻 | 任务 |
| --- | --- |
| 16:00 | ingest |
| 16:30 | boxscan |
| 17:00 | screen |
| 17:30 | poolsync |
| 18:00 | signal |
| 18:30 | plan |
| 19:00 | report |
| 周六 08:00 | weekly |

时刻唯一事实源：`scheduler.Schedule` + `docs/01 §4.1/§4.3`，由一致性检查 C4 锁定。

### 5.3 人工职责

| 环节 | 谁 | 做什么 |
| --- | --- | --- |
| 收盘后 | 人 | 通达信「下载数据」 |
| 盘后 | serve | 自动七任务 + 备份 |
| 复核 | 人 | `darvas ui` 看候选/跟踪池/计划 |
| 次日盘前 | 人 | 按日报/计划在券商挂条件单 |
| 午间/收盘后 | 人 | `darvas fills` 录成交 |

---

## 6. CLI 参考

### 6.1 数据与体检

```powershell
darvas ingest [--date D] [--since 2024-01-01|full|all] [--db P]
darvas doctor [--db P]
darvas ladder sh600519 [--db P]
```

`doctor` 检查：schema 版本、证券/行情行数、指数最新日期、止损触发器是否存在、权益恒等式审计、过期 pending 订单数。

### 6.2 流水线与补跑

```powershell
darvas run [--date D]
darvas boxscan|screen|poolsync|signal|plan|report [--date D]
darvas weekly [--date D]      # 缺省锚定最近周六
```

单任务补跑与调度器共用 `pipeline.RerunTask`，均写 `daily_run` 心跳，幂等。

补跑顺序建议与依赖链一致：

```
ingest → boxscan → screen → poolsync → signal → plan → report
```

### 6.3 参数

```powershell
darvas param                 # 列出全部
darvas param get box.volmult
darvas param set box.volmult 2.2
```

参数热调立即生效（下次任务读取）；`param` 表保留 `old_value` 审计。

### 6.4 跟踪池人工操作

```powershell
darvas pool list [stage]                  # stage 可选
darvas pool suspend sh600519 --resume 12.5
darvas pool remove sh600519 --reason "基本面恶化"
```

GUI 只读，所有状态修改走 CLI。

### 6.5 复盘（已交易股票历史库）

```powershell
darvas review list [code]
darvas review add <position_id> --lesson "追高买入" --error-type "追高" --rule "violated"
```

三记录制度：买入理由（`reason_json`）、卖出理由（`close_reason + realized_pnl`）、亏损教训（人工录入）。

### 6.6 成交录入

```powershell
# 文本（同花顺/通达信/CSV 三种格式），默认预览
Get-Content fills.txt | .\darvas.exe fills
.\darvas.exe fills fills.txt --yes

# 炒股手导出 CSV（GBK）
.\darvas.exe fills export.csv --yes
```

支持的文本格式：

| 格式 | 示例 |
| --- | --- |
| 同花顺类 | `20261016 600519 买入 1700.50 100 170050.00` |
| 通达信类 | `2026-10-16 15:00:01 sh600519 贵州茅台 证券买入 1700.50 100` |
| CSV 类 | `2026/10/16,SZ000001,S,12.34,500,6170.00,5.00` |

炒股手 CSV 为 13 列 GBK，严格表头校验；解析后打印**执行质量审核报告**：

1. 漏执行（区间内仍 pending/placed）
2. 价格滑点（成交价 vs 计划限价）
3. 数量偏差
4. 计划外成交 / 重复跳过

**幂等**：同 `code+date+side+price+qty` 自动跳过；宁可拒绝不可错账。

### 6.7 GUI 与常驻

```powershell
darvas ui [--addr 127.0.0.1:8787]
darvas serve [--addr 127.0.0.1:8787]
```

GUI 视图：仪表盘、候选、信号、跟踪池、个股 K 线/箱体、交易记录、时刻表。所有 API 只读。

### 6.8 回测

```powershell
darvas backtest --src I:\Darvas-box-trading\var\full_hist.db `
  --mode progressive --from 2014-01-01 --to 2014-12-31 `
  --set box.volmult=2.2,box.min_width=0.05
```

| 模式 | 用途 |
| --- | --- |
| `progressive` | 逐日灌 K 线，未来数据物理缺席（地面真值） |
| `prefill` | 全历史预灌 + 查询边界自律（更快） |
| `verify` | 双模式重放 + 全决策输出差分 |
| `regime` | 长窗口 + 分体制报告 |

`--set` 只覆盖 scratch 库参数，不碰生产库。

---

## 7. 备份与恢复

### 7.1 自动备份

`BackupDaily` 在每次 `RunDaily` 尾部与 serve 每日 `report` 成功后执行：

```
VACUUM INTO var/backup/darvas_YYYYMMDD.db
```

按文件名排序保留最近 30 份。

### 7.2 手动恢复

```powershell
# 停掉 serve
Copy-Item var\backup\darvas_20261020.db var\darvas.db -Force
# 删除可能残留的 WAL/SHM
Remove-Item var\darvas.db-wal,var\darvas.db-shm -ErrorAction SilentlyContinue
.\darvas.exe doctor
```

恢复后建议跑一次 `darvas run --date <最新交易日>` 补齐。

---

## 8. 排障

| 现象 | 排查 |
| --- | --- |
| `ingest` 后 `fresh=false` | 通达信未下载当日数据；确认 `tdx.root` |
| 调度不触发 | 非交易日/未下载数据；`daily_run` 无心跳；进程是否被托管 |
| 任务显示 failed | `daily_run.error`；单任务补跑定位 |
| GUI 空白/无数据 | 检查 `--db` 是否指向主库；日期下拉是否有数据 |
| 计划订单为 0 | 无信号 / 观察期拦截 / 持仓上限 / 追高闸门 / 无上轨 |
| 卖单重复 | 检查 `pendingSell` 逻辑与订单状态 |
| 止损未上移 | 确认使用 `LatestNonFormingBox`，检查触发器 |
| 推送失败 | 网络代理抖动，重试 1–2 次 |
| 日期显示异常 | 查询日期列未 `CAST(... AS TEXT)` |

常用 SQL：

```sql
SELECT * FROM daily_run WHERE date='2026-10-27' ORDER BY id;
SELECT * FROM param ORDER BY key;
SELECT * FROM plan_order WHERE status='pending';
```

---

## 9. 质量门禁

提交前必须：

```powershell
python scripts\check_consistency.py
go test ./...
```

也可加：

```powershell
go vet ./...
```

一致性检查 C1–C4 详解见 [07](07-dependencies-and-testing.md#3-一致性检查)。

---

## 10. 部署清单

- [ ] `go test ./...` 通过
- [ ] `python scripts/check_consistency.py` 通过
- [ ] `darvas doctor` OK
- [ ] 备份目录可写、磁盘充足
- [ ] `serve` 由计划任务/服务托管
- [ ] GUI 仅监听 `127.0.0.1`
- [ ] 通达信每日下载有人负责
