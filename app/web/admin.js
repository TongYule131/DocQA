import { createApiClient, encoded } from './api-client.js';
import { node } from './answer-view.js';

const $ = id => document.getElementById(id), api = createApiClient();
const view = document.body.dataset.workspaceView || 'knowledge';
const names = { knowledge: '知识库管理', users: '用户管理', settings: '系统设置' };
let overview = null, users = [], page = 1, editingBase = null, resettingUser = null, busy = false;
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const date = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }).replaceAll('/', '-') : '—';
function tell(text, kind = 'info') { $('notice').textContent = text; $('notice').dataset.kind = kind; $('notice').hidden = !text; }
async function run(work, button, errorTarget) {
  if (busy) return;
  busy = true; if (button) button.disabled = true;
  document.querySelectorAll('dialog .close-dialog').forEach(b => { b.disabled = true; });
  try { await work(); }
  catch (error) {
    const text = (error.name === 'AbortError' || error instanceof TypeError ? '连接中断或等待超时，请刷新核对；操作未自动重试。' : error.message) + (error.supportId ? `（支持编号：${error.supportId}）` : '');
    if (errorTarget) $(errorTarget).textContent = text; else tell(text, 'error');
  } finally { busy = false; if (button?.isConnected) button.disabled = false; document.querySelectorAll('dialog .close-dialog').forEach(b => { b.disabled = false; }); }
}
function statCards(target, items) {
  const root = $(target); root.replaceChildren();
  for (const [label, value, icon] of items) {
    const card = node('article', '', 'stat-card'), text = node('div');
    text.append(node('p', label), node('strong', String(value))); card.append(node('span', icon, 'stat-icon'), text); root.append(card);
  }
}
function cell(row, text, className) { const item = node('td', text, className); row.append(item); return item; }
function pill(text, kind) { return node('span', text, `status-pill ${kind}`); }
function button(text, onClick, className = '') { const item = node('button', text, className); item.type = 'button'; item.onclick = () => run(() => onClick(item), item); return item; }
function openBase(base = null) {
  editingBase = base; $('base-form').reset(); $('base-error').textContent = '';
  $('base-name').value = base?.name || ''; $('base-description').value = base?.description || '';
  $('base-collection').value = base?.collection_name || '';
  $('base-embedding').replaceChildren(new Option(base?.embedding_model || overview.embedding_model, base?.embedding_model || overview.embedding_model));
  $('base-chunk-max').value = base?.chunk_max_chars || 800; $('base-chunk-overlap').value = base?.chunk_overlap_chars ?? 120;
  for (const id of ['base-collection','base-embedding','base-chunk-max','base-chunk-overlap']) $(id).disabled = Boolean(base);
  $('base-dialog-title').textContent = base ? '编辑知识库' : '新建知识库'; $('save-base').textContent = base ? '保存修改' : '创建知识库'; $('base-dialog').showModal();
}
function renderBases() {
  if (!overview) return;
  const s = overview.stats;
  statCards('base-stats', [['知识库', s.knowledge_bases, '▤'], ['全部文档', s.documents, '▧'], ['含文档知识库', s.populated_bases, '▱'], ['创建用户数', s.creators, '♙']]);
  $('base-total').textContent = s.knowledge_bases;
  $('embedding-model').textContent = overview.embedding_model; $('index-storage').textContent = overview.index_storage;
  const query = $('base-search').value.trim().toLocaleLowerCase();
  const filtered = overview.bases.filter(b => b.name.toLocaleLowerCase().includes(query));
  const pages = Math.max(1, Math.ceil(filtered.length / 10)); page = Math.min(page, pages);
  $('base-rows').replaceChildren(); $('base-empty').hidden = filtered.length > 0;
  if (!filtered.length) {
    $('base-empty').querySelector('h3').textContent = query ? '没有找到匹配的知识库' : '建立第一个团队知识库';
    $('base-empty').querySelector('p').textContent = query ? '尝试使用其他名称搜索。' : '按业务主题组织资料，发布后再授权给客户使用。';
  }
  for (const base of filtered.slice((page - 1) * 10, page * 10)) {
    const row = node('tr'), name = cell(row, ''), link = node('a', base.name, 'base-name');
    link.href = `/knowledge?base=${encoded(base.id)}`; name.append(node('span', `${base.collection_name || ''} · ${base.embedding_model || overview.embedding_model}`, 'base-sub')); name.append(link, node('span', base.description || `${base.member_count} 位获授权客户`, 'base-sub'));
    cell(row, String(base.document_count)); cell(row, base.owner_name);
    cell(row, '').append(pill(base.active_snapshot_id ? `已发布 v${base.revision}` : '草稿', base.active_snapshot_id ? 'published' : ''));
    cell(row, date(base.created_at)); cell(row, date(base.updated_at));
    const controls = node('div', '', 'row');
    controls.append(button('编辑', async () => openBase(base)));
    const manage = node('a', '管理文档', 'button secondary'); manage.href = link.href; controls.append(manage);
    const remove = button('删除', async () => {
      if (!confirm(`删除空知识库“${base.name}”？`)) return;
      await api(`/api/knowledge-bases/${encoded(base.id)}`, { method: 'DELETE' }); await loadBases(); tell('空知识库已删除。');
    }, 'danger');
    remove.disabled = base.document_count > 0 || Boolean(base.active_snapshot_id);
    if (remove.disabled) remove.title = '仅可删除无文档且从未发布的空知识库';
    controls.append(remove); cell(row, '').append(controls); $('base-rows').append(row);
  }
  $('base-count').textContent = `共 ${filtered.length} 个知识库`;
  $('page-label').textContent = `${page} / ${pages}`; $('prev').disabled = page <= 1; $('next').disabled = page >= pages;
}
async function loadBases() { overview = await api('/api/admin/overview'); renderBases(); }
function openUser(user = null) {
  resettingUser = user; $('user-form').reset(); $('user-error').textContent = '';
  $('user-create-fields').hidden = Boolean(user);
  for (const id of ['new-username', 'new-display', 'new-role']) $(id).disabled = Boolean(user);
  $('user-password-fields').hidden=Boolean(user);$('new-password').disabled=Boolean(user);$('user-reset-hint').hidden=!user;
  $('user-dialog-title').textContent = user ? `重设密码 · ${user.username}` : '创建账号';
  $('save-user').textContent = user ? '确认重置为初始密码' : '创建账号'; $('user-dialog').showModal();
}
function renderUsers() {
  statCards('user-stats', [['团队成员', users.length, '♙'], ['管理员', users.filter(u => u.role === 'admin').length, '◇'], ['客户账号', users.filter(u => u.role === 'customer').length, '♧'], ['有效账号', users.filter(u => u.active).length, '✓']]);
  const query = $('user-search').value.trim().toLocaleLowerCase();
  const filtered = users.filter(u => `${u.username} ${u.display_name}`.toLocaleLowerCase().includes(query));
  $('user-rows').replaceChildren(); $('user-empty').hidden = filtered.length > 0;
  for (const user of filtered) {
    const row = node('tr'), name = cell(row, ''); name.append(node('span', user.display_name.slice(0, 1), 'user-avatar'), node('span', user.display_name));
    cell(row, user.username); cell(row, '').append(pill(user.role === 'admin' ? '管理员' : '客户', user.role === 'admin' ? 'admin' : ''));
    const state=cell(row, '');state.append(pill(user.active ? '正常' : '已停用', user.active ? 'active' : ''));if(user.must_change_password)state.append(pill('须修改初始密码','')); cell(row, date(user.created_at));
    const controls = node('div', '', 'row');
    const toggle = button(user.active ? '停用' : '启用', async () => {
      if (user.active && !confirm(`停用“${user.username}”？该账号的登录会立即撤销。`)) return;
      await api(`/api/admin/users/${encoded(user.id)}`, json('PATCH', { active: !user.active })); await loadUsers(); tell('账号状态已更新。');
      if (user.id === window.docqaSession.user?.id) location.assign('/login');
    });
    toggle.disabled = user.active && user.role === 'admin' && users.filter(u => u.active && u.role === 'admin').length <= 1;
    if (toggle.disabled) toggle.title = '不能停用最后一个有效管理员';
    controls.append(button('重设密码', async () => openUser(user)), toggle); cell(row, '').append(controls); $('user-rows').append(row);
  }
}
async function loadUsers() {
  users = await api('/api/admin/users'); renderUsers();
  const events=await api('/api/admin/account-events');$('account-event-rows').replaceChildren();
  const labels={display_name_changed:'修改显示名称',password_changed:'用户修改密码',password_reset:'管理员重置初始密码'};
  for(const event of events){const row=node('tr');cell(row,event.target_username);cell(row,labels[event.action]||'账号操作');cell(row,event.actor_username);cell(row,date(event.created_at));$('account-event-rows').append(row);}
  $('account-events-empty').hidden=events.length>0;
}
function config(target, entries) {
  $(target).replaceChildren(...entries.map(([name, value]) => { const row = node('div'); row.append(node('dt', name), node('dd', String(value))); return row; }));
}
async function loadSettings() {
  const data = await api('/api/admin/settings'), g = data.generation, e = data.embedding, p = data.parsing;
  $('model-badge').textContent = g.configured ? '已配置' : '未配置'; $('embedding-badge').textContent = e.configured ? '已配置' : '未配置';
  $('test-model').disabled = !g.configured; $('test-embedding').disabled = !e.configured;
  config('model-config', [['提供方', g.provider], ['模型', g.model], ['连接主机', g.endpoint], ['思考模式', g.thinking], ['推理强度', g.reasoning_effort]]);
  config('embedding-config', [['模型', e.model], ['连接主机', e.endpoint], ['每批文本数', e.batch_size], ['密钥', e.configured ? '服务器已配置' : '未配置']]);
  config('parser-config', [['解析服务', p.endpoint], ['OCR 引擎', p.ocr_engine], ['OCR 语言', p.ocr_lang], ['PDF 页数上限', `${p.max_pdf_pages} 页`]]);
  config('access-config', [['访问模式', data.team_mode ? '团队账号隔离' : '本地单用户（尚未启用隔离）'], ['会话 Cookie', data.cookie_secure ? '仅 HTTPS' : '本机 HTTP 试用'], ['文件上传上限', `${data.limits.max_upload_mb} MB`], ['临时附件有效期', `${data.limits.attachment_ttl_hours} 小时`], ['每人有效临时附件', data.limits.attachment_max_active]]);
  $('config-source').textContent = data.configuration_source;
}
async function loadRuntime() {
  const status = await api('/api/runtime/status');
  $('runtime-roles').replaceChildren();
  const labels = { web: 'Web 服务', 'parse-worker': '文档解析', 'analysis-worker': '分析与问答' };
  for (const item of [status.web, ...(status.workers || [])].filter(Boolean)) {
    const card = node('div', '', 'runtime-card'); card.append(node('strong', labels[item.role] || '本地服务'), node('p', item.ready ? '已就绪' : '尚未就绪')); $('runtime-roles').append(card);
  }
  $('runtime-summary').textContent = `数据库结构 v${status.schema_version ?? status.database_schema_version ?? '—'} · 状态来自本地进程记录；模型连通性需单独测试。`;
}
for (const item of document.querySelectorAll('nav [data-page]')) { if (item.dataset.page === view) { item.classList.add('active'); item.setAttribute('aria-current', 'page'); } }
$(view + '-view').hidden = false; $('breadcrumb').textContent = names[view]; document.title = names[view] + ' · DocQA 管理后台';
document.querySelectorAll('.close-dialog').forEach(b => { b.onclick = () => b.closest('dialog').close(); });
document.querySelectorAll('dialog').forEach(d => { d.oncancel = event => { if (busy) event.preventDefault(); }; d.addEventListener('close', () => { $('new-password').value = ''; }); });
$('new-base').onclick = $('empty-create').onclick = () => openBase();
$('base-search-form').onsubmit = event => { event.preventDefault(); page = 1; renderBases(); };
$('base-search').oninput = () => { page = 1; renderBases(); };
$('prev').onclick = () => { --page; renderBases(); }; $('next').onclick = () => { ++page; renderBases(); };
$('base-form').onsubmit = event => { event.preventDefault(); run(async () => {
  $('base-error').textContent = '';
  await api(editingBase ? `/api/knowledge-bases/${encoded(editingBase.id)}` : '/api/knowledge-bases', json(editingBase ? 'PATCH' : 'POST', { name: $('base-name').value.trim(), description: $('base-description').value.trim(), ...(!editingBase ? { collection_name: $('base-collection').value.trim() || null, embedding_model: $('base-embedding').value, chunk_max_chars: Number($('base-chunk-max').value), chunk_overlap_chars: Number($('base-chunk-overlap').value) } : {}) }));
  $('base-dialog').close(); await loadBases(); tell(editingBase ? '知识库信息已保存。' : '知识库已创建。进入“管理文档”即可上传、准备并授权。');
}, $('save-base'), 'base-error'); };
$('new-user').onclick = () => openUser(); $('user-search').oninput = renderUsers;
$('user-form').onsubmit = event => { event.preventDefault(); run(async () => {
  $('user-error').textContent = ''; const user = resettingUser;
  const path = user ? `/api/admin/users/${encoded(user.id)}/password` : '/api/admin/users';
  const data = user ? {} : { username: $('new-username').value.trim(), display_name: $('new-display').value.trim(), role: $('new-role').value, password: $('new-password').value };
  try { await api(path, json('POST', data)); }
  finally { $('new-password').value = ''; }
  $('user-dialog').close();
  if (user?.id === window.docqaSession.user?.id) { location.assign('/login'); return; }
  await loadUsers(); tell(user ? '密码已重置为初始密码，原登录已全部撤销；用户下次登录须先修改密码。' : '账号已创建。请到知识库“发布与访问授权”为客户分配资料。');
}, $('save-user'), 'user-error'); };
$('refresh-bases').onclick = () => run(loadBases, $('refresh-bases'));
$('refresh-users').onclick = () => run(loadUsers, $('refresh-users'));
$('refresh-settings').onclick = () => run(loadSettings, $('refresh-settings'));
$('refresh-runtime').onclick = () => run(loadRuntime, $('refresh-runtime'));
for (const [id, path, target, label] of [['test-model', '/api/model/test', 'model-test', '回答模型'], ['test-embedding', '/api/embedding/test', 'embedding-test', '向量模型']]) {
  $(id).onclick = () => run(async () => { $(target).textContent = '正在测试；仅执行本次请求，失败不会自动重试…'; await api(path, { method: 'POST', timeoutMs: 360000 }); $(target).textContent = `${label}连接测试通过。`; }, $(id), target);
}
$('test-parser').onclick = () => run(async () => { const status = await api('/api/parsing/status'); $('parser-test').textContent = status.reachable ? '解析服务可达；不代表文档识别质量已经验证。' : '解析服务暂不可达，请检查服务器配置及运行状态。'; }, $('test-parser'), 'parser-test');
try {
  const session = await window.docqaAuthReady;
  if (!session.team_mode) {
    const setup = await api('/api/auth/setup-status'); $('setup-banner').hidden = !setup.available;
    $('new-user').disabled = true; $('new-user').title = '请先完成首位管理员设置，启用团队登录';
  }
  if (view === 'knowledge') await loadBases();
  if (view === 'users') await loadUsers();
  if (view === 'settings') { await loadSettings(); await loadRuntime(); }
} catch (error) { tell(error.message || '无法读取后台，请刷新。', 'error'); }
