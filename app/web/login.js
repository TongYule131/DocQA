import { createApiClient } from './api-client.js';
import { installPasswordToggle } from './password-field.js';
const api = createApiClient(), form = document.getElementById('login-form'), message = document.getElementById('message');
// 提示按类型着色：进行中为中性信息，成功与失败不再共用红色。
const tell = (text, kind = 'info') => { message.textContent = text; message.dataset.kind = kind; };
const session = await window.docqaAuthReady;
const landing=user=>user.must_change_password?'/account/password':user.role==='admin'?'/admin':'/app';
if (session.user) location.replace(landing(session.user));
else if (!session.team_mode) { const setup = await api('/api/auth/setup-status'); location.replace(setup.available ? '/setup' : '/app'); }
installPasswordToggle(document.getElementById('password'));
if(new URLSearchParams(location.search).get('password_changed')==='1')tell('密码已修改，请使用新密码重新登录。','ok');
form.onsubmit = async event => {
  event.preventDefault(); const button = document.getElementById('login'); button.disabled = true; tell('正在登录…','info');
  try {
    const result = await api('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: document.getElementById('username').value.trim(), password: document.getElementById('password').value }) });
    document.getElementById('password').value = ''; location.replace(landing(result.user));
  } catch (error) { tell(error.message,'error'); document.getElementById('password').focus(); }
  finally { button.disabled = false; }
};
