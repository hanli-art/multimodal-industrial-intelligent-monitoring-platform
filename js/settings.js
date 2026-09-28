/* 系统配置页逻辑（R9）：读写 config 表配置，保存后检测链路即时生效 */

/* 页面上所有配置控件都带 data-key，键名与后端 CONFIG_DEFAULTS 一致 */
function setMsg(text, isErr) {
  const el = document.getElementById('settingsMsg');
  el.textContent = text || '';
  el.className = 'settings-msg' + (isErr ? ' err' : '');
}

function syncConfLabel() {
  const v = parseFloat(document.getElementById('cfgYoloConf').value);
  document.getElementById('cfgYoloConfVal').textContent = isNaN(v) ? '—' : v.toFixed(2);
}

function fillForm(cfg) {
  document.querySelectorAll('[data-key]').forEach(function (el) {
    const value = cfg[el.getAttribute('data-key')];
    if (el.type === 'checkbox') {
      el.checked = String(value) === '1';
    } else {
      el.value = value != null ? String(value) : '';
    }
  });
  syncConfLabel();
}

function collectForm() {
  const items = {};
  document.querySelectorAll('[data-key]').forEach(function (el) {
    items[el.getAttribute('data-key')] = el.type === 'checkbox'
      ? (el.checked ? '1' : '0')
      : String(el.value).trim();
  });
  return items;
}

async function loadConfig() {
  const res = await Auth.apiFetch('/api/config');
  const data = await res.json();
  if (data.status !== 'ok') {
    setMsg('配置读取失败：' + (data.message || ''), true);
    return;
  }
  fillForm(data.config || {});
  setMsg('');
}

async function saveConfig() {
  const items = collectForm();

  const conf = parseFloat(items.yolo_conf);
  if (isNaN(conf) || conf < 0.3 || conf > 0.9) {
    setMsg('置信度阈值需在 0.30 ~ 0.90 之间', true);
    return;
  }
  const debounce = parseInt(items.alarm_debounce, 10);
  if (isNaN(debounce) || debounce < 1 || debounce > 3600) {
    setMsg('防抖时长需为 1 ~ 3600 的整数秒', true);
    return;
  }

  const btn = document.getElementById('btnSave');
  btn.disabled = true;
  try {
    const res = await Auth.apiFetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: items }),
    });
    const data = await res.json();
    if (data.status !== 'ok') {
      setMsg(data.detail || data.message || '保存失败', true);
      return;
    }
    // 回填后端归一化后的结果，避免阈值小数位与库内不一致
    fillForm(data.config || items);
    setMsg(data.changed > 0
      ? '已保存 ' + data.changed + ' 项配置，检测链路已即时生效'
      : '配置无变化');
  } catch (e) {
    setMsg('保存失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('cfgYoloConf').addEventListener('input', syncConfLabel);
document.getElementById('btnSave').onclick = saveConfig;
document.getElementById('btnReload').onclick = loadConfig;

loadConfig();