# 43 · 卖出出口分解与参数事实源对账（T4/T5）

> 状态：**代码已实施（2026-11-02）**，待回测重跑验证（realized_pnl 逐笔不变、仅标签变）。
> 定位：工程治理，先于策略实验（docs/29 §五队列第 0 项）。
> 前置：T2 追踪链已完成（`plan_order.reason_json` 已写入真实 box_id + `source_signal_id`）。
> 证据库：`var/bt/t7fix_test/bt_fix_2017.db`（T7 修复版，除权识别已查真实记录）。

---

## 一、结论先行

1. **T5 卖出出口**：根因已定位——`position.close_reason` 只有二分类（`stop`/`manual`），
   而卖出订单 `plan_order.reason_json.signal` **早已有四分类**（`initial_stop` / `time_stop_noprofit`
   / `stop` / `failed_breakout`），只是清仓那一刻没把它提取进 `close_reason`。
   **另有隐藏分类 bug**：除权清仓（`sim:dividend`）走 `ApplyFill` 直接成交、`order==nil`，
   被误记为 `manual`（6 笔）或 `stop`（7 笔），把「规则强平」错当成「人工卖出」。
2. **T4 参数对账**：现状干净。`check_consistency.py` C3 全绿（26 活键、29 死键已清、参数表与种子对齐）。
   唯一盲区 = **生产机（老机 `darvas serve` 0.5.0-m8）实际运行参数值未纳入对账**。

---

## 二、T5 卖出出口现状（实测分布）

`bt_fix_2017.db`（平仓 364 笔）当前 `position.close_reason` 分布：

| close_reason | 笔数 | 实际包含 |
|---|---|---|
| `stop` | 358 | 价格止损 + 箱体下轨止损 + 失败突破 + 时间止损 + 7 笔除权清仓 |
| `manual` | 6 | 6 笔除权清仓（无订单 → 误判）+ 真正人工卖出 |

而 `plan_order.reason_json.signal`（卖出订单，实测分布）已经能拆出：

| signal 值 | 笔数 | 语义 |
|---|---|---|
| `initial_stop` | 1222 | 首腿初始/价格止损（trigger=pos.StopPrice，ST1） |
| `time_stop_noprofit` | 315 | 无利时间止损（staledays 到期仍不获利） |
| `stop` | 21 | 跌破箱体下轨止损（SignalStop） |
| `failed_breakout` | 16 | 突破失败跌回（SignalFailed） |
| （无订单）`sim:dividend` | 13 | 除权除息日强平（match.go §3.8，走 `ApplyFill` 直接成交） |

> 注：`initial_stop` 笔数（1222）远大于平仓笔数，因为它是**持仓期每日常驻**的止损单，
> 绝大多数未触发即被撤（成交的是触发的那张）。信号是「挂单」，`close_reason` 是「成交后的归属」。

---

## 三、六分类映射表（改造目标，已钉死）

最终 `position.close_reason` 六值及其判定来源：

| 最终出口标签 | 数据来源 | 判定方式 |
|---|---|---|
| `stop`（价格止损） | `reason_json.signal == "initial_stop"` | 清仓时读成交订单的 signal |
| `failed_breakout` | `reason_json.signal == "failed_breakout"` | 同上 |
| `time_stop` | `reason_json.signal == "time_stop_noprofit"` | 同上 |
| `stop_box`（箱体下轨止损） | `reason_json.signal == "stop"`（SignalStop） | 同上 |
| `dividend_clear` | `trade_fill.raw_text == "sim:dividend"` | 除权强平，无订单 |
| `manual`（人工） | 真正无订单 **且** 非除权强平 | 兜底 |

### 待定项：`stop` 与 `stop_box` 是否合并

