import { node } from './answer-view.js';
import { icon } from './ui-icons.js';

const sidebar=document.querySelector('.admin-sidebar'),nav=sidebar?.querySelector('nav');
if (sidebar && nav) {
  const path=location.pathname,view=new URLSearchParams(location.search).get('view')==='list'?'list':'tree';
  const brand=sidebar.querySelector('.brand');
  brand.replaceChildren(node('span','D','brand-symbol'),node('span','DocQA','nav-label'));
  const toggle=node('button','','admin-collapse');toggle.type='button';toggle.append(icon('panel'));sidebar.insertBefore(toggle,brand.nextSibling);
  function collapse(value) {
    document.body.classList.toggle('admin-nav-collapsed',value);toggle.setAttribute('aria-label',value?'展开后台侧栏':'收起后台侧栏');toggle.title=value?'展开侧栏':'收起侧栏';toggle.setAttribute('aria-expanded',String(!value));
    try { localStorage.setItem('docqa.ui.adminCollapsed',String(value)); } catch { }
  }
  let collapsed=window.innerWidth<=800;try {const saved=localStorage.getItem('docqa.ui.adminCollapsed');if(saved!==null)collapsed=saved==='true';}catch{}
  collapse(collapsed);toggle.onclick=()=>collapse(!document.body.classList.contains('admin-nav-collapsed'));
  nav.replaceChildren();
  function link(label,href,name,active=false) {
    const a=node('a');a.href=href;a.title=label;a.setAttribute('aria-label',label);a.append(icon(name),node('span',label,'nav-label'));
    if(active){a.classList.add('active');a.setAttribute('aria-current','page');}return a;
  }
  nav.append(link('知识库管理','/admin','book',(path==='/admin'||path==='/knowledge'||path==='/admin/chunks')));
  const group=node('div','','admin-nav-group'),trigger=node('button','','admin-nav-trigger'),children=node('div','','admin-subnav');
  trigger.type='button';trigger.title='意图管理';trigger.setAttribute('aria-label','意图管理');trigger.setAttribute('aria-controls','intent-navigation');children.id='intent-navigation';
  trigger.append(icon('layers'),node('span','意图管理','nav-label'),icon('chevron'));
  const treeLink=link('意图树配置','/admin/intents?view=tree','tree',path==='/admin/intents'&&view==='tree');
  const listLink=link('意图列表','/admin/intents?view=list','list',path==='/admin/intents'&&view==='list');children.append(treeLink,listLink);
  function expanded(value){children.hidden=!value;trigger.setAttribute('aria-expanded',String(value));}
  expanded(path==='/admin/intents');trigger.onclick=()=>expanded(children.hidden);group.append(trigger,children);nav.append(group);
  nav.append(link('链路追踪','/admin/traces','trace',path==='/admin/traces'),link('用户管理','/admin/users','users',path==='/admin/users'),link('系统设置','/admin/settings','settings',path==='/admin/settings'));
  const bottom=sidebar.querySelector('.sidebar-bottom');if(bottom)bottom.replaceChildren(link('返回对话','/app','chat'));
  window.addEventListener('docqa:intent-view',event=>{
    for (const [a,key] of [[treeLink,'tree'],[listLink,'list']]) { const active=event.detail===key;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current'); }
  });
}
