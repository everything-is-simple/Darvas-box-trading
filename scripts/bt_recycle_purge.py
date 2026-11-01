#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回收站清理器 —— 归档后真正释放空间用。

为什么需要它（2026-11-01 踩坑）：
    I 盘上 python `Path.unlink()` 删掉 4.2 GB 回测库后，文件并没有真正消亡，而是被
    移进了 `I:\\$RECYCLE.BIN\\<SID>\\`。`df` 实测确认：删 88 GB 后可用空间 0 增长，
    直到定向清掉回收站条目才回到 185 GB。也就是说
        bt_archive.py --delete  →  库"看不见了"但空间没还给系统
    归档脚本自身无法绕过（它只管删源），所以每次 --delete 之后必须跑一次本脚本。

用法：
    python scripts/bt_recycle_purge.py            # 检查：列出将被清理的条目，不动手
    python scripts/bt_recycle_purge.py --apply    # 清理
    python scripts/bt_recycle_purge.py --min-gb 1 --apply

安全边界（三道）：
  1. 只处理 `$R*` 数据文件 + 其配对 `$I*` 元数据，配对缺失的一律 SKIP（不瞎删）；
  2. 只处理「原路径含 `\\var\\bt\\`」的条目（靠 `$I` 里的 UTF-16 原路径判定），
     即回测中间库，用户的其他回收站内容一律不动；
  3. 只处理 ≥ --min-gb 的库文件，小文件（日志/报告）不碰。
"""
import argparse
import os
import struct
import sys
from pathlib import Path

ALLOW_TOKEN = os.sep.join(["var", "bt"])
RECYCLE = "$RECYCLE.BIN"


def drives():
    out = []
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{c}:\\"
        if os.path.exists(root):
            out.append(Path(root))
    return out


def bin_dirs():
    """所有盘符下 $RECYCLE.BIN\\<SID>\\ 目录。"""
    for d in drives():
        rb = d / RECYCLE
        if not rb.is_dir():
            continue
        try:
            for sub in rb.iterdir():
                if sub.is_dir():
                    yield sub
        except OSError:
            continue


def orig_path(ifile: Path) -> str:
    """$I 文件：8B 头 + 8B 大小 + 8B 删除时间 + 4B 路径字符数 + UTF-16LE 原路径。"""
    b = ifile.read_bytes()
    n = struct.unpack("<I", b[24:28])[0]
    return b[28:28 + n * 2].decode("utf-16-le").rstrip("\x00")


def scan(min_bytes: int):
    hits = []
    for bdir in bin_dirs():
        try:
            entries = list(bdir.iterdir())
        except OSError:
            continue  # 无权访问的 SID 目录（其他用户的回收站）直接跳过
        for p in entries:
            try:
                if not p.is_file() or not p.name.startswith("$R"):
                    continue
                if p.stat().st_size < min_bytes:
                    continue
                meta = bdir / ("$I" + p.name[2:])
                if not meta.exists():
                    continue
                orig = orig_path(meta)
            except OSError:
                continue
            if ALLOW_TOKEN not in orig:
                continue
            hits.append((p, meta, orig, p.stat().st_size))
    hits.sort(key=lambda r: r[3], reverse=True)
    return hits


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def main():
    ap = argparse.ArgumentParser(description="清理回收站里的回测库残留（归档后释放空间）")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只检查）")
    ap.add_argument("--min-gb", type=float, default=1.0, help="只看 ≥ 该尺寸的条目（默认 1 GB）")
    a = ap.parse_args()

    hits = scan(int(a.min_gb * 1024 ** 3))
    total = sum(h[3] for h in hits)
    print(f"{'[APPLY]' if a.apply else '[CHECK]'} 命中 {len(hits)} 个条目 / {human(total)}")
    for p, meta, orig, sz in hits:
        print(f"  - {orig}  ({human(sz)})")
    if not hits:
        return 0
    if not a.apply:
        print("\n未删除。确认无误后加 --apply。")
        return 0

    freed, failed = 0, 0
    for p, meta, orig, sz in hits:
        for f, is_data in ((p, True), (meta, False)):
            try:
                f.unlink()
                if is_data:
                    freed += sz
            except OSError as e:
                failed += 1
                print(f"  ✗ {f.name}: {e}")
    print(f"已释放 {human(freed)}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
