(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  // --- Chart: heartbeat（22 小时 25 次提交）---
  var el = document.getElementById('chart-heartbeat');
  if (!el || typeof echarts === 'undefined') return;
  var chart = echarts.init(el, null, { renderer: 'svg' });

  var typeColor = { feat: accent, fix: accent2, docs: muted, chore: 'rgba(150,156,171,0.55)' };
  var typeLabel = { feat: 'feat', fix: 'fix', docs: 'docs', chore: 'chore' };

  // h = 距 2026-10-17 08:00 的小时数
  var commits = [
    { h: 0.73,  t: '10-17 08:44', m: 'M0 骨架与全套设计文档', ty: 'feat' },
    { h: 0.78,  t: '10-17 08:47', m: '一致性检查脚本（门禁先于功能）', ty: 'chore' },
    { h: 1.02,  t: '10-17 09:01', m: 'M1.1 性能优化列为首要项', ty: 'docs' },
    { h: 1.48,  t: '10-17 09:29', m: '归并唯一本地库', ty: 'docs' },
    { h: 3.27,  t: '10-17 11:16', m: 'M1.1 性能优化 + M2 箱体引擎（35 用例）', ty: 'feat' },
    { h: 4.22,  t: '10-17 12:13', m: 'M5 端到端流水线打通', ty: 'feat' },
    { h: 5.95,  t: '10-17 13:57', m: 'M4 收尾：成交解析 + 账本审计', ty: 'feat' },
    { h: 6.10,  t: '10-17 14:06', m: 'M6 准备：RunRepo 心跳查询', ty: 'feat' },
    { h: 6.98,  t: '10-17 14:59', m: 'M6 本地 HTTP UI 四视图', ty: 'feat' },
    { h: 7.27,  t: '10-17 15:16', m: 'M7 常驻调度 serve + 备份轮转', ty: 'feat' },
    { h: 8.78,  t: '10-17 16:47', m: 'M6.5 UI 改进 + 调度竞态修复', ty: 'feat' },
    { h: 9.13,  t: '10-17 17:08', m: 'M7.2 weekly 周报', ty: 'feat' },
    { h: 9.20,  t: '10-17 17:12', m: 'M7.4 移植演练实录', ty: 'docs' },
    { h: 9.22,  t: '10-17 17:13', m: 'fix：weekly 幂等', ty: 'fix' },
    { h: 9.70,  t: '10-17 17:42', m: 'fix：股票池收敛至沪深 A 股', ty: 'fix' },
    { h: 12.73, t: '10-17 20:44', m: 'fix：首次事件语义（回放发现）', ty: 'fix' },
    { h: 14.02, t: '10-17 22:01', m: 'fix：回填脚本表名对齐', ty: 'fix' },
    { h: 14.60, t: '10-17 22:36', m: 'fix：执行日=下一交易日', ty: 'fix' },
    { h: 15.32, t: '10-17 23:20', m: 'cleanup 脚本入库', ty: 'chore' },
    { h: 15.34, t: '10-17 23:20', m: 'gofmt 格式修正', ty: 'chore' },
    { h: 22.10, t: '10-18 06:06', m: 'B1 首腿止损（原书校对）', ty: 'fix' },
    { h: 22.45, t: '10-18 06:27', m: 'B2 淘汰生命周期五规则', ty: 'feat' },
    { h: 22.67, t: '10-18 06:40', m: 'B3 已交易股票历史库', ty: 'feat' },
    { h: 22.78, t: '10-18 06:47', m: 'C 炒股手 CSV 导入器', ty: 'feat' },
    { h: 22.92, t: '10-18 06:55', m: 'D 工程还债', ty: 'chore' }
  ];

  var data = commits.map(function(c) {
    return {
      value: [c.h, 1],
      time: c.t, msg: c.m, ty: c.ty,
      itemStyle: { color: typeColor[c.ty], borderColor: bg2, borderWidth: 1.5 }
    };
  });

  var xLabels = { 0: '10-17 08:00', 4: '12:00', 8: '16:00', 12: '20:00', 16: '10-18 00:00', 20: '04:00', 24: '08:00' };

  chart.setOption({
    animation: false,
    tooltip: {
      appendToBody: true,
      backgroundColor: '#151a23',
      borderColor: rule,
      textStyle: { color: ink, fontSize: 12 },
      formatter: function(p) {
        return '<span style="color:#969cab;font-size:11px">' + p.data.time + ' · ' + typeLabel[p.data.ty] + '</span><br>' + p.data.msg;
      }
    },
    grid: { left: 16, right: 22, top: 64, bottom: 44 },
    xAxis: {
      type: 'value', min: 0, max: 24, interval: 4,
      axisLine: { show: true, lineStyle: { color: rule } },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: {
        color: muted, fontSize: 11, margin: 12,
        formatter: function(v) { return xLabels[v] || ''; }
      }
    },
    yAxis: { type: 'value', min: 0, max: 2, show: false },
    series: [{
      type: 'scatter',
      symbol: 'circle',
      symbolSize: 14,
      data: data,
      markArea: {
        silent: true,
        itemStyle: { color: 'rgba(201,163,92,0.045)' },
        label: { show: true, position: 'insideTop', color: muted, fontSize: 11, distance: 8 },
        data: [
          [{ name: '骨架与门禁', xAxis: 0 }, { xAxis: 2.4 }],
          [{ name: '引擎与流水线', xAxis: 2.4 }, { xAxis: 4.6 }],
          [{ name: 'GUI 与调度（M0→M7 六个半小时）', xAxis: 4.6 }, { xAxis: 10.4 }],
          [{ name: '夜间深水排雷', xAxis: 10.4 }, { xAxis: 16.5 }],
          [{ name: '晨间五连击 B1→D', xAxis: 16.5 }, { xAxis: 23.3 }]
        ]
      }
    }]
  });

  window.addEventListener('resize', function() { chart.resize(); });
})();
