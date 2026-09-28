/* 操作日志页逻辑（R10）：时间 / 用户 / 操作类型筛选 + 分页查询 */

const PAGE_SIZE = 20;

let page = 1;

function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* datetime-local 的值（2026-09-28T20:00）转成接口需要的 YYYY-MM-DD HH:MM:SS */
function toSqlTime(value) {
  if (!value) return '';
  return value.replace('T', ' ') + ':00';
}

function queryParams() {
  const params = new URLSearchParams();
  const start = toSqlTime(document.getElementById('startTime').value);
  const end = toSqlTime(document.getElementById('endTime').value);
  const username = document.getElementById('userFilter').value;
  const opType = document.getElementById('typeFilter').value;
  if (start) params.set('start_time', start);
  if (end) params.set('end_time', end);
  if (username) params.set('username', username);
  if (opType) params.set('op_type', opType);
  return params;
}

/* 下拉选项只在首次加载时拉取，避免查询后被重置 */
async function loadFilters() {
  try {
    const res = await Auth.apiFetch('/api/logs/filters');
    const data = await res.json();
    if (data.status !== 'ok') return;
    const userBox = document.getElementById('userFilter');
    const typeBox = document.getElementById('typeFilter');
    data.usernames.forEach(function (u) {
      userBox.insertAdjacentHTML('beforeend',
        '<option value="' + escapeHtml(u) + '">' + escapeHtml(u) + '</option>');
    });
    data.op_types.forEach(function (t) {
      typeBox.insertAdjacentHTML('beforeend',
        '<option value="' + escapeHtml(t) + '">' + escapeHtml(t) + '</option>');
    });
  } catch (e) {
    /* 筛选项拉取失败不影响列表查询 */
  }
}

async function loadTable() {
  const params = new URLSearchParams(queryParams());
  params.set('page', page);
  params.set('page_size', PAGE_SIZE);

  const res = await Auth.apiFetch('/api/logs?' + params.toString());
  const data = await res.json();
  const tbody = document.getElementById('tbody');
  if (data.status !== 'ok') {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">' +
      escapeHtml(data.message || '加载失败') + '</td></tr>';
    return;
  }

  document.getElementById('totalTip').textContent = '共 ' + data.total + ' 条日志';

  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">没有符合条件的操作日志</td></tr>';
    renderPagination(0);
    return;
  }

  tbody.innerHTML = data.items.map(function (log) {
    return '<tr>' +
      '<td class="dim">' + escapeHtml(String(log.op_time || '').replace('T', ' ')) + '</td>' +
      '<td>' + escapeHtml(log.username || '—') + '</td>' +
      '<td>' + escapeHtml(log.op_type) + '</td>' +
      '<td>' + escapeHtml(log.detail) + '</td>' +
      '</tr>';
  }).join('');

  renderPagination(data.total);
}

function renderPagination(total) {
  const box = document.getElementById('pagination');
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  box.innerHTML = '<span>第 ' + page + ' / ' + pages + ' 页</span>' +
    '<button class="page-btn" data-page="' + (page - 1) + '"' + (page <= 1 ? ' disabled' : '') + '>上一页</button>' +
    '<button class="page-btn" data-page="' + (page + 1) + '"' + (page >= pages ? ' disabled' : '') + '>下一页</button>';
  box.querySelectorAll('.page-btn').forEach(function (btn) {
    btn.onclick = function () {
      page = parseInt(btn.getAttribute('data-page'), 10);
      loadTable();
    };
  });
}

document.getElementById('btnSearch').onclick = function () { page = 1; loadTable(); };
document.getElementById('btnReset').onclick = function () {
  document.getElementById('startTime').value = '';
  document.getElementById('endTime').value = '';
  document.getElementById('userFilter').value = '';
  document.getElementById('typeFilter').value = '';
  page = 1;
  loadTable();
};

(async function () {
  await loadFilters();
  await loadTable();
})();