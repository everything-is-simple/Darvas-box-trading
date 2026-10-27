-- 0018: A′ 50% 权益预算落地（2026-10-27 用户拍板，docs/00 §5.6.9）
-- 动机：永久留 50% 现金、总仓位 ≤50%；只数上限 pool.max_hold=7 与 50% 预算不兼容
--   （7 × 实测均值 2.6% ≈ 18%，吃不满 50%），故废止，改由权益预算单独约束。
-- 规则：下单金额 = min(单位仓 1/2/4/6%, 50% 剩余额度, 单票 8% 权益)；
--   已用额度 = Σ持仓市值 + Σ未成交买单名义（挂单生成即预占，撤单/过期释放）。
-- 同步：docs/00 §5.6.9 落地注记、docs/01 §10 参数表、docs/33 §2.2/§三/§六。

-- 1) 只数上限废止：0 = 不限（仅 B-4 变体对照时临时设 7/14）
INSERT INTO param(key, value) VALUES ('pool.max_hold', '0')
    ON CONFLICT(key) DO UPDATE SET value = '0';

-- 2) 权益预算三参数（pos.equity_gate=off 可复现旧语义基线）
INSERT INTO param(key, value) VALUES
    ('pos.equity_gate',  'on'),
    ('pos.equity_limit', '0.5'),
    ('pos.single_limit', '0.08')
    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
