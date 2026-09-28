/* ============================================
   R5 实时告警推送与提醒
   - /ws/notify 订阅新告警广播
   - 右下角弹窗（抓拍图 + 违规类型 + 等级色），二/三级 5 秒自动消失，一级手动关闭
   - speechSynthesis 语音播报
   - 顶部滚动告警条（最近 10 条未处理，一级置顶）
   - 静音开关
   ============================================ */
(function () {

  var LEVEL_TEXT  = { 1: '一级', 2: '二级', 3: '三级' };
  var LEVEL_COLOR = { 1: '#ff3366', 2: '#f59e0b', 3: '#00f0ff' };
  var AUTO_CLOSE_MS = 5000;   // 二/三级告警自动消失时长

  var notifyWs = null;
  var muted = false;
  var shouldReconnect = true;

  /* ==================== 工具 ==================== */
  function esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function imgUrlOf(alarm) {
    return alarm.image_path ? encodeURI('/' + alarm.image_path) : '';
  }

  /* ==================== 语音播报 ==================== */
  function speak(type) {
    if (muted) return;
    if (!('speechSynthesis' in window)) return;
    try {
      // 先清掉上一条，避免多条告警排队积压
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance('注意，检测到' + type + '违规');
      u.lang = 'zh-CN';
      u.rate = 1;
      window.speechSynthesis.speak(u);
    } catch (e) { /* 浏览器不支持或未授权时静默跳过 */ }
  }

  /* ==================== 右下角弹窗 ==================== */
  function showPopup(alarm) {
    var stack = document.getElementById('alarmPopStack');
    if (!stack) return;

    var level = Number(alarm.level) || 3;
    var url = imgUrlOf(alarm);

    var pop = document.createElement('div');
    pop.className = 'alarm-pop level-' + level;
    pop.innerHTML =
      '<div class="alarm-pop-header">' +
        '<span>' + esc(LEVEL_TEXT[level] || level + '级') + '告警</span>' +
        '<button class="alarm-pop-close" title="关闭">✕</button>' +
      '</div>' +
      '<div class="alarm-pop-body">' +
        (url ? '<img src="' + url + '" alt="抓拍图">' : '') +
        '<div class="alarm-pop-info">' +
          '<div class="alarm-pop-type">' + esc(alarm.violation_type) + '</div>' +
          '<div class="alarm-pop-meta">' +
            esc(String(alarm.alarm_time || '').replace('T', ' ')) + '<br>' +
            (alarm.location ? '点位：' + esc(alarm.location) : '点位：未配置') +
          '</div>' +
        '</div>' +
      '</div>';

    var close = function () {
      if (pop._timer) clearTimeout(pop._timer);
      if (pop.parentNode) pop.parentNode.removeChild(pop);
    };
    pop.querySelector('.alarm-pop-close').onclick = close;
    stack.appendChild(pop);

    // 一级告警必须手动关闭，其余自动消失
    if (level !== 1) pop._timer = setTimeout(close, AUTO_CLOSE_MS);

    // 弹窗最多堆 5 条，超出移除最早的
    while (stack.children.length > 5) stack.removeChild(stack.firstElementChild);
  }

  /* ==================== 顶部滚动告警条 ==================== */
  function renderTicker(items) {
    var bar = document.getElementById('alarmTicker');
    if (!bar) return;

    if (!items.length) {
      bar.innerHTML = '<div class="ticker-empty">暂无未处理告警</div>';
      return;
    }

    var html = items.map(function (a) {
      var level = Number(a.level) || 3;
      return '<span class="ticker-item level-' + level + '">' +
        '<span class="tt-time">' + esc(String(a.alarm_time || '').replace('T', ' ').slice(5)) + '</span>' +
        '<span class="tt-type">[' + esc(LEVEL_TEXT[level] || '') + '] ' + esc(a.violation_type) + '</span>' +
        '<span class="tt-loc">' + esc(a.location || '未配置点位') + '</span>' +
      '</span>';
    }).join('');

    bar.innerHTML = '<div class="ticker-track"><div>' + html + '</div></div>';

    // 内容超宽时复制一份实现无缝滚动
    var track = bar.querySelector('.ticker-track');
    var inner = track.firstElementChild;
    if (inner.scrollWidth > track.clientWidth) {
      inner.innerHTML += html;
      // 速度固定约 60px/s，长度不同时滚动时长随之变化
      inner.style.animationDuration = Math.max(10, Math.round(inner.scrollWidth / 2 / 60)) + 's';
      track.classList.add('marquee');
    }
  }

  function loadRecent() {
    fetch('/api/alarms/recent?limit=10')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'ok') renderTicker(d.items || []);
      })
      .catch(function () { /* 拉取失败保持现有内容 */ });
  }

  /* ==================== 静音开关 ==================== */
  function setMuted(v) {
    muted = v;
    var btn = document.getElementById('btnMute');
    if (btn) {
      btn.textContent = muted ? '🔕 已静音' : '🔔 语音';
      btn.classList.toggle('muted', muted);
    }
    if (muted && 'speechSynthesis' in window) window.speechSynthesis.cancel();
  }

  function bindMute() {
    var btn = document.getElementById('btnMute');
    if (!btn) return;
    btn.onclick = function () { setMuted(!muted); };
  }

  /* ==================== 广播通道 ==================== */
  function handleMessage(data) {
    if (!data || data.status !== 'ok' || data.type !== 'alarm') return;
    (data.alarms || []).forEach(function (a) {
      showPopup(a);
      speak(a.violation_type);
    });
    loadRecent();   // 告警条刷新为最新状态
  }

  function initNotifyWebSocket() {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    notifyWs = new WebSocket(proto + location.host + '/ws/notify');

    notifyWs.onopen = function () {
      console.log('[notify] 告警推送通道已连接');
    };

    notifyWs.onmessage = function (event) {
      var data;
      try { data = JSON.parse(event.data); } catch (e) { return; }
      handleMessage(data);
    };

    notifyWs.onclose = function () {
      if (!shouldReconnect) return;
      setTimeout(initNotifyWebSocket, 3000);
    };
  }

  /* ==================== 启动 ==================== */
  window.addEventListener('DOMContentLoaded', function () {
    bindMute();
    loadRecent();
    initNotifyWebSocket();
  });

  window.addEventListener('beforeunload', function () {
    shouldReconnect = false;
    if (notifyWs) { try { notifyWs.close(); } catch (e) {} }
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  });

})();