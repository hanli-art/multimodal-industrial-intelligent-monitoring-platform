/* ==================== 全局变量 ==================== */
let ws = null, wsReady = false;
let qwenWs = null, qwenReady = false;
let qwenAnalyzing = false, qwenAutoOn = false, qwenAutoTimer = null;

let videoEl = null;
let rawCanvas = null, rawCtx = null;
let sendCanvas = null, sendCtx = null;
let canvas = null, ctx = null;

let cameraStream = null, rafId = null;
let fpsCounter = 0, fpsStamp = performance.now();

let isPlaying = false, detecting = false, detectTimer = null;
let realDetections = [];
let lastPersonCnt = 0, lastVehicleCnt = 0;
let detectInflight = false, detectSentAt = 0;   // 背压标记：上一帧结果未返回时不再发新帧

const DETECT_INTERVAL   = 200;
const DETECT_TIMEOUT    = 5000;   // 单帧结果最长等待，超时视为丢失并恢复发送
const QWEN_INTERVAL     = 5000;
const SEND_MAX_WIDTH    = 960;
const SEND_JPEG_QUALITY = 0.75;

/* ==================== 工具函数 ==================== */
function escapeHtml(str) {
  return String(str == null ? '' : str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function addLog(text, level = 'info') {
  const container = document.getElementById('logContainer');
  if (!container) return;
  const ph = document.getElementById('logPlaceholder');
  if (ph) ph.style.display = 'none';

  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const item = document.createElement('div');
  item.className = 'log-item ' + level;
  item.innerHTML = `<span class="log-time">[${time}]</span><span class="log-text">${escapeHtml(text)}</span>`;
  container.prepend(item);

  const counter = document.getElementById('logCount');
  const items = container.querySelectorAll('.log-item');
  if (counter) counter.textContent = items.length;
  if (items.length > 200) items[items.length - 1].remove();
}

function formatTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  return String(Math.floor(sec/60)).padStart(2,'0') + ':' + String(sec%60).padStart(2,'0');
}

function grabFrame() {
  if (!rawCanvas || !rawCanvas.width) return null;
  const srcW = rawCanvas.width, srcH = rawCanvas.height;
  let w = srcW, h = srcH;
  if (w > SEND_MAX_WIDTH) { h = Math.round(srcH * SEND_MAX_WIDTH / srcW); w = SEND_MAX_WIDTH; }
  if (sendCanvas.width !== w || sendCanvas.height !== h) {
    sendCanvas.width = w; sendCanvas.height = h;
  }
  sendCtx.drawImage(rawCanvas, 0, 0, w, h);
  return sendCanvas.toDataURL('image/jpeg', SEND_JPEG_QUALITY);
}

/* ==================== YOLO 通道 ==================== */
function initWebSocket() {
  if (ws) { try { ws.close(); } catch(e){} }
  const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  ws = new WebSocket(proto + location.host + '/ws/detect');

  ws.onopen = () => {
    wsReady = true;
    addLog('YOLO 检测通道连接成功', 'ok');
    const b = document.getElementById('wsBadge');
    if (b) { b.textContent = 'ONLINE'; b.classList.add('online'); }
  };

  ws.onmessage = (event) => {
    detectInflight = false;   // 成功或失败都释放，否则丢帧会永久停发
    let data; try { data = JSON.parse(event.data); } catch(e) { return; }
    if (data.status !== 'ok') return;
    realDetections = data.detections || [];
    updateStats(realDetections);
    checkAlarm(realDetections);
  };

  ws.onclose = () => {
    wsReady = false;
    detectInflight = false;
    const b = document.getElementById('wsBadge');
    if (b) { b.textContent = 'OFFLINE'; b.classList.remove('online'); }
    setTimeout(initWebSocket, 3000);
  };
}

function updateStats(dets) {
  const v = dets.filter(d => ['car','truck','bus','motorcycle'].includes(d.class_name)).length;
  const p = dets.filter(d => d.class_name === 'person').length;
  const vEl = document.getElementById('vehicleCount');
  const pEl = document.getElementById('personCount');
  if (vEl) vEl.textContent = v;
  if (pEl) pEl.textContent = p;
}

function checkAlarm(dets) {
  const p = dets.filter(d => d.class_name === 'person').length;
  const v = dets.filter(d => ['car','truck','bus'].includes(d.class_name)).length;
  if (p > 0 && lastPersonCnt === 0) addLog(`检测到行人进入作业区域（${p} 人）`, 'danger');
  if (v - lastVehicleCnt >= 3) addLog(`车辆突增（${lastVehicleCnt} → ${v}）`, 'warn');
  lastPersonCnt = p; lastVehicleCnt = v;
}

function startDetectLoop() {
  if (detectTimer) return;
  detectTimer = setInterval(() => {
    if (!detecting || !wsReady || !isPlaying) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    // 背压：上一帧结果未返回就丢弃本帧，避免帧堆积导致画框越来越滞后于画面
    if (detectInflight) {
      if (Date.now() - detectSentAt < DETECT_TIMEOUT) return;
      detectInflight = false;   // 结果超时丢失，自救恢复发送
    }
    const frame = grabFrame();
    if (frame) {
      try {
        ws.send(JSON.stringify({ base64_image: frame }));
        detectInflight = true;
        detectSentAt = Date.now();
      } catch(e) { detectInflight = false; }
    }
  }, DETECT_INTERVAL);
}

function stopDetectLoop() {
  if (detectTimer) { clearInterval(detectTimer); detectTimer = null; }
}

/* ==================== 千问通道 ==================== */
function initQwenWebSocket() {
  if (qwenWs && (qwenWs.readyState === WebSocket.OPEN || qwenWs.readyState === WebSocket.CONNECTING)) return;
  const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  qwenWs = new WebSocket(proto + location.host + '/ws/qwen');

  qwenWs.onopen = () => {
    qwenReady = true;
    setQwenBadge('READY', 'ready');
    addLog('通义千问分析通道已连接', 'ok');
  };

  qwenWs.onmessage = (event) => {
    let data; try { data = JSON.parse(event.data); } catch(e) { return; }
    qwenAnalyzing = false;
    if (data.status === 'blocked') {
      setQwenBadge('BLOCKED', 'offline');
      addLog('图片未通过安全检查：' + data.message, 'danger');
      renderQwenError('图片存在安全隐患，已阻断');
      return;
    }
    if (data.status !== 'ok') {
      setQwenBadge('READY', 'ready');
      addLog('千问分析失败：' + (data.message || '未知错误'), 'danger');
      renderQwenError(data.message || '分析失败');
      return;
    }
    setQwenBadge('READY', 'ready');
    renderQwenResult(data);
  };

  qwenWs.onclose = () => {
    qwenReady = false; qwenAnalyzing = false;
    setQwenBadge('OFFLINE', 'offline');
    setTimeout(initQwenWebSocket, 3000);
  };
}

function requestQwenAnalysis(manual = false) {
  if (!qwenReady || !qwenWs || qwenWs.readyState !== WebSocket.OPEN) {
    if (manual) addLog('千问通道尚未就绪', 'warn');
    return;
  }
  if (qwenAnalyzing) { if (manual) addLog('上一轮分析还在进行中', 'warn'); return; }
  if (!isPlaying) { if (manual) addLog('请先播放视频', 'warn'); return; }

  const frame = grabFrame();
  if (!frame) { if (manual) addLog('当前没有可用画面', 'warn'); return; }

  qwenAnalyzing = true;
  setQwenBadge('ANALYZING', 'analyzing');

  const ctxData = realDetections.map(d => ({ class_name: d.class_name, conf: d.conf }));
  try {
    qwenWs.send(JSON.stringify({
      base64_image: frame,
      detections: ctxData,
      timestamp: Date.now()
    }));
    addLog('已提交千问场景分析请求…', 'info');
  } catch(e) {
    qwenAnalyzing = false;
    setQwenBadge('READY', 'ready');
    addLog('千问请求发送失败：' + e.message, 'danger');
  }
}

function renderQwenResult(data) {
  const body = document.getElementById('qwenBody');
  if (!body) return;
  const risk = data.risk_level || '低';
  const riskClass = { '高':'high','中':'mid','低':'low' }[risk] || 'low';
  const viosCn = data.violations_cn || [];
  const suggestions = data.suggestions || [];

  body.innerHTML = `
    <div class="qwen-meta">
      <span class="qwen-risk ${riskClass}">风险等级：${escapeHtml(risk)}</span>
      <span class="qwen-time">${escapeHtml(data.timestamp || '')}</span>
      <span class="qwen-model">${escapeHtml(data.model || '未知模型')}</span>
    </div>
    <div class="qwen-summary">${escapeHtml(data.summary || '（无结论）')}</div>
    <div class="qwen-sec-title">违规检测</div>
    ${viosCn.length
      ? `<ul class="qwen-list danger">${viosCn.map(v => `<li>⚠ ${escapeHtml(v)}</li>`).join('')}</ul>`
      : `<div class="qwen-safe">✓ 未检测到违规行为</div>`}
    ${suggestions.length
      ? `<div class="qwen-sec-title">处置建议</div>
         <ul class="qwen-list adv">${suggestions.map(s => `<li>${escapeHtml(s)}</li>`).join('')}</ul>`
      : ''}
  `;
  if (risk === '高') addLog('⚠ 千问判定【高风险】：' + viosCn.join('、'), 'danger');
  else if (risk === '中') addLog('千问判定【中风险】：' + viosCn.join('、'), 'warn');
  else addLog('千问安全审核：未检测到违规行为', 'ok');
}

function renderQwenError(msg) {
  const body = document.getElementById('qwenBody');
  if (body) body.innerHTML = `<div class="qwen-error">分析失败：${escapeHtml(msg)}</div>`;
}

function setQwenBadge(text, cls) {
  const b = document.getElementById('qwenBadge');
  if (b) { b.textContent = text; b.className = 'qwen-badge ' + (cls || ''); }
}

/* ==================== UI 注入 ==================== */
function injectQwenStyle() {
  if (document.getElementById('qwenStyle')) return;
  const s = document.createElement('style');
  s.id = 'qwenStyle';
  s.textContent = `
    .qwen-panel{margin-top:12px;border:1px solid rgba(0,212,255,.25);border-radius:8px;
      background:rgba(0,20,35,.55);display:flex;flex-direction:column;
      min-height:150px;max-height:320px;overflow:hidden;}
    .qwen-header{display:flex;align-items:center;justify-content:space-between;
      padding:8px 12px;border-bottom:1px solid rgba(0,212,255,.18);background:rgba(0,212,255,.06);}
    .qwen-title{font-size:13px;letter-spacing:1px;color:#7fe3ff;}
    .qwen-badge{font-size:11px;padding:2px 8px;border-radius:10px;letter-spacing:1px;
      border:1px solid rgba(255,255,255,.25);color:#9aa7b4;}
    .qwen-badge.ready{color:#3ddc97;border-color:rgba(61,220,151,.5);}
    .qwen-badge.analyzing{color:#ffb300;border-color:rgba(255,179,0,.5);animation:qwenPulse 1s infinite;}
    .qwen-badge.offline{color:#ff4d4f;border-color:rgba(255,77,79,.5);}
    @keyframes qwenPulse{0%,100%{opacity:1}50%{opacity:.35}}
    .qwen-body{padding:10px 12px;overflow-y:auto;font-size:12.5px;line-height:1.7;color:#c8d6e5;}
    .qwen-empty{color:#5d6b7a;text-align:center;padding:26px 0;}
    .qwen-error{color:#ff7a7a;padding:16px 0;text-align:center;}
    .qwen-meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px;}
    .qwen-risk{padding:2px 10px;border-radius:10px;font-size:11.5px;}
    .qwen-risk.high{background:rgba(255,77,79,.18);color:#ff6b6b;border:1px solid rgba(255,77,79,.5);}
    .qwen-risk.mid{background:rgba(255,179,0,.15);color:#ffb300;border:1px solid rgba(255,179,0,.45);}
    .qwen-risk.low{background:rgba(61,220,151,.15);color:#3ddc97;border:1px solid rgba(61,220,151,.45);}
    .qwen-time,.qwen-model{font-size:11px;color:#6b7a8a;}
    .qwen-summary{color:#e6f2ff;margin-bottom:8px;}
    .qwen-sec-title{color:#7fe3ff;font-size:12px;margin:8px 0 4px;
      border-left:2px solid #00d4ff;padding-left:6px;}
    .qwen-list{margin:0;padding-left:18px;}
    .qwen-list li{margin-bottom:3px;}
    .qwen-list.danger li{color:#ff7a7a;}
    .qwen-list.adv li{color:#9fe8c6;}
    .qwen-safe{color:#3ddc97;padding:4px 0;}
    .qwen-auto{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:#8fa3b8;cursor:pointer;}
    .qwen-auto input{accent-color:#00d4ff;cursor:pointer;}
    #btnQwen{border-color:rgba(0,212,255,.55);color:#7fe3ff;}
  `;
  document.head.appendChild(s);
}

function buildQwenPanel() {
  const grid = document.querySelector('.status-grid');
  if (!grid || document.getElementById('qwenPanel')) return;
  const p = document.createElement('div');
  p.className = 'qwen-panel';
  p.id = 'qwenPanel';
  p.innerHTML = `
    <div class="qwen-header">
      <span class="qwen-title">⬢ 通义千问 · 场景语义分析</span>
      <span class="qwen-badge offline" id="qwenBadge">CONNECTING</span>
    </div>
    <div class="qwen-body" id="qwenBody">
      <div class="qwen-empty">等待分析… 点击「AI 场景分析」或勾选自动分析</div>
    </div>`;
  grid.insertAdjacentElement('afterend', p);
}

function buildQwenControls() {
  const controls = document.querySelector('.controls');
  if (!controls || document.getElementById('btnQwen')) return;
  const btn = document.createElement('button');
  btn.id = 'btnQwen';
  btn.textContent = 'AI 场景分析';
  const label = document.createElement('label');
  label.className = 'qwen-auto';
  label.innerHTML = '<input type="checkbox" id="qwenAuto"> 自动分析';
  controls.appendChild(btn);
  controls.appendChild(label);

  btn.addEventListener('click', () => requestQwenAnalysis(true));
  label.querySelector('#qwenAuto').addEventListener('change', (e) => {
    qwenAutoOn = e.target.checked;
    if (qwenAutoOn) {
      if (qwenAutoTimer) clearInterval(qwenAutoTimer);
      qwenAutoTimer = setInterval(() => { if (isPlaying && !qwenAnalyzing) requestQwenAnalysis(false); }, QWEN_INTERVAL);
      addLog(`已开启自动分析（每 ${QWEN_INTERVAL/1000} 秒一次）`, 'ok');
      requestQwenAnalysis(false);
    } else {
      if (qwenAutoTimer) { clearInterval(qwenAutoTimer); qwenAutoTimer = null; }
      addLog('已关闭自动分析', 'info');
    }
  });
}

/* ==================== 视频渲染 ==================== */
const CLASS_COLORS = {
  person: '#ff4d4f', car: '#00d4ff', truck: '#ffb300',
  bus: '#9c6bff', motorcycle: '#3ddc97', default: '#3ddc97'
};

function drawDetections() {
  if (!ctx || !canvas.width) return;
  const scale = Math.max(2, canvas.width / 700);
  const fontSize = Math.max(13, canvas.width / 65);

  realDetections.forEach(d => {
    const color = CLASS_COLORS[d.class_name] || CLASS_COLORS.default;
    const w = d.x2 - d.x1, h = d.y2 - d.y1;
    ctx.strokeStyle = color; ctx.lineWidth = scale;
    ctx.strokeRect(d.x1, d.y1, w, h);
    const c = Math.min(20, w*0.25, h*0.25);
    ctx.lineWidth = scale * 1.6;
    ctx.beginPath();
    ctx.moveTo(d.x1, d.y1+c); ctx.lineTo(d.x1, d.y1); ctx.lineTo(d.x1+c, d.y1);
    ctx.moveTo(d.x2-c, d.y1); ctx.lineTo(d.x2, d.y1); ctx.lineTo(d.x2, d.y1+c);
    ctx.moveTo(d.x1, d.y2-c); ctx.lineTo(d.x1, d.y2); ctx.lineTo(d.x1+c, d.y2);
    ctx.moveTo(d.x2-c, d.y2); ctx.lineTo(d.x2, d.y2); ctx.lineTo(d.x2, d.y2-c);
    ctx.stroke();

    const label = `${d.class_name} ${Number(d.conf).toFixed(2)}`;
    ctx.font = `${fontSize}px "Consolas", monospace`;
    const tw = ctx.measureText(label).width;
    ctx.fillStyle = color;
    ctx.fillRect(d.x1, Math.max(0, d.y1 - fontSize - 6), tw + 12, fontSize + 6);
    ctx.fillStyle = '#001018';
    ctx.fillText(label, d.x1 + 6, Math.max(fontSize - 1, d.y1 - 6));
  });
}

function renderLoop() {
  rafId = requestAnimationFrame(renderLoop);
  if (!videoEl || videoEl.readyState < 2) return;
  const w = videoEl.videoWidth, h = videoEl.videoHeight;
  if (!w || !h) return;

  if (rawCanvas.width !== w || rawCanvas.height !== h) {
    rawCanvas.width = w; rawCanvas.height = h;
    canvas.width = w; canvas.height = h;
  }
  rawCtx.drawImage(videoEl, 0, 0, w, h);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(rawCanvas, 0, 0);
  drawDetections();

  fpsCounter++;
  const now = performance.now();
  if (now - fpsStamp >= 1000) {
    const el = document.getElementById('sysFps');
    if (el) el.textContent = fpsCounter + ' FPS';
    fpsCounter = 0; fpsStamp = now;
  }

  if (videoEl.duration && isFinite(videoEl.duration)) {
    const bar = document.getElementById('progressBar');
    const label = document.getElementById('timeLabel');
    if (bar && !bar.dataset.dragging) bar.value = Math.round((videoEl.currentTime / videoEl.duration) * 1000);
    if (label) label.textContent = formatTime(videoEl.currentTime);
  }
}

/* ==================== 数据源与播放 ==================== */
function initDom() {
  canvas = document.getElementById('videoCanvas');
  if (canvas) ctx = canvas.getContext('2d');

  videoEl = document.createElement('video');
  videoEl.playsInline = true;
  videoEl.preload = 'auto';
  videoEl.style.display = 'none';
  document.body.appendChild(videoEl);

  rawCanvas = document.createElement('canvas');
  rawCtx = rawCanvas.getContext('2d');
  sendCanvas = document.createElement('canvas');
  sendCtx = sendCanvas.getContext('2d');

  videoEl.addEventListener('ended', () => { isPlaying = false; updatePlayButton(); });
}

function updatePlayButton() {
  const btn = document.getElementById('playPauseBtn');
  if (btn) btn.textContent = isPlaying ? '⏸ 暂停' : '▶ 播放';
}

async function loadVideoFile(file) {
  await stopAll();
  videoEl.srcObject = null;
  videoEl.src = URL.createObjectURL(file);
  videoEl.loop = true;

  videoEl.onloadedmetadata = () => {
    const playBtn = document.getElementById('playPauseBtn');
    const detectBtn = document.getElementById('btnStartDetected');
    if (playBtn) playBtn.disabled = false;
    if (detectBtn) detectBtn.disabled = false;
    videoEl.play().then(() => {
      isPlaying = true;
      updatePlayButton();
      addLog(`已加载视频：${file.name}`, 'ok');
    }).catch(() => addLog('自动播放被拦截，请手动点击播放', 'warn'));
  };
}

async function startCamera() {
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } } });
    videoEl.srcObject = cameraStream;
    videoEl.loop = false;
    await videoEl.play();
    isPlaying = true;
    updatePlayButton();
    addLog('摄像头已开启', 'ok');
    const detectBtn = document.getElementById('btnStartDetected');
    if (detectBtn) detectBtn.disabled = false;
  } catch(e) { addLog('摄像头开启失败：' + e.message, 'danger'); }
}

