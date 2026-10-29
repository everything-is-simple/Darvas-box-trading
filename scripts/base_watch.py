#!/usr/bin/env python3
"""新基线逐年监视器 —— 每完成一年，立即生成该年统计报告。

独立于 base_run.py 运行（后者已在跑，不能改它）。只读年库，零写库。
判定「完成」= equity_snapshot 最后日期 ≥ 当年 12-15（防半成品误判）。

产出：
  var/bt/base/base_yearly_reports.md      人类可读，逐年追加
  var/bt/base/year_json/bt_baseA_<年>.json 单年结构化数据
  var/bt/base/base_watch.log               监视器自己的日志

用法：python scripts/base_watch.py [--interval 60] [--once]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from base_year_summary import load_year  # noqa: E402

BASE = ROOT / "var" / "bt" / "base"
OUT_MD = BASE / "base_yearly_reports.md"
OUT_JSON = BASE / "year_json"
STATE = BASE / ".watch_state.json"
LOG = BASE / "base_watch.log"
YEARS = list(range(2005, 2019))


def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # 日志文件被外部占用（如 bat 重定向到同一文件）时不能中断监视
        pass


def db_path(year: int) -> Path:
    return BASE / f"bt_baseA_{year}.db"


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"reported": []}


def save_state(st: dict):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def extra_stats(db: Path) -> dict:
    """补充统计：持有天数、单笔极值、出场原因分布。"""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = c.execute(
            """SELECT p.opened_date, p.closed_date, p.realized_pnl, p.close_reason,
                      p.code, COALESCE(SUM(l.qty*l.price), 0)
               FROM position p LEFT JOIN position_leg l ON l.position_id = p.id
               WHERE p.realized_pnl IS NOT NULL AND p.status = 'closed'
               GROUP BY p.id"""
        ).fetchall()
        hold_days = []
        reasons: dict[str, list] = {}
        best = worst = None
        for od, cd, pnl, reason, code, cost in rows:
            try:
                d1 = datetime.strptime(od, "%Y-%m-%d")
                d2 = datetime.strptime(cd, "%Y-%m-%d")
                hold_days.append((d2 - d1).days)
            except Exception:
                pass
            r = reasons.setdefault(reason or "?", [0, 0.0])
            r[0] += 1
            r[1] += pnl
            rec = (pnl, code, od, cd, cost)
            if best is None or pnl > best[0]:
                best = rec
            if worst is None or pnl < worst[0]:
                worst = rec
        hold_days.sort()
        med = hold_days[len(hold_days) // 2] if hold_days else 0
        return dict(
            hold_median=med,
            hold_max=hold_days[-1] if hold_days else 0,
            hold_min=hold_days[0] if hold_days else 0,
            reasons={k: dict(n=v[0], pnl=v[1]) for k, v in reasons.items()},
            best=dict(pnl=best[0], code=best[1], open=best[2], close=best[3], cost=best[4]) if best else None,
            worst=dict(pnl=worst[0], code=worst[1], open=worst[2], close=worst[3], cost=worst[4]) if worst else None,
        )
    finally:
        c.close()


def render_block(r: dict, x: dict) -> str:
    y = r["year"]
    pf = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
    share = "—" if r["top3_share"] != r["top3_share"] else f"{r['top3_share']*100:.0f}%"
    gap = (r["win_rate"] - r["breakeven"]) * 100
    verdict = "正 edge" if gap > 3 else ("边际" if gap > -3 else "**负 edge**")
    lines = [
        f"\n## {y} 年　完成于 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "| 指标 | 值 | 指标 | 值 |",
        "|---|---|---|---|",
        f"| 净利 | **{r['net']:,.0f}** | 终值 | {r['last_equity']:,.0f} |",
        f"| 笔数 | {r['trades']} | 胜/负 | {r['wins']} / {r['losses']} |",
        f"| 胜率 | {r['win_rate']*100:.1f}% | 保本胜率 | {r['breakeven']*100:.1f}% |",
        f"| 缺口(胜率−保本) | {gap:+.1f}pp → {verdict} | PF | {pf} |",
        f"| 最大回撤 | {r['mdd']*100:.2f}% ({r['mdd_date']}) | 每单位净利 | {r['per_unit']:,.0f} |",
        f"| 前三赢家占比 | {share} | 剔除前三净利 | **{r['net_ex_top3']:,.0f}** |",
        f"| 笔均净利 | {r['net']/r['trades']:,.0f} | 均盈 / 均亏 | {r['avg_win']:,.0f} / {r['avg_loss']:,.0f} |",
        f"| 持有时长(中位/最长/最短) | {x['hold_median']} / {x['hold_max']} / {x['hold_min']} 日 | 交易日数 | {r['days']} |",
        "",
    ]
    if x["best"]:
        b, w = x["best"], x["worst"]
        lines.append(f"**最大单笔**：{b['code']}　{b['pnl']:+,.0f}（{b['open']} → {b['close']}，成本 {b['cost']:,.0f}）")
        lines.append(f"**最大单亏**：{w['code']}　{w['pnl']:+,.0f}（{w['open']} → {w['close']}，成本 {w['cost']:,.0f}）")
        lines.append("")
    lines.append("**出场原因**：" + "　".join(
        f"`{k}` {v['n']} 笔 {v['pnl']:+,.0f}" for k, v in
        sorted(x["reasons"].items(), key=lambda kv: -kv[1]["n"])
    ))
    lines.append("")
    return "\n".join(lines)


def current_year() -> int | None:
    """从 base_run 日志读「当前正在跑哪一年」，避免对写入中的库空转报错。"""
    p = BASE / "base_run_full.log"
    if not p.exists():
        return None
    cur = None
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "] START " in line:
                    parts = line.split("] START ", 1)[1].split()
                    if parts and parts[0].isdigit():
                        cur = int(parts[0])
                elif "] DONE " in line or "] FAILED " in line:
                    cur = None
    except Exception:
        return None
    return cur


def check_once(st: dict) -> bool:
    """扫一遍，返回是否有新年完成。"""
    found = False
    running = current_year()
    for y in YEARS:
        if y in st["reported"] or y == running:
            continue
        p = db_path(y)
        if not p.exists():
            continue
        stats = load_year(p)
        if not stats or not stats.get("complete"):
            continue
        x = extra_stats(p)
        block = render_block(stats, x)
        with OUT_MD.open("a", encoding="utf-8") as f:
            f.write(block)
        OUT_JSON.mkdir(exist_ok=True)
        (OUT_JSON / f"bt_baseA_{y}.json").write_text(
            json.dumps({**stats, **x}, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        st["reported"].append(y)
        save_state(st)
        log(f"✅ {y} 完成 → 净利 {stats['net']:,.0f}，{stats['trades']} 笔，"
            f"胜率 {stats['win_rate']*100:.1f}%，MDD {stats['mdd']*100:.2f}%，已写入报告")
        found = True
    return found


def _run_finished() -> bool:
    """base_run 是否已收尾：看最后一次 run started 之后有没有 run exited。"""
    p = BASE / "base_run_full.log"
    if not p.exists():
        return False
    try:
        last_start = last_exit = -1
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if "run started" in line:
                    last_start = i
                elif "run exited rc=" in line:
                    last_exit = i
        return last_start != -1 and last_exit > last_start
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    if not OUT_MD.exists():
        OUT_MD.write_text(
            "# 新基线（单镜上证）逐年统计 · 跑完一年即出一年\n\n"
            "> 参数：newhigh_ladder=on lookback=252 cutoff=40 units=3/2/1/0.5 "
            "max_hold=14 equity_gate=off gate.ma60=stop@sh000001 staledays=2\n"
            "> 生成：`scripts/base_watch.py`（只读年库，完成判据 = 数据到当年 12-15 之后）\n",
            encoding="utf-8",
        )

    st = load_state()
    log(f"监视器启动：已报告 {st['reported'] or '无'}，目标 {YEARS[0]}-{YEARS[-1]}")

    if a.once:
        check_once(st)
        return

    idle_rounds = 0
    while True:
        try:
            if check_once(st):
                idle_rounds = 0
            else:
                idle_rounds += 1
            if len(st["reported"]) >= len(YEARS):
                log("🎉 14 年全部完成，监视器退出")
                return
            if _run_finished() and idle_rounds >= 2:
                missing = [y for y in YEARS if y not in st["reported"]]
                log(f"⚠️ base_run 已退出，仍未完成年份：{missing}，监视器退出")
                return
        except Exception as e:
            log(f"⚠️ 轮询异常 {type(e).__name__}: {e}")
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
