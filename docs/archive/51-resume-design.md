> **📦 已归档（2026-11-04 文档治理）**：阶段 D 已落地并实测，**判据 V6-1…V6-5 已折叠进
> [`docs/49-baseline-rebuild-charter.md`](../49-baseline-rebuild-charter.md) §六**；V6-3 已于同日加固为
> 「续跑一律拒绝 `--set`」（原实现漏 7 个 default-only 键）。代码为事实源（`darvas backtest --resume-run`）。
> 本稿仅作设计语义留痕，**不再作为事实源**。

# 51 · 阶段 D 断点续跑（`--resume-run`）实施规格

> 定位：docs/49 §五 5.2 改造②「断点续跑」。依赖阶段 B（基线身份，已落地）+ 阶段 C（连续单库，已落地）。
> 本文是阶段 D 的**唯一实施规格**，代码照此写。

---

## 一、目标与语义承诺

从 docs/49 §5.4 复制三条承诺，本阶段只落地「可续」这一条：

- **幂等**：同一窗口重跑，产物全等（`daily_run` UNIQUE 兜底 + `equity_snapshot` 非空判据）。
- **可续**（本阶段）：中断后从断点续，**不等同于从头重跑**，也不丢已跑结果。
- **可对账**（依赖阶段 B）：断点前后账本连得上，靠基线身份 + 参数指纹。

**一句话**：一条 14 年连续库跑到第 6 年崩了，`--resume-run` 能从第 6 年的断点继续，而不是删库重跑 6 年。

---

## 二、三个已拍板的决策（2026-11-04 用户拍板）

| 决策 | 结论 |
|---|---|
| 续跑入口 | **复用 regime 模式加 `--resume-run`**，不新建命令 |
| 断点判据 | **六任务全 `ok` 才算某日跑完**（`COUNT(status='ok')=6`） |
| 身份校验 | **严格校验、不一致拒绝续跑**（防串基线） |

---

## 三、现状诊断（已摸清的代码事实）

### 3.1 断点判据已存在

`daily_run` 表（0001_init.sql:224）：
```sql
UNIQUE(date, task)   -- date + task 唯一
status IN ('running','ok','failed','skipped')
```

- 六任务链 = `boxscan, screen, poolsync, signal, plan, report`（`backtest.go:73`）。
- **断点 = `MAX(date)` 满足 `COUNT(status='ok') = 6`** 的那一天。
- 某日任一任务 `failed`/`running` → 该日「未完成」，续跑从该日**重跑**（幂等，UNIQUE 兜底不重复插入）。

### 3.2 头号障碍：Bootstrap「存在即删」

`Bootstrap`（backtest.go:371）当前逻辑：
1. `os.Remove(scratch + ext)`（删掉旧 scratch）
2. `copyFile(src, scratch)`（重新拷贝源库）
3. `rawWipeExisting` + `wipeTables`（清空全部事务态）

`runRegime`（backtest.go:212）**每次都无条件调 `Bootstrap`** → scratch 存在即删。这是断点续跑的直接矛盾：续跑需要**保留已跑结果**，但 Bootstrap 第一行就删了它。

### 3.3 对账锚：阶段 B 已落地

`baseline` 表（0027）已有 `param_fingerprint` + `engine_sha256` + `latest` 标记。但**只有写方法 `RecordBaseline`，没有读方法**——断点续跑需要读 `latest=1` 那行来做校验，这部分要补。

### 3.4 命名冲突：`--resume` 已被 pool 占用

`backtest` 命令的断点续跑 flag **不能叫 `--resume`**——因为所有子命令共享同一个
`flag.FlagSet`（main.go:40 `fs := flag.NewFlagSet(cmd, ...)`），而 `pool suspend --resume <价>`
（挂起激活价，float64）已注册了 `resume` 这个名字。实测 `go build` 后运行 `darvas backtest`
会 panic `flag redefined: resume`。

**解决**：backtest 的断点续跑 flag 命名为 **`--resume-run`**（bool），语义仍是「续跑」，
避开与 pool 的 `--resume`（float）冲突。`base_run.py` 的 Python 侧开关名保留 `--resume`
（独立的 argparse 命名空间，不冲突），内部透传给 darvas 时拼成 `--resume-run`。

---

## 四、实施方案（按依赖顺序）

### 4.1 store 层：补两个读取方法（repo_baseline.go / repo_run.go）

**① `LatestBaseline()`**（repo_baseline.go 新增）：
```go
// LatestBaseline 读本库最新一次重建的基线身份（latest=1）。
// 返回 (baselineID, paramFingerprint, engineSHA256, error)。
func (s *Store) LatestBaseline() (id, fingerprint, engineSHA string, err error)
```
- SQL：`SELECT baseline_id, param_fingerprint, engine_sha256 FROM baseline WHERE latest=1`
- 无行 → 返回错误「无基线身份（未跑过 RecordBaseline）」。

**② `LastCompleteDate()`**（repo_run.go 新增）：
```go
// LastCompleteDate 六任务全 ok 的最大完成日（断点）。无则返回空串。
func (s *Store) LastCompleteDate() (string, error)
```
- SQL：`SELECT date FROM daily_run WHERE status='ok' GROUP BY date HAVING COUNT(*)=6 ORDER BY date DESC LIMIT 1`
- 注意：`sixTasks` 长度是 6，但 `daily_run` 里可能混入 `ingest`/`weekly` 等其他任务——用 `COUNT(*)=6` 会漏。**更稳妥**：`HAVING COUNT(*) = (SELECT COUNT(DISTINCT task) FROM daily_run WHERE date = d)` 太绕。
- **改用硬编码 6**，与 `sixTasks` 语义绑定，但在注释里写死「回放六任务链，不含 ingest/weekly」。或者更简单：`WHERE task IN ('boxscan','screen','poolsync','signal','plan','report') AND status='ok' GROUP BY date HAVING COUNT(*)=6`。