async function stopAll() {
  isPlaying = false; detecting = false;
  stopDetectLoop();
  detectInflight = false;
  realDetections = [];
  updateStats([]);
  updatePlayButton();
  const detectBtn = document.getElementById('btnStartDetected');
  if (detectBtn) detectBtn.textContent = '开始检测';
  const status = document.getElementById('detectStatus');
  if (status) status.textContent = 'STANDBY';
  if (cameraStream) { cameraStream.getTracks().forEach(t => t.stop()); cameraStream = null; }
  if (videoEl) { try { videoEl.pause(); } catch(e){} }
}

function togglePlay() {
  if (!videoEl || !videoEl.src) return;
  if (videoEl.paused) { videoEl.play(); isPlaying = true; }
  else { videoEl.pause(); isPlaying = false; }
  updatePlayButton();
}

function toggleDetect() {
  const btn = document.getElementById('btnStartDetected');
  const status = document.getElementById('detectStatus');
  detecting = !detecting;
  if (detecting) {
    startDetectLoop();
    if (btn) btn.textContent = '停止检测';
    if (status) status.textContent = 'DETECTING';
    addLog('已开启实时目标检测', 'ok');
  } else {
    stopDetectLoop();
    detectInflight = false;
    realDetections = []; updateStats([]);
    if (btn) btn.textContent = '开始检测';
    if (status) status.textContent = 'STANDBY';
    addLog('已停止实时目标检测', 'info');
  }
}

