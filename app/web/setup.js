import { createApiClient } from './api-client.js';
import { installPasswordToggle } from './password-field.js';
const api = createApiClient(), $ = id => document.getElementById(id);
// 提示带类型，成功/进行中不再沿用失败配色。
const tell = (text, kind = 'info') => { $('message').textContent = text; $('message').dataset.kind = kind; };
for (const id of ['password', 'password-confirm']) installPasswordToggle($(id));
try {
  await window.docqaAuthReady;
  const state = await api('/api/auth/setup-status');
  $('setup-form').hidden = !state.available;
  tell(state.available ? '无默认密码。请设置后妥善保管管理员凭证。' : '已初始化或当前不在服务器本机。请使用已有账号登录，或联系部署人员。', 'info');
} catch (error) { tell(error.message || '无法读取初始化状态，请刷新重试。', 'error'); }
$('setup-form').onsubmit = async event => {
  event.preventDefault();
  if ($('setup-submit').disabled) return;
  if ($('password').value !== $('password-confirm').value) { tell('两次输入的密码不一致。', 'error'); $('password-confirm').focus(); return; }
  $('setup-submit').disabled = true; tell('正在初始化管理员…', 'info');
  try {
    await api('/api/auth/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: $('username').value.trim(), display_name: $('display-name').value.trim(), password: $('password').value }) });
    location.replace('/admin');
  } catch (error) { tell(error.message, 'error'); }
  finally { $('password').value = ''; $('password-confirm').value = ''; $('setup-submit').disabled = false; }
};