### 4.2 backtest 层：Options 加 `Resume` 字段 + `runRegime` 分流

**① `Options` 加字段**（backtest.go:62）：
```go
Resume bool // 断点续跑：scratch 已存在且身份一致时，从断点续，不重建
```

**② `runRegime` 改造**（backtest.go:198）：

```
if o.Resume && scratch 存在 && 可续：
    1. store.Open(scratch)（只读校验）
    2. LatestBaseline() → 读指纹/引擎hash
    3. 校验指纹 == 本次参数指纹、引擎hash == 本次 os.Executable() sha256
       → 不一致：返回错误「基线身份不一致，拒绝续跑」
    4. LastCompleteDate() → 断点
    5. days 过滤：只保留 date > 断点 的部分
    6. 跳过 Bootstrap（不清库），跳过 recordBaseline（身份已在）
    7. Replay(scratch, days[断点后:], nil)
else:
    原逻辑：Bootstrap → applyParamOverrides → recordBaseline → Replay
```

**关键：身份校验的「本次参数指纹」怎么算？**

`recordBaseline` 内部调用 `RecordBaseline` 才产生指纹。续跑时**不重建**，所以要用「本次 CLI 参数」独立算一个指纹来对比。

- 方案：把 `RecordBaseline` 的指纹计算逻辑抽成一个纯函数 `paramFingerprint(params map[string]string) string`，供「写入」和「续跑校验」两处复用。
- 续跑校验时：`store.Open(scratch)` → `ParamRepo.All()` → `paramFingerprint(...)` → 与 `LatestBaseline().fingerprint` 比。
- 但注意：续跑时 scratch 里的 param 表**已经是 applyParamOverrides 之后的状态**（上次跑时写的）。所以直接读 scratch 的 param 表算指纹，与 baseline 表存的指纹比即可——**不用重新注入 --set**。

**引擎 hash 校验**：`os.Executable()` 的 sha256 与 `LatestBaseline().engineSHA` 比。若换二进制 → 拒绝续跑（HARD① 铁律）。

**参数冲突校验（V6-3，2026-11-04 补）**：续跑不改参数，若 CLI 带了 `--set` 且与库内已生效值不一致，
`resumeRegime` 在指纹校验**之前**先逐键比对 override，冲突即拒绝（报「续跑不允许改参数」）。
否则「用户以为改了参数、实际静默忽略」——旧实现 resume 分支完全不用 `o.ParamOverrides`，
V6-3 无法经 CLI 触发，是真实缺口。与库内一致（或未带 `--set`）才放行。

### 4.3 cmd/darvas 层：加 `--resume-run` 开关

main.go 的 backtest 子命令：
```go
btResume := fs.Bool("resume-run", false, "断点续跑：scratch 已存在且基线身份一致时，从断点续")
```
传入 `backtest.Options{... Resume: *btResume ...}`。注意 flag 名是 `resume-run`（见 §3.4 冲突）。

### 4.4 base_run.py 层：连续模式透传

`base_run.py` 的 `run_cont`（阶段 C 已加）加 `--resume`（Python 侧开关名，独立 argparse 命名空间）
透传，内部拼成 darvas 的 `--resume-run`，让连续库中断后可续：
```
python scripts/base_run.py --cont --from 2005-01-01 --to 2018-12-31 --resume
```
- `--resume` 时：不删 `bt_baseA_cont.db`，直接续跑（传给 darvas `--resume-run`）。
- 非 `--resume`：现有逻辑（删库重建）。

---

## 五、验收判据（V6）

跑通以下场景即算阶段 D 完成：

1. **V6-1 幂等**：同一窗口跑两遍（第二遍 `--resume-run`），第二遍识别「已跑完」→ 0 个交易日需要续 → 直接报「已是最新，无断点可续」退出。

2. **V6-2 断点续跑**：跑 2005-2006 连续窗口到中途 kill → `--resume-run` → 从 `MAX(ok date)+1` 续，不重跑前段；续跑后 `daily_run` 无重复、`equity_snapshot` 连续。

3. **V6-3 身份拒续**：改一个参数（如 `position.staledays=3`）再 `--resume-run` → 拒绝续跑（指纹不一致）。

4. **V6-4 换二进制拒续**：换一个旧 darvas.exe 再 `--resume-run` → 拒绝续跑（引擎 hash 不一致）。

5. **V6-5 六任务全 ok 判据**：某日只跑完 5 个任务（1 个 failed）→ `LastCompleteDate` 返回前一日 → 续跑从该「未完成日」重跑。

---

## 六、边界与不做的事

- **不做**：阶段 E（日追加 append）、阶段 C 的「实际跑 14 年库」（另排白天长任务）。
- **不做**：`pool suspend --resume` 的语义改造（那是 B2 条件挂起，与本阶段无关）。
- **命名冲突（已解决）**：backtest 断点续跑 flag 叫 `--resume-run`（bool），避开 pool 的
  `--resume`（float）——两者共享同一 flagset，同名会 panic（§3.4）。

---

## 七、依赖与顺序

```
阶段 B（基线身份，✅）──┐
                        ├─→ 阶段 D（本规格）
阶段 C（连续单库，✅）──┘
                        └─→ 阶段 E（日追加）依赖 D
```

- 本阶段**依赖**：B 的 `baseline` 表（读身份）、C 的 `base_run.py --cont`（透传 resume）。
- 本阶段**被依赖**：E（日追加 append 需要先有断点续跑）。
