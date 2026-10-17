// csv.go —— 炒股手训练软件成交 CSV 导入（C，第二数据源）。
//
// 数据源：H:\2025炒股手训练软件 EXPORTT\*.csv（用户在炒股手中模拟执行系统
// 下单指令，导出成交回流审核执行质量；docs/04 §7 fills 子命令）。
// 格式（2009/2015/2019/2024 四个年代样本验证稳定）：
//   - GBK 编码，首行13列表头
//   - 列：序号,日期(YYYYMMDD),操作(买/卖),股票代码("300096"_ 引号外下划线),
//     股票名称,成交价,成本价,盈亏情况,盈亏比率,成交数量(800.00),成交金额,手续费,交易理由
//   - 盈亏两列不信任导出值（系统 realized_pnl 自算含费净现金流）；
//     交易理由（"强制平仓""自动止损"…）留原文审计。
package ledger

import (
	"fmt"
	"math"
	"strconv"
	"strings"

	"golang.org/x/text/encoding/simplifiedchinese"

	"darvas/internal/contract"
	"darvas/internal/store"
)

// ParseChaoGushouCsv 解析炒股手导出 CSV（GBK）为成交清单。
// 严格表头校验（非炒股手格式直接报错，宁可拒绝不可错账）；
// 解析失败的行报错带行号与原文。
func (l *Ledger) ParseChaoGushouCsv(raw []byte) ([]contract.Fill, error) {
	text, err := simplifiedchinese.GBK.NewDecoder().Bytes(raw)
	if err != nil {
		return nil, fmt.Errorf("GBK 解码失败: %w", err)
	}
	lines := strings.Split(string(text), "\n")
	if len(lines) < 2 || !isChaoGushouHeader(csvFields(strings.TrimSpace(lines[0]))) {
		return nil, fmt.Errorf("非炒股手导出格式：首行应为 13 列表头（序号,日期,操作,…）")
	}
	var fills []contract.Fill
	for i, line := range lines {
		line = strings.TrimSpace(strings.ReplaceAll(line, "\r", ""))
		if line == "" {
			continue
		}
		fields := csvFields(line)
		if isChaoGushouHeader(fields) {
			continue // 重复表头（多段导出拼接）
		}
		f, err := parseChaoGushouRow(fields)
		if err != nil {
			return nil, fmt.Errorf("第%d行解析失败: %w\n原文: %s", i+1, err, line)
		}
		f.RawText = line
		fills = append(fills, f)
	}
	if len(fills) == 0 {
		return nil, fmt.Errorf("CSV 中未解析出任何成交记录")
	}
	return fills, nil
}

// isChaoGushouHeader 表头特征：序号/日期/操作三列齐。
func isChaoGushouHeader(fields []string) bool {
	return len(fields) >= 13 &&
		strings.Contains(fields[0], "序号") &&
		strings.Contains(fields[1], "日期") &&
		strings.Contains(fields[2], "操作")
}

// parseChaoGushouRow 解析单行成交（13 列语义映射）。
func parseChaoGushouRow(fields []string) (contract.Fill, error) {
	var f contract.Fill
	if len(fields) < 13 {
		return f, fmt.Errorf("列数=%d want 13", len(fields))
	}
	// 日期 YYYYMMDD → YYYY-MM-DD
	d := strings.TrimSpace(fields[1])
	if len(d) != 8 {
		return f, fmt.Errorf("日期 %q 非8位YYYYMMDD", d)
	}
	if _, err := strconv.Atoi(d); err != nil {
		return f, fmt.Errorf("日期 %q 含非数字", d)
	}
	f.Date = d[:4] + "-" + d[4:6] + "-" + d[6:8]

	// 操作 买/卖
	switch strings.TrimSpace(fields[2]) {
	case "买":
		f.Side = "buy"
	case "卖":
		f.Side = "sell"
	default:
		return f, fmt.Errorf("操作 %q 非买/卖", fields[2])
	}

	// 代码：`300096_` → normalizeCode（无前缀按首位推断 sz/sh）
	code, err := normalizeCode(strings.Trim(fields[3], "\"_ \t"))
	if err != nil {
		return f, err
	}
	f.Code = code

	// 成交价 / 成交数量(浮点文本) / 手续费
	price, err := strconv.ParseFloat(strings.TrimSpace(fields[5]), 64)
	if err != nil || price <= 0 {
		return f, fmt.Errorf("成交价 %q 非法", fields[5])
	}
	qtyF, err := strconv.ParseFloat(strings.TrimSpace(fields[9]), 64)
	if err != nil || qtyF <= 0 || qtyF != math.Trunc(qtyF) {
		return f, fmt.Errorf("成交数量 %q 非法（须为整数股）", fields[9])
	}
	fee, err := strconv.ParseFloat(strings.TrimSpace(fields[11]), 64)
	if err != nil || fee < 0 {
		return f, fmt.Errorf("手续费 %q 非法", fields[11])
	}
	f.Price, f.Qty, f.Fee = price, int(qtyF), fee
	return f, nil
}

