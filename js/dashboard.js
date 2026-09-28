/* 数据总览页逻辑（R7）：卡片数据 + ECharts 图表 + 最新告警列表 */

const LEVEL_TEXT = { 1: '一级', 2: '二级', 3: '三级' };
const STATUS_CLASS = {
  '待处理': 'tag-status-pending',
  '已处理': 'tag-status-done',
  '已驳回': 'tag-status-rejected',
};

let pieChart = null, lineChart = null, barChart = null;

function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* 无数据时在图表中央提示，避免出现空白画布 */
function emptyTitle(text) {
  return {
    title: {
      text: text, left: 'center', top: '45%',
      textStyle: { fontSize: 12, fontWeight: 'normal', color: '#9ca3af' },
    },
  };
}

function renderRecent(items) {
  const tbody = document.getElementById('recentBody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">暂无告警记录</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(function (a) {
    const levelCls = 'tag tag-level-' + a.level;
    const statusCls = 'tag ' + (STATUS_CLASS[a.status] || 'tag-status-rejected');
    return '<tr>' +
      '<td class="dim">' + escapeHtml(String(a.alarm_time || '').replace('T', ' ')) + '</td>' +
      '<td>' + escapeHtml(a.location || '未分配点位') + '</td>' +
      '<td>' + escapeHtml(a.violation_type) + '</td>' +
      '<td><span class="' + levelCls + '">' + escapeHtml(LEVEL_TEXT[a.level] || a.level) + '</span></td>' +
      '<td><span class="' + statusCls + '">' + escapeHtml(a.status) + '</span></td>' +
      '</tr>';
  }).join('');
}

function renderCharts(data) {
  if (!window.echarts) return;

  const ratio = data.type_ratio || [];
  pieChart = pieChart || echarts.init(document.getElementById('pieChart'));
  pieChart.setOption(Object.assign({
    tooltip: { trigger: 'item', formatter: '{b}：{c} 条（{d}%）' },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11, color: '#6b7280' } },
    color: ['#d93025', '#e8871e', '#e0b400', '#2563eb', '#15803d', '#6b7280'],
    series: [{
      type: 'pie',
      radius: ['40%', '66%'],
      center: ['50%', '44%'],
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      label: { formatter: '{b} {c}', fontSize: 11, color: '#374151' },
      data: ratio,
    }],
  }, ratio.length ? {} : emptyTitle('近 7 日暂无告警')));

  const trend = data.trend || [];
  lineChart = lineChart || echarts.init(document.getElementById('lineChart'));
  lineChart.setOption({
    grid: { left: 44, right: 20, top: 24, bottom: 32 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: trend.map(function (t) { return t.date; }),
      axisLine: { lineStyle: { color: '#e5e7eb' } },
      axisLabel: { fontSize: 11, color: '#6b7280' },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      splitLine: { lineStyle: { color: '#f3f4f6' } },
      axisLabel: { fontSize: 11, color: '#6b7280' },
    },
    series: [{
      type: 'line',
      smooth: true,
      symbolSize: 6,
      data: trend.map(function (t) { return t.count; }),
      itemStyle: { color: '#2563eb' },
      lineStyle: { width: 2, color: '#2563eb' },
      areaStyle: { color: 'rgba(37, 99, 235, 0.08)' },
    }],
  });

  const rank = data.workshop_rank || [];
  barChart = barChart || echarts.init(document.getElementById('barChart'));
  barChart.setOption(Object.assign({
    grid: { left: 60, right: 24, top: 24, bottom: 32 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: {
      type: 'category',
      data: rank.map(function (r) { return r.name; }),
      axisLine: { lineStyle: { color: '#e5e7eb' } },
      axisLabel: { fontSize: 11, color: '#6b7280' },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      splitLine: { lineStyle: { color: '#f3f4f6' } },
      axisLabel: { fontSize: 11, color: '#6b7280' },
    },
    series: [{
      type: 'bar',
      barMaxWidth: 42,
      data: rank.map(function (r) { return r.value; }),
      itemStyle: { color: '#2563eb' },
      label: { show: true, position: 'top', fontSize: 11, color: '#374151' },
    }],
  }, rank.length ? {} : emptyTitle('近 7 日暂无告警')));
}

let lastStats = null;

async function loadDashboard() {
  const res = await Auth.apiFetch('/api/stats?days=7');
  const data = await res.json();
  if (data.status !== 'ok') {
    document.getElementById('recentBody').innerHTML =
      '<tr><td colspan="5" class="empty-state">' + escapeHtml(data.message || '统计数据加载失败') + '</td></tr>';
    return;
  }

  const c = data.cards;
  setText('cardToday', c.today_count);
  setText('cardPending', c.pending_count);
  setText('cardDevices', c.device_online);
  setText('cardLevel1', c.today_level1);
  const rate = c.device_total ? Math.round(c.device_online / c.device_total * 100) : 0;
  setText('cardDeviceSub', '共 ' + c.device_total + ' 台，在线率 ' + rate + '%');

  lastStats = data;
  renderRecent(data.recent || []);
  renderCharts(data);
}

/* ECharts 走 CDN，主源失败时换备用源，避免图表区空白 */
function ensureEcharts(cb) {
  if (window.echarts) { cb(); return; }
  const s = document.createElement('script');
  s.src = 'https://cdn.bootcdn.net/ajax/libs/echarts/5.4.3/echarts.min.js';
  s.onload = cb;
  s.onerror = function () {
    document.querySelectorAll('.chart-box').forEach(function (box) {
      box.innerHTML = '<div class="empty-state">图表库加载失败，请检查网络后刷新</div>';
    });
  };
  document.head.appendChild(s);
}

window.addEventListener('resize', function () {
  [pieChart, lineChart, barChart].forEach(function (c) { if (c) c.resize(); });
});

// 卡片与列表先出，图表库就绪后再补画图，避免 CDN 慢时整页白屏
(async function () {
  await loadDashboard();
  ensureEcharts(function () { if (lastStats) renderCharts(lastStats); });
})();