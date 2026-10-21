-- 0015: 箱体最小宽度参数化（2026-10-22 校订 A2）
-- 动机（用户拍板的三方对照校订，A2）：
--   箱体宽度校验 (top-bottom)/top >= MinBoxWidth 是工程假设，达瓦斯原书无此量化定义
--   （原书箱体宽度由「三日法则」自然走出，不设下限）。此前该值硬编码在
--   box/scanner.go 与 cmd/darvas/main.go 的 0.05，既不可调、又在文档中「实现有、
--   文档无」，属隐藏规则。
--   校订决定：保留 5% 下限（过滤窄幅噪音，使箱体形态更有意义），但参数化为
--   box.min_width=0.05，供运行时/回测校准，并在 docs/00 §3 诚实标注为工程假设。
-- 同步：docs/00 §3、docs/04 §6.1 BoxParams 说明。

INSERT OR IGNORE INTO param (key, value) VALUES ('box.min_width', '0.05');