/* ==================== 事件绑定 ==================== */
function bindEvents() {
  const sourceType = document.getElementById('sourceType');
  const videoFile  = document.getElementById('videoFile');
  const playBtn    = document.getElementById('playPauseBtn');
  const detectBtn  = document.getElementById('btnStartDetected');
  const progress   = document.getElementById('progressBar');

  if (sourceType) {
    sourceType.addEventListener('change', async () => {
      const isFile = sourceType.value === 'file';
      const fileBox = document.querySelector('.file-select');
      if (fileBox) fileBox.style.display = isFile ? '' : 'none';
      const hint = document.getElementById('sourceHint');
      if (hint) hint.textContent = isFile ? '当前：本地视频' : '当前：摄像头实时流';
      await stopAll();
      if (isFile) {
        if (playBtn) playBtn.disabled = true;
        if (detectBtn) detectBtn.disabled = true;
      } else {
        if (playBtn) playBtn.disabled = true;
        if (detectBtn) detectBtn.disabled = true;
        await startCamera();
      }
    });
  }

  if (videoFile) {
    videoFile.addEventListener('change', (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      const nameEl = document.getElementById('fileName');
      if (nameEl) nameEl.textContent = file.name;
      loadVideoFile(file);
    });
  }

  if (playBtn)   playBtn.addEventListener('click', togglePlay);
  if (detectBtn) detectBtn.addEventListener('click', toggleDetect);

  if (progress) {
    progress.addEventListener('mousedown', () => { progress.dataset.dragging = '1'; });
    progress.addEventListener('touchstart', () => { progress.dataset.dragging = '1'; });
    progress.addEventListener('input', () => {
      if (videoEl && videoEl.duration && isFinite(videoEl.duration))
        videoEl.currentTime = (progress.value / 1000) * videoEl.duration;
    });
    const release = () => { delete progress.dataset.dragging; };
    progress.addEventListener('mouseup', release);
    progress.addEventListener('touchend', release);
  }
}

function startClock() {
  const el = document.getElementById('clock');
  if (!el) return;
  const tick = () => el.textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  tick(); setInterval(tick, 1000);
}

/* ==================== 启动 ==================== */
window.addEventListener('DOMContentLoaded', () => {
  initDom();
  injectQwenStyle();
  buildQwenPanel();
  buildQwenControls();
  bindEvents();
  startClock();
  initWebSocket();
  initQwenWebSocket();
  renderLoop();
});

window.addEventListener('beforeunload', () => {
  if (rafId) cancelAnimationFrame(rafId);
  stopDetectLoop();
  if (qwenAutoTimer) clearInterval(qwenAutoTimer);
  if (cameraStream) cameraStream.getTracks().forEach(t => t.stop());
  if (ws) { try { ws.close(); } catch(e){} }
  if (qwenWs) { try { qwenWs.close(); } catch(e){} }
});