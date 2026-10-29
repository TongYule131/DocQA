// 工作台导航只改变展示，不重建会话，也不触发模型请求。
import { icon } from './ui-icons.js';
import { installProfile } from './profile.js';

const sidebar = document.querySelector('.sidebar,.kb-chat-sidebar');
if (sidebar) {
  const brand = sidebar.querySelector('.brand');
  const label = document.createElement('span'); label.className = 'brand-label'; label.textContent = 'DocQA';
  brand.replaceChildren(brand.firstElementChild, label);
  const toggle = document.createElement('button');
  toggle.type = 'button'; toggle.className = 'chat-collapse'; toggle.append(icon('panel'));
  sidebar.id ||= 'workbench-sidebar'; toggle.setAttribute('aria-controls', sidebar.id);
  brand.after(toggle);
  const key = window.innerWidth<=800?'docqa.ui.chatCollapsedMobile':'docqa.ui.chatCollapsed';
  let collapsed = window.innerWidth<=800;
  try { const saved=localStorage.getItem(key);if(saved!==null)collapsed=saved==='true'; } catch { /* 私密模式仍可切换。 */ }
  function paint() {
    document.body.classList.toggle('chat-nav-collapsed', collapsed);
    toggle.setAttribute('aria-expanded', String(!collapsed));
    toggle.setAttribute('aria-label', collapsed ? '展开工作台侧栏' : '收起工作台侧栏');
    toggle.title = collapsed ? '展开侧栏' : '收起侧栏';
  }
  toggle.onclick = () => { collapsed = !collapsed; paint(); try { localStorage.setItem(key, String(collapsed)); } catch {} };
  paint();
  const newChat = sidebar.querySelector('#new-conversation,#new-question,#new-chat');
  if (newChat) {
    const text = newChat.textContent.replace(/^＋\s*/, '');
    const span = document.createElement('span'); span.className = 'nav-text'; span.textContent = text;
    newChat.replaceChildren(icon('plus'), span); newChat.setAttribute('aria-label', text); newChat.title = text;
  }
  const history = document.createElement('button'); history.className = 'rail-history'; history.type = 'button';
  history.title = '查看会话'; history.setAttribute('aria-label', '展开侧栏查看会话'); history.append(icon('chat'));
  history.onclick = () => { collapsed = false; paint(); try { localStorage.setItem(key, 'false'); } catch {} sidebar.querySelector('#history-search,#kb-picker')?.focus(); };
  sidebar.querySelector('#kb-history')?.addEventListener('click',event=>{
    if(window.innerWidth<=800&&event.target.closest('button')){collapsed=true;paint();try{localStorage.setItem(key,'true');}catch{}}
  });
  if (newChat) newChat.after(history);
}
for (const button of document.querySelectorAll('.send-button,#kb-send')) {
  button.replaceChildren(icon('send')); button.setAttribute('aria-label', '发送问题'); button.title = '发送问题';
}
for (const button of document.querySelectorAll('.composer-add')) button.replaceChildren(icon('plus'));
for (const button of document.querySelectorAll('.history-title .icon-button')) button.replaceChildren(icon('refresh'));
for (const summary of document.querySelectorAll('.workspace-menu>summary')) summary.setAttribute('aria-label', '打开个人与工作空间菜单');
const menus = [...document.querySelectorAll('.workspace-menu,.chat-settings,.history-options')];
document.addEventListener('click', event => { for (const menu of menus) if (!menu.contains(event.target)) menu.open = false; });
document.addEventListener('keydown', event => { if (event.key === 'Escape') for (const menu of menus) menu.open = false; });
window.docqaAuthReady?.then(installProfile).catch(() => {});
