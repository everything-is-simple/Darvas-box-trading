-- 0026: 基线显式化 —— 把「碰巧等于代码默认」的 8 个结构参数写成种子（2026-11-03 用户拍板）
--
-- 动机（docs/44 新建参数台账时发现）：
--   基线 47 键里有 15 键只走代码默认值、库里没有行。其中 8 键是**基线结构本身**
--   （阶梯制四档 + 回看 + 档界 + MA60 镜子 + 复利起点），它们的值正确纯属「代码默认
--   恰好等于实验结论」。这违反用户铁律「改参数默认值必须写 migration」（0017 教训：
--   种子缺失会让代码默认悄悄变成事实源）。
--   → 本迁移把这 8 键固化进 param 表，基线从此**可从库里直接读出**，不依赖代码默认。
--
-- 为什么是这 8 个、不是全部 15 个：
--   · buy.uptrend_only / buy.highest_box_mode / buy.highest_box_window：
--     P3-8 已否决（docs/29），设计上就是「不注入即关」，故意不种子化。
--   · regime.a.*（3 键）：A 判据已不决定仓位单位（阶梯制接管），仅作第 10 维归因原料。
--   · tdx.root：**机器相关路径**（本机 D:\new_tdx64），code 已改为自动探测，禁止写死进库。
--   （其余 7 键中 equity.base 也属基线结构，故并列；合起来 8 键。）
--
-- 语义：与 0025 同套路，仅改种子值，不动 contract.go 的代码默认；
--   回测经 --set 显式注入时不受影响（Bootstrap 跑完迁移后才 applyParamOverrides）。
-- 依据：docs/44 台账（生成器 scripts/param_ledger.py）+ docs/01 §6。

-- 阶梯制四档（M9.0 第 5 维，pos.newhigh_ladder=on 时生效）
INSERT INTO param(key, value) VALUES ('pos.newhigh.lookback', '252')
    ON CONFLICT(key) DO UPDATE SET value = '252';
INSERT INTO param(key, value) VALUES ('pos.newhigh.cutoff', '40')
    ON CONFLICT(key) DO UPDATE SET value = '40';
INSERT INTO param(key, value) VALUES ('pos.newhigh.units.t1', '3')
    ON CONFLICT(key) DO UPDATE SET value = '3';
INSERT INTO param(key, value) VALUES ('pos.newhigh.units.t2', '2')
    ON CONFLICT(key) DO UPDATE SET value = '2';
INSERT INTO param(key, value) VALUES ('pos.newhigh.units.t3', '1')
    ON CONFLICT(key) DO UPDATE SET value = '1';
INSERT INTO param(key, value) VALUES ('pos.newhigh.units.t4', '0.5')
    ON CONFLICT(key) DO UPDATE SET value = '0.5';

-- MA60 镜子（单镜上证定案；中证500 只作历史对照锚，需 --set 显式换镜）
INSERT INTO param(key, value) VALUES ('gate.ma60.index', 'sh000001')
    ON CONFLICT(key) DO UPDATE SET value = 'sh000001';

-- 复利起点（回放/雪球账本的初始权益）
INSERT INTO param(key, value) VALUES ('equity.base', '1000000')
    ON CONFLICT(key) DO UPDATE SET value = '1000000';
