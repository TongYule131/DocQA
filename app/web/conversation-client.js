import { encoded } from './api-client.js';

const active = job => ['queued', 'running'].includes(job?.status);
const storageKey = 'docqa.conversation.pending';
export function createConversationClient({ api, storage, changed = () => {}, uuid = () => crypto.randomUUID(),
  schedule = setTimeout, unschedule = clearTimeout, visible = () => !document.hidden }) {
  const state = { documents: [], conversations: [], conversation: null, documentId: null, configured: false,
    loading: true, mutating: false, pending: null, notice: '', index: null, failures: 0, archivedList: false, attachmentPending: null, draftMemoryEnabled: true };
  let revision = 0, timer = null;
  const emit = () => changed(state), stop = () => { if (timer !== null) unschedule(timer); timer = null; };
  const route = id => `/api/conversations/${encoded(id)}`;
  function saved() {
    try { const p = JSON.parse(storage.getItem(storageKey) || 'null');
      return p && typeof p.question === 'string' && typeof p.turn_key === 'string' && typeof p.create_key === 'string' ? p : null;
    } catch { return null; }
  }
  function persist(value) {
    if (value) storage.setItem(storageKey, JSON.stringify(value)); else storage.removeItem(storageKey);
    state.pending = value;
  }
  function error(err, text) { state.notice = (err?.status ? err.message : text) + (err?.supportId ? `（支持编号：${err.supportId}）` : ''); emit(); }
  function remember(id) { try { storage.setItem('docqa.conversation.selected', id || ''); } catch { /* 阅读不依赖存储。 */ } }
  function plan() {
    stop();
    if (visible() && (state.conversation?.turns?.some(t => active(t.job))
      || state.index?.status === 'indexing' || ['queued', 'running'].includes(state.conversation?.document?.task_status)) && state.failures <= 6)
      timer = schedule(() => refresh(), Math.min(15000, 2500 * 2 ** state.failures));
    else if (visible() && state.conversation?.attachment && state.failures <= 6) {
      const remaining = Date.parse(state.conversation.attachment.expires_at) - Date.now();
      timer = schedule(() => refresh(), Math.min(60000, Math.max(1000, remaining)));
    }
  }
  async function read(id, token) {
    const data = await api(route(id));
    if (token !== revision) return;
    if (data.id !== id) throw new Error('会话响应不一致');
    state.conversation = data; state.documentId = data.document_id; remember(id);
    const pending = saved();
    if (pending?.conversation_id === id && data.turns.some(t => t.request_key === pending.turn_key)) persist(null);
  }
  async function open(id) {
    if (state.mutating) return;
    const token = ++revision; stop(); state.loading = true; state.index = null; state.notice = ''; emit();
    try {
      await read(id, token);
      if (token !== revision) return;
      const index = await api(`/api/documents/${encoded(state.documentId)}/index`);
      if (token !== revision) return;
      state.index = index;
      state.failures = 0;
    } catch (err) { if (token === revision) { state.conversation = null; error(err, '暂时无法读取会话，未重新生成'); } }
    finally { if (token === revision) { state.loading = false; emit(); plan(); } }
  }
  async function draft(documentId = state.documentId) {
    if (state.mutating || state.pending) return;
    const token = ++revision; stop(); state.conversation = null; state.documentId = documentId;
    state.index = null; state.loading = true; state.notice = ''; remember(null); emit();
    try { if (documentId) { const index = await api(`/api/documents/${encoded(documentId)}/index`); if (token === revision) state.index = index; } }
    catch (err) { if (token === revision) error(err, '暂时无法读取资料状态'); }
    finally { if (token === revision) { state.loading = false; emit(); } }
  }
  async function refresh() {
    if (state.mutating) return;
    const token = ++revision; stop();
    try {
      const list = await api('/api/conversations' + (state.archivedList ? '?archived=true' : ''));
      if (token !== revision) return;
      state.conversations = list;
      const attachmentKey = storage.getItem('docqa.attachment.pending');
      if (attachmentKey) {
        const found = list.find(c => c.request_key === `temporary/${attachmentKey}`);
        if (found) { storage.removeItem('docqa.attachment.pending'); state.attachmentPending = null; await read(found.id, token); }
        else state.attachmentPending = attachmentKey;
      }
      const p = saved(); state.pending = p;
      if (p) {
        const conv = list.find(c => c.id === p.conversation_id || c.request_key === p.create_key);
        if (conv) { persist({ ...p, conversation_id: conv.id }); await read(conv.id, token); }
      } else if (state.conversation) await read(state.conversation.id, token);
      if (token !== revision) return;
      if (state.conversation?.attachment) state.index = await api(`/api/documents/${encoded(state.documentId)}/index`);
      if (token !== revision) return;
      state.failures = 0; state.notice = state.pending ? '原提交尚未确认，请核对后明确确认；不会自动重发。' : '';
      emit(); plan();
    } catch (err) {
      if (token !== revision) return;
      if ([401, 403, 404, 410].includes(err.status)) {
        state.conversation = null; state.index = null; remember(null);
      }
      state.failures++; error(err, state.failures > 6 ? '自动查询已暂停，请刷新记录。未重新生成。' : '暂时无法读取进度，后台可能仍在处理；未重新生成。'); plan();
    }
  }
  async function boot() {
    try {
      const [documents, status, list] = await Promise.all([api('/api/documents'), api('/api/rag/status'), api('/api/conversations')]);
      state.documents = documents; state.configured = Boolean(status.configured); state.conversations = list; state.pending = saved();
      const attachmentKey = storage.getItem('docqa.attachment.pending');
      state.attachmentPending = attachmentKey;
      const query = new URLSearchParams(globalThis.location?.search || '').get('document');
      let selected; try { selected = storage.getItem('docqa.conversation.selected'); } catch { /* 可只读打开。 */ }
      if (attachmentKey && list.some(c => c.request_key === `temporary/${attachmentKey}`)) {
        storage.removeItem('docqa.attachment.pending'); state.attachmentPending = null;
        await open(list.find(c => c.request_key === `temporary/${attachmentKey}`).id);
      } else if (state.pending) { state.loading = false; await refresh(); if (state.conversation) await open(state.conversation.id); else await draftForPending(); }
      else if (!query && list.some(c => c.id === selected)) await open(selected);
      else await draft(documents.some(d => d.id === query) ? query : documents[0]?.id || null);
    } catch (err) { state.loading = false; error(err, '无法读取会话工作台，请刷新重试'); }
  }
  async function draftForPending() {
    state.documentId = state.pending.document_id;
    try { state.index = await api(`/api/documents/${encoded(state.documentId)}/index`); } catch { /* 明确确认时服务端仍会校验权限。 */ }
    state.loading = false; emit();
  }
  const blocked = () => state.loading || state.mutating || Boolean(state.pending) || Boolean(state.conversation?.archived)
    || Boolean(state.attachmentPending)
    || state.conversation?.turns?.some(t => active(t.job));
  async function post(pending) {
    if (state.mutating) return;
    const token = ++revision; stop(); state.mutating = true; state.notice = ''; emit();
    let accepted = false;
    try {
      let identifier = pending.conversation_id;
      if (!identifier) {
        const created = await api('/api/conversations', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ document_id: pending.document_id, idempotency_key: pending.create_key }) });
        identifier = created.id;
        if (!identifier || created.document_id !== pending.document_id) throw new Error('会话响应不完整');
        pending = { ...pending, conversation_id: identifier }; persist(pending); remember(identifier);
      }
      const current = await api(route(identifier));
      if (!current.turns.some(t => t.request_key === pending.turn_key)) {
        if (pending.new_conversation && pending.memory_enabled === false && current.memory?.enabled !== false) {
          await api(route(identifier), { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ expected_revision: current.revision, memory_enabled: false }) });
        }
        await api(`${route(identifier)}/turns`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: pending.question, idempotency_key: pending.turn_key }) });
      }
      accepted = true; persist(null); await read(identifier, token);
      const list = await api('/api/conversations' + (state.archivedList ? '?archived=true' : '')); if (token === revision) state.conversations = list;
    } catch (err) {
      if (token !== revision) return;
      if ([400, 401, 403, 404, 409, 413, 422].includes(err.status)) {
        persist(null); error(err, '提交被拒绝，请刷新后检查');
      } else error(err, accepted ? '任务已接收，记录暂不可读。请刷新核对，不要重复提问。' : '提交结果尚未确认，请先刷新记录；不会自动重新提交。');
    } finally { state.mutating = false; emit(); plan(); }
  }
  async function submit(question) {
    const text = question.trim();
    if (blocked() || !text || text.length > 4000 || !state.documentId || !state.configured || state.index?.status !== 'indexed') return;
    const p = { document_id: state.documentId, conversation_id: state.conversation?.id || null,
      create_key: uuid(), turn_key: uuid(), question: text, new_conversation: !state.conversation,
      memory_enabled: state.conversation ? Boolean(state.conversation.memory?.enabled) : state.draftMemoryEnabled };
    try { persist(p); } catch { error(null, '浏览器无法保存恢复记录，请启用会话存储后再提问'); return; }
    await post(p);
  }
  async function update(fields) {
    if (!state.conversation || state.mutating || state.pending) return;
    const token = revision, id = state.conversation.id; state.mutating = true; emit();
    try {
      const data = await api(route(id), { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: state.conversation.revision, ...fields }) });
      if (token === revision) state.conversation = data;
    } catch (err) { if (token === revision) error(err, '操作结果尚未确认，请刷新核对；未自动重发'); }
    finally { state.mutating = false; emit(); await refresh(); }
  }
  async function cancel(jobId) {
    if (state.mutating || !state.conversation?.turns?.some(t => t.job.id === jobId)) return;
    state.mutating = true; emit();
    try { await api(`/api/question-jobs/${encoded(jobId)}/cancel`, { method: 'POST' }); }
    catch (err) { error(err, '停止状态尚未确认，请刷新核对'); }
    finally { state.mutating = false; await refresh(); }
  }
  async function upload(file) {
    if (!file || state.loading || state.mutating || state.pending || state.conversation) return;
    let key;
    try { key = storage.getItem('docqa.attachment.pending') || uuid(); storage.setItem('docqa.attachment.pending', key); }
    catch { error(null, '无法保存上传恢复记录，请启用会话存储后再上传'); return; }
    state.mutating = true; state.attachmentPending = key; state.notice = ''; emit();
    try {
      const form = new FormData(); form.append('file', file);
      const result = await api('/api/conversations/temporary', { method: 'POST', headers: { 'Idempotency-Key': key }, body: form });
      storage.removeItem('docqa.attachment.pending'); state.attachmentPending = null;
      state.mutating = false; await open(result.id); await refresh();
    } catch (err) {
      if ([400, 403, 409, 410, 413, 422].includes(err.status)) { storage.removeItem('docqa.attachment.pending'); state.attachmentPending = null; }
      error(err, '上传结果未确认，请先刷新；没有记录时重新选择原文件，使用原请求键确认，不会自动重传。');
    } finally { state.mutating = false; emit(); }
  }
  async function prepareAttachment(kind) {
    if (blocked() || !state.conversation?.attachment || !['parse', 'index', 'remove'].includes(kind)) return;
    const c = state.conversation;
    if (kind === 'index' && (state.index?.status === 'indexed' || state.index?.status === 'indexing')) return;
    state.mutating = true; state.notice = ''; emit();
    try {
      const keyName = `docqa.attachment.parse.${c.id}`;
      const key = storage.getItem(keyName) || uuid(); if (kind === 'parse') storage.setItem(keyName, key);
      await api(`${route(c.id)}/attachment${kind === 'remove' ? '' : '/' + kind}`, {
        method: kind === 'remove' ? 'DELETE' : 'POST', headers: kind === 'parse' ? { 'Idempotency-Key': key } : {} });
      if (kind === 'remove') { state.conversation = null; state.documentId = null; state.index = null; remember(null); }
    } catch (err) { error(err, '操作结果未确认，请刷新核对；未自动重试。建索引在途请求可能已经计费。'); }
    finally { state.mutating = false; await refresh(); }
  }
  return { state, boot, draft, open, refresh, submit, update, cancel, blocked,
    setDraftMemory(enabled) { if (!state.conversation && !blocked()) { state.draftMemoryEnabled = Boolean(enabled); emit(); } },
    upload, prepareAttachment,
    archived(value) { state.archivedList = value; return refresh(); },
    confirm: () => state.pending && !state.loading ? post(state.pending) : Promise.resolve(),
    visibility() { if (visible()) plan(); else stop(); }, dispose() { ++revision; stop(); } };
}
