// 凭证只在 HttpOnly cookie 中；网页仅持有当前会话的 CSRF 随机值。
window.docqaAuthReady = (async () => {
  const response = await fetch('/api/auth/me', { cache: 'no-store' });
  if (!response.ok) throw new Error('无法读取登录状态，请刷新页面');
  const session = await response.json(); window.docqaSession = session;
  const identity = session.user?.id || 'local';
  try {
    const prior = sessionStorage.getItem('docqa.identity');
    if ((prior && prior !== identity) || (!prior && session.team_mode)) {
      Object.keys(sessionStorage).filter(key => key.startsWith('docqa.')).forEach(key => sessionStorage.removeItem(key));
    }
    sessionStorage.setItem('docqa.identity', identity);
  } catch { /* 浏览器存储不可用时，提交控制器仍会阻止不能恢复的提问。 */ }
  document.body.dataset.role = session.user?.role || (session.team_mode ? 'anonymous' : 'admin');
  if (session.demo_providers) {
    const banner = document.createElement('div'); banner.className = 'demo-banner';
    banner.textContent = '开发预览：支持真实上传与解析；向量和回答使用合成桩，未连接收费模型。';
    banner.style.cssText = 'background:#fff6e6;color:#806331;text-align:center;padding:8px 16px;font-size:12px;line-height:1.6;flex:none';
    document.body.prepend(banner);
    const shell = document.querySelector('.app-shell'); if (shell) shell.style.height = 'calc(100dvh - 35px)';
  }
  document.querySelectorAll('.local-label, .sidebar-footer p').forEach(item => {
    item.textContent = session.team_mode ? `${session.user?.display_name || '请登录'} · ${session.user?.role === 'admin' ? '管理员' : '客户'}` : '本地开发版 · 单用户';
  });
  document.querySelectorAll('[data-admin-only]').forEach(item => { item.hidden = session.team_mode && session.user?.role !== 'admin'; });
  if (session.team_mode && !session.user && !['/login', '/setup'].includes(location.pathname)) location.replace('/login');
  document.querySelectorAll('[data-logout]').forEach(button => {
    button.hidden = !session.team_mode;
    button.onclick = async () => {
      button.disabled = true;
      try { const result = await window.docqaFetch('/api/auth/logout', { method: 'POST' }); if (result.ok) location.assign('/login'); }
      finally { button.disabled = false; }
    };
  });
  return session;
})();
// 旧工具页与新模块共用同一传输桥，防止旧 POST 漏带 CSRF。
window.docqaFetch = async (path, options = {}) => {
  if (typeof path !== 'string' || !path.startsWith('/api/')) throw new Error('请求地址不受支持');
  const session = await window.docqaAuthReady;
  const headers = new Headers(options.headers || {});
  if (!['GET', 'HEAD', 'OPTIONS'].includes((options.method || 'GET').toUpperCase()) && session.csrf_token) headers.set('X-CSRF-Token', session.csrf_token);
  const response = await fetch(path, { ...options, headers, cache: 'no-store' });
  if (response.status === 401 && location.pathname !== '/login') location.assign('/login');
  return response;
};
window.docqaAuthReady.catch(() => {
  const notice = document.getElementById('notice') || document.getElementById('message');
  if (notice) { notice.hidden = false; notice.textContent = '无法读取登录状态，请刷新页面。'; }
});