// csvFields 按逗号切分（引号内不切；`"300096"_` 引号外后缀随引号剔除后保留下划线）。
func csvFields(line string) []string {
	var fields []string
	var cur strings.Builder
	inQuote := false
	for _, r := range line {
		switch {
		case r == '"':
			inQuote = !inQuote
		case r == ',' && !inQuote:
			fields = append(fields, cur.String())
			cur.Reset()
		default:
			cur.WriteRune(r)
		}
	}
	fields = append(fields, cur.String())
	return fields
}

// ---------------------------------------------------------------------------
// 执行质量审核报告（C：指令 vs 实际执行对照）
// ---------------------------------------------------------------------------

// FillsAuditReport 生成审核报告（导入后打印）。
// 四个维度：①漏执行（范围内仍 pending/placed）②价格滑点（成交 vs 订单限价）
// ③数量偏差 ④计划外成交 / 重复跳过。
func (l *Ledger) FillsAuditReport(fills []contract.Fill, results []ApplyResult) string {
	if len(fills) == 0 || len(results) == 0 {
		return "（无成交可审核）"
	}
	from, to := fills[0].Date, fills[0].Date
	for _, f := range fills {
		if f.Date < from {
			from = f.Date
		}
		if f.Date > to {
			to = f.Date
		}
	}
	orders, err := l.st.TradeRepo.OrdersBetween(from, to)
	if err != nil {
		return fmt.Sprintf("⚠ 审核查询失败: %v", err)
	}

	var b strings.Builder
	fmt.Fprintf(&b, "── 执行质量审核（%s ~ %s）──\n", from, to)

	// 订单执行状态分布
	executed, missed, expired, other := 0, 0, 0, 0
	for _, o := range orders {
		switch o.Status {
		case "filled":
			executed++
		case "pending", "placed":
			missed++
		case "expired", "cancelled":
			expired++
		default:
			other++
		}
	}
	fmt.Fprintf(&b, "订单 %d 笔：执行%d 漏执行%d 已过期/取消%d\n",
		len(orders), executed, missed, expired+other)

	// ②③ 偏差：按 ApplyResult.OrderID 对照订单限价/数量
	matched, unplanned, dup, slippageLines, qtyDiff := 0, 0, 0, 0, 0
	orderByID := make(map[int64]store.OrderAuditRow, len(orders))
	for _, o := range orders {
		orderByID[o.ID] = o
	}
	for _, r := range results {
		if r.Duplicate {
			dup++
			continue
		}
		if !r.Matched {
			unplanned++
			continue
		}
		matched++
		o, ok := orderByID[r.OrderID]
		if !ok {
			continue
		}
		dp := r.Fill.Price - o.LimitPx
		if math.Abs(dp) >= 0.01 {
			slippageLines++
			fmt.Fprintf(&b, "  滑点 %s %s：成交%.2f vs 限价%.2f（%+.2f）\n",
				r.Fill.Code, sideCN(r.Fill.Side), r.Fill.Price, o.LimitPx, dp)
		}
		if r.Fill.Qty != o.Qty {
			qtyDiff++
			fmt.Fprintf(&b, "  数量偏差 %s：成交%d vs 订单%d\n", r.Fill.Code, r.Fill.Qty, o.Qty)
		}
	}
	fmt.Fprintf(&b, "成交 %d 笔：匹配订单%d 计划外%d 重复跳过%d；滑点≥0.01元 %d笔 数量偏差 %d笔\n",
		len(results), matched, unplanned, dup, slippageLines, qtyDiff)

	// 全部重复跳过（幂等重导）→ 无新增执行，不评纪律
	if dup == len(results) {
		b.WriteString("（全部为重复跳过，幂等重导无新增执行）\n")
		return b.String()
	}

	// ① 漏执行明细
	for _, o := range orders {
		if o.Status == "pending" || o.Status == "placed" {
			fmt.Fprintf(&b, "  ⚠ 漏执行 订单#%d %s %s 限价%.2f×%d（执行日%s）\n",
				o.ID, o.Code, sideCN(o.Side), o.LimitPx, o.Qty, o.ExecuteDate)
		}
	}
	if unplanned > 0 {
		fmt.Fprintf(&b, "  ⚠ 计划外成交 %d 笔（系统未下指令，核对是否手痒）\n", unplanned)
	}
	if missed == 0 && unplanned == 0 && slippageLines == 0 && qtyDiff == 0 {
		b.WriteString("✓ 纪律满分：全部指令按计划执行\n")
	}
	return b.String()
}

// sideCN 买卖中文。
func sideCN(side string) string {
	if side == "buy" {
		return "买"
	}
	return "卖"
}
