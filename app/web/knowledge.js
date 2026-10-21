import { createApiClient, encoded } from './api-client.js';
import { node } from './answer-view.js';

const $ = id => document.getElementById(id), api = createApiClient();
let bases = [], documents = [], users = [], detail = null, baseId = null, busy = false, limit = 20, configured = false;
let indexJobs = [];
let timer = null, revision = 0, uploadRecord = null, pollFailures = 0;
const uploadStorageKey = 'docqa.pendingUpload';
function readUpload() { try { return JSON.parse(sessionStorage.getItem(uploadStorageKey) || 'null'); } catch { return null; } }
const active = document => ['queued', 'running'].includes(document.task_status);
function tell(text, kind = 'info') { $('notice').hidden = !text; $('notice').textContent = text; $('notice').dataset.kind = kind; }
function json(method, body) { return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }; }
function disable() {
  document.querySelectorAll('button,input,select').forEach(item => { item.disabled = busy; });
  $('publish').disabled = busy || !detail?.documents.length;
  document.querySelectorAll('[data-build]').forEach(button => { button.disabled = busy || !configured || button.dataset.ready !== 'true'; });
  document.querySelectorAll('[data-parse]').forEach(button => { button.disabled = busy || button.dataset.ready !== 'true'; });
}
async function action(work) {
  if (busy) return;
  busy = true; disable(); tell('');
  try { await work(); }
  catch (error) { tell((error.name === 'AbortError' || error instanceof TypeError ? '连接中断或等待超时。操作可能仍在执行，请先刷新核对，未自动重试。' : error.message) + (error.supportId ? `（支持编号：${error.supportId}）` : ''), 'error'); }
  finally { busy = false; disable(); }
}
function stopPoll() { if (timer !== null) clearTimeout(timer); timer = null; }
function schedulePoll() {
  stopPoll();
  if (!document.hidden && pollFailures < 5 && (documents.some(active) || indexJobs.some(j => ['queued','running'].includes(j.status)))) timer = setTimeout(async () => {
    if (busy) { schedulePoll(); return; }
    try { await refresh(); pollFailures = 0; }
    catch { ++pollFailures; tell('暂时无法读取处理进度，请刷新核对。没有重新提交解析。', 'error'); schedulePoll(); }
  }, Math.min(2500 * (pollFailures + 1), 15000));
}
async function refresh() {
  const version = ++revision;
  const [nextBases, nextDocuments, nextUsers, nextIndexJobs] = await Promise.all([api('/api/knowledge-bases'), api('/api/documents'), api('/api/admin/users'), api('/api/admin/knowledge/index-tasks')]);
  if (version !== revision) return;
  bases = nextBases; documents = nextDocuments; users = nextUsers; indexJobs = nextIndexJobs;
  if (baseId && !bases.some(base => base.id === baseId)) baseId = null;
  detail = baseId ? await api(`/api/knowledge-bases/${encoded(baseId)}`) : null;
  if (version !== revision) return;
  render(); schedulePoll();
}
async function choose(id) { baseId = id; sessionStorage.setItem('docqa.selectedBase', id || ''); await refresh(); }
function render() {
  const list = $('base-list'); list.replaceChildren();
  const inbox = node('button', '全部资料 / 收件箱', !baseId ? 'active' : ''); inbox.type = 'button'; inbox.onclick = () => action(() => choose(null)); list.append(inbox);
  for (const base of bases) {
    const button = node('button', base.name, base.id === baseId ? 'active' : ''); button.type = 'button'; button.onclick = () => action(() => choose(base.id)); list.append(button);
  }
  $('base-title').textContent = detail?.name || '资料收件箱';
  $('base-description').textContent = detail ? '这里显示待发布资料。准备完成后，检查并发布给已授权客户。' : '上传后可直接准备文档，也可将已有资料加入知识库。';
  $('base-status').textContent = detail ? detail.active_snapshot_id ? `已发布 v${detail.revision}` : '尚未发布' : '本地资料';
  const selectedDocs = detail ? detail.documents : documents;
  $('documents').replaceChildren();
  if (!selectedDocs.length) $('documents').append(node('p', detail ? '这个知识库还没有资料，请上传或加入已有文档。' : '暂无资料。上传一份文档开始。', 'muted'));
  for (const document of selectedDocs) renderDocument(document);
  $('publish-panel').hidden = !detail; $('bind-form').hidden = !detail;
  const select = $('unbound-document'); select.replaceChildren();
  const inside = new Set(detail?.documents.map(d => d.id) || []);
  for (const document of documents.filter(d => !inside.has(d.id))) select.append(new Option(document.filename, document.id));
  if (!select.options.length) select.append(new Option('暂无可加入的文档', ''));
  if (detail) {
    $('published-summary').textContent = detail.active_snapshot_id ? `当前发布 v${detail.revision} · ${detail.published_documents.length} 份资料。变更后需要重新发布。` : '尚无发布版，已授权客户也无法检索草稿。';
    const members = $('members'); members.replaceChildren();
    if (!users.filter(u => u.role === 'customer' && u.active).length) members.append(node('p', '先到后台“用户管理”创建客户，再在此分配访问权限。', 'muted'));
    for (const user of users.filter(u => u.role === 'customer' && u.active)) {
      const row = node('label', '', 'account-row'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = detail.members.includes(user.id);
      checkbox.onchange = () => action(async () => { await api(`/api/knowledge-bases/${encoded(baseId)}/members/${encoded(user.id)}`, json('PUT', { enabled: checkbox.checked })); await refresh(); tell('访问权限已更新。'); });
      row.append(checkbox, node('span', `${user.display_name}（${user.username}）`)); members.append(row);
    }
  }
  disable();
}
function renderDocument(document) {
  const card = node('article', '', 'document-card'), head = node('div', '', 'row spread');
  const labels = { uploaded: '待解析', parsing: '解析中', parsed: '已解析', failed: '解析失败' };
  head.append(node('h3', document.filename), node('span', active(document) ? '正在解析' : labels[document.status] || '待准备', 'badge'));
  card.append(head, node('p', `${document.format?.toUpperCase() || '文档'} · ${(document.size / 1024).toFixed(1)} KB${document.active_parse_version_id ? ` · ${document.chunk_count} 个片段` : ''}`, 'muted'));
  if (document.task_error_message) card.append(node('p', document.task_error_message, 'muted'));
  const controls = node('div', '', 'row');
  const parse = node('button', active(document) ? '解析已排队' : document.active_parse_version_id ? '解析已完成' : '执行分块', 'secondary');
  parse.disabled = active(document) || Boolean(document.active_parse_version_id); parse.dataset.parse = '';
  parse.dataset.ready = String(!parse.disabled);
  parse.onclick = () => action(async () => {
    const keyName = `docqa.parse.${document.id}`; let key = sessionStorage.getItem(keyName);
    if (!key) { key = crypto.randomUUID(); sessionStorage.setItem(keyName, key); }
    await api(`/api/documents/${encoded(document.id)}/parse`, json('POST', { idempotency_key: key }));
    await refresh(); tell('解析任务已提交；刷新只读取进度。扫描件和 Office 文档由现有 Docling 服务处理。');
  });
  controls.append(parse);
  const inspect = node('a', '检查原文与告警', 'button secondary'); inspect.href = `/admin/documents?document=${encoded(document.id)}`; controls.append(inspect);
  const build = node('button', '建立问答索引', 'secondary'); build.dataset.build = document.id; build.dataset.ready = String(Boolean(document.active_parse_version_id) && !active(document));
  const job = indexJobs.find(j => j.document_id === document.id);
  const isIndexActive = job && ['queued','running'].includes(job.status);
  build.onclick = () => action(async () => {
    if (job && ['failed','needs_attention'].includes(job.status)) {
      const uncertain = job.calls.some(c => ['intent','uncertain'].includes(c.status));
      if (uncertain && !confirm('原调用可能已计费。是否明确重试？预算不会重置。')) return;
      await api(`/api/kb-tasks/${encoded(job.id)}/retry`, json('POST', { acknowledge_uncertain: uncertain }));
    } else {
      const storage = `docqa.index.${document.id}.${document.active_parse_version_id}`;
      let key = sessionStorage.getItem(storage); if (!key) { key = crypto.randomUUID(); sessionStorage.setItem(storage, key); }
      await api(`/api/admin/knowledge/documents/${encoded(document.id)}/index-tasks`, json('POST', { request_key: key }));
    }
    await refresh(); tell('索引任务已提交。每批向量请求独立记账；刷新与离开页面不会重复提交。');
  }); controls.append(build);
  if (job) { const trace = node('a', '查看索引链路', 'button secondary'); trace.href = `/admin/traces?trace=${encoded(job.id)}`; controls.append(trace); card.append(node('p', `索引任务：${({ queued:'等待处理', running:'正在建立', succeeded:'完成', failed:'失败', needs_attention:'待确认', cancelled:'已停止' })[job.status]} · 请求 ${job.requests_used}/${job.max_requests}${job.error_message ? ' · '+job.error_message : ''}`, 'muted')); }
  const chunks = node('a', '分块管理', 'button secondary'); chunks.href = `/admin/chunks?document=${encoded(document.id)}`; if (document.active_parse_version_id) controls.append(chunks);
  const chat = node('a', '前往问答 →', 'button secondary'); chat.href = detail?.active_snapshot_id ? `/app/knowledge?base=${encoded(baseId)}` : `/app?document=${encoded(document.id)}`;
  chat.onclick = () => sessionStorage.setItem('docqa.selectedDocument', document.id); controls.append(chat);
  if (detail) {
    const remove = node('button', '从草稿移除', 'secondary'); remove.onclick = () => action(async () => { await api(`/api/knowledge-bases/${encoded(baseId)}/documents/${encoded(document.id)}`, { method: 'DELETE' }); await refresh(); tell('已从草稿移除；旧发布版保留，重新发布后才变更客户范围。'); }); controls.append(remove);
  }
  card.append(controls); $('documents').append(card);
  // 索引状态独立读取；即使服务端仍在构建，也绝不自动重发收费 POST。
  const seq = revision;
  api(`/api/documents/${encoded(document.id)}/index`).then(info => {
    if (revision !== seq || !card.isConnected) return;
    const ready = info.status === 'indexed' && info.matches_active_version;
    build.textContent = isIndexActive ? '索引已排队 / 处理中' : job && ['failed','needs_attention'].includes(job.status) ? '复用检查点重试索引' : ready ? '索引已就绪' : info.status === 'indexing' ? '正在建立索引' : '建立问答索引';
    build.dataset.ready = String(Boolean(document.active_parse_version_id) && !active(document) && !ready && !isIndexActive && info.status !== 'indexing');
    build.disabled = busy || !configured || build.dataset.ready !== 'true';
    if (ready) { head.lastChild.textContent = '可以问答'; head.lastChild.classList.add('ready'); }
  }).catch(() => { if (card.isConnected) build.textContent = '索引状态未知，请刷新'; build.dataset.ready = 'false'; build.disabled = true; });
}
$('base-form').onsubmit = event => { event.preventDefault(); action(async () => { const base = await api('/api/knowledge-bases', json('POST', { name: $('base-name').value.trim() })); $('base-name').value = ''; await choose(base.id); tell('知识库已创建，尚未向客户发布。'); }); };
$('bind-form').onsubmit = event => { event.preventDefault(); if ($('unbound-document').value) action(async () => { await api(`/api/knowledge-bases/${encoded(baseId)}/documents`, json('POST', { document_id: $('unbound-document').value })); await refresh(); tell('资料已加入草稿。'); }); };
$('publish').onclick = () => action(async () => { await api(`/api/knowledge-bases/${encoded(baseId)}/publish`, json('POST', { expected_revision: detail.revision })); await refresh(); tell('知识库已发布，获授权客户可使用当前版本。'); });
$('refresh').onclick = () => action(async () => { pollFailures = 0; await refresh(); tell('已读取最新状态，没有重新执行任务。'); });
$('upload-file').onchange = () => {
  const file = $('upload-file').files[0], prior = readUpload();
  uploadRecord = file && prior && prior.name === file.name && prior.size === file.size && prior.modified === file.lastModified ? prior : null;
  $('upload-status').textContent = uploadRecord ? '将复用上次上传请求键；已保存的原件不会重复建立。' : '';
  $('upload').textContent = uploadRecord ? '确认原上传' : '上传文档';
};
$('upload-form').onsubmit = event => {
  event.preventDefault(); const file = $('upload-file').files[0]; if (!file) return;
  action(async () => {
    if (file.size > limit * 1024 * 1024) throw new Error(`文件超过 ${limit} MB 上限，请选择较小文件。`);
    if (!uploadRecord) uploadRecord = { key: crypto.randomUUID(), baseId, name: file.name, size: file.size, modified: file.lastModified };
    // 请求前保存恢复键。浏览器不能持久化 File；刷新后重新选择同一原件仍可安全确认。
    sessionStorage.setItem(uploadStorageKey, JSON.stringify(uploadRecord));
    const data = new FormData(); data.append('file', file);
    $('upload-status').textContent = '正在上传，请勿重复操作…';
    try {
      const uploadPath = uploadRecord.baseId ? `/api/admin/knowledge/bases/${encoded(uploadRecord.baseId)}/documents` : '/api/documents';
      const result = await api(uploadPath, { method: 'POST', headers: { 'Idempotency-Key': uploadRecord.key }, body: data, timeoutMs: 120000 });
      const identifier = result.document.id;
      sessionStorage.setItem('docqa.selectedDocument', identifier); $('upload-form').reset(); uploadRecord = null; sessionStorage.removeItem(uploadStorageKey);
      $('upload-status').textContent = '上传成功'; $('upload').textContent = '上传文档'; await refresh(); tell('原件已保存。下一步请解析文档，核对原文后建立问答索引。');
    } catch (error) { $('upload-status').textContent = '上传未确认；可刷新收件箱核对，或复用原请求键确认。'; $('upload').textContent = '确认原上传'; throw error; }
  });
};
document.addEventListener('visibilitychange', () => { if (document.hidden) stopPoll(); else schedulePoll(); });
window.addEventListener('pagehide', stopPoll);
try {
  await window.docqaAuthReady;
  const capability = await api('/api/capabilities'); limit = capability.max_upload_mb || 20; configured = capability.embedding_configured;
  $('upload-help').textContent = `支持 PDF、DOCX、XLSX、UTF-8 TXT / Markdown，单文件最多 ${limit} MB。上传只保存原件，解析与建立索引分别操作。`;
  const params = new URLSearchParams(location.search);
  baseId = params.has('inbox') ? null : params.get('base') || sessionStorage.getItem('docqa.selectedBase') || null; await refresh();
  const pending = readUpload();
  if (pending) tell(`上次“${pending.name}”上传尚未确认。请先在收件箱核对，或重新选择同一文件后确认原上传；不会自动重发。`);
} catch (error) { tell(error.message || '无法读取管理工作区，请刷新。', 'error'); }
