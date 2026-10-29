import { createApiClient } from './api-client.js';
const api = createApiClient(), form = document.getElementById('login-form'), message = document.getElementById('message');
const session = await window.docqaAuthReady;
const landing=user=>user.must_change_password?'/account/password':user.role==='admin'?'/admin':'/app';
if (session.user) location.replace(landing(session.user));
else if (!session.team_mode) { const setup = await api('/api/auth/setup-status'); location.replace(setup.available ? '/setup' : '/app'); }
if(new URLSearchParams(location.search).get('password_changed')==='1')message.textContent='密码已修改，请使用新密码重新登录。';
form.onsubmit = async event => {
  event.preventDefault(); const button = document.getElementById('login'); button.disabled = true; message.textContent = '正在登录…';
  try {
    const result = await api('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: document.getElementById('username').value.trim(), password: document.getElementById('password').value }) });
    document.getElementById('password').value = ''; location.replace(landing(result.user));
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
};
