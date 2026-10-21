import { createApiClient } from './api-client.js';

export async function installProfile(session) {
  const name = session.user?.display_name || '工作空间';
  for (const label of document.querySelectorAll('.account-name')) label.textContent = name;
  let profile = { avatar_url: null }, previewUrl = null, selected = null, busy = false;
  const request = createApiClient();
  const dialog = document.createElement('dialog'); dialog.className = 'profile-dialog';
  // 固定表单通过DOM构建，账号名及图片URL不拼接到HTML。
  const heading = document.createElement('h2'); heading.id = 'profile-heading'; heading.textContent = '个人设置';
  dialog.setAttribute('aria-labelledby', heading.id);
  const header = document.createElement('div'); header.className = 'profile-heading';
  const close = document.createElement('button'); close.type = 'button'; close.className = 'profile-close'; close.textContent = '×'; close.setAttribute('aria-label', '关闭个人设置');
  header.append(heading, close);
  const preview = document.createElement('span'); preview.className = 'account-avatar profile-preview';
  const user = document.createElement('p'); user.className = 'profile-name'; user.textContent = name;
  const hint = document.createElement('p'); hint.className = 'profile-hint'; hint.textContent = 'PNG、JPG、WebP，最大2MB。保存时居中裁剪为方形。';
  const input = document.createElement('input'); input.type = 'file'; input.id = 'avatar-file'; input.accept = 'image/png,image/jpeg,image/webp'; input.setAttribute('aria-label', '选择头像图片');
  const status = document.createElement('p'); status.className = 'profile-status'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const actions = document.createElement('div'); actions.className = 'profile-actions';
  const reset = document.createElement('button'); reset.type = 'button'; reset.className = 'secondary'; reset.textContent = '恢复默认头像';
  const save = document.createElement('button'); save.type = 'button'; save.className = 'primary'; save.textContent = '保存头像'; save.disabled = true;
  actions.append(reset, save); dialog.append(header, preview, user, hint, input, status, actions); document.body.append(dialog);
  function avatar(element, url) {
    element.replaceChildren();
    if (url) {
      const img = document.createElement('img'); img.src = url; img.alt = name + '的头像';
      img.onerror = () => { element.textContent = Array.from(name)[0] || 'D'; }; element.append(img);
    } else element.textContent = name === '工作空间' ? 'D' : Array.from(name)[0];
  }
  function paint() {
    for (const element of document.querySelectorAll('.account-avatar:not(.profile-preview)')) avatar(element, profile.avatar_url);
    avatar(preview, previewUrl || profile.avatar_url);
    save.disabled = busy || !selected; reset.disabled = busy || !profile.avatar_url; input.disabled = busy; close.disabled = busy;
  }
  function discard(clearInput = true) { if (previewUrl) URL.revokeObjectURL(previewUrl); previewUrl = null; selected = null; if (clearInput) input.value = ''; }
  paint();
  if (!session.user) { dialog.remove(); return; }
  let loaded = false;
  async function load() { profile = await request('/api/auth/profile'); loaded = true; paint(); }
  for (const menu of document.querySelectorAll('.workspace-menu')) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = '个人设置';
    menu.querySelector('.workspace-menu-items').prepend(button);
    button.onclick = async () => {
      menu.open = false; discard(); status.textContent = ''; paint(); dialog.showModal();
      if (!loaded) try { await load(); } catch (error) { status.textContent = error.message; }
    };
  }
  close.onclick = () => { if (!busy) dialog.close(); };
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => { discard(); paint(); });
  input.onchange = () => {
    const file = input.files?.[0]; discard(false); status.textContent = '';
    if (!file) { paint(); return; }
    if (file.size > 2 * 1024 * 1024 || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      status.textContent = '请选择2MB以内的PNG、JPG或WebP图片'; paint(); return;
    }
    selected = file; previewUrl = URL.createObjectURL(file); paint();
  };
  async function change(options) {
    busy = true; status.textContent = '正在保存…'; paint();
    try { profile = await request('/api/auth/avatar', options); loaded = true; discard(); status.textContent = '头像已保存'; }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; paint(); }
  }
  save.onclick = () => { if (busy || !selected) return; const body = new FormData(); body.append('file', selected); return change({ method: 'PUT', body }); };
  reset.onclick = () => { if (!busy) return change({ method: 'DELETE' }); };
  try { await load(); } catch { /* 菜单打开时可重新读取，不打断正在阅读的回答。 */ }
}
