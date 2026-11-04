#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新基线执行器（docs/29 §六之二，2026-10-28 立项；2026-10-29 定案单镜上证）。

MA60 闸门镜子 = 上证 sh000001（单镜；换镜已证伪，双镜擂台已由用户裁定终止）。
三阶段：1 冒烟（单年）→ 2 代表年 → 3 全量 14 年。串行，失败即停。

参数（跑前写死，docs/29 §六之二）：
  共同：pos.newhigh_ladder=on, lookback=252, cutoff=40, units.t1=3/t2=2/t3=1/t4=0.5
        + pool.max_hold=14 + pos.equity_gate=off + gate.ma60.mode=stop + gate.ma60.index=sh000001
  （B 镜中证500 sh000905 仅历史对照保留，不再作为默认/主跑，见 MIRRORS）

用法：
  python scripts/base_run.py --dry-run
  python scripts/base_run.py --years 2007 --mirrors A           # 冒烟：上证
  python scripts/base_run.py --years 2007,2010,2013,2018 --mirrors A   # 代表年
  python scripts/base_run.py --years 2005..2018 --mirrors A     # 全量 14 年（单镜）
  python scripts/base_run.py --cont --from 2005-01-01 --to 2018-12-31 --mirrors A  # 连续单库（docs/50 阶段 C）
  python scripts/base_run.py --cont --from 2005-01-01 --to 2018-12-31 --mirrors A --resume  # 断点续跑（docs/51 阶段 D）
