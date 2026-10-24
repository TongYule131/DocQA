import { createApiClient, encoded } from './api-client.js';
import { node, locationText } from './answer-view.js';
import { installIntentTest } from './intent-test.js';

const $ = id => document.getElementById(id), api = createApiClient();
const moduleName = location.pathname.endsWith('/intents') ? 'intents' : location.pathname.endsWith('/traces') ? 'traces' : 'chunks';
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const date = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const duration = ms => ms === null || ms === undefined ? '未记录' : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`;
const statusNames = { succeeded: '成功', failed: '失败', running: '运行中', queued: '排队中', needs_attention: '待确认', cancelled: '已取消', interrupted: '已中断' };
let busy = false, intents = [], bases = [], tools = [], selected = null, editing = null, parentNew = null, intentPage = 1, tracePage = 1, traceTotal = 0;
let chunkData = null, edits = [], selectedChunk = 0, chunkJob = null, timer = null, generation = 0;
function tell(text, kind = 'info') { $('notice').textContent = text; $('notice').hidden = !text; $('notice').dataset.kind = kind; }
async function run(work, errorTarget = null) {
  if (busy) return; busy = true;
  try { await work(); } catch (error) { const text = error.status ? error.message : '连接中断，操作可能已执行，请刷新核对；未自动重试。'; if (errorTarget) $(errorTarget).textContent = text; else tell(text, 'error'); }
  finally { busy = false; }
}
function button(text, work, className = 'secondary') { const b = node('button', text, className); b.type = 'button'; b.onclick = () => run(work); return b; }
function cell(row, text) { const c = node('td', text); row.append(c); return c; }
function pill(text, kind = '') { return node('span', text, `status-pill ${kind}`); }
function traceStatus(status) { return pill(statusNames[status] || '未知状态', `trace-status ${Object.hasOwn(statusNames, status) ? status : 'unknown'}`); }
function traceRow(status) { const row = node('tr', '', 'trace-row'); row.dataset.status = Object.hasOwn(statusNames, status) ? status : 'unknown'; return row; }
function option(select, text, value) { const o = node('option', text); o.value = value; select.append(o); }
function stats(target, values) {
  target.replaceChildren();
  for (const [label, value] of values) { const card = node('article', '', 'stat-card'), wrap = node('div'); wrap.append(node('p', label), node('strong', String(value))); card.append(wrap); target.append(card); }
}
document.querySelectorAll('.close-dialog').forEach(b => { b.onclick = () => { if (!busy) b.closest('dialog').close(); }; });
document.querySelector(`[data-module="${moduleName}"]`)?.classList.add('active');
$('breadcrumb').textContent = { intents: '意图管理', traces: '链路追踪', chunks: '分块管理' }[moduleName];
$(`${moduleName}-page`).hidden = false;
document.title = `${$('breadcrumb').textContent} · DocQA`;

const testbench = moduleName === 'intents' ? installIntentTest() : null;

async function loadIntents() {
  [intents, bases, tools] = await Promise.all([api('/api/admin/intents'), api('/api/knowledge-bases'), api('/api/admin/mcp-tools')]);
  const parent = $('intent-parent'), current = parent.value; parent.replaceChildren(); option(parent, '全部父节点', ''); option(parent, '根节点', 'root');
  intents.forEach(i => option(parent, i.name, i.id)); parent.value = current;
  if (!selected || !intents.some(i => i.id === selected)) selected = intents[0]?.id || null;
  testbench?.setBases(bases);
  renderIntents();
}
function filteredIntents() {
  const text = $('intent-search').value.trim().toLowerCase(), parent = $('intent-parent').value;
  return intents.filter(i => (!text || `${i.name} ${i.code}`.toLowerCase().includes(text)) && (!$('intent-level').value || String(i.level) === $('intent-level').value) && (!$('intent-kind').value || i.kind === $('intent-kind').value) && (!$('intent-enabled').value || String(i.enabled) === $('intent-enabled').value) && (!parent || parent === 'root' && !i.parent_id || i.parent_id === parent));
}
function renderIntents() {
  const filtered = filteredIntents(), tree = $('intent-tree'); tree.replaceChildren();
  const visible = new Set(filtered.map(i => i.id));
  function branch(parent) {
    for (const i of intents.filter(item => item.parent_id === parent)) {
      if (visible.has(i.id)) {
        const b = button('', () => { selected = i.id; renderIntents(); }, 'tree-node' + (selected === i.id ? ' selected' : ''));
        b.style.setProperty('--depth', String(i.level)); b.append(node('span', i.parent_id ? '└' : '▧'), node('span', i.name), pill(i.kind, i.kind), node('small', i.enabled ? '启用' : '停用', 'muted-detail')); tree.append(b);
      }
      branch(i.id);
    }
  }
  branch(null);
  if (!filtered.length) tree.append(node('p', '暂无匹配节点', 'empty-state'));
  const detail = $('intent-detail'), i = intents.find(item => item.id === selected); detail.replaceChildren();
  if (i) {
    detail.append(node('h2', i.name), node('p', i.code, 'node-code'));
    const badges = node('div', '', 'row'); badges.append(pill(i.level ? `第 ${i.level + 1} 层` : '根节点'), pill(i.kind, i.kind), pill(i.enabled ? '启用' : '停用')); detail.append(badges);
    const actions = node('div', '', 'node-actions'); actions.append(button('＋ 新建子节点', () => openIntent(null, i)), button('编辑节点', () => openIntent(i)), button('删除节点', async () => { if (!confirm(`删除“${i.name}”？有子节点时不能删除。`)) return; await api(`/api/admin/intents/${encoded(i.id)}?revision=${i.revision}`, { method: 'DELETE' }); selected = null; await loadIntents(); })); actions.append(button('测试此节点', () => { testbench.prefill(i.examples[0] || '', i.base_id); selectIntentView('test'); })); detail.append(actions);
    const dl = node('dl');
    for (const [label, value] of [['父节点', intents.find(p => p.id === i.parent_id)?.name || 'ROOT'], ['关联知识库', bases.find(b => b.id === i.base_id)?.name || '未绑定'], ['工具', tools.find(t => t.id === i.tool_id)?.name || '—'], ['节点 TopK', i.top_k || '全局默认'], ['匹配策略', {exact:'完整示例',interaction:'限定纯交互',keywords:'业务关键词'}[i.match_mode] || '完整示例'], ['业务关键词', (i.keywords || []).join('、') || '—'], ['路径', i.path], ['描述', i.description || '—']]) dl.append(node('dt', label), node('dd', String(value)));
    detail.append(dl, node('h3', '示例问题'));
    const examples = node('div', '', 'example-list'); i.examples.forEach(text => examples.append(node('span', text, 'example-chip'))); if (!i.examples.length) examples.append(node('p', '暂无示例', 'muted')); detail.append(examples);
    if (i.response_text) detail.append(node('h3', '系统回复'), node('p', i.response_text));
  } else detail.append(node('p', '从左侧选择一个节点', 'empty-state'));
  const rows = $('intent-rows'); rows.replaceChildren(); const pages = Math.max(1, Math.ceil(filtered.length / 15)); intentPage = Math.min(intentPage, pages);
  for (const i of filtered.slice((intentPage - 1) * 15, intentPage * 15)) {
    const row = node('tr'), name = cell(row, i.name); name.append(node('p', i.code, 'node-code')); cell(row, i.level ? `第 ${i.level + 1} 层` : '根节点').append(); cell(row, '').append(pill(i.kind, i.kind)); cell(row, i.path); cell(row, bases.find(b => b.id === i.base_id)?.name || i.tool_id || '系统交互'); cell(row, String(i.examples.length)); cell(row, i.enabled ? '启用' : '停用'); const actions = cell(row, ''); actions.append(button('编辑', () => openIntent(i)), button('定位树', () => { selected = i.id; treeTab(); renderIntents(); })); rows.append(row);
  }
  $('intent-count').textContent = `共 ${filtered.length} 条`; $('intent-page').textContent = `${intentPage} / ${pages}`; $('intent-prev').disabled = intentPage === 1; $('intent-next').disabled = intentPage === pages;
}
function selectIntentView(view) {
  view = ['list','test'].includes(view) ? view : 'tree';
  document.querySelector('#intents-page .filter-bar').hidden = view === 'test';
  $('intent-tree-view').hidden = view !== 'tree'; $('intent-list-view').hidden = view !== 'list';
  if (testbench) testbench.section.hidden = view !== 'test';
  $('tree-tab').setAttribute('aria-selected', String(view === 'tree')); $('list-tab').setAttribute('aria-selected', String(view === 'list'));
  const url = new URL(location.href); url.searchParams.set('view', view); history.replaceState(null, '', url);
  const title = { tree:'意图树配置', list:'意图列表', test:'意图测试台' }[view];
  document.querySelector('#intents-page h1').textContent=title; document.title=title+' · DocQA'; $('breadcrumb').textContent=title;
  window.dispatchEvent(new CustomEvent('docqa:intent-view', { detail: view }));
}
function treeTab() { selectIntentView('tree'); }
// 同页切换保留测试输入和上次结果，便于编辑规则后直接比较；不把客户问题写入浏览器存储。
if (moduleName === 'intents') document.addEventListener('click', event => {
  const link = event.target.closest?.('#intent-navigation a');
  if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  selectIntentView(new URL(link.href).searchParams.get('view'));
});
function openIntent(i = null, parent = null) {
  editing = i; parentNew = parent; $('intent-form').reset(); $('intent-error').textContent = ''; $('intent-form-title').textContent = i ? '编辑节点' : parent ? '新建子节点' : '新建根节点';
  const ps = $('node-parent'); ps.replaceChildren(); option(ps, 'ROOT · 无父节点', ''); intents.filter(p => p.id !== i?.id).forEach(p => option(ps, p.path, p.id)); ps.value = i?.parent_id || parent?.id || '';
  const bs = $('node-base'); bs.replaceChildren(); option(bs, '未绑定', ''); bases.forEach(b => option(bs, b.name, b.id)); bs.value = i?.base_id || parent?.base_id || '';
  const ts = $('node-tool'); ts.replaceChildren(); option(ts, '未关联工具', ''); tools.forEach(t => option(ts, `${t.name} · ${t.enabled ? '启用' : '停用'}`, t.id)); ts.value = i?.tool_id || '';
  $('node-name').value = i?.name || ''; $('node-code').value = i?.code || ''; $('node-kind').value = i?.kind || parent?.kind || 'SYSTEM'; $('node-description').value = i?.description || '';
  $('node-mode').value=i?.match_mode || 'exact'; $('node-family').value=i?.interaction_kind || ''; $('node-keywords').value=(i?.keywords || []).join('\n');
  $('node-examples').value = i?.examples.join('\n') || ''; $('node-response').value = i?.response_text || ''; $('node-topk').value = i?.top_k || ''; $('node-order').value = i?.sort_order || 0; $('node-enabled').checked = i?.enabled ?? true; $('intent-dialog').showModal();
}
$('intent-create').onclick = () => openIntent(); $('intent-refresh').onclick = () => run(loadIntents);
$('tree-tab').onclick = treeTab; $('list-tab').onclick = () => selectIntentView('list');
for (const id of ['intent-search', 'intent-level', 'intent-kind', 'intent-enabled', 'intent-parent']) $(id).addEventListener('input', () => { intentPage = 1; renderIntents(); });
$('intent-clear').onclick = () => { for (const id of ['intent-search', 'intent-level', 'intent-kind', 'intent-enabled', 'intent-parent']) $(id).value = ''; intentPage = 1; renderIntents(); };
$('intent-prev').onclick = () => { intentPage--; renderIntents(); }; $('intent-next').onclick = () => { intentPage++; renderIntents(); };
$('intent-form').onsubmit = event => { event.preventDefault(); run(async () => {
  $('intent-error').textContent = ''; const body = { name: $('node-name').value.trim(), code: $('node-code').value.trim(), parent_id: $('node-parent').value || null, kind: $('node-kind').value, base_id: $('node-base').value || null, tool_id: $('node-tool').value || null, description: $('node-description').value, examples: $('node-examples').value.split('\n').map(s => s.trim()).filter(Boolean), response_text: $('node-response').value, top_k: $('node-topk').value ? Number($('node-topk').value) : null, sort_order: Number($('node-order').value), enabled: $('node-enabled').checked, match_mode: $('node-mode').value, interaction_kind: $('node-family').value || null, keywords: $('node-keywords').value.split('\n').map(s=>s.trim()).filter(Boolean), expected_revision: editing?.revision || null };
  const value = await api(editing ? `/api/admin/intents/${encoded(editing.id)}` : '/api/admin/intents', json(editing ? 'PUT' : 'POST', body)); selected = value.id; $('intent-dialog').close(); await loadIntents(); tell('意图配置已保存，后续新任务立即使用。');
}, 'intent-error'); };
$('tool-create').onclick = () => { $('tool-form').reset(); $('tool-schema').value = '{"type":"object","properties":{},"required":[]}'; $('tool-error').textContent = ''; $('tool-dialog').showModal(); };
$('tool-form').onsubmit = event => { event.preventDefault(); run(async () => {
  let schema; try { schema = JSON.parse($('tool-schema').value); } catch { $('tool-error').textContent = '参数Schema必须是合法JSON'; return; }
  const id = $('tool-id').value, old = tools.find(t => t.id === id);
  await api('/api/admin/mcp-tools', json('POST', { id, name: $('tool-name').value.trim(), endpoint: $('tool-endpoint').value, tool_name: $('tool-remote').value, auth_env: $('tool-auth').value || null, input_schema: schema, protocol_version: '2025-11-25', read_only: true, enabled: $('tool-enabled').checked, expected_revision: old?.revision || null }));
  $('tool-dialog').close(); await loadIntents(); tell('工具配置已保存，没有发起远端连接。请创建绑定知识库的MCP意图后验证。');
}, 'tool-error'); };

async function loadTraces() {
  const version = ++generation, q = $('trace-search').value.trim(), status = $('trace-status').value;
  const data = await api(`/api/admin/traces?q=${encoded(q)}&status=${encoded(status)}&page=${tracePage}`); if (version !== generation) return;
  traceTotal = data.total; const s = data.stats;
  stats($('trace-stats'), [['成功 / 失败 / 运行中', `${s.succeeded} / ${s.failed} / ${s.running}`], ['成功率', s.success_rate === null ? '—' : `${s.success_rate}%`], ['平均耗时', duration(s.average_ms)], ['P95 耗时', duration(s.p95_ms)]]);
  $('trace-window').textContent = data.window; const rows = $('trace-rows'); rows.replaceChildren();
  for (const i of data.items) { const row = traceRow(i.status); cell(row, i.name); cell(row, i.id).className = 'node-code'; cell(row, i.owner); cell(row, duration(i.elapsed_ms)); cell(row, '').append(traceStatus(i.status)); cell(row, date(i.started_at || i.created_at)); cell(row, '').append(button('查看链路 →', () => showTrace(i.id))); rows.append(row); }
  if (!data.items.length) { const row = node('tr'), c = cell(row, '暂无匹配运行记录'); c.colSpan = 7; rows.append(row); }
  $('trace-count').textContent = `共 ${data.total} 条`; $('trace-page').textContent = `${tracePage} / ${Math.max(1, Math.ceil(data.total / 20))}`; $('trace-prev').disabled = tracePage === 1; $('trace-next').disabled = tracePage * 20 >= data.total;
}
async function showTrace(id) {
  const data = await api(`/api/admin/traces/${encoded(id)}`); $('trace-list').hidden = true; $('trace-detail-view').hidden = false;
  const root = $('trace-detail'); root.replaceChildren(); const title = node('div', '', 'trace-detail-title'); title.append(node('h2', data.name), traceStatus(data.status)); root.append(title);
  const meta = node('div', '', 'trace-meta'); meta.append(node('span', `# ${data.id}`), node('span', date(data.started_at)), node('span', data.owner), node('strong', `总耗时 ${duration(data.elapsed_ms)}`)); root.append(meta);
  if (data.error_code) root.append(node('p', `本次任务未成功 · ${data.error_code}`, 'trace-error-summary'));
  if (data.diagnostics?.length) {
    const details = node('details', '', 'trace-diagnostics'); details.append(node('summary', '检索与校验诊断'));
    data.diagnostics.forEach(d => {
      const p = d.location || {}, group = p.category === 'conclusion' ? '结论' : p.category === 'explanation' ? '依据' : '论断';
      const index = Number.isInteger(p.ordinal) ? p.ordinal + 1 : Number.isInteger(p.fact_index) ? p.fact_index + 1 : null;
      const text = d.kind === 'retrieval' ? `第 ${d.attempt} 次：装入 ${d.admitted_count} 项证据，${d.context_chars} 字符，预算舍弃 ${d.drop_count} 项。` :
        `第 ${d.attempt} 次：${group}${index ? `第 ${index} 条` : ''}校验失败 · ${d.reason}${p.numeric_count ? ` · 涉及 ${p.numeric_count} 个数值` : ''}`;
      details.append(node('p', text));
      if(d.slot_counts)details.append(node('p',`证据槽位 ${d.slot_counts.total} 个，未装入 ${d.slot_counts.unfilled} 个。槽位命中不代表语义正确率。`));
      if(d.math_evidence?.total||d.math_evidence?.blocked_count)details.append(node('p',`数学证据组 ${d.math_evidence.total} 个，未完整装入 ${d.math_evidence.incomplete_count} 个；不可读数学片段 ${d.math_evidence.blocked_count} 个。公式与正文保留各自引用，公式语义尚未自动认证。`));
      if(d.coverage)details.append(node('p',`阅读范围：${d.coverage.route==='complete_indexed_text'?'全部索引原文已装入':'仅本轮入选证据'} · ${d.coverage.admitted_chunk_count}/${d.coverage.indexed_chunk_count} 块`));
      if(d.sources?.length)details.append(node('p',`源文诊断：${d.sources.length} 份解析版本；缺定位 ${d.sources.reduce((n,s)=>n+s.missing_location_count,0)} 块；异常替代字符 ${d.sources.reduce((n,s)=>n+s.replacement_chars,0)} 个。OCR 与公式语义仍须核对。`));
    }); root.append(details);
  }
  if (data.intent) { const info=node('section','','panel'); info.append(node('h2','意图决策'),node('p',`${data.intent.name} · ${data.intent.code}`),node('p',`${data.intent.reason} · ${data.intent.scope === 'document' ? '文档会话' : '知识库会话'}`),node('small',`规则版本 ${data.intent.version} · 配置 ${data.intent.configuration.slice(0,12)}`)); root.append(info); }
  if(data.query_plan){const details=node('details','','panel');details.append(node('summary',`查询计划 · ${data.query_plan.name}`));data.query_plan.effects.forEach(effect=>details.append(node('p',effect)));if(data.query_plan.required_evidence.length)details.append(node('p',`所需证据：${data.query_plan.required_evidence.join('、')}`));details.append(node('small',`${data.query_plan.version} · 本地规则规划，新增模型调用 0 次；权限范围沿用当前资料选择。`));root.append(details);}
  const panel = node('section', '', 'panel'); panel.append(node('h2', '执行时序'));
  const scroll = node('div', '', 'table-scroll'), table = node('table', '', 'trace-grid'), head = node('thead'), header = node('tr'); ['节点', '类型', '时间线', '耗时 / 起点'].forEach(t => header.append(node('th', t))); head.append(header); table.append(head);
  const body = node('tbody'), byId = new Map(data.spans.map(s => [s.id, s]));
  const windowMs = Math.max(1, data.elapsed_ms || 0, ...data.spans.map(s => (s.offset_ms || 0) + (s.elapsed_ms || 0)));
  for (const s of data.spans) {
    const row = traceRow(s.status), name = node('span', s.name, 'span-name'); let depth = 0, p = s.parent_id; const seen = new Set([s.id]);
    while (p && byId.has(p) && !seen.has(p)) { seen.add(p); depth++; p = byId.get(p).parent_id; } name.style.setProperty('--depth', String(Math.min(depth, 6))); name.append(traceStatus(s.status)); cell(row, '').append(name); cell(row, s.kind);
    const track = node('div', '', 'trace-track'), bar = node('div', '', `trace-bar ${s.status}`); bar.style.left = `${Math.max(0, Math.min(100, 100 * (s.offset_ms || 0) / windowMs))}%`; bar.style.width = `${Math.max(0, Math.min(100, 100 * (s.elapsed_ms || 0) / windowMs))}%`; bar.title = `${statusNames[s.status] || s.status} · ${duration(s.elapsed_ms)}`; track.append(bar); cell(row, '').append(track);
    const t = cell(row, duration(s.elapsed_ms)); t.className = 'span-time'; t.append(node('p', `@ ${duration(s.offset_ms)}`, 'muted-detail')); if (s.error_code) t.append(node('small', s.error_code, 'form-error')); body.append(row);
  }
  if (!data.spans.length) { const row = node('tr'), c = cell(row, '任务尚未执行，暂无节点'); c.colSpan = 4; body.append(row); }
  table.append(body); scroll.append(table); panel.append(scroll, node('p', data.timing_note, 'footnote')); root.append(panel, node('p', data.content_policy, 'footnote'));
  const url = new URL(location.href); url.searchParams.set('trace', id); history.replaceState(null, '', url);
}
$('trace-query').onclick = () => run(async () => { tracePage = 1; await loadTraces(); }); $('trace-search').onkeydown = e => { if (e.key === 'Enter') $('trace-query').click(); };
$('trace-refresh').onclick = () => run(() => $('trace-detail-view').hidden ? loadTraces() : showTrace(new URLSearchParams(location.search).get('trace')));
$('trace-back').onclick = () => run(async () => { $('trace-list').hidden = false; $('trace-detail-view').hidden = true; history.replaceState(null, '', '/admin/traces'); await loadTraces(); });
$('trace-prev').onclick = () => run(async () => { tracePage--; await loadTraces(); }); $('trace-next').onclick = () => run(async () => { tracePage++; await loadTraces(); });

