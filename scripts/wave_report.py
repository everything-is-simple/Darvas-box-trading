#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 寿命报告生成器：读两版 summary CSV → 单文件 HTML。

口径：
  M=3  = 三天低点出场（斯波朗迪「三天高-低价准则」版，灵敏，wave 短）
  M=20 = 箱体下轨出场（此岸 box → 彼岸 box 的完整渡河，稳健，wave 长）
排名分档（斯波朗迪判读）：
  <0.25 年轻 | 0.25~0.50 青壮 | 0.50~0.75 壮年 | 0.75~0.90 衰老 | >=0.90 极老
  「到达中位数时胜算 5:5，超过后胜算 <1:1」——衰老段起不再加仓，极老段只减不加。
"""
import ast
import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAGS = [("wave_N20_M3", "M3"), ("wave_N20_M20", "M20")]
OUT = ROOT / "var" / "bt" / "wave_lifespan_report.html"


def load(tag):
    rows = {}
    for r in csv.DictReader(open(ROOT / f"var/bt/{tag}_summary.csv",
                                 encoding="utf-8")):
        try:
            sv = ast.literal_eval(r["survival"]) if r["survival"] not in ("", "{}") else {}
        except Exception:
            sv = {}
        rows[r["code"]] = {
            "code": r["code"], "n": int(r["n_waves"]),
            "p50": int(r["life_p50"] or 0), "lmin": int(r["life_min"] or 0),
            "lmax": int(r["life_max"] or 0),
            "cs": r["cur_start"], "ct": int(r["cur_span"] or 0),
            "cg": float(r["cur_gain_pct"] or 0),
            "rk": float(r["cur_rank_pct"]) if r["cur_rank_pct"] else None,
            "sv": sv}
    return rows


def dist(tag):
    s = sorted(int(r["span_days"]) for r in
               csv.DictReader(open(ROOT / f"var/bt/{tag}_waves.csv",
                                   encoding="utf-8")))
    n = len(s)
    hist = Counter(min(int(x / 5) * 5, 120) for x in s)
    return {"n": n, "p10": s[int(n * .1)], "p25": s[int(n * .25)],
            "p50": s[n // 2], "p75": s[int(n * .75)], "p90": s[int(n * .9)],
            "max": s[-1], "hist": hist}


def main():
    D = {k: load(t) for t, k in TAGS}
    DS = {k: dist(t) for t, k in TAGS}

    # 合并主表：M20 为主口径，M3 为对照
    codes = sorted(set(D["M20"]) | set(D["M3"]),
                   key=lambda c: -((D["M20"].get(c) or D["M3"].get(c) or {})
                                   .get("rk") or -1))
    table = []
    for c in codes:
        m20 = D["M20"].get(c)
        m3 = D["M3"].get(c)
        if not (m20 and m20["cs"]) and not (m3 and m3["cs"]):
            continue
        e = (m20 or m3)
        row = {"code": c, "n": e["n"], "p50": e["p50"],
               "lmin": e["lmin"], "lmax": e["lmax"], "cs": e["cs"],
               "m20": m20 and m20["cs"], "m3": m3 and m3["cs"]}
        for k in ("M20", "M3"):
            d = D[k].get(c)
            if d and d["cs"]:
                row[k.lower()] = {
                    "ct": d["ct"], "cg": round(d["cg"], 1),
                    "rk": None if d["rk"] is None else round(d["rk"], 3),
                    "sv": {x: (round(v, 1) if isinstance(v, float) else v)
                           for x, v in d["sv"].items()}}
            else:
                row[k.lower()] = None
        table.append(row)

    # 分档（M20 口径）
    bins = Counter()
    for r in table:
        d = r["m20"]
        if not d or d["rk"] is None:
            continue
        rk = d["rk"]
        if rk < .25: bins["young"] += 1
        elif rk < .50: bins["prime"] += 1
        elif rk < .75: bins["mature"] += 1
        elif rk < .90: bins["old"] += 1
        else: bins["ancient"] += 1

    import json
    payload = {"table": table, "dist": {k: {kk: vv for kk, vv in v.items()}
               for k, v in DS.items()}, "bins": dict(bins)}

    html = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>Wave 寿命排名报告（全市场）</title>
<style>
 body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;background:#f5f6f8;color:#1c2733}
 .wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 10px;color:#24435c}
 .sub{color:#68788a;font-size:13px;margin-bottom:18px}
 .cards{display:flex;gap:12px;flex-wrap:wrap}
 .card{background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:14px 18px;min-width:200px;flex:1}
 .card b{font-size:26px;color:#0f5b3f} .card.a b{color:#a33d2f}
 .card span{display:block;font-size:12px;color:#68788a;margin-top:4px}
 .note{background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:14px 18px;font-size:13px;line-height:1.8;color:#3a4a5a}
 .note b{color:#1c2733}
 .bar{display:flex;height:26px;border-radius:6px;overflow:hidden;font-size:12px;color:#fff;text-align:center;line-height:26px;margin:8px 0}
 .bar div{min-width:0}
 input{padding:8px 12px;border:1px solid #ccd6df;border-radius:8px;font-size:14px;width:260px}
 select{padding:8px;border:1px solid #ccd6df;border-radius:8px;font-size:13px}
 table{border-collapse:collapse;width:100%;background:#fff;font-size:12.5px;margin-top:10px}
 th{background:#eef2f6;padding:8px 6px;text-align:right;position:sticky;top:0;cursor:pointer;white-space:nowrap}
 th:first-child,td:first-child{text-align:left}
 td{padding:6px;border-bottom:1px solid #eef1f4;text-align:right;white-space:nowrap}
 tr:hover td{background:#f2f7fb}
 tr.ancient td{background:#fdeeed} tr.old td{background:#fdf5ee}
 tr.young td:first-child{color:#0f7b4f}
 .rk{font-weight:600} .ancient .rk{color:#b03424} .old .rk{color:#b06a24}
 .scroll{max-height:640px;overflow:auto;border:1px solid #e3e8ee;border-radius:10px}
 .tag{font-size:10px;border-radius:4px;padding:1px 5px;color:#fff}
 .t20{background:#2b6cb0}.t3{background:#7d8da1}
 .legend{font-size:12px;color:#68788a;margin:6px 0}
 .mini{font-size:11px;color:#8a99a8}
</style></head><body><div class="wrap">
<h1>Wave 寿命排名报告</h1>
<div class="sub">全市场 9,815 标的 · 此岸 box(N=20 日区间) → wave → 彼岸 · 数据截至最新交易日 · 生成于 2026-11-07</div>

<div class="cards" id="cards"></div>

<h2>读法（斯波朗迪《专业投机原理》第 26 章「平均剩余寿命」）</h2>
<div class="note">
<b>盈利的本质 = 离开此岸 box，借一段 wave 抵达彼岸 box。</b><br>
· <b>M20（蓝标签）</b>= 箱体下轨出场：一段完整的渡河，寿命中位 33 日。<br>
· <b>M3（灰标签）</b>= 三天低点出场：斯波朗迪三天高-低价准则，灵敏短线口径，寿命中位 8 日。<br>
· <b>历史排名</b> = 当前 wave 已活天数在本标的历史全部已终止 wave 中的分位（严格小于口径，样本 ≥30 才给排名）。<br>
· <b>剩余寿命</b> = 条件生存：只统计活得比当前 wave 更久的历史 wave，看它们还各活了多久。<br>
· <b>判读铁律：排名到 0.50 时胜算 5:5；超过 0.50 后胜算 <1:1 → 不再加仓；进入衰老段(≥0.75)只减不加。</b>
</div>

<h2>当前存活 wave 的衰老度分档（M20 口径，n=__NB__）</h2>
<div class="bar">
 <div style="background:#2f9e6e;width:__WY__%">年轻 __Y__</div>
 <div style="background:#7fb069;width:__WP__%">青壮 __P__</div>
 <div style="background:#d9b23f;width:__WM__%">壮年 __M__</div>
 <div style="background:#e08b3a;width:__WO__%">衰老 __O__</div>
 <div style="background:#c0453a;width:__WA__%">极老 __A__</div>
</div>
<div class="legend">年轻 = 历史排名 &lt;0.25（大把寿命）｜极老 = ≥0.90（历史几乎无先例，随时终结）</div>

<h2>全市场 wave 寿命分布对照 v1 账本</h2>
<div class="note" id="distnote"></div>

<h2>存活 wave 明细（__NT__ 只 · 点列头排序 · 搜代码）</h2>
<input id="q" placeholder="搜索代码，如 sh601988 / sz300">
<select id="f"><option value="">全部档位</option><option value="ancient">仅极老 ≥0.90</option>
<option value="old">衰老 0.75~0.90</option><option value="mature">壮年 0.50~0.75</option>
<option value="young">年轻 &lt;0.25</option><option value="hasrank">仅可排名</option></select>
<span class="mini" id="cnt"></span>
<div class="scroll"><table id="tbl"><thead><tr>
<th data-k="code">代码</th><th>历史wave数</th><th>寿命中位(min~max)</th>
<th>当前起始</th><th>M20 已活</th><th>浮盈%</th><th>M20排名</th><th>剩余中位</th><th>P再活20日</th><th>P再活40日</th>
<th>M3 已活</th><th>M3排名</th>
</tr></thead><tbody></tbody></table></div>
<div class="mini" style="margin-top:14px">
注：死标的（数据断流早于 2026-06-01）的截断 wave 不计入。排名 None = 历史样本 &lt;30 段（诚实退化，不猜）。
剩余中位 / P再活 = 条件生存分布（同池样本 &lt;10 时为空）。P(再活x日) = 历史上活得比当前久的 wave 中，还再活了 ≥x 日的比例。
</div>
</div>
<script>
const DATA = __JSON__;
const CLS = r => { const d=r.m20; if(!d||d.rk==null) return "";
 if(d.rk>=.90) return "ancient"; if(d.rk>=.75) return "old"; if(d.rk<.25) return "young"; return ""; };
function fmt(d, k, pct){ if(!d) return "—";
 if(k==="ct") return d.ct; if(k==="cg") return (d.cg>0?"+":"")+d.cg;
 if(k==="rk") return d.rk==null?"—":d.rk.toFixed(2);
 if(k==="rem") return d.sv&&d.sv.rem_p50!=null?("+"+d.sv.rem_p50+"日"):"—";
 if(pct&&d.sv&&d.sv[pct]!=null) return d.sv[pct].toFixed(0)+"%"; return "—"; }
let sortK=null, sortAsc=false;
function render(){
 const q=document.getElementById('q').value.trim(), f=document.getElementById('f').value;
 let rows=DATA.table.filter(r=>{
  if(q&&!r.code.includes(q)) return false;
  const c=CLS(r);
  if(f==="hasrank") return r.m20&&r.m20.rk!=null||r.m3&&r.m3.rk!=null;
  if(f&&c!==f) return false; return true;});
 if(sortK){const key=r=>{if(sortK==="code")return r.code;
  const d=r.m20||r.m3; return d?d[sortK]??-1:-1;};
  rows.sort((a,b)=>{const x=key(a),y=key(b);return (x>y?1:x<y?-1:0)*(sortAsc?-1:1);});}
 document.querySelector('#cnt').textContent = rows.length+" 只";
 const tb=document.querySelector('#tbl tbody');
 tb.innerHTML = rows.slice(0,800).map(r=>{const d=r.m20,m=r.m3,c=CLS(r);
  return `<tr class="${c}"><td><b>${r.code}</b></td><td>${r.n}</td>
  <td>${r.p50}(${r.lmin}~${r.lmax})</td><td>${r.cs}</td>
  <td>${d?`<span class="tag t20">M20</span> `+fmt(d,"ct"):"—"}</td>
  <td>${d?fmt(d,"cg"):"—"}</td>
  <td class="rk">${d?fmt(d,"rk"):"—"}</td>
  <td>${d?fmt(d,"rem"):"—"}</td>
  <td>${d?fmt(d,"x","P20"):"—"}</td>
  <td>${d?fmt(d,"x","P40"):"—"}</td>
  <td>${m?`<span class="tag t3">M3</span> `+fmt(m,"ct"):"—"}</td>
  <td>${m?fmt(m,"rk"):"—"}</td></tr>`;}).join("");
 if(rows.length>800) tb.innerHTML+=`<tr><td colspan="12" class="mini" style="text-align:center;padding:10px">仅显示前 800 行，共 ${rows.length} 只，请用搜索缩小范围</td></tr>`;
}
document.querySelectorAll('th').forEach(th=>th.onclick=()=>{
 const k=th.dataset.k; if(!k) return;
 sortAsc = sortK===k ? !sortAsc : false; sortK=k; render();});
document.getElementById('q').oninput=render;
document.getElementById('f').onchange=render;
// 卡片
const d3=DATA.dist.M3, d20=DATA.dist.M20;
document.getElementById('cards').innerHTML = [
 `<div class="card"><b>${d20.n.toLocaleString()}</b><span>M20 箱体下轨口径 · UP wave 总段数<br>寿命 p50=${d20.p50} / p90=${d20.p90} / max=${d20.max} 日</span></div>`,
 `<div class="card"><b>${d3.n.toLocaleString()}</b><span>M3 三天低点口径 · UP wave 总段数<br>寿命 p50=${d3.p50} / p90=${d3.p90} / max=${d3.max} 日</span></div>`,
 `<div class="card a"><b>${DATA.bins.ancient||0}</b><span>极老 wave（M20 排名 ≥0.90）<br>历史几乎无先例，随时终结</span></div>`,
 `<div class="card"><b>${DATA.bins.young||0}</b><span>年轻 wave（排名 <0.25）<br>剩余寿命充裕</span></div>`].join("");
document.getElementById('distnote').innerHTML =
 `M20 分布（日）：p10=${d20.p10} · p25=${d20.p25} · <b>p50=${d20.p50}</b> · p75=${d20.p75} · p90=${d20.p90} · max=${d20.max}<br>` +
 `M3 分布（日）：p10=${d3.p10} · p25=${d3.p25} · <b>p50=${d3.p50}</b> · p75=${d3.p75} · p90=${d3.p90} · max=${d3.max}<br>` +
 `<b>v1 账本实际持仓（1,142 笔）：p50=5 日，>20 日仅 222 笔（19.4%）—— 中位 5 天是旧 staledays 强平 bug 的指纹；` +
 `自然三天低点口径（M3）的中位是 8 天。D3+D4 拆刹车后，持仓分布应回到 M3~M20 之间。</b>`;
render();
</script></body></html>"""

    nb = sum(bins.values())
    html = (html.replace("__JSON__", json.dumps(payload, ensure_ascii=False))
            .replace("__NB__", str(nb)).replace("__NT__", str(len(table)))
            .replace("__Y__", str(bins.get("young", 0)))
            .replace("__P__", str(bins.get("prime", 0)))
            .replace("__M__", str(bins.get("mature", 0)))
            .replace("__O__", str(bins.get("old", 0)))
            .replace("__A__", str(bins.get("ancient", 0)))
            .replace("__WY__", str(round(bins.get("young", 0) / max(nb, 1) * 100, 1)))
            .replace("__WP__", str(round(bins.get("prime", 0) / max(nb, 1) * 100, 1)))
            .replace("__WM__", str(round(bins.get("mature", 0) / max(nb, 1) * 100, 1)))
            .replace("__WO__", str(round(bins.get("old", 0) / max(nb, 1) * 100, 1)))
            .replace("__WA__", str(round(bins.get("ancient", 0) / max(nb, 1) * 100, 1))))
    OUT.write_text(html, encoding="utf-8")
    print(f"已写 {OUT}  存活标的 {len(table)}  分档 {dict(bins)}")


if __name__ == "__main__":
    main()
