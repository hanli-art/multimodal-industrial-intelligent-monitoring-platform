/* 统计报表页逻辑（R7）：日/周/月维度台账查询 + CSV 导出 */

const PAGE_SIZE = 20;
const STATUS_CLASS = {
  '待处理': 'tag-status-pending',
  '已处理': 'tag-status-done',
  '已驳回': 'tag-status-rejected',
};

let period = 'day';
let page = 1;

function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function pad(n) { return String(n).padStart(2, '0'); }

function fmtDate(d) {
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

/* 日报表=今天；周报表=近 7 天（含今天）；月报表=本月 1 号至今 */
function currentRange() {
  const now = new Date();
  const start = new Date(now);
  let label = '本日';
  if (period === 'week') {
    start.setDate(now.getDate() - 6);
    label = '近 7 天';
  } else if (period === 'month') {
    start.setDate(1);
    label = '本月';
  }
  const startTime = fmtDate(start) + ' 00:00:00';
  const endTime = fmtDate(now) + ' 23:59:59';
  return {
    startTime: startTime,
    endTime: endTime,
    text: label + '（' + startTime.slice(0, 10) + ' ~ ' + endTime.slice(0, 10) + '）',
  };
}

/* 查询条件：报表页只按时间范围 + 违规行为 + 状态过滤 */
function queryParams() {
  const range = currentRange();
  const params = new URLSearchParams();
  params.set('start_time', range.startTime);
  params.set('end_time', range.endTime);
  const type = document.getElementById('typeFilter').value;
  if (type) params.set('violation_type', type);
  const status = document.getElementById('statusFilter').value;
  if (status) params.set('status', status);
  return { params: params, range: range };
}

async function loadTable() {
  const q = queryParams();
  document.getElementById('rangeTip').textContent = '统计范围：' + q.range.text;

  const params = new URLSearchParams(q.params);
  params.set('page', page);
  params.set('page_size', PAGE_SIZE);

  const res = await Auth.apiFetch('/api/alarms?' + params.toString());
  const data = await res.json();
  const tbody = document.getElementById('tbody');
  if (data.status !== 'ok') {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">' +
      escapeHtml(data.message || '加载失败') + '</td></tr>';
    return;
  }

  document.getElementById('totalTip').textContent = '共 ' + data.total + ' 条隐患记录';

  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">该时间范围内暂无告警记录</td></tr>';
    renderPagination(0);
    return;
  }

  tbody.innerHTML = data.items.map(function (a) {
    const statusCls = 'tag ' + (STATUS_CLASS[a.status] || 'tag-status-rejected');
    return '<tr>' +
      '<td class="dim">HZ' + String(a.id).padStart(5, '0') + '</td>' +
      '<td class="dim">' + escapeHtml(String(a.alarm_time || '').replace('T', ' ')) + '</td>' +
      '<td>' + escapeHtml(a.location || '未分配点位') + '</td>' +
      '<td>' + escapeHtml(a.violation_type) + '</td>' +
      '<td><span class="' + statusCls + '">' + escapeHtml(a.status) + '</span></td>' +
      '</tr>';
  }).join('');

  renderPagination(data.total);
}

function renderPagination(total) {
  const box = document.getElementById('pagination');
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  let html = '<span>第 ' + page + ' / ' + pages + ' 页</span>' +
    '<button class="page-btn" data-page="' + (page - 1) + '"' + (page <= 1 ? ' disabled' : '') + '>上一页</button>' +
    '<button class="page-btn" data-page="' + (page + 1) + '"' + (page >= pages ? ' disabled' : '') + '>下一页</button>';
  box.innerHTML = html;
  box.querySelectorAll('.page-btn').forEach(function (btn) {
    btn.onclick = function () {
      page = parseInt(btn.getAttribute('data-page'), 10);
      loadTable();
    };
  });
}

async function exportCsv() {
  const q = queryParams();
  const btn = document.getElementById('btnExport');
  btn.disabled = true;
  try {
    const res = await Auth.apiFetch('/api/alarms/export?' + q.params.toString());
    if (!res.ok) { alert('导出失败，请稍后重试'); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '告警台账_' + period + '_' + fmtDate(new Date()) + '.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('导出失败：' + e.message);
  } finally {
    btn.disabled = false;
  }
}

document.querySelectorAll('.tab').forEach(function (tab) {
  tab.onclick = function () {
    document.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
    tab.classList.add('active');
    period = tab.getAttribute('data-period');
    page = 1;
    loadTable();
  };
});

document.getElementById('btnSearch').onclick = function () { page = 1; loadTable(); };
document.getElementById('btnReset').onclick = function () {
  document.getElementById('typeFilter').value = '';
  document.getElementById('statusFilter').value = '';
  page = 1;
  loadTable();
};
document.getElementById('btnExport').onclick = exportCsv;

loadTable();