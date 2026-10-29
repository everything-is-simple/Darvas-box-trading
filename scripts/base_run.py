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


def db_complete(path):
    """库是否「完整可用」：存在 + 能正常读 + equity_snapshot 非空。

    只看文件存在会把「被中断的半成品」误判为已完成（2026-10-29 踩过：
    强杀进程留下 malformed 库，导致全量静默跳过该年）。
    判据用 equity_snapshot（每个交易日一条，回放跑过就必有）——比
    integrity_check 快得多（后者对 4.4GB 库要数十秒），损坏库在查询时
    同样抛 malformed，判据等价。"""
    if not path.exists():
        return False
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return c.execute("SELECT COUNT(*) FROM equity_snapshot").fetchone()[0] > 0
        finally:
            c.close()
    except Exception:
        return False


def run_one(year, mirror, force=False):
    idx, name = MIRRORS[mirror]
    db = OUT / f"bt_base{mirror}_{year}.db"
    logp = OUT / f"bt_base{mirror}_{year}.log"
    if not force and db_complete(db):
        log(f"跳过（已完整）：{db.name}", logp)
        return 0
    if db.exists() and not force:
        log(f"发现不完整库（损坏或无 metrics），删除重跑：{db.name}", logp)
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
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    years = parse_years(a.years)
    mirrors = [m for m in a.mirrors.split(",")]
    for m in mirrors:
        if m not in MIRRORS:
            raise SystemExit(f"未知镜子 {m}，可选 {sorted(MIRRORS)}")
    OUT.mkdir(parents=True, exist_ok=True)
    if not EXE.exists():
        raise SystemExit(f"二进制不存在：{EXE}（须先编译 darvas_base.exe）")
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
