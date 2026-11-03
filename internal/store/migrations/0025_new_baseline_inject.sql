-- 0025: 重建新基线 —— 4 个实验结论注入项落地（2026-11-03 用户拍板，方案①迁移种子化）
-- 背景：docs/44 §一之二 生产机对账发现，7 个「实验结论注入项」里 4 个代码默认值指向旧基线/关闭态。
--   P1/P2 实验（P3-8、大赢家加仓、甜点复验、止损上移、入场旋钮）已全部收官，触发「重建新基线」条件。
-- 落法：改 param 种子值（与 0024 同套路，启动自动补跑），不动 contract.go 代码默认。
--   回测经 --set 显式注入时不受影响；旧库重跑（schema 已含本 migration）会自动落到新值。
-- 依据（逐键）：
--   pos.newhigh_ladder=on   —— 现行生产仓位=阶梯制（距52周新高四档 3/2/1/0.5），非已退役的 A M1。docs/44 §一第3条。
--   gate.ma60.mode=stop     —— MA60 闸门（防守端）：上证收盘 < MA60 不生成买单。P0-3 B 类结论，docs/44 §一。
--   pool.max_hold=14        —— 只数上限，B-4 实验（2026-10-28）终判：14 只四年 2.235× 胜 7 只，预算闸门否决。
--   pos.equity_gate=off     —— 关闭 50% 权益预算闸门（B-4 已否决），复现「只数上限」语义基线。docs/44 §一第4条。
-- 注：pool.max_hold 用户曾提「14→20（2.6%×20≈52%）」，但 20 档 B-4 未验、且逼近被否决的 50% 闸门，
--   故本次落 14（已验证结论值），20 作为「待验证的进一步放宽」记入待办（docs/44 §四），验后再动。

INSERT INTO param(key, value) VALUES ('pos.newhigh_ladder', 'on')
    ON CONFLICT(key) DO UPDATE SET value = 'on';

INSERT INTO param(key, value) VALUES ('gate.ma60.mode', 'stop')
    ON CONFLICT(key) DO UPDATE SET value = 'stop';

INSERT INTO param(key, value) VALUES ('pool.max_hold', '14')
    ON CONFLICT(key) DO UPDATE SET value = '14';

INSERT INTO param(key, value) VALUES ('pos.equity_gate', 'off')
    ON CONFLICT(key) DO UPDATE SET value = 'off';
