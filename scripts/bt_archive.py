#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测库归档器 —— 把跑完的实验库搬到冷存储盘，校验通过才删源。

背景：单个回测库 4.15 GB 里 4.10 GB 是全历史行情副本（与 full_hist_10pct.db
逐行相同），真正的结果只有 ~50 MB。I 盘（NVMe 工作盘）只留活跃库，跑完即归档。

用法：
    python scripts/bt_archive.py --files var/bt/p26/*.db --dst A:/Darvas-archive/p26
    python scripts/bt_archive.py --files <库> --dst <目录> --delete      # 校验通过后删源
    python scripts/bt_archive.py --manifest                              # 打印归档台账

纪律：
- 校验 = sha256(源) == sha256(目标) 且 position/equity_snapshot 业务统计全等；
- 任一校验失败 → 删掉目标半成品、保留源、退出码 1；
- 只用 --delete 才删源，默认保留。
"""
import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DST = Path("A:/Darvas-archive")
MANIFEST_HEADER = "time\tname\tsize_bytes\tsha256\tposition_n\tsum_pnl\tequity_n\tstaledays\tsrc\n"


def sha256_of(path, chunk=16 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def biz_stats(path):
    """业务指纹：库还活着就该对得上的几个数。"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        pos = con.execute(
            "select count(*), coalesce(sum(realized_pnl),0) from position"
        ).fetchone()
        eq = con.execute("select count(*) from equity_snapshot").fetchone()[0]
        try:
            prm = con.execute(
                "select value from param where key='position.staledays'"
            ).fetchone()
            sd = prm[0] if prm else "?"
        except sqlite3.OperationalError:
            sd = "?"
    finally:
        con.close()
    return pos[0], round(pos[1] or 0, 2), eq, sd


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def archive_one(src, dst_dir, do_delete, check_hash=True):
    src = Path(src)
    dst = dst_dir / src.name
    if not src.exists():
        print(f"  ✗ 源不存在: {src}")
        return False
    if dst.exists():
        print(f"  ✗ 目标已存在（拒绝覆盖）: {dst}")
        return False

    size = src.stat().st_size
    t0 = time.time()
    src_stat = biz_stats(src)

    print(f"  → 复制 {src.name}  {human(size)} ...", flush=True)
    shutil.copy2(src, dst)
    with open(dst, "rb+") as f:  # 强制落盘
        os.fsync(f.fileno())
    dt = time.time() - t0
    speed = size / dt / 1024 / 1024 if dt else 0
    print(f"    复制完成 {dt:.1f}s  ({speed:.0f} MB/s)")

    # 校验
    ok = True
    if dst.stat().st_size != size:
        print(f"    ✗ 大小不符 {dst.stat().st_size} != {size}")
        ok = False
    dst_stat = biz_stats(dst) if ok else None
    if ok and dst_stat != src_stat:
        print(f"    ✗ 业务统计不符\n      源  : {src_stat}\n      目标: {dst_stat}")
        ok = False
    elif ok:
        print(f"    业务统计一致 position={dst_stat[0]} Σpnl={dst_stat[1]:,.2f} eq={dst_stat[2]} sd={dst_stat[3]}")

    if ok and check_hash:
        t1 = time.time()
        hs, hd = sha256_of(src), sha256_of(dst)
        if hs != hd:
            print(f"    ✗ sha256 不符\n      源  : {hs}\n      目标: {hd}")
            ok = False
        else:
            print(f"    sha256 一致 {hs[:16]}…  ({(time.time()-t1):.0f}s)")
    else:
        hs = "-"

    if not ok:
        print("    → 删除目标半成品，保留源")
        try:
            dst.unlink()
        except OSError:
            pass
        return False

    # 台账
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    new = not MANIFEST.exists()
    with open(MANIFEST, "a", encoding="utf-8") as f:
        if new:
            f.write(MANIFEST_HEADER)
        f.write(
            f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{src.name}\t{size}\t{hs}\t"
            f"{dst_stat[0]}\t{dst_stat[1]}\t{dst_stat[2]}\t{dst_stat[3]}\t{src}\n"
        )

    if do_delete:
        src.unlink()
        print(f"    ✓ 归档成功，源已删除，释放 {human(size)}")
    else:
        print("    ✓ 归档成功（源保留）")
    return True


def main():
    ap = argparse.ArgumentParser(description="回测库归档器")
    ap.add_argument("--files", nargs="+", help="要归档的 .db 文件（支持通配，由 shell 展开）")
    ap.add_argument("--dst", default=str(DEFAULT_DST), help=f"目标目录（默认 {DEFAULT_DST}）")
    ap.add_argument("--delete", action="store_true", help="校验通过后删除源（默认保留）")
    ap.add_argument("--no-hash", action="store_true", help="跳过 sha256（快但不硬）")
    ap.add_argument("--manifest", action="store_true", help="打印归档台账")
    a = ap.parse_args()

    if a.manifest:
        m = DEFAULT_DST / "MANIFEST.tsv"
        print(m.read_text(encoding="utf-8") if m.exists() else "台账为空")
        return 0

    if not a.files:
        ap.print_help()
        return 2

    dst_dir = Path(a.dst)
    dst_dir.mkdir(parents=True, exist_ok=True)

    srcs = [Path(f) for f in a.files if Path(f).suffix == ".db"]
    total = sum(p.stat().st_size for p in srcs if p.exists())
    free = shutil.disk_usage(dst_dir).free
    print(f"目标 {dst_dir}  可用 {human(free)}  待归档 {len(srcs)} 个 / {human(total)}")
    if free < total * 1.02:
        print(f"✗ 目标空间不足（需 {human(total*1.02)}）")
        return 1

    failed = []
    for p in srcs:
        print(f"\n[{p.name}]")
        if not archive_one(p, dst_dir, a.delete, not a.no_hash):
            failed.append(p.name)

    print(f"\n完成：成功 {len(srcs)-len(failed)} / 失败 {len(failed)}")
    if failed:
        print("失败清单：" + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
