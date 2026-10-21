import { createApiClient } from './api-client.js';
const api = createApiClient(), $ = id => document.getElementById(id);
try {
  await window.docqaAuthReady;
  const state = await api('/api/auth/setup-status');
  $('setup-form').hidden = !state.available;
  $('message').textContent = state.available ? '无默认密码。请设置后妥善保管管理员凭证。' : '已初始化或当前不在服务器本机。请使用已有账号登录，或联系部署人员。';
} catch (error) { $('message').textContent = error.message; }
$('setup-form').onsubmit = async event => {
  event.preventDefault();
  if ($('setup-submit').disabled) return;
  if ($('password').value !== $('password-confirm').value) { $('message').textContent = '两次输入的密码不一致。'; return; }
  $('setup-submit').disabled = true; $('message').textContent = '正在初始化管理员…';
  try {
    await api('/api/auth/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: $('username').value.trim(), display_name: $('display-name').value.trim(), password: $('password').value }) });
    location.replace('/admin');
  } catch (error) { $('message').textContent = error.message; }
  finally { $('password').value = ''; $('password-confirm').value = ''; $('setup-submit').disabled = false; }
};