`initial_stop`（首腿紧止损，-3% 量级）与 `stop`（箱体下轨，-10~20%）是**两种性质不同的止损**：
前者是「首腿紧止损」，后者是「跌破箱体」。docs/29 原口径把它们都叫「stop」，但统计上分开更有价值
（能看出大赢家回吐是发生在紧止损还是箱体下轨）。**建议分开**：`stop`（紧止损）vs `stop_box`（箱体下轨）。
此命名待用户拍板。

---

## 四、改造设计（代码，待实施）

**唯一改动点**：`internal/ledger/ledger.go` 的 `applySell`（第 460-463 行）。

现状：
```go
if newQty == 0 {
    reason := "stop" // 计划内止损离场
    if order == nil {
        reason = "manual" // 计划外人工卖出
    }
    l.st.TradeRepo.ClosePosition(pos.ID, f.Date, reason)
    ...
}
```

目标：`applySell` 拿到 `order` 后，从 `order.Reason["signal"]` 提取出口标签；
无订单时判断该笔 fill 是否为 `sim:dividend`（除权强平）。

```go
if newQty == 0 {
    reason := "manual" // 兜底：无订单且非除权 = 人工
    if order != nil {
        reason = exitReasonFromSignal(order.Reason["signal"])
    } else if f.RawText == "sim:dividend" {
        reason = "dividend_clear"
    }
    l.st.TradeRepo.ClosePosition(pos.ID, f.Date, reason)
    ...
}
```

其中 `exitReasonFromSignal` 映射：
- `initial_stop` → `stop`（或 `stop`，见待定项）
- `failed_breakout` → `failed_breakout`
- `time_stop_noprofit` → `time_stop`
- `stop` → `stop_box`（或 `stop`）

### 配套

1. **migration 0023**：`position.close_reason` 无 CHECK 约束（当前是自由 TEXT，见 PRAGMA），
   无需改 DDL；但需**登记六值清单**到 docs/03 ERD（当前 close_reason 语义未在 ERD 枚举化）。
2. **测试**：`ledger` 测试补六分类断言（除权清仓 → `dividend_clear`、人工 → `manual`、
   时间止损 → `time_stop` 等）。
3. **回测重跑**：改的是「统计归属」，**不改撮合/成交价/盈亏**，理论上重跑后每笔 realized_pnl 不变、
   只有 close_reason 标签变。须用 2017 对照验证：`realized_pnl` 逐笔不变、仅标签变化。
4. **历史库**：`base` 基线库（14 年）的 close_reason 是旧二分类，**不回溯改写**（历史事实），
   但新跑的实验库从改版起用六分类。历史二分类库仅在需要出口分解时，用 `plan_order.reason_json.signal`
   联查重算（T5 报告脚本可回填，不改库）。

---

## 五、T4 参数事实源对账（现状 + 待办）

**已干净**（`check_consistency.py` 全绿）：
- C3 参数键对齐：migrations 种子键 == docs/01 §6 活参数表键（26 键）。
- 29 死参数已清（0019）、`max_hold` 7→14 已定案（0018）、`max_growth_days` 已种子化（0020）。

**唯一盲区（待办）**：生产机参数值未纳入对账。老机 `darvas serve` 0.5.0-m8 的 `param` 表实际值，
与代码默认值 + 种子值是否一致，尚未核过。核对方式：
```sql
-- 生产机 darvas.db（只读）
SELECT key, value FROM param ORDER BY key;
```
与 `contract.go` 默认值 + migrations 种子逐键比对，产出差异表。**此项不阻塞 T5，但须在上线前完成。**

---

## 六、下一步

1. **用户拍板两个口径**：① `stop` vs `stop_box` 是否分开；② 除权清仓标签名（`dividend_clear` 是否采纳）。
2. 改 `applySell` + migration 0023 登记 + 测试。
3. 2017 重跑对照验证（realized_pnl 不变、仅标签变）。
4. T4 生产机参数对账（上线前）。
5. 交回 docs/29 队列，进入「止损随 Box 上移事件研究」。
