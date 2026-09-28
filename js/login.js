/* R8 登录页逻辑：图形验证码 + 账密登录 */

function $(id) { return document.getElementById(id); }

let captchaId = '';

/* 已登录直接进首页，避免重复登录 */
if (Auth.getToken()) location.replace('./dashboard.html');

function showError(msg) {
  $('loginErr').textContent = msg || '';
}

async function refreshCaptcha() {
  try {
    const res = await fetch('/api/captcha');
    const data = await res.json();
    if (data.status !== 'ok') return;
    captchaId = data.captcha_id;
    $('captchaImg').src = data.image;
    $('captcha').value = '';
  } catch (e) {
    showError('验证码加载失败，请点击图片重试');
  }
}

async function doLogin(e) {
  e.preventDefault();
  showError('');

  const username = $('username').value.trim();
  const password = $('password').value;
  const captcha = $('captcha').value.trim();

  if (!username || !password) { showError('请输入账号和密码'); return; }
  if (!captcha) { showError('请输入验证码'); return; }

  $('btnLogin').disabled = true;
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username, password: password, captcha_id: captchaId, captcha: captcha }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      Auth.setSession(data.token, { username: data.username, role: data.role });
      location.href = './dashboard.html';
      return;
    }
    showError(data.detail || data.message || '登录失败');
  } catch (err) {
    showError('网络异常，请稍后重试');
  }

  $('btnLogin').disabled = false;
  refreshCaptcha();   // 验证码一次性使用，无论成败都换一张
}

$('loginForm').onsubmit = doLogin;
$('captchaImg').onclick = refreshCaptcha;

refreshCaptcha();