#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 raw 口径实验报告批量插入口径横幅（docs/55 §6.1）。

为什么需要独立脚本而不是手改：
  实验报告（.md/.html）由 `scripts/e17_*.py` 生成，`OUT_MD.write_text(...)` 直接覆写。
  手改会在下次重生成时被静默覆盖。所以横幅必须由一个幂等脚本施加，
  并在任何报告重生成之后**再跑一次**（`check_consistency.py` C7 会抓漏）。

判据（docs/55 §5-Q1）：
  - 一切读取 raw（不复权）库的个股价格或交易账本的报告 → 加横幅
  - 🟢 免做不加：A0 引擎确定性（与价格无关）、市场镜子选型（只读指数，指数不复权）

用法：
  python scripts/apply_caliber_banner.py            # 写入缺失的横幅
  python scripts/apply_caliber_banner.py --check    # 只检查，缺则退出码 1（供 C7 调用）
  python scripts/apply_caliber_banner.py --dry-run  # 只打印将改动的文件
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "docs" / "experiments"

MARK = "口径横幅：本报告基于 raw"

# 基线名（不含扩展名） → (分类, 说明)。分类决定报告里的归类，不影响横幅内容。
TARGETS = {
    # ── A 类后验（docs/55 §3.1，11 项）────────────────────────────
    "e17_newhigh_distance_report":            ("A", "M9.0 距新高细分"),
    "e17_box_height_report":                  ("A", "P0-1 box 高度分带"),
    "e17_regime_attribution_report":          ("A", "P0-3 regime 归因"),
    "e17_giveback_report":                    ("A", "P2-5 浮盈回吐 MFE"),
    "e17_lb1_lifespan_report":                ("A", "LB-1 box 寿命"),
    "e17_lb2_ladderspeed_report":             ("A", "LB-2 阶梯速度"),
    "e17_lb5_lineagedepth_report":            ("A", "LB-5 谱系深度"),
    "e17_lb4_growthspeed_report":             ("A", "LB-4 成长速度（A 类）"),
    "e17_bigwinner_addon_a_report":           ("A", "大赢家加仓 A 类"),
    "e17_newhigh_sweetspot_baseA_report":     ("A", "甜点新基线复核"),
    "e17_regime_scale_a_report":              ("A", "攻防转换 A 类（宽度序列）"),
    # ── B 类对照回测（docs/55 §3.2，7 项）────────────────────────
    "e17_regime_gate_report":                 ("B", "B-1/B-2 MA60 闸门"),
    "e17_lb4_growthspeed_bt_report":          ("B", "LB-4 B 类 准入门槛"),
    "e17_volmult_report":                     ("B", "P1-4 量比门槛"),
    "e17_staledays_report":                   ("B", "P2-6a staledays 梯度"),
    "e17_sell_discount_report":               ("B", "T3 sell.discount 梯度"),
    "e17_p38_report":                         ("B", "P3-8 升势/最高箱准入"),
    "e17_regime_attribution_report_000905":   ("B", "换镜归因 000905"),
    # ── 附加与矩阵外（同样读 raw 账本，按同一条判据覆盖）──────────
    "e17_position_profile_report":            ("X", "仓位画像（§3.4 #21）"),
    "e17_snowball_report":                    ("X", "滚雪球复利（§3.4 #22）"),
    "e17_breakout_report":                    ("X", "P1-4 突破强度 A 类后验"),
    "e17_knobs_report":                       ("X", "入场/池管理旋钮梯度"),
    "e17_position_semantics_report":          ("X", "B-4 仓位语义成对重跑"),
    "e17_stop_ratchet_report":                ("X", "raiseStops 止损上移事件研究"),
    "e17_baseline_single_index_report":       ("X", "新基线单镜 14 年复利（raw 账本）"),
    "e17_and_gate_projection":                ("X", "双镜 AND 门逐笔并集推演"),
}

