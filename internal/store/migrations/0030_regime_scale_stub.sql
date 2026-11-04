-- 0030: 攻防转换（docs/48 新基线特征二）传导接口桩 —— 仅种子化 mode 键，默认 off（2026-11-04）
-- 定位：特征二的「传导」落点 = planner.go 算完阶梯单位后、lotQty 前的一处乘法（docs/48 §四 L3）。
--   本文只把「开关键」真实化 + 挂点占位，**不写状态机、不跑 B 类、不种子化 on 的建议值**。
--   传导实现依赖后复权（docs/53）之后的新宽度序列，判据基准须新基线账本重算
--   （docs/49 §五之二 特征二，2026-11-04 拍板）。
-- 语义：mode=off → 单位乘数恒 1.0，与 0029 基线逐字节等价（等价性锚 TestRegimeScaleMultStub）。
--   mode=on → 读 L1 状态机（宽度 b50 + 距高 dd52 双确认 + 3 日滞回）映射乘数，**留待新基线**。
--   其余 7 个建议值键（attack_mult/defend_mult/breadth_min/breadth_exit/dd_max/dd_exit/confirm_days）
--   在 on 实现时随状态机一并种子化，现在不入库——避免库内多出代码不读的「幽灵键」。

INSERT INTO param(key, value) VALUES ('pos.regime_scale.mode', 'off')
    ON CONFLICT(key) DO UPDATE SET value = 'off';
