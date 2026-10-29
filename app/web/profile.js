import { createApiClient } from './api-client.js';
let installed=false;

export async function installProfile(session) {
  if(installed)return;installed=true;
  let name = session.user?.display_name || '工作空间';
  const stylesheet = new URL('./profile.css', import.meta.url).pathname;
  if(!document.querySelector(`link[href="${stylesheet}"]`)) { const style=document.createElement('link');style.rel='stylesheet';style.href=stylesheet;document.head.append(style); }
  for (const label of document.querySelectorAll('.account-name')) label.textContent = name;
  let profile = { avatar_url: null }, previewUrl = null, selected = null, busy = false,loading=false,loadToken=0;
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
  const nameLabel=document.createElement('label');nameLabel.htmlFor='profile-display-name';nameLabel.textContent='显示名称';
  const nameInput=document.createElement('input');nameInput.id='profile-display-name';nameInput.maxLength=80;nameInput.value=name;nameInput.autocomplete='nickname';
  const nameSave=document.createElement('button');nameSave.type='button';nameSave.className='secondary';nameSave.textContent='保存显示名称';
  const account=document.createElement('p');account.className='profile-hint';account.textContent=`登录账号：${session.user?.username||''}`;
  const security=document.createElement('a');security.className='profile-security';security.href='/account/password';security.textContent='账号安全 · 修改密码';
  const hint = document.createElement('p'); hint.className = 'profile-hint'; hint.textContent = 'PNG、JPG、WebP，最大2MB。保存时居中裁剪为方形。';
  const input = document.createElement('input'); input.type = 'file'; input.id = 'avatar-file'; input.accept = 'image/png,image/jpeg,image/webp'; input.setAttribute('aria-label', '选择头像图片');
  const status = document.createElement('p'); status.className = 'profile-status'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const actions = document.createElement('div'); actions.className = 'profile-actions';
  const reset = document.createElement('button'); reset.type = 'button'; reset.className = 'secondary'; reset.textContent = '恢复默认头像';
  const save = document.createElement('button'); save.type = 'button'; save.className = 'primary'; save.textContent = '保存头像'; save.disabled = true;
  actions.append(reset, save); dialog.append(header, preview, user, account,nameLabel,nameInput,nameSave,hint, input, status, actions,security); document.body.append(dialog);
  function avatar(element, url) {
    element.replaceChildren();
    if (url) {
      const img = document.createElement('img'); img.src = url; img.alt = name + '的头像';
      img.onerror = () => { element.textContent = Array.from(name)[0] || 'D'; }; element.append(img);
    } else element.textContent = name === '工作空间' ? 'D' : Array.from(name)[0];
  }
  function paint() {
    name=profile.display_name||name;user.textContent=name;
    for(const label of document.querySelectorAll('.account-name'))label.textContent=name;
    if(session.user){session.user.display_name=name;for(const label of document.querySelectorAll('.local-label, .sidebar-footer p'))label.textContent=`${name} · ${session.user.role==='admin'?'管理员':'客户'}`;}
    for (const element of document.querySelectorAll('.account-avatar:not(.profile-preview)')) avatar(element, profile.avatar_url);
    avatar(preview, previewUrl || profile.avatar_url);
    save.disabled = busy || loading || !selected; reset.disabled = busy || loading || !profile.avatar_url; input.disabled = busy || loading; close.disabled = busy;
    nameInput.disabled=busy||loading;nameSave.disabled=busy||loading||!nameInput.value.trim()||nameInput.value.trim()===name;
  }
  function discard(clearInput = true) { if (previewUrl) URL.revokeObjectURL(previewUrl); previewUrl = null; selected = null; if (clearInput) input.value = ''; }
  paint();
  if (!session.user) { dialog.remove(); return; }
  let loaded = false;
  async function load() {
    const token=++loadToken;loading=true;paint();
    try{const value=await request('/api/auth/profile');if(token===loadToken){profile=value;loaded=true;nameInput.value=profile.display_name;}}
    finally{if(token===loadToken){loading=false;paint();}}
  }
  const open=async()=>{discard();nameInput.value=name;status.textContent='';paint();dialog.showModal();try{await load();}catch(error){status.textContent=error.message;}};
  for(const button of document.querySelectorAll('[data-profile-open]'))button.onclick=open;
  for (const menu of document.querySelectorAll('.workspace-menu')) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = '个人设置';
    menu.querySelector('.workspace-menu-items').prepend(button);
    button.onclick = () => {menu.open=false;return open();};
  }
  close.onclick = () => { if (!busy) dialog.close(); };
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => { ++loadToken;loading=false;discard(); paint(); });
  input.onchange = () => {
    const file = input.files?.[0]; discard(false); status.textContent = '';
    if (!file) { paint(); return; }
    if (file.size > 2 * 1024 * 1024 || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      status.textContent = '请选择2MB以内的PNG、JPG或WebP图片'; paint(); return;
    }
    selected = file; previewUrl = URL.createObjectURL(file); paint();
  };
  async function change(options) {
    ++loadToken;loading=false;busy = true; status.textContent = '正在保存…'; paint();
    try { profile = await request('/api/auth/avatar', options); loaded = true; discard(); status.textContent = '头像已保存'; }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; paint(); }
  }
  save.onclick = () => { if (busy || !selected) return; const body = new FormData(); body.append('file', selected); return change({ method: 'PUT', body }); };
  reset.onclick = () => { if (!busy) return change({ method: 'DELETE' }); };
  nameInput.oninput=paint;
  nameSave.onclick=async()=>{
    if(busy||nameSave.disabled)return;++loadToken;loading=false;busy=true;status.textContent='正在保存…';paint();
    try{profile=await request('/api/auth/profile',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_name:nameInput.value.trim()})});loaded=true;nameInput.value=profile.display_name;status.textContent='显示名称已保存';}
    catch(error){status.textContent=error.message;}finally{busy=false;paint();}
  };
  try { await load(); } catch { /* 菜单打开时可重新读取，不打断正在阅读的回答。 */ }
}
