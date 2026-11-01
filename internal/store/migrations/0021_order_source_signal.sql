-- 0021: 交易对象追踪链（2026-11-02，docs/40 §2 T2 / docs/03 §2.1）
-- 动机：三份审计（GPT-37 / Claude 38.a / 38.b）按代码裁定后确认的两个硬缺口（docs/40 §1）：
--   1) position.entry_box_id 全 NULL——planner 生成订单的 reason_json 从未写 box_id，
--      ledger.applyBuy 的 reasonBoxID()（早已就位）取不到值 → 持仓与入场箱体关联断裂；
--      历史分析只能用 (code, box_top, box_bottom) 反向匹配 box 表（LB 系列已验证唯一命中 100%）。
--   2) signal → plan_order 无显式外键——突破事件到订单只能靠 reason_json 间接追。
-- 改动：plan_order 追加 source_signal_id 列（signal.id 强引用）。planner 生成信号驱动订单时写入；
--   非信号驱动订单（time_stop_noprofit / initial_stop / 手动粘贴单）为 NULL——语义正确，不是缺失。
-- 历史行兼容：**不回填、不伪造**。NULL = 「早于 0021 的订单」的诚实标记；
--   历史库分析继续用反向匹配口径，新老两种口径在报告里必须显式声明。
-- 同 commit 同步：docs/03 §2.1/§4/§6.1、docs/04 §2、docs/06 §3.1。
-- 注意：本迁移只加列与索引，不改任何参数默认值（无 param 种子，不触发 docs/01 §6 同步义务）。

ALTER TABLE plan_order ADD COLUMN source_signal_id INTEGER REFERENCES signal(id);

CREATE INDEX IF NOT EXISTS idx_plan_order_source_signal ON plan_order(source_signal_id);
