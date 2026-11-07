#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上证指数涨幅 × 四组双均线 × 方向/速度/加速度 —— 五柳不閑生模板版。
结构参考《市场情绪表-手工》五柳不閑生（新周期模板）：
  原始数据（快线/慢线的方向·速度·加速度） + 总结三栏（方向/速度/加速度，各四种表述） + G点标记。
"""
import csv
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CSV = BASE / "var/bt/shindex_curve_groups.csv"
OUT = BASE / "var/bt/shindex_report.html"

GROUPS = [("g1", 5, 20), ("g2", 10, 30), ("g3", 30, 50), ("g4", 50, 200)]
BEARS = [("2007-10-15", "2008-12-01", "熊市① 2008 阴跌（顶 2007-10-16，6124点）"),
         ("2015-04-15", "2015-09-30", "熊市② 2015 股灾（崩盘点 2015-06-15）"),
         ("2018-01-15", "2018-12-28", "熊市③ 2018 阴跌（顶 2018-01-29，3587点）")]


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt(v):
    x = num(v)
    return "—" if x is None else f"{x*100:+.2f}"


def cls_num(v):
    x = num(v)
    return "na" if x is None else ("pos" if x >= 0 else "neg")


def arrow(d):
    x = num(d)
    if x is None or x == 0:
        return "—", "na"
    return ("↑", "up") if x > 0 else ("↓", "dn")


# ---- 总结三栏：四种表述 ----
def sum_dir(df, db):
    """方向四态：同↑ / 同↓ / 顶背离(快↑慢↓) / 底背离(快↓慢↑)。"""
    af, _ = arrow(df)
    ab, _ = arrow(db)
    if af == "—" or ab == "—":
        return "—", "na"
    if af == "↑" and ab == "↑":
        return "同↑", "up"
    if af == "↓" and ab == "↓":
        return "同↓", "dn"
    if af == "↑" and ab == "↓":
        return "顶背离", "divt"
    return "底背离", "divb"


def sum_pair(vf, vb, up_t, dn_t, fup_t, fdn_t):
    """速度/加速度四态：同X / 快X慢Y / 快Y慢X / 同Y。"""
    f, b = num(vf), num(vb)
    if f is None or b is None:
        return "—", "na"
    if f >= 0 and b >= 0:
        return up_t, "up"
    if f < 0 and b < 0:
        return dn_t, "dn"
    if f >= 0 and b < 0:
        return fup_t, "mix"
    return fdn_t, "mix2"


rows = list(csv.DictReader(open(CSV, encoding="utf-8")))
print(f"读取 {len(rows)} 行")

CSS = """
body{font-family:"Microsoft YaHei",sans-serif;background:#fafafa;color:#222;margin:24px;font-size:13px}
h1{font-size:20px} h2{font-size:16px;margin-top:36px;border-left:4px solid #c00;padding-left:8px}
.legend{margin:12px 0;padding:10px;background:#fff;border:1px solid #ddd;border-radius:6px;line-height:1.9}
table{border-collapse:collapse;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-bottom:24px}
th,td{border:1px solid #e0e0e0;padding:4px 7px;text-align:right;white-space:nowrap}
th{background:#f0f0f0;text-align:center}
th.raw{background:#f7f7f7;font-weight:normal}
th.sum{background:#e8eef7}
td.date{text-align:left;font-family:Consolas,monospace}
.up{color:#c00;font-weight:bold} .dn{color:#080;font-weight:bold}
.pos{color:#c00} .neg{color:#080} .na{color:#bbb}
.divt{color:#e60;font-weight:bold} .divb{color:#60c;font-weight:bold}
.mix{color:#a60} .mix2{color:#06a}
tr.alldn td{background:#ffe9e9}
tr.allup td{background:#e9ffe9}
"""

parts = [f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>上证指数 × 四组双均线 × 方向/速度/加速度（模板版）</title>
<style>{CSS}</style></head><body>
<h1>上证指数每日涨幅 × 四组双均线 —— 方向 / 速度 / 加速度</h1>
<div class="legend">
<b>结构</b>（参考五柳不閑生新周期模板：原始数据 + 系数总结 + G点）：每组均线 = 快线慢线原始三列 + <b>总结三栏</b>，行尾一栏 <b>G点</b>。
<br><b>总结三栏，各四种表述</b>：
<br>　<b>方向</b>：<b class="up">同↑</b>（快慢双上行）｜<b class="dn">同↓</b>（双下行）｜<b class="divt">顶背离</b>（快↑慢↓）｜<b class="divb">底背离</b>（快↓慢↑）
<br>　<b>速度</b>（快慢线 5 日斜率符号组合）：<b class="up">同升</b>｜<b class="dn">同降</b>｜<span class="mix">快升慢降</span>｜<span class="mix2">快降慢升</span>
<br>　<b>加速度</b>（快慢线速度变化符号组合）：<b class="up">同增</b>｜<b class="dn">同减</b>｜<span class="mix">快增慢减</span>｜<span class="mix2">快减慢增</span>
<br><b>G点</b>：该行出现顶背离/底背离的组（如 g1·g3），无则空白。<b>顶部序列</b>：同↑同升 → 顶背离 → 同↓同降；<b>底部序列</b>：同↓同降 → 底背离 → 同↑同升。
<br>原始列：方向=↑/↓；速度=5日斜率(%)；加速度=速度5日变化(%)。红=正、绿=负。
<span style="background:#ffe9e9">粉红行</span>=八条线全↓；<span style="background:#e9ffe9">浅绿行</span>=全↑。口径为上证涨幅（大票表面行情），慢熊段噪声大。
</div>"""]

for s, e, name in BEARS:
    seg = [r for r in rows if s <= r["date"] <= e]
    if not seg:
        continue
    seg = seg[::5]

    head1 = ['<tr><th rowspan="3" class="date">日期</th>']
    head2 = ["<tr>"]
    head3 = ["<tr>"]
    for gid, na, nb in GROUPS:
        head1.append(f'<th colspan="9" class="grphead">{gid} MA{na}/{nb}</th>')
        head2.append(f'<th colspan="3" class="raw">快线 MA{na}</th>')
        head2.append(f'<th colspan="3" class="raw">慢线 MA{nb}</th>')
        head2.append('<th colspan="3" class="sum">总结</th>')
        head3.extend(['<th class="raw">方向</th><th class="raw">速度</th><th class="raw">加速度</th>'])
        head3.extend(['<th class="raw">方向</th><th class="raw">速度</th><th class="raw">加速度</th>'])
        head3.extend(['<th class="sum">方向</th><th class="sum">速度</th><th class="sum">加速度</th>'])
    head1.append('<th rowspan="3" class="sum">G点</th></tr>')
    head2.append("</tr>")
    head3.append("</tr>")

    body = []
    n_alldn = 0
    for r in seg:
        cells = [f'<td class="date">{r["date"]}</td>']
        dirs_all, gdots = [], []
        for gid, na, nb in GROUPS:
            df, db_ = r.get(f"{gid}_dir{na}"), r.get(f"{gid}_dir{nb}")
            vf, vb = r.get(f"{gid}_vel{na}"), r.get(f"{gid}_vel{nb}")
            af, ab = r.get(f"{gid}_acc{na}"), r.get(f"{gid}_acc{nb}")
            # 快线原始
            s1, c1 = arrow(df)
            cells += [f'<td class="{c1}">{s1}</td>',
                      f'<td class="{cls_num(vf)}">{fmt(vf)}</td>',
                      f'<td class="{cls_num(af)}">{fmt(af)}</td>']
            # 慢线原始
            s2, c2 = arrow(db_)
            cells += [f'<td class="{c2}">{s2}</td>',
                      f'<td class="{cls_num(vb)}">{fmt(vb)}</td>',
                      f'<td class="{cls_num(ab)}">{fmt(ab)}</td>']
            # 总结三栏（四种表述）
            dt, dtc = sum_dir(df, db_)
            vt, vtc = sum_pair(vf, vb, "同升", "同降", "快升慢降", "快降慢升")
            at, atc = sum_pair(af, ab, "同增", "同减", "快增慢减", "快减慢增")
            cells += [f'<td class="{dtc}">{dt}</td>',
                      f'<td class="{vtc}">{vt}</td>',
                      f'<td class="{atc}">{at}</td>']
            dirs_all += [s1, s2]
            if dt in ("顶背离", "底背离"):
                gdots.append(gid)
        all_dn = all(x == "↓" for x in dirs_all)
        all_up = all(x == "↑" for x in dirs_all)
        if all_dn:
            n_alldn += 1
        cells.append(f'<td>{"·".join(gdots) if gdots else ""}</td>')
        cls = "alldn" if all_dn else ("allup" if all_up else "")
        body.append(f'<tr{" class=" + cls if cls else ""}>' + "".join(cells) + "</tr>")

    parts.append(f"<h2>{name} <span style='font-size:12px;color:#666'>（{len(seg)} 行，每5交易日一行；八线全↓共 {n_alldn} 次）</span></h2>")
    parts.append("<table>" + "".join(head1) + "".join(head2) + "".join(head3) + "".join(body) + "</table>")

parts.append("</body></html>")
OUT.write_text("\n".join(parts), encoding="utf-8")
print(f"已写 {OUT}")
