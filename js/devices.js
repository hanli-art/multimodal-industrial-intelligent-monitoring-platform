/* 设备管理页逻辑（R6）：车间分组树 + 设备 CRUD + 在线状态心跳模拟 */

/* ==================== 工具函数 ==================== */
function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* 心跳上报间隔 10s；后端离线判定阈值 60s（超过 3 次未上报即离线） */
const HEARTBEAT_INTERVAL_MS = 10000;

let currentWorkshop = '';    // '' 表示全部设备
let deviceCache = {};        // id -> 设备对象（当前列表）
let heartbeatIds = [];       // 需要模拟上报心跳的设备 id（基准在线）
let currentDevice = null;    // 正在编辑的设备，null 表示新增
let deleteTarget = null;
let hbEnabled = true;

/* ==================== 车间分组树 ==================== */
async function loadWorkshops() {
  const res = await Auth.apiFetch('/api/devices/workshops');
  const data = await res.json();
  if (data.status !== 'ok') return;

  let html = '<li class="tree-item' + (currentWorkshop === '' ? ' active' : '') + '" data-workshop="">' +
    '<span class="tree-name">全部设备</span>' +
    '<span class="tree-count">' + data.total + '</span></li>';

  data.items.forEach(function (w) {
    const active = currentWorkshop === w.workshop ? ' active' : '';
    html += '<li class="tree-item' + active + '" data-workshop="' + escapeHtml(w.workshop) + '">' +
      '<span class="tree-name">' + escapeHtml(w.workshop) + '</span>' +
      '<span class="tree-count">' + w.online_count + '/' + w.total + '</span></li>';
  });

  const list = document.getElementById('treeList');
  list.innerHTML = html;
  list.querySelectorAll('.tree-item').forEach(function (el) {
    el.onclick = function () {
      currentWorkshop = el.getAttribute('data-workshop') || '';
      loadWorkshops();
      loadDevices();
    };
  });
}

/* ==================== 设备列表 ==================== */
async function loadDevices() {
  const params = new URLSearchParams();
  if (currentWorkshop) params.set('workshop', currentWorkshop);
  const kw = document.getElementById('keyword').value.trim();
  if (kw) params.set('keyword', kw);
  const online = document.getElementById('onlineFilter').value;
  if (online !== '') params.set('online', online);

  const res = await Auth.apiFetch('/api/devices?' + params.toString());
  const data = await res.json();
  const tbody = document.getElementById('tbody');

  if (data.status !== 'ok') {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">' +
      escapeHtml(data.message || '加载失败') + '</td></tr>';
    return;
  }
  deviceCache = {};
  data.items.forEach(function (d) { deviceCache[d.id] = d; });
  renderTable(data.items);
}

function renderTable(items) {
  const tbody = document.getElementById('tbody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">暂无设备</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(function (d) {
    const onlineTag = d.online == 1
      ? '<span class="status-online"><i class="dot dot-online"></i>在线</span>'
      : '<span class="status-offline"><i class="dot dot-offline"></i>离线</span>';
    const aiTag = d.ai_enabled == 1
      ? '<span class="tag tag-ai-on">开启</span>'
      : '<span class="tag tag-ai-off">关闭</span>';
    return '<tr>' +
      '<td>' + escapeHtml(d.name) + '</td>' +
      '<td class="dim">' + escapeHtml(d.code) + '</td>' +
      '<td>' + escapeHtml(d.type || '—') + '</td>' +
      '<td>' + escapeHtml(d.workshop || '未分组') + '</td>' +
      '<td>' + escapeHtml(d.location || '—') + '</td>' +
      '<td class="dim">' + escapeHtml(d.ip || '—') + '</td>' +
      '<td>' + onlineTag + '</td>' +
      '<td>' + aiTag + '</td>' +
      '<td><div class="col-ops">' +
        '<button class="btn btn-sm" onclick="openForm(' + d.id + ')">编辑</button>' +
        '<button class="btn btn-sm btn-danger" onclick="openDelete(' + d.id + ')">删除</button>' +
      '</div></td>' +
      '</tr>';
  }).join('');
}

/* ==================== 新增 / 编辑 ==================== */
function openForm(id) {
  currentDevice = id ? deviceCache[id] : null;
  const d = currentDevice || {};
  document.getElementById('formTitle').textContent = currentDevice ? '编辑设备' : '新增设备';
  document.getElementById('fCode').value = d.code || '';
  document.getElementById('fName').value = d.name || '';
  document.getElementById('fType').value = d.type || '';
  document.getElementById('fWorkshop').value = d.workshop || '';
  document.getElementById('fLocation').value = d.location || '';
  document.getElementById('fIp').value = d.ip || '';
  document.getElementById('fOnline').value = String(d.online_status != null ? d.online_status : 1);
  document.getElementById('fAi').value = String(d.ai_enabled != null ? d.ai_enabled : 1);
  document.getElementById('formMask').classList.add('show');
}

