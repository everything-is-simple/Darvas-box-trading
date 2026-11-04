# 52 · 阶段 E 日追加 + CatchUp 多日补跑（永续闭环）实施规格

> 定位：docs/49 §五 5.2 改造③「日追加」+ §5.3「生产端 CatchUp 多日补跑」。
> 依赖：阶段 B（基线身份 ✅）+ 阶段 C（连续单库 ✅）+ 阶段 D（断点续跑 ✅）。
> 本文是阶段 E 的**唯一实施规格**，代码照此写。

---

## 一、目标与语义承诺

把 docs/49 §五 5.1 的「D 日之后能**日追加**，中断能**断点续跑**」补上最后一块——
**日追加（append）**，使连续库成为真正的「永续账本」：不用整段重跑，每天只追加新的一天。

阶段 E 分两个**独立**子任务，性质不同、可并行：

| 子任务 | 补哪个缺口 | 层面 | 依赖长跑库？ |
|---|---|---|---|
| **E1 生产端 CatchUp 多日补跑** | F8（停机一周留洞） | 生产 `scheduler.go` | ❌ 不依赖 |
| **E2 回测「日追加」append** | F3（整段重跑） | 回测 `backtest.go` + `base_run.py` | ✅ 依赖连续库 |

---

## 二、E1 生产端 CatchUp 多日补跑（F8）

### 2.1 现状（代码实读，scheduler.go:167-191）

`CatchUp()` 现在只做三件事：
1. **① 前一交易日整日补跑**：`prev = PrevDate(sh000001, today)`，若 `prev` 无任何 ok 心跳 → 整日补跑。
2. **② 今天已过点的触发点立即消费**：`fireDue(now)`。
3. **③ 周六 weekly 周报**：`fireWeekly(now)`。

**缺口**：① 只补 `prev`（**前一交易日**）。若停机一周（6 个交易日），只补前一天，中间 5 天留洞——
这 5 天的数据既不会自动回放（包注释明说「更早的历史缺口不自动回放」），也不会被手动补
（用户不知道有洞）。

### 2.2 改造目标

① 从「只补前一交易日」改成「**从断点连续补到前一交易日**」：

```
断点 = 生产链（ingest+boxscan+screen+poolsync+signal+plan+report）全 ok 的最大日期
（无任何完整日 → 用「DB 日历里最早缺失 ok 心跳的日期」兜底）

for date in [断点+1, ..., 前一交易日]:
    if date 是交易日（DB 有 K 线或 TDX 有数据）:
        runOverdue(ctx, date, endOfDay(date))
```

停机一周 → 从断点连续补 6 天，不留洞。

### 2.3 关键决策（需拍板）

**决策①：生产链「全 ok」判据用几任务？**

生产链 = `ingest + boxscan + screen + poolsync + signal + plan + report` = **7 任务**。
阶段 D 的 `LastCompleteDate()` 只数 6 个**回放**任务（`boxscan/screen/poolsync/signal/plan/report`），
**不含 ingest**——那是回测断点口径，不是生产断点口径。

生产 CatchUp 需要的是**生产断点**：某日 7 任务全 ok 才算「这天跑完」，否则续补这天。
→ 需要新增一个 `LastCompleteProdDate()`（或让 `LastCompleteDate()` 加参数），口径=7 任务含 ingest。

**决策②：断点找不到时（新库/全空）怎么兜底？**

新库 `daily_run` 全空 → `LastCompleteProdDate()` 返回空串。此时不能「从断点+1」无限回补
（会从 2005 年补到现在，14 年×7 任务不可行）。
→ 兜底：断点为空时，**沿用现有「前一交易日整日补跑」逻辑**（`PrevDate` + `anyOK`），
  不触发多日回补。多日回补只在「库已有历史、中间留洞」的场景触发。

**决策③：多日回补的「交易日」判定**

补的每一天都要判「是否交易日」（DB 有 K 线 / TDX 有数据）。非交易日跳过。
用现有 `isTradingDay()` 即可。

### 2.4 改造落点

- `internal/store/repo_run.go`：新增 `LastCompleteProdDate()`（7 任务含 ingest 全 ok 的最大日期）。
- `internal/scheduler/scheduler.go`：`CatchUp()` 的 ① 改为「断点连续补」：
  ```
  prodLast = st.RunRepo.LastCompleteProdDate()
  if prodLast == "":
      # 兜底：沿用现有「前一交易日整日补跑」
      原逻辑
  else:
      # 从 prodLast+1 连续补到 prev（前一交易日），每天判交易日
      cur = NextDate(sh000001, prodLast)
      while cur != "" and cur < today:
          if anyOK(cur) == false or 未完整:
              runOverdue(ctx, cur, endOfDay(cur))
          cur = NextDate(sh000001, cur)
  ```
- `internal/scheduler/scheduler_test.go`：新增测试——停机多日（DB 日历里有多个交易日、中间几天无 ok 心跳）→ `CatchUp()` 从断点连续补全。

### 2.5 验收（V7）

1. **V7-1 多日补跑**：DB 日历有 10-13~10-16 四个交易日，10-13 已全 ok、10-14/15/16 无心跳，
   周六 10-17 启动 `CatchUp` → 连续补 10-14/15/16 三天（非交易日跳过），不留洞。
2. **V7-2 断点空兜底**：新库全空 → 仍走「前一交易日整日补跑」，不触发多日回补（不无限回补）。
3. **V7-3 幂等**：补完后再次 `CatchUp` → 不重跑（已 ok 跳过）。

---

## 三、E2 回测「日追加」append（F3）

### 3.1 现状

阶段 C/D 已让 `runRegime` 支持「断点续跑」（`--resume-run` 从断点续，不重建）。
「日追加」是它的**日常形态**：连续库跑到 D 日，第二天只 append D+1 日，而非重跑整段。

### 3.2 关键认识（需确认，可能已有）

`--resume-run` 的语义其实**已经覆盖「日追加」**：scratch 存在 + 身份一致 → 从断点续，
只 Replay 断点之后的交易日。若 `--to` 只比断点晚 1 天，就只 append 1 天。

→ **E2 可能只是「`base_run.py` 加一个 `--append-to <日期>` 的便捷入口」**，底层复用 `--resume-run`。
   需要先验证：`--resume-run --to 断点+1日` 是否真的只追加 1 天（而非重跑）。

### 3.3 待定（E2 依赖连续库先跑出来，本规格先列，不急着写代码）

- 连续库 `bt_baseA_cont.db` 尚未产出（需白天 6.6h 长跑）。
- E2 的「日追加」入口设计，等连续库跑出来后，用真实产物验证 `--resume-run` 的 append 语义。

---

## 四、边界与不做的事

- **E1 不做**：生产端「历史任意日期的自动回放」（数据多半未入库，用 `darvas run --date X` 手动补）。
  E1 只做「停机期间**已入库/已下载**的交易日连续补跑」。
- **E2 不做**：阶段 F（DarvasBox 身份链 dead_date）、阶段 G（牛熊攻防）——那是另外两个特征。

---

## 五、依赖与顺序

```
阶段 B/C/D（✅）──→ 阶段 E
                     ├─ E1 生产 CatchUp 多日补跑（不依赖长跑库，可立即做）
                     └─ E2 日追加 append（依赖连续库长跑产物）
```

**建议顺序**：先做 E1（纯代码，立即闭环 + 可测），E2 等 14 年连续库跑完再做。