function joined(edit) { const byId = new Map(chunkData.chunks.map(c => [c.id, c])); return edit.chunk_ids.map(id => byId.get(id).text).join('\n'); }
function chunkText(edit) { return joined(edit).slice(edit.start, edit.end); }
function resetChunks() { edits = chunkData.chunks.map(c => ({ chunk_ids: [c.id], start: 0, end: c.text.length })); selectedChunk = 0; renderChunks(); }
async function loadChunks(version = null) {
  const id = $('chunk-document').value; if (!id) return;
  chunkData = await api(`/api/admin/knowledge/documents/${encoded(id)}/chunks${version ? `?version_id=${encoded(version)}` : ''}`);
  const vs = $('chunk-version'); vs.replaceChildren(); chunkData.versions.forEach(v => option(vs, `${date(v.created_at)} · ${v.parser_name} · ${v.id === chunkData.document.active_parse_version_id ? '当前草稿' : '历史版本'}`, v.id)); vs.value = chunkData.version.id;
  resetChunks();
}
function renderChunks() {
  const list = $('chunk-list'); list.replaceChildren(); $('chunk-count').textContent = edits.length;
  edits.forEach((edit, index) => { const text = chunkText(edit), b = button('', () => { selectedChunk = index; renderChunks(); }, `chunk-item ${index === selectedChunk ? 'selected' : ''}`); b.append(node('strong', `片段 ${index + 1}`), node('small', `${text.length} 字符 · ${edit.chunk_ids.length} 个原始片段`), node('span', text.slice(0, 100) + (text.length > 100 ? '…' : ''))); list.append(b); });
  const edit = edits[selectedChunk]; $('chunk-text').textContent = edit ? chunkText(edit) : ''; $('chunk-source').textContent = edit ? [...new Set(chunkData.chunks.filter(c => edit.chunk_ids.includes(c.id)).flatMap(c => c.sources.map(locationText)))].join(' / ') : '';
  $('chunk-save').disabled = chunkData.version.id !== chunkData.document.active_parse_version_id; $('split-at').max = edit ? chunkText(edit).length - 1 : 0;
}
$('chunk-split').onclick = () => { const edit = edits[selectedChunk], offset = Number($('split-at').value); if (!edit || !Number.isInteger(offset) || offset <= 0 || offset >= chunkText(edit).length) { tell('拆分位置应在当前原文范围内', 'error'); return; } const at = edit.start + offset; edits.splice(selectedChunk, 1, { ...edit, end: at }, { ...edit, start: at }); renderChunks(); };
$('chunk-merge').onclick = () => {
  const a = edits[selectedChunk], b = edits[selectedChunk + 1]; if (!b) { tell('已是最后一个分块'); return; }
  if (JSON.stringify(a.chunk_ids) === JSON.stringify(b.chunk_ids) && a.end === b.start) edits.splice(selectedChunk, 2, { ...a, end: b.end });
  else if (a.start === 0 && a.end === joined(a).length && b.start === 0 && b.end === joined(b).length) { const ids = [...a.chunk_ids, ...b.chunk_ids]; const edit = { chunk_ids: ids, start: 0, end: 0 }; edit.end = joined(edit).length; edits.splice(selectedChunk, 2, edit); }
  else { tell('请先合并同一原始片段的拆分部分，再跨原块合并。', 'error'); return; } renderChunks();
};
$('chunk-save').onclick = () => run(async () => { const reason = $('chunk-reason').value.trim(); if (!reason) { tell('请填写修改原因', 'error'); return; } const result = await api(`/api/admin/knowledge/documents/${encoded(chunkData.document.id)}/chunk-revisions`, json('POST', { version_id: chunkData.version.id, edits, reason })); chunkData = result; await loadChunks(result.version.id); tell('新草稿已保存。重新建立索引并发布后，客户才使用新版。'); });
$('chunk-reset').onclick = () => { if (chunkData) resetChunks(); }; $('chunk-document').onchange = () => run(() => loadChunks()); $('chunk-version').onchange = () => run(() => loadChunks($('chunk-version').value)); $('chunk-refresh').onclick = () => run(() => loadChunks($('chunk-version').value));
$('chunk-index').onclick = () => run(async () => { const storage = `docqa.index.${chunkData.document.id}.${chunkData.document.active_parse_version_id}`; let key = sessionStorage.getItem(storage); if (!key) { key = crypto.randomUUID(); sessionStorage.setItem(storage,key); } const job = await api(`/api/admin/knowledge/documents/${encoded(chunkData.document.id)}/index-tasks`, json('POST', { request_key: key, rebuild: false })); chunkJob = job.id; await pollChunkJob(); });
async function pollChunkJob() {
  if (!chunkJob) return; const job = await api(`/api/kb-tasks/${encoded(chunkJob)}`); $('chunk-job').textContent = `${statusNames[job.status] || job.status} · 已用 ${job.requests_used} / ${job.max_requests} 次请求${job.error_message ? ` · ${job.error_message}` : ''}`;
  clearTimeout(timer); if (['queued', 'running'].includes(job.status) && !document.hidden) timer = setTimeout(() => run(pollChunkJob), 2000);
}
window.addEventListener('pagehide', () => clearTimeout(timer));
try {
  await window.docqaAuthReady;
  if (moduleName === 'intents') { await loadIntents(); selectIntentView(new URLSearchParams(location.search).get('view')); }
  if (moduleName === 'traces') { await loadTraces(); const trace = new URLSearchParams(location.search).get('trace'); if (trace) await showTrace(trace); }
  if (moduleName === 'chunks') { const documents = await api('/api/documents'); documents.filter(d => d.active_parse_version_id).forEach(d => option($('chunk-document'), d.filename, d.id)); const id = new URLSearchParams(location.search).get('document'); if (id) $('chunk-document').value = id; await loadChunks(); }
} catch (error) { tell(error.message || '页面加载失败，请刷新重试', 'error'); }
