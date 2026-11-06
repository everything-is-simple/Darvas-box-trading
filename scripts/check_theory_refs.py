#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_theory_refs.py —— 代码注释里的理论条目引用核对（docs/06 §3.3）

规则（docs/06 §3.3）：每个领域判定规则的 Go 注释必须引用理论条目号，例如
    // BreakoutValid implements docs/00-darvas-theory.md §4.2 ...
所以「注释里的 §x 在 docs/00 里必须真的存在」是文档正确性的一部分：
重构 docs/00 的章节号时，代码不会报错，只会指向一条不存在的规则。

用法：
    python scripts/check_theory_refs.py          # 退出码 0=全存在；1=有悬空引用
    python scripts/check_theory_refs.py --root . # 指定仓库根
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REF = re.compile(
    r"docs/00(?:-darvas-theory\.md)?\s*(?:§\s*(\d+(?:\.\d+)*|[一二三四五六七八九十]+)"
    r"|第\s*(\d+(?:\.\d+)*)\s*条)")
HEAD = re.compile(r"^#{2,4}\s*(?:§)?\s*(\d+(?:\.\d+)*|[一二三四五六七八九十]+)\s*[、.·]?\s")
HEAD_ALT = re.compile(r"^#{2,4}\s*第\s*(\d+(?:\.\d+)*)\s*条")
D = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
     "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn2arabic(text: str) -> str:
    if re.fullmatch(r"\d+(\.\d+)*", text):
        return text
    if "十" in text:
        a, _, b = text.partition("十")
        return str((D.get(a, 1) if a else 1) * 10 + (D.get(b, 0) if b else 0))
    return str(D[text]) if len(text) == 1 and text in D else text


def sections_of(doc: Path) -> set[str]:
    got: set[str] = set()
    for line in doc.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = HEAD.match(line) or HEAD_ALT.match(line)
        if m:
            got.add(cn2arabic(m.group(1)))
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    a = ap.parse_args()
    root = Path(a.root)
    doc = root / "docs" / "00-darvas-theory.md"
    if not doc.exists():
        print(f"✗ 找不到 {doc}")
        return 1
    secs = sections_of(doc)

    bad, total = [], 0
    for d in ("internal", "cmd"):
        base = root / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*.go")):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in REF.finditer(text):
                total += 1
                sec = m.group(1) or m.group(2)
                if cn2arabic(sec) not in secs:
                    line = text[: m.start()].count("\n") + 1
                    bad.append(f"{f.relative_to(root).as_posix()}:{line} §{sec}")

    if bad:
        print(f"✗ 理论引用悬空 {len(bad)}/{total} 处（docs/00 无该节）")
        for b in bad[:12]:
            print("   ", b)
        if len(bad) > 12:
            print(f"    … 另 {len(bad) - 12} 处")
        print("  修法：改注释指向真实存在的条目，或在 docs/00 补节（补节属理论变更，需登记）")
        return 1
    print(f"✓ 理论引用全部存在（{total} 处，docs/00 共 {len(secs)} 节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