"""
import argparse
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
OUT = BT / "base"
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_base.exe"

MIRRORS = {
    "A": ("sh000001", "上证"),
    "B": ("sh000905", "中证500"),
}

# 共同参数（跑前写死，不含镜子指数——那是每组唯一差异）
COMMON = {
    "pos.newhigh_ladder": "on",
    "pos.newhigh.lookback": "252",
    "pos.newhigh.cutoff": "40",
    "pos.newhigh.units.t1": "3",
    "pos.newhigh.units.t2": "2",
    "pos.newhigh.units.t3": "1",
    "pos.newhigh.units.t4": "0.5",
    "pool.max_hold": "14",
    "pos.equity_gate": "off",
    "gate.ma60.mode": "stop",
    "position.staledays": "2",
}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, path=None):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    if path:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def setargs(mirror):
    idx, _ = MIRRORS[mirror]
    p = dict(COMMON)
    p["gate.ma60.index"] = idx
    return ",".join(f"{k}={v}" for k, v in sorted(p.items()))


def db_complete(path, min_end):
    """库是否「完整可用」（docs/54 #10 加固判据）。

    旧判据只看 equity_snapshot 非空——合成库插 1 行即可骗过（审查实测复现）。
    新判据三条同时满足才算完整：
      1. equity_snapshot 非空（覆盖 2026-10-29 malformed 库踩坑）；
      2. max(date) ≥ min_end（年末终点条件，等价 docs/53 的 ≥12-01；
         连续库 smoke 窗口时传 --to 前推）；
      3. 逐日六任务链：不存在「boxscan/screen/poolsync/signal/plan/report 任一
         非 ok 或缺失」的日期（回放每日六任务全 ok 才算该日跑完，
         堵住「末日成功但中间缺日」的跨洞半成品）。
    损坏库查询即抛错 → 返回 False，同旧行为。"""
    if not path.exists():
        return False
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            n = c.execute("SELECT COUNT(*) FROM equity_snapshot").fetchone()[0]
            if n == 0:
                return False
            maxd = c.execute("SELECT MAX(date) FROM equity_snapshot").fetchone()[0]
            if maxd is None or str(maxd) < min_end:
                return False
            bad = c.execute(
                "SELECT COUNT(*) FROM equity_snapshot es WHERE es.date NOT IN ("
                " SELECT date FROM daily_run"
                " WHERE task IN ('boxscan','screen','poolsync','signal','plan','report')"
                " GROUP BY date"
                " HAVING SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) = 6)"
            ).fetchone()[0]
            return bad == 0
        finally:
            c.close()
    except Exception:
        return False


def run_one(year, mirror, force=False):
    idx, name = MIRRORS[mirror]
    db = OUT / f"bt_base{mirror}_{year}.db"
    logp = OUT / f"bt_base{mirror}_{year}.log"
    # 完整判据（docs/54 #10）：年末终点 + 逐日六任务全 ok（min_end=12-01，
    # 等价 docs/53 判据；该年 12 月后段必在窗口内）。
    if not force and db_complete(db, f"{year}-12-01"):
        log(f"跳过（已完整）：{db.name}", logp)
        return 0
    if db.exists() and not force:
        log(f"发现不完整库（年末终点/逐日六任务不满足），删除重跑：{db.name}", logp)
        for ext in ("", "-wal", "-shm"):
            p = Path(str(db) + ext)
            if p.exists():
                p.unlink()
    sa = setargs(mirror)
    cmd = [str(EXE), "backtest", "--src", str(SRC),
           "--from", f"{year}-01-01", "--to", f"{year}-12-31",
           "--mode", "regime", "--db", str(db), "--set", sa]
    log(f"START {year} {mirror}({name}) idx={idx} → {db.name}", logp)
    log(f"  set: {sa}", logp)
    t0 = time.time()
    with logp.open("a", encoding="utf-8") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time() - t0
    if rc != 0:
        log(f"FAILED {year} {mirror} rc={rc} 耗时 {dt/60:.1f}min", logp)
        return rc
    log(f"DONE {year} {mirror} 耗时 {dt/60:.1f}min", logp)
    return 0


def run_cont(mirror, from_date, to_date, force=False, resume=False):
    """连续单库（docs/50 阶段 C）：一次跑完 [from, to] 一条真实资金约束的账本，
    替代"单年库 + 事后链式复利"。复用 regime 模式 + COMMON 参数，只改窗口推导。
    resume=True 时断点续跑（docs/51 阶段 D）：保留已跑库，交给 darvas --resume-run 从断点续。"""
    idx, name = MIRRORS[mirror]
    db = OUT / f"bt_base{mirror}_cont.db"
    logp = OUT / f"bt_base{mirror}_cont.log"
    # 完整判据（docs/54 #10）：终点 = to_date 前推 35 天（覆盖年末 12-01
    # 等价判据与 smoke 短窗口两种场景）。
    to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    min_end = (to_dt.replace(month=12, day=1) if to_dt.month == 12
               else to_dt.fromordinal(to_dt.toordinal() - 35)).strftime("%Y-%m-%d")
    if not force and not resume and db_complete(db, min_end):
        log(f"跳过（已完整）：{db.name}", logp)
        return 0
    if db.exists() and not force and not resume:
        log(f"发现不完整连续库，删除重跑：{db.name}", logp)
        for ext in ("", "-wal", "-shm"):
            p = Path(str(db) + ext)
            if p.exists():
                p.unlink()
    sa = setargs(mirror)
    cmd = [str(EXE), "backtest", "--src", str(SRC),
           "--from", from_date, "--to", to_date,
           "--mode", "regime", "--db", str(db), "--set", sa]
    if resume:
        cmd.append("--resume-run")
    log(f"START CONT {from_date}~{to_date} {mirror}({name}) idx={idx} → {db.name}"
        f"{' [resume]' if resume else ''}", logp)
    log(f"  set: {sa}", logp)
    t0 = time.time()
    with logp.open("a", encoding="utf-8") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time() - t0
    if rc != 0:
        log(f"FAILED CONT {mirror} rc={rc} 耗时 {dt/60:.1f}min", logp)
        return rc
    log(f"DONE CONT {mirror} 耗时 {dt/60:.1f}min", logp)
    return 0


def parse_years(spec):
    """解析年份参数：支持 "2007,2010" 与 "2005..2018" 两种写法（可混用）。"""
    out = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ".." in tok:
            a, b = tok.split("..", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(tok))
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2007",
                    help="逗号分隔或区间，如 2007,2010,2013,2018 或 2005..2018")
    ap.add_argument("--mirrors", default="A", help="A=上证 B=中证500（B 已随换镜证伪搁置），逗号分隔")
    ap.add_argument("--cont", action="store_true",
                    help="连续单库模式（docs/50 阶段 C）：一次跑完 --from~--to 一条真实资金约束账本")
    ap.add_argument("--from", dest="from_date", default="2005-01-01",
                    help="连续模式窗口起点（默认 2005-01-01）")
    ap.add_argument("--to", dest="to_date", default="2018-12-31",
                    help="连续模式窗口终点（默认 2018-12-31）")
    ap.add_argument("--resume", action="store_true",
                    help="连续模式断点续跑（docs/51 阶段 D）：保留已跑库，交给 darvas --resume-run 从断点续")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    mirrors = [m for m in a.mirrors.split(",")]
    for m in mirrors:
        if m not in MIRRORS:
            raise SystemExit(f"未知镜子 {m}，可选 {sorted(MIRRORS)}")
    OUT.mkdir(parents=True, exist_ok=True)
    if not a.dry_run and not EXE.exists():
        raise SystemExit(f"二进制不存在：{EXE}（须先编译 darvas_base.exe）")
    if a.cont:
        if a.dry_run:
            print(f"二进制 {EXE.name}  源库 {SRC.name}")
            for m in mirrors:
                idx, name = MIRRORS[m]
                tag = " [resume]" if a.resume else ""
                print(f"\n连续单库 {a.from_date}~{a.to_date} {m}({name}) idx={idx}{tag}")
                for kv in setargs(m).split(","):
                    print(f"   {kv}")
            return 0
        for m in mirrors:
            tag = " [resume]" if a.resume else ""
            print(f"\n=== 连续单库 [{a.from_date}~{a.to_date}] {m}{tag} ===", flush=True)
            if run_cont(m, a.from_date, a.to_date, a.force, a.resume) != 0:
                print(f"失败即停：连续 {m}", flush=True)
                return 1
        print("\n连续单库全部完成")
        return 0
    years = parse_years(a.years)
    if a.dry_run:
        print(f"二进制 {EXE.name}  源库 {SRC.name}")
        for y in years:
            for m in mirrors:
                idx, name = MIRRORS[m]
                print(f"\n{y} {m}({name}) idx={idx}")
                for kv in setargs(m).split(","):
                    print(f"   {kv}")
        return 0
    total = len(years) * len(mirrors)
    i = 0
    for y in years:
        for m in mirrors:
            i += 1
            print(f"\n=== [{i}/{total}] {y} {m} ===", flush=True)
            if run_one(y, m, a.force) != 0:
                print(f"失败即停：{y} {m}", flush=True)
                return 1
    print("\n全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
