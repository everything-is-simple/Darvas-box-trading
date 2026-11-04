"""把 docs/44 的 47 键台账导出成一份清爽清单（Markdown），并校验库内实际种子值。

用法：python scripts/gen_baseline_ledger.py [--db path] [--out path]
只读回测库 + docs/44，不写库、不改 docs。
"""
import argparse
import re
import sqlite3

DOC = "docs/44-new-baseline-config.md"

ORDER = [
    "数据/账户", "买入", "箱体", "卖出", "持仓", "跟踪池",
    "筛选", "撮合", "大盘闸门", "宽度/A 判据", "费用",
]


def parse_doc(path=DOC):
    txt = open(path, encoding="utf-8").read()
    seg = txt.split("BEGIN GENERATED:params")[1].split("END GENERATED:params")[0]
    rows, group = [], ""
    for ln in seg.splitlines():
        m = re.match(r"^\*\*(.+?)\*\*$", ln.strip())
        if m:
            group = m.group(1)
            continue
        m = re.match(
            r"^\| `([^`]+)` \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| (.*?) \|$",
            ln,
        )
        if m:
            rows.append({
                "group": group,
                "key": m.group(1),
                "val": m.group(2).strip().strip("`"),
                "type": m.group(3).strip(),
                "src": m.group(4).strip(),
                "default": m.group(5).strip().strip("`"),
                "status": m.group(6).strip(),
                "why": m.group(7).strip(),
            })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="var/bt/base/bt_baseA_cont.db")
    ap.add_argument("--out", default="var/reports/baseline_47_params.md")
    a = ap.parse_args()

    rows = parse_doc()
    assert len(rows) == 47, f"docs/44 生成区应为 47 键，实得 {len(rows)}"

    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=5)
    live = dict(c.execute("SELECT key, value FROM param").fetchall())
    c.close()

    bad = []
    for r in rows:
        lv = live.get(r["key"])
        r["live"] = lv
        if lv is None:
            if "代码默认" not in r["src"]:
                bad.append((r["key"], "库内缺失但来源非代码默认"))
        elif str(lv).strip() != r["val"]:
            bad.append((r["key"], f"库内={lv} 文档={r['val']}"))

    out = []
    out.append("# 新基线参数清单（47 键）\n")
    out.append("> 事实源：`docs/44-new-baseline-config.md`（唯一）；本文件由 "
               "`scripts/gen_baseline_ledger.py` 生成，**不要手改**。")
    out.append("> 47 键 = **40 键库内种子** + **7 键只走代码默认**（标 `—`）。"
               "「库内」列是回测库 `param` 表实读。\n")
    out.append(f"**校验**：库内实读 vs 文档 → {'✅ 47 键全对' if not bad else '❌ ' + str(bad)}\n")

    out.append("| # | 键 | 生效值 | 库内 | 验证状态 |")
    out.append("|---|---|---|---|---|")
    i = 0
    for g in ORDER:
        grs = [r for r in rows if r["group"] == g]
        if not grs:
            continue
        out.append(f"| | **{g}**（{len(grs)}） | | | |")
        for r in grs:
            i += 1
            seeded = "✓" if r["live"] is not None else "—"
            out.append(f"| {i} | `{r['key']}` | `{r['val']}` | {seeded} | {r['status']} |")
    out.append("")

    from collections import Counter
    cnt = Counter(r["status"] for r in rows)
    out.append("## 验证状态分布\n")
    out.append("| 状态 | 数量 | 含义 |")
    out.append("|---|---|---|")
    meaning = {
        "已验": "有明确实验结论，进基线",
        "维持": "实验判噪声内，不改",
        "否决": "实验否决，留表但不生效",
        "事实": "工程事实（费用/复利起点/机器路径）",
        "地基": "结构约束，当前不触发",
        "未验": "工程默认值，无实验背书",
    }
    for k in ["已验", "维持", "否决", "事实", "地基", "未验"]:
        out.append(f"| {k} | {cnt[k]} | {meaning[k]} |")
    out.append(f"| **合计** | **{sum(cnt.values())}** | |")

    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    print(f"已生成 {a.out}（{len(rows)} 键）")
    print("库内实读 vs 文档:", "全对" if not bad else bad)
    print("状态分布:", dict(cnt))


if __name__ == "__main__":
    main()
