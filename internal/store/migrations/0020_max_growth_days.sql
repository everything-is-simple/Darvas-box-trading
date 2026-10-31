-- 0020: Box 成长速度买入准入门槛（2026-10-31 LB-4 B 类立项）
-- 动机：LB-4 A 类后验（docs/experiments/e17_lb4_growthspeed_report）发现「成长速度」
--   （入场 Box 上轨确认 top_confirmed → 下轨确认 bottom_confirmed 的交易日数）与突破后
--   交易质量单调相关——箱体成型越快，突破后每单位净利越高（4-5 日 +1,015 → 11 日+ +320）。
--   4–5 日带是 LB 系列唯一「剔除前三仍成片为正」的强带。
-- 本参数把该结论做成**买入准入门槛**（B 类验证）：成长速度超过阈值的 Box 不产生买单。
--   成长速度 = daily_bar 在闭区间 [top_confirmed, bottom_confirmed] 的交易日计数（含两端）。
-- 默认 0 = 关闭（无限阈值），生产行为与历史基线完全一致；回测经 --set 注入阈值。
-- 单因子轮换：只动 box.max_growth_days 一个变量，其余参数 100% 复刻新基线。
-- 同步：docs/01 §6、docs/29 §四/§五之二、docs/00 §3.6（维 11）。

INSERT INTO param(key, value) VALUES ('box.max_growth_days', '0')
    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
