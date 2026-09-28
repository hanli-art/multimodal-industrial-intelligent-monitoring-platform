/* ============================================
   R8 前端鉴权公共脚本
   - 会话（token + 用户信息）存 localStorage
   - apiFetch：自动带上 Authorization 头，401 时清会话回登录页
   - 页面元素声明式控制：[data-perm] 按角色显隐、[data-user-name]/[data-user-role]/[data-logout]
   - 未登录或角色不足时由 <html data-page-perm="..."> 声明并自动跳转
   ============================================ */
(function () {
  var TOKEN_KEY = 'ims_token';
  var USER_KEY = 'ims_user';

  var ROLE_ADMIN = '超级管理员';
  var ROLE_SAFETY = '安全管理员';

  function getToken() {
    return localStorage.getItem(TOKEN_KEY) || '';
  }

  function getUser() {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY) || 'null');
    } catch (e) {
      return null;
    }
  }

  function setSession(token, user) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(user || {}));
  }

  function clearSession() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  }

  function toLogin() {
    location.replace('./login.html');
  }

  /* 权限判断，与后端 allowed_roles 保持一致 */
  function can(perm) {
    var role = (getUser() || {}).role || '';
    if (!role) return false;
    if (perm === 'device') return role === ROLE_ADMIN;
    if (perm === 'alarm_handle' || perm === 'evidence') {
      return role === ROLE_ADMIN || role === ROLE_SAFETY;
    }
    return true;
  }

  /* 带鉴权的请求；未登录/过期时统一跳登录页 */
  async function apiFetch(url, options) {
    options = options || {};
    var headers = Object.assign({}, options.headers || {});
    headers['Authorization'] = 'Bearer ' + getToken();
    options.headers = headers;
    var res = await fetch(url, options);
    if (res.status === 401) {
      clearSession();
      toLogin();
      throw new Error('登录已过期，请重新登录');
    }
    return res;
  }

  async function logout() {
    try {
      await apiFetch('/api/logout', { method: 'POST' });
    } catch (e) {
      /* 会话已失效也照常清理本地并回登录页 */
    }
    clearSession();
    toLogin();
  }

  /* DOM 就绪后按角色调整页面元素 */
  function applyRoleUI() {
    var user = getUser() || {};

    document.querySelectorAll('[data-perm]').forEach(function (el) {
      if (!can(el.getAttribute('data-perm'))) el.remove();
    });
    document.querySelectorAll('[data-user-name]').forEach(function (el) {
      el.textContent = user.username || '';
    });
    document.querySelectorAll('[data-user-role]').forEach(function (el) {
      el.textContent = user.role || '';
    });
    document.querySelectorAll('[data-logout]').forEach(function (el) {
      el.onclick = function (e) {
        e.preventDefault();
        logout();
      };
    });
  }

  /* 进入受保护页面前先做拦截（<html data-page-perm> 由页面声明） */
  var isLoginPage = /login\.html$/.test(location.pathname);
  if (!isLoginPage) {
    if (!getToken()) {
      toLogin();
    } else {
      var pagePerm = document.documentElement.getAttribute('data-page-perm');
      if (pagePerm && !can(pagePerm)) location.replace('./alarms.html');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyRoleUI);
  } else {
    applyRoleUI();
  }

  window.Auth = {
    getToken: getToken,
    getUser: getUser,
    setSession: setSession,
    clearSession: clearSession,
    toLogin: toLogin,
    can: can,
    apiFetch: apiFetch,
    logout: logout,
  };
})();