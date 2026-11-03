#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3-8 升势 / 最高 Box 准入扫描执行器（docs/experiments/e17_p38_framework.md，2026-11-03）。

只动两个变量（buy.uptrend_only / buy.highest_box_mode[+window]），其余参数 100% 复刻
新基线（var/bt/base/bt_baseA_<year>.db 的共同参数，即 docs/44 §2.1 的 B-4 结论基线）。
源库 = full_hist_10pct.db，二进制 = darvas_p38.exe（含 P3-8 准入逻辑，默认 off 与基线逐单等价），
模式 = regime。

档位（7 档，二维组合，非笛卡尔积）：
  off    : 升势 off + 最高 Box off（= 基线等价，HARD① 对照）
  up     : 升势 on  + 最高 Box off（纯升势硬门槛）
  t60    : 升势 off + 最高 Box trend N=60
  t120   : 升势 off + 最高 Box trend N=120
  t250   : 升势 off + 最高 Box trend N=250
  all    : 升势 off + 最高 Box alltime
  up_all : 升势 on  + 最高 Box alltime（框架 §三「升势硬门槛 + 最高 Box 择优」完整形态）

代表年（docs/29 §2.4 通用代表年）：2007（单边牛）/ 2010（震荡）/ 2013（脱钩牛）/ 2018（洗耳熊）。

用法：
  python scripts/e17_p38_bt_run.py --dry-run
  python scripts/e17_p38_bt_run.py --years 2007 --tiers off          # 冒烟：等价性验证
  python scripts/e17_p38_bt_run.py --years 2007,2010,2013,2018 --tiers off,up,t60,t120,t250

注：`all` / `up_all`（历史最高箱及其组合）已于 2026-11-03 13:20 被否决，不再进队列；
    定义保留在 REJECTED_TIERS 中仅供复现历史库。
"""
import argparse
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = ROOT / "var" / "bt"
OUT = BT / "p38_bt"            # 本次扫描库输出目录
SRC = BT / "full_hist_10pct.db"
EXE = BT / "darvas_p38.exe"

# 基线共同参数（100% 复刻 e17_lb4_bt_run.py COMMON；off 档不额外注入 P3-8 三键）
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
    "gate.ma60.index": "sh000001",
    "position.staledays": "2",
    "box.max_growth_days": "0",   # 本轮 P3-8 验证关闭 LB-4 成长门槛，排除交叉干扰
}

# 档位映射：tier -> (uptrend_only, highest_box_mode, window)
# ⚠️ 2026-11-03 13:20 用户裁决：`alltime`（历史最高箱）口径**已否决**，不再跑。
#    2007 证据：n 98→56、已实现 457,133→29,555（−93.5%）、剔除前三 214,934→−78,100（转负）、
#    挡掉 74 笔赚钱交易(+43.8万)只换回 32 笔(+1.3万)。连带的 `up_all`（升势+alltime）一并作废。
TIERS = {
    "off":    ("off", "off",    "120"),
    "up":     ("on",  "off",    "120"),
    "t60":    ("off", "trend",  "60"),
    "t120":   ("off", "trend",  "120"),
    "t250":   ("off", "trend",  "250"),
}

# 已否决档位：保留定义以便复现历史库，但不进扫描队列
REJECTED_TIERS = {
    "all":    ("off", "alltime", "120"),
    "up_all": ("on",  "alltime", "120"),
}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, path=None):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    if path:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def setargs(tier):
    up, mode, win = TIERS[tier]
    p = dict(COMMON)
    if tier != "off":
        p["buy.uptrend_only"] = up
        p["buy.highest_box_mode"] = mode
        p["buy.highest_box_window"] = win
    return ",".join(f"{k}={v}" for k, v in sorted(p.items()))


def db_complete(path, year):
    """库是否完整：存在 + equity_snapshot 覆盖到该年年末（最后交易日）。"""
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
            return bool(maxd) and maxd >= f"{year}-12-01"
        finally:
            c.close()
    except Exception:
        return False


def run_one(year, tier, force=False):
    db = OUT / f"bt_p38_{tier}_{year}.db"
    logp = OUT / f"bt_p38_{tier}_{year}.log"
    if not force and db_complete(db, year):
        log(f"跳过（已完整）：{db.name}", logp)
        return 0
    if db.exists() and not force:
        log(f"发现不完整库，删除重跑：{db.name}", logp)
        for ext in ("", "-wal", "-shm"):
            p = Path(str(db) + ext)
            if p.exists():
                p.unlink()
    sa = setargs(tier)
    cmd = [str(EXE), "backtest", "--src", str(SRC),
           "--from", f"{year}-01-01", "--to", f"{year}-12-31",
           "--mode", "regime", "--db", str(db), "--set", sa]
    log(f"START {year} tier={tier} → {db.name}", logp)
    log(f"  set: {sa}", logp)
    t0 = time.time()
    with logp.open("a", encoding="utf-8") as f:
        rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)
    dt = time.time() - t0
    if rc != 0:
        log(f"FAILED {year} tier={tier} rc={rc} 耗时 {dt/60:.1f}min", logp)
        return rc
    log(f"DONE {year} tier={tier} 耗时 {dt/60:.1f}min", logp)
    # 每完成一库即刷新进度报表（MD + HTML），失败不影响回测主流程
    try:
        stats = ROOT / "scripts" / "e17_p38_stats.py"
        md = ROOT / "docs" / "experiments" / "e17_p38_progress.md"
        htm = ROOT / "docs" / "experiments" / "e17_p38_progress.html"
        import sys as _sys
        subprocess.call([_sys.executable, str(stats), "--md", str(md), "--html", str(htm)])
        log("报表已刷新", logp)
    except Exception as e:
        log(f"报表刷新失败（忽略）：{e}", logp)
    return 0


def parse_years(spec):
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
                    help="逗号分隔或区间，如 2007,2010,2013,2018")
    ap.add_argument("--tiers", default="off",
                    help="逗号分隔档位，如 off,up,t60,t120,t250,all,up_all")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    years = parse_years(a.years)
    tiers = [t.strip() for t in a.tiers.split(",")]
    for t in tiers:
        if t in REJECTED_TIERS:
            print(f"⚠️ 档位 {t} 已于 2026-11-03 被否决（历史最高箱口径），仅在复现历史库时使用")
        elif t not in TIERS:
            raise SystemExit(f"未知档位 {t}，可选 {sorted(TIERS)}")
    OUT.mkdir(parents=True, exist_ok=True)
    if not EXE.exists():
        raise SystemExit(f"二进制不存在：{EXE}")
    if a.dry_run:
        print(f"二进制 {EXE.name}  源库 {SRC.name}")
        for y in years:
            for t in tiers:
                print(f"\n{y} tier={t}")
                for kv in setargs(t).split(","):
                    print(f"   {kv}")
        return 0
    total = len(years) * len(tiers)
    i = 0
    for y in years:
        for t in tiers:
            i += 1
            print(f"\n=== [{i}/{total}] {y} tier={t} ===", flush=True)
            if run_one(y, t, a.force) != 0:
                print(f"失败即停：{y} tier={t}", flush=True)
                return 1
    print("\n全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
