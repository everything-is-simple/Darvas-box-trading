#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-6a 阶段 1 链式执行器：等基线档完成 → 核 HARD① → 通过才续跑其余档。

为什么要链：单档约 1.8h，5 档串行近 9h，不能靠人守在会话里等。
但 HARD①（基线可复现）是生死线——若 staledays=2 跑不回历史基线的样子，
其余四档的差异就无法归因于 staledays，**必须先核对再决定是否续跑**。

本脚本只做三件事：
1. 轮询基线档日志，等 DONE / FAILED；
2. 跑 p26_report.py --check 取 HARD① 结论；
3. HARD① 通过 → 串行启动其余档；不通过 → 立即停止并留证据。

用法：python scripts/p26_chain.py
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
P26 = ROOT / "var" / "bt" / "p26"
PY = sys.executable
BASE_LOG = P26 / "p26_sd2_2012.log"
CHAIN_LOG = P26 / "p26_chain.log"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with CHAIN_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def wait_base(deadline_h=4.0):
    """等基线档结束：日志出现 DONE / FAILED。"""
    t0 = time.time()
    while time.time() - t0 < deadline_h * 3600:
        if BASE_LOG.exists():
            txt = BASE_LOG.read_text(encoding="utf-8", errors="replace")
            if "FAILED 2012 staledays=2" in txt:
                return "failed"
            if "DONE 2012 staledays=2" in txt:
                return "done"
        time.sleep(60)
    return "timeout"


def check_hard():
    """跑报告生成器的校验层，取 HARD① 结果。"""
    r = subprocess.run([PY, str(ROOT / "scripts" / "p26_report.py"), "--check"],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=3600)
    print(r.stdout, flush=True)
    for line in r.stdout.splitlines():
        if line.startswith("HARD①") and "2012" in line:
            return "PASS" in line, line
    return False, r.stdout.strip()[:500]


def main():
    log("链式执行器启动，等待基线档 2012 staledays=2 …")
    st = wait_base()
    log(f"基线档状态：{st}")
    if st != "done":
        log("基线档未正常完成，链停止")
        return 1
    ok, line = check_hard()
    log(f"HARD① 结果：{line}")
    if not ok:
        log("HARD① 不通过 —— 单变量对照的前提不成立，其余档一律不跑。"
            "请先查二进制/参数/迁移差异。")
        return 2
    log("HARD① 通过，启动其余四档（0 / 5 / 10 / 100000），串行 …")
    r = subprocess.run([PY, str(ROOT / "scripts" / "p26_run.py"),
                        "--years", "2012", "--staledays", "0,5,10,100000"],
                       cwd=str(ROOT), text=True)
    if r.returncode != 0:
        log(f"其余档执行失败 rc={r.returncode}")
        return 3
    log("五档全部完成，生成报告 …")
    subprocess.run([PY, str(ROOT / "scripts" / "p26_report.py")], cwd=str(ROOT))
    log("阶段 1 完成，报告已生成 docs/experiments/e17_staledays_report.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
