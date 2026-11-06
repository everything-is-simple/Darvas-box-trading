#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""K1 前视截尾检验比对脚本（docs/65 阶段 0 / docs/60 K1）。

目的：证明箱体边界不含未来信息。箱体「成立」需回溯确认，天生有前视陷阱。
方法：全窗库 vs 截尾库（终点前推 60 交易日），比对重叠窗口的持仓序列。

判断依据（见 docs/60 K1 行）：
  仓位序列逐行一致 = 通过；重叠区间任一建仓/清仓/持仓不同 = 前视污染，全窗作废。

比对字段（用户拍板只比这五字段，派生值不比）：
  position 的 (date, code, qty, opened_date, entry_box_id, closed_date, close_reason)
  其中 date = 该持仓在当日存在的时间点；qty/opened_date/entry_box_id 刻画「哪一腿」，
  closed_date/close_reason 刻画「何时为何清仓」。

实现：对两个库，把 position 展开成「逐日持仓快照」：
  对每笔 position（opened_date ~ closed_date 的每个交易日，未平仓用窗口末日），
  记 (date, code, qty, opened_date, entry_box_id) 为「在仓标记」；
  清仓日额外记 (closed_date, code, close_reason) 为「清仓标记」。
  比对截尾窗口（<= 截尾终点）内的全部标记。

用法：
  python scripts/k1_compare.py --full var/bt/base/bt_baseA_cont_adj.db \
    --trunc var/bt/arms/bt_k1_trunc.db --cut 2018-09-28
"""
import argparse
import sqlite3
from collections import defaultdict


def trade_calendar(c, start="2005-01-01"):
    """取库内交易日历（equity_snapshot 每天一条，天然是交易日）。"""
    rows = c.execute(
        "SELECT date FROM equity_snapshot WHERE date>=? ORDER BY date", (start,)
    ).fetchall()
    return [r[0] for r in rows]


def position_marks(c, cutoff):
    """展开 position 为 (date, code, qty, opened, box) 在仓标记 + (date, code, reason) 清仓标记。

    返回两个 dict：hold[date] -> set of (code,qty,opened,box)；close[date] -> set of (code,reason)。
    未平仓持仓（status='open'）其「在仓」持续到 min(closed_date, cutoff)；
    这里只需覆盖 <= cutoff 的交易日，故用 cutoff 截断即可，不引入未来信息。
    """
    hold = defaultdict(set)
    close = defaultdict(set)
    cal = trade_calendar(c)
    # 逐日定位：用日期索引加速
    idx = {d: i for i, d in enumerate(cal)}
    rows = c.execute(
        "SELECT code, opened_date, entry_box_id, qty, closed_date, close_reason "
        "FROM position ORDER BY opened_date"
    ).fetchall()
    for code, opened, box, qty, closed, reason in rows:
        if opened not in idx:
            continue  # 建仓日在窗口前，跳过（截尾库/全窗库对称处理）
        end = closed if closed and closed <= cutoff else cutoff
        # 在仓区间 [opened, end]，逐个交易日标记
        i0 = idx[opened]
        # end 可能不在 cal（如 closed 在窗口外），用 <= end 截断
        for d in cal[i0:]:
            if d > end:
                break
            hold[d].add((code, qty, opened, box))
        # 清仓标记：仅在清仓日且 <= cutoff
        if closed and closed <= cutoff:
            close[closed].add((code, reason))
    return hold, close


def compare(full_db, trunc_db, cutoff):
    fc = sqlite3.connect(f"file:{full_db}?mode=ro", uri=True)
    tc = sqlite3.connect(f"file:{trunc_db}?mode=ro", uri=True)
    try:
        fhold, fclose = position_marks(fc, cutoff)
        thold, tclose = position_marks(tc, cutoff)
    finally:
        fc.close()
        tc.close()

    # 重叠窗口 = 两库共同覆盖、且 <= cutoff 的交易日
    cal = trade_calendar(sqlite3.connect(f"file:{full_db}?mode=ro", uri=True))
    days = [d for d in cal if d <= cutoff]

    diffs = []
    for d in days:
        fh, th = fhold.get(d, set()), thold.get(d, set())
        fcl, tcl = fclose.get(d, set()), fclose.get(d, set())
        if fh != th:
            diffs.append((d, "hold", sorted(fh - th), sorted(th - fh)))
        if fcl != tcl:
            diffs.append((d, "close", sorted(fcl - tcl), sorted(tcl - fcl)))

    print(f"重叠窗口：{days[0]} ~ {days[-1]}（{len(days)} 交易日，截尾终点 {cutoff}）")
    print(f"全窗库持仓标记日期数：{len(fhold)}；截尾库：{len(thold)}")
    print(f"差异条数：{len(diffs)}")
    if diffs:
        print("\n=== 差异明细（前 50 条）===")
        for d, kind, f_only, t_only in diffs[:50]:
            print(f"[{d}] {kind}: 全窗有={f_only} 截尾有={t_only}")
        if len(diffs) > 50:
            print(f"... 共 {len(diffs)} 条，仅显示前 50")
        print("\n结论：❌ 重叠区间仓位序列不一致 → 存在前视污染，全窗结论作废")
        return 1
    print("\n结论：✅ 重叠窗口仓位序列逐行一致 → 箱体边界不含未来信息，K1 通过")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", required=True, help="全窗连续库")
    ap.add_argument("--trunc", required=True, help="截尾连续库")
    ap.add_argument("--cut", required=True, help="截尾终点 YYYY-MM-DD（前推 60 交易日的末日）")
    a = ap.parse_args()
    raise SystemExit(compare(a.full, a.trunc, a.cut))


if __name__ == "__main__":
    main()
