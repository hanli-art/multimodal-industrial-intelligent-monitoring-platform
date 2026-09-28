/* 告警管理页逻辑 */

/* ==================== 工具函数 ==================== */
function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const LEVEL_TAG = { 1: 'tag-level-1', 2: 'tag-level-2', 3: 'tag-level-3' };
const LEVEL_TEXT = { 1: '一级', 2: '二级', 3: '三级' };
const STATUS_TAG = {
  '待处理': 'tag-status-pending',
  '已处理': 'tag-status-done',
  '已驳回': 'tag-status-rejected',
};

let currentPage = 1;
const pageSize = 10;
let total = 0;
let currentAction = null; // { alarmId, status }

/* ==================== 数据加载 ==================== */
function getFilters() {
  const startTime = document.getElementById('startTime').value;
  const endTime = document.getElementById('endTime').value;
  return {
    start_time: startTime ? startTime.replace('T', ' ') + ':00' : '',
    end_time: endTime ? endTime.replace('T', ' ') + ':00' : '',
    violation_type: document.getElementById('violationType').value,
    level: document.getElementById('level').value,
    status: document.getElementById('status').value,
  };
}

async function loadAlarms() {
  const f = getFilters();
  const params = new URLSearchParams({ page: currentPage, page_size: pageSize });
  if (f.start_time) params.set('start_time', f.start_time);
  if (f.end_time) params.set('end_time', f.end_time);
  if (f.violation_type) params.set('violation_type', f.violation_type);
  if (f.level) params.set('level', f.level);
  if (f.status) params.set('status', f.status);

  const res = await fetch('/api/alarms?' + params.toString());
  const data = await res.json();
  if (data.status !== 'ok') {
    document.getElementById('tbody').innerHTML =
      `<tr><td colspan="8" class="empty-state">${escapeHtml(data.message || '加载失败')}</td></tr>`;
    return;
  }
  total = data.total;
  renderTable(data.items);
  renderPagination();
}

function renderTable(items) {
  const tbody = document.getElementById('tbody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-state">暂无告警记录</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(function (a) {
    const levelTag = LEVEL_TAG[a.level] || '';
    const levelText = LEVEL_TEXT[a.level] || a.level;
    const statusTag = STATUS_TAG[a.status] || '';
    const confidence = a.confidence ? escapeHtml(a.confidence) : '—';
    const imgUrl = a.image_path ? encodeURI('/' + a.image_path) : '';
    const snapshot = imgUrl
      ? `<img src="${imgUrl}" style="width:48px;height:36px;object-fit:cover;cursor:pointer;border-radius:2px;" onclick="previewImage('${imgUrl}')" alt="抓拍图">`
      : '<span style="font-size:12px;color:#9ca3af;">—</span>';
    const ops = a.status === '待处理'
      ? '<div class="col-ops">' +
        `<button class="btn btn-sm btn-primary" onclick="openModal(${a.id}, '已处理')">标记已处理</button>` +
        `<button class="btn btn-sm" onclick="openModal(${a.id}, '已驳回')">驳回误报</button>` +
        '</div>'
      : '<span style="font-size:12px;color:#9ca3af;">—</span>';
    return '<tr>' +
      `<td>${escapeHtml(a.alarm_time || '')}</td>` +
      `<td>${escapeHtml(a.location || '—')}</td>` +
      `<td>${escapeHtml(a.violation_type || '')}</td>` +
      `<td><span class="tag ${levelTag}">${levelText}</span></td>` +
      `<td>${confidence}</td>` +
      `<td>${snapshot}</td>` +
      `<td><span class="tag ${statusTag}">${escapeHtml(a.status)}</span></td>` +
      `<td>${ops}</td>` +
      '</tr>';
  }).join('');
}

function renderPagination() {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  document.getElementById('pagination').innerHTML =
    `<span>共 ${total} 条</span>` +
    `<button class="page-btn" ${currentPage <= 1 ? 'disabled' : ''} onclick="goPage(${currentPage - 1})">上一页</button>` +
    `<button class="page-btn current">${currentPage} / ${totalPages}</button>` +
    `<button class="page-btn" ${currentPage >= totalPages ? 'disabled' : ''} onclick="goPage(${currentPage + 1})">下一页</button>`;
}

function goPage(p) {
  currentPage = p;
  loadAlarms();
}

/* ==================== 处理 / 驳回 ==================== */
function openModal(alarmId, status) {
  currentAction = { alarmId: alarmId, status: status };
  document.getElementById('modalTitle').textContent = status === '已处理' ? '标记已处理' : '驳回误报';
  document.getElementById('modalTip').textContent = status === '已处理' ? '请填写整改备注' : '请填写驳回原因（可选）';
  document.getElementById('modalRemark').value = '';
  document.getElementById('modalMask').classList.add('show');
}

function closeModal() {
  document.getElementById('modalMask').classList.remove('show');
  currentAction = null;
}

async function confirmAction() {
  if (!currentAction) return;
  const remark = document.getElementById('modalRemark').value.trim();
  const res = await fetch('/api/alarms/' + currentAction.alarmId, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status: currentAction.status, remark: remark }),
  });
  const data = await res.json();
  closeModal();
  if (data.status === 'ok') {
    loadAlarms();
  } else {
    alert(data.detail || '操作失败');
  }
}

/* ==================== 抓拍图预览 ==================== */
function previewImage(url) {
  document.getElementById('previewImg').src = url;
  document.getElementById('imageMask').classList.add('show');
}

/* ==================== 事件绑定 ==================== */
document.getElementById('btnSearch').onclick = function () { currentPage = 1; loadAlarms(); };
document.getElementById('btnReset').onclick = function () {
  document.getElementById('startTime').value = '';
  document.getElementById('endTime').value = '';
  document.getElementById('violationType').value = '';
  document.getElementById('level').value = '';
  document.getElementById('status').value = '';
  currentPage = 1;
  loadAlarms();
};
document.getElementById('btnCancel').onclick = closeModal;
document.getElementById('btnConfirm').onclick = confirmAction;
document.getElementById('modalMask').onclick = function (e) {
  if (e.target.id === 'modalMask') closeModal();
};
document.getElementById('btnCloseImage').onclick = function () {
  document.getElementById('imageMask').classList.remove('show');
};
document.getElementById('imageMask').onclick = function (e) {
  if (e.target.id === 'imageMask') document.getElementById('imageMask').classList.remove('show');
};

/* ==================== 初始化 ==================== */
loadAlarms();
