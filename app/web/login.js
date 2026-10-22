import { createApiClient } from './api-client.js';
const api = createApiClient(), form = document.getElementById('login-form'), message = document.getElementById('message');
const session = await window.docqaAuthReady;
if (session.user) location.replace(session.user.role === 'admin' ? '/admin' : '/app');
else if (!session.team_mode) { const setup = await api('/api/auth/setup-status'); location.replace(setup.available ? '/setup' : '/app'); }
form.onsubmit = async event => {
  event.preventDefault(); const button = document.getElementById('login'); button.disabled = true; message.textContent = '正在登录…';
  try {
    const result = await api('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: document.getElementById('username').value.trim(), password: document.getElementById('password').value }) });
    document.getElementById('password').value = ''; location.replace(result.user.role === 'admin' ? '/admin' : '/app');
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
};
