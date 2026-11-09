-- 0033: 干净默认复现 —— 突破即买 + 停用两处反向刹车（2026-11-09 用户「开工」授权）
--
-- 依据：
--   docs/65 §1.2.4 第 1 项：`buy.premium=1.03` 判 **类别 Ⅰ ＝ O4 原著规则缺失**，
--     不是 A 股涨停板制度补偿、不是段 C 数值尺度 → 段 A 必须改成「突破上轨即买」1.00。
--   docs/65 §1.2.5 第 2 项：O2／O3 生产默认仍是 `position.staledays=2`／
--     `pool.blacklist_months=12`（迁移 0016／0031），实验臂靠 `--set` 注入 0；
--     完成线前必须写迁移把干净默认改成原装值，否则冻结的是带注入的实验臂。
--   docs/65 §1.2.2 O2／O3 验收契约：干净默认（不带 `--set`）下两键为禁用档。
--   docs/65 §1.2.4 第 2 项：`pos.single_limit=0.08` 保持不动，本迁移不碰。
--
-- 三键合成一次迁移 + 一次不带 `--set` 的全窗干净默认复跑（docs/65 §1.2.4 / §1.2.5）。
-- 顺序理由：先改买点再跑阶段 3 金字塔加仓；若先跑加仓再改买点，整本账作废。
--
-- 实现口径：
--   · 代码默认值同步改为 1.00 / 0 / 0（param_ledger.py 扫 Get* 字面量，C6 据此对齐）。
--   · 本迁移用 upsert 覆盖 0001／0016／0031 的旧种子值；append-only，旧迁移不改。
--   · 起跑走 `python scripts/base_run.py ... --clean-default`（完全不传 `--set`），
--     所有参数取库内种子值；base_run.py 的 COMMON 保留历史臂复现用，不参与本次。
--   · 不替换 docs/anchors 锚点（60 §6「锚点收官／升基准暂缓」仍生效），
--     本次只冻结新账本为现行收官库，阶段 5 完成线才走 doc_anchors.py --write。
--
-- ⚠ 全窗前视未验：K1 全量已裁定不补跑（65 §1.2.5），本账本及任何引用其数字的
--   报告首行须照写此警示。

INSERT INTO param(key, value) VALUES ('buy.premium', '1.00')
    ON CONFLICT(key) DO UPDATE SET value = '1.00';

INSERT INTO param(key, value) VALUES ('position.staledays', '0')
    ON CONFLICT(key) DO UPDATE SET value = '0';

INSERT INTO param(key, value) VALUES ('pool.blacklist_months', '0')
    ON CONFLICT(key) DO UPDATE SET value = '0';
