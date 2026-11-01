# 今日工作交接单 · 2026-11-02（可直接粘贴的启动 prompt）

> 用法：把 §1 的 prompt 整块复制给新会话即可无缝接上。
> 本单是 docs/40 的「执行版」——40 是昨日写的明日计划，本单补上今晨实测结论与今日具体第一步。

---

## 1. 启动 prompt（整块复制）

```text
接续达瓦斯箱体交易系统（Go + SQLite，唯一本地库 I:\Darvas-box-trading）开发。今天是 2026-11-02。

【必读，按顺序】
1. docs/40-handoff-2026-11-02.md —— 昨日审计裁定 + 执行顺序（含我今晨补的 §1.1 实测结论）
2. docs/29-box-gradient-experiment-plan.md —— 唯一实验队列事实源
3. docs/00-darvas-theory.md §9（单一理论红线）、docs/01 §4（每日时点）+ §6（参数表）
4. .workbuddy/memory/MEMORY.md —— 长期红线与运维坑

【当前状态（2026-11-02 06:40 核实）】
- 工作区干净，最新 commit 1e9bf75，与远程 main 一致。
- LB-4 B 类已收官：box.max_growth_days 最优 ≤6 日（migration 0020，默认 0=off，生产行为未变）。
- 回测库已归档：A:\Darvas-archive\lb4\（21 库 / 87.3G，sha256 + 台账全等）。
- 磁盘：I 盘 185G 可用，A 盘 305G 可用。
- 测试基线（今晨实跑）：除 internal/gui.TestAPIEndpoints 外全部通过；GUI 失败已定性为
  「执行环境隔离回环 TCP」——纯 Python 自连本机随机端口同样超时，8787 也超时。

【T1 已定性收口，不要再花时间】
`internal/gui.TestAPIEndpoints` 失败 = **机器环境问题，代码无罪**（docs/40 §1.1.3）。
机制：Windows 防火墙**入站按程序完整路径白名单**放行——同一份 python.exe，原名能监听、复制改名即被丢 SYN；
Go 作客户端正常、作服务端被丢 SYN；`go test` 的二进制每次落在新的 `%TEMP%\go-build*`，天然进不了名单。
TUN 关闭只解除了叠加的第一层，主体是这条白名单。
若要让它真跑起来（管理员，一次性）：
  go test -c -o I:\Darvas-box-trading\var\tmp\gui.test.exe ./internal/gui
  New-NetFirewallRule -DisplayName "Darvas GUI Test (loopback inbound)" -Direction Inbound `
    -Program "I:\Darvas-box-trading\var\tmp\gui.test.exe" -Action Allow -Profile Any
之后跑 var\tmp\gui.test.exe -test.run TestAPIEndpoints -v 即可。

【今日工作顺序（docs/40 §2）】
T1 GUI 测试定性收口 → T2 恢复交易对象追踪链（reason 写真实 box_id + plan_order.source_signal_id）
→ T3 sell.discount 专项梯度 0.95/0.97/0.99（单独预注册，初始止损与移动止损分开统计）
→ T4 生产/回测参数 SSoT 对账 → T5 卖出出口分解报表 → T6 raiseStops 止损上移事件研究。

【红线（每次动手前默念）】
- 只实现达瓦斯箱体理论，禁 MALF/立花/均线交叉/MACD 等。
- 回测库一律只读（file:?mode=ro）；回测串行，勿并发；只用 scratch 库，勿污染实时库心跳。
- 改参数默认值必须写 migration（append-only）；新增 param 键必须同步 docs/01 §6（否则 C3 校验失败）。
- 提交前必跑 python scripts/check_consistency.py。
- 2019+ 数据冻结，规则冻结前禁止查看或据此调参。
- 明确不插队：大赢家加仓、牛冲熊守、trail%、docs/35 的 pinbar/量=分歧、LB-3/LB-6。

【环境备忘】
- Go 1.27.2 在 I:\tools\go，GOPATH=I:/tools/gopath，GOCACHE=I:/tools/gocache（依赖已缓存，可离线编译）。
- 归档两步走：bt_archive.py --delete 后必须再跑 bt_recycle_purge.py --apply（否则空间不释放，
  I 盘删除会进 $RECYCLE.BIN）。
- git push 必须去代理：env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy git push

【风格要求】
说人话、先结论后数字、别绕。结论给完再列证据。每完成一项就更新 docs/29 或对应实验框架并提交推送。
```

---

## 2. 今晨已做的事（不需要重做）

| 事项 | 结果 |
|---|---|
| 跑 `go test ./internal/...` | 除 `internal/gui` 外全通过；GUI 失败可复现 |
| 排除代理假设 | 去掉全部 proxy 变量后仍失败 |
| 排除 Go/httptest 假设 | 纯 Python 自连本机随机端口同样超时 |
| 排除端口段假设 | 8787/18787/28787/38787/48787 全超时 |
| **定性结论** | **环境约束（入站按程序路径白名单），GUI 测试失败不是代码 bug** |

依据已写入 `docs/40-handoff-2026-11-02.md §1.1`。

> **06:45 沙箱外复核增补**：解除沙箱后 GUI 测试仍失败；定性升级为「机器级 WFP 过滤：新建监听者的
> 入站 SYN 一律丢弃」（证据与推理链见 docs/40 §1.1.1）。顺带修正：本机代理是 verge-mihomo
> （127.0.0.1:7897 活着），`http_proxy=57669` 只是过期错误端口。
>
> **07:20 最终定性（docs/40 §1.1.3）**：关 TUN 后本进程回环已恢复（Python 三种绑定全通），但 Go exe
> **作服务端仍被丢 SYN、作客户端正常**；`python.exe` 复制改名为 `copy_py.exe` 后立刻被拦。
> → **入站按程序完整路径白名单放行**，与语言/TUN 无关，`httptest` 必然超时。**本条从「工程硬伤」改为
> 「环境约束」**，审计三份的「发布阻断」判断作废；代码侧不需要任何改动。

---

## 3. 今日建议的收工标准

最少完成 **T1（GUI 定性收口）+ T2 开工**。T2 是纯工程改动（reason 写 box_id + source_signal_id），
不碰策略参数，风险低、收益高——它一旦打通，「卖什么 / 何时卖」的对象链就能审计了。

若时间富余，直接进 **T3 `sell.discount` 专项梯度**：这是三份审计唯一共识、且确实没做过的 P0 策略实验。

---

## 4. 命令速查

```bash
# Go 环境
export PATH="/i/tools/go/bin:$PATH" GOPATH="I:/tools/gopath" GOCACHE="I:/tools/gocache"

# 测试
cd /i/Darvas-box-trading && go test ./internal/... 2>&1 | tail -30

# 一致性（提交前必跑）
python scripts/check_consistency.py

# 归档（两步）
python scripts/bt_archive.py --files var/bt/<实验>/*.db --dst A:/Darvas-archive/<实验> --delete
python scripts/bt_recycle_purge.py --apply

# 从归档盘复算 LB-4
python scripts/e17_lb4_bt.py --dir A:/Darvas-archive/lb4

# 推送（去代理）
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy git push
```