function closeForm() {
  document.getElementById('formMask').classList.remove('show');
  currentDevice = null;
}

async function saveForm() {
  const payload = {
    code: document.getElementById('fCode').value.trim(),
    name: document.getElementById('fName').value.trim(),
    type: document.getElementById('fType').value.trim(),
    workshop: document.getElementById('fWorkshop').value.trim(),
    location: document.getElementById('fLocation').value.trim(),
    ip: document.getElementById('fIp').value.trim(),
    online_status: parseInt(document.getElementById('fOnline').value, 10),
    ai_enabled: parseInt(document.getElementById('fAi').value, 10),
  };
  if (!payload.code) { alert('请填写设备编号'); return; }
  if (!payload.name) { alert('请填写设备名称'); return; }

  const editing = currentDevice ? currentDevice.id : null;
  const res = await Auth.apiFetch(editing ? '/api/devices/' + editing : '/api/devices', {
    method: editing ? 'PUT' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (data.status === 'ok') {
    closeForm();
    await tick();
  } else {
    alert(data.detail || '保存失败');
  }
}

/* ==================== 删除 ==================== */
function openDelete(id) {
  const d = deviceCache[id];
  deleteTarget = id;
  document.getElementById('delText').textContent =
    '确定删除设备「' + (d ? d.name : '') + '」吗？删除后不可恢复。';
  document.getElementById('delMask').classList.add('show');
}

function closeDelete() {
  document.getElementById('delMask').classList.remove('show');
  deleteTarget = null;
}

async function confirmDelete() {
  if (!deleteTarget) return;
  const res = await Auth.apiFetch('/api/devices/' + deleteTarget, { method: 'DELETE' });
  const data = await res.json();
  closeDelete();
  if (data.status === 'ok') {
    await tick();
  } else {
    alert(data.detail || '删除失败');
  }
}

/* ==================== 心跳模拟 ==================== */
async function refreshHeartbeatIds() {
  try {
    // 取未过滤全量列表，找出「基准在线」的设备去上报心跳
    const res = await Auth.apiFetch('/api/devices');
    const data = await res.json();
    if (data.status !== 'ok') return;
    heartbeatIds = data.items.filter(function (d) { return d.online_status == 1; })
      .map(function (d) { return d.id; });
  } catch (e) {
    /* 忽略单次失败，下个周期重试 */
  }
}

async function sendHeartbeats() {
  await Promise.all(heartbeatIds.map(function (id) {
    return Auth.apiFetch('/api/devices/' + id + '/heartbeat', { method: 'POST' }).catch(function () {});
  }));
}

async function tick() {
  if (hbEnabled) {
    await refreshHeartbeatIds();
    await sendHeartbeats();
  }
  await loadWorkshops();
  await loadDevices();
}

/* ==================== 事件绑定 ==================== */
document.getElementById('btnSearch').onclick = loadDevices;
document.getElementById('btnReset').onclick = function () {
  document.getElementById('keyword').value = '';
  document.getElementById('onlineFilter').value = '';
  loadDevices();
};
document.getElementById('btnAdd').onclick = function () { openForm(null); };
document.getElementById('btnFormCancel').onclick = closeForm;
document.getElementById('btnFormSave').onclick = saveForm;
document.getElementById('btnDelCancel').onclick = closeDelete;
document.getElementById('btnDelConfirm').onclick = confirmDelete;
document.getElementById('formMask').onclick = function (e) {
  if (e.target.id === 'formMask') closeForm();
};
document.getElementById('delMask').onclick = function (e) {
  if (e.target.id === 'delMask') closeDelete();
};
document.getElementById('hbSwitch').onchange = function (e) {
  hbEnabled = e.target.checked;
  document.getElementById('hbTip').textContent = hbEnabled
    ? '每 10 秒为「基准在线」的设备上报心跳；停止后 60 秒内自动转为离线。'
    : '心跳模拟已停止，60 秒内设备将陆续转为离线。';
};

/* ==================== 初始化 ==================== */
async function init() {
  // 先上报一轮心跳再渲染，避免首屏把在线设备误显示为离线
  await tick();
  setInterval(tick, HEARTBEAT_INTERVAL_MS);
}

init();