# 🟢 免做：明确不加横幅，写在这里是为了让「为什么没加」可查（docs/55 §2.1）
EXEMPT = {
    "e17_a0_determinism_report": "引擎三路重建一致性，与价格口径无关",
    "e17_market_index_report":   "只读指数（指数不复权），序列不变",
}

MD_BANNER = """> ⚠️ **口径横幅：本报告基于 raw（不复权）数据，已作废，不得作为现行依据。**
> 除权日价格腰斩 → box 边界、距新高距离、MFE、出口分布全部被污染。
> adj 口径重做结果见同目录 `*_adj` 版本；裁定见 `docs/55` §5-Q1。
"""

HTML_BANNER = """<div style="margin:0 0 18px;padding:12px 16px;border-left:4px solid #d9480f;background:#fff4e6;color:#8a3b00;font-size:14px;line-height:1.65;">
<strong>⚠️ 口径横幅：本报告基于 raw（不复权）数据，已作废，不得作为现行依据。</strong><br>
除权日价格腰斩 → box 边界、距新高距离、MFE、出口分布全部被污染。adj 口径重做结果见同目录 <code>*_adj</code> 版本；裁定见 docs/55 §5-Q1。
</div>
"""


def apply_md(path: Path, text: str) -> str:
    """插在 H1 之后（保留 H1 与紧随其后的空行结构）。"""
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            rest = lines[i + 1:]
            # 跳过 H1 后的空行，让横幅紧贴标题
            j = 0
            while j < len(rest) and rest[j].strip() == "":
                j += 1
            head = lines[: i + 1] + ["\n"]
            return "".join(head) + MD_BANNER + "\n" + "".join(rest[j:])
    # 没有 H1：直接放最前面
    return MD_BANNER + "\n" + text


def apply_html(path: Path, text: str) -> str:
    """插在 <body> 开标签之后；找不到 body 就插在文件最前。"""
    low = text.lower()
    k = low.find("<body")
    if k != -1:
        end = text.find(">", k)
        if end != -1:
            return text[: end + 1] + "\n" + HTML_BANNER + text[end + 1:]
    return HTML_BANNER + text


def missing_banners():
    """返回缺横幅的文件名列表（供 C7 调用）。"""
    out = []
    for base in TARGETS:
        for ext in (".md", ".html"):
            p = EXP / f"{base}{ext}"
            if p.exists() and MARK not in p.read_text(encoding="utf-8"):
                out.append(p.name)
    return out


def main() -> int:
    check_only = "--check" in sys.argv
    dry = "--dry-run" in sys.argv
    changed, missing, exempt_hits = [], [], []

    for base, (kind, label) in TARGETS.items():
        for ext, fn in ((".md", apply_md), (".html", apply_html)):
            p = EXP / f"{base}{ext}"
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8")
            if MARK in text:
                continue
            missing.append(p.name)
            if check_only:
                continue
            if dry:
                changed.append(p.name)
                continue
            p.write_text(fn(p, text), encoding="utf-8")
            changed.append(p.name)

    for base in EXEMPT:
        for ext in (".md", ".html"):
            p = EXP / f"{base}{ext}"
            if p.exists() and MARK in p.read_text(encoding="utf-8"):
                exempt_hits.append(p.name)

    if check_only:
        if missing:
            print(f"✗ C7 口径横幅缺失 {len(missing)} 份：")
            for n in missing:
                print("   ", n)
            return 1
        print(f"✓ C7 口径横幅齐全（{len(TARGETS)} 份报告族）")
        if exempt_hits:
            print(f"⚠ 免做文件被误加横幅：{exempt_hits}")
            return 1
        return 0

    tag = "[dry-run] 将修改" if dry else "已加横幅"
    print(f"{tag} {len(changed)} 个文件：")
    for n in changed:
        print("   ", n)
    if exempt_hits:
        print(f"⚠ 免做文件被误加横幅（请手工移除）：{exempt_hits}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
