import { encoded } from './api-client.js';

export const activeStatuses = ['queued', 'running', 'cancel_requested'];
export const jobLabels = { queued: '正在排队', running: '正在处理', succeeded: '已完成',
  cancelled: '已取消', failed: '处理失败', needs_attention: '需要确认', cancel_requested: '正在停止' };
export const stageLabels = { queued: '等待处理', preparing: '准备资料', retrieving: '查找资料',
  retrieval: '查找资料', evidence_ready: '资料已就绪', generating: '生成回答', validating: '核对引用',
  saving: '保存回答', done: '已完成', failed: '已停止', cancelled: '已取消' };

// 页面控制器不拥有生成任务，只提交明确操作并读取服务端任务。刷新与恢复只有 GET。
export function createQuestionClient({ api, storage, changed = () => {}, uuid = () => crypto.randomUUID(),
  schedule = setTimeout, unschedule = clearTimeout, isVisible = () => true }) {
  const state = { documents: [], documentId: null, index: null, configured: false, loading: true,
    jobs: [], job: null, result: null, question: '', mutating: false, pending: null,
    notice: '', noticeKind: 'info', pollFailures: 0 };
  let revision = 0, timer = null;
  const emit = () => changed(state);
  const key = id => `docqa.question.${id}`; // 与已有工作台共用恢复键，切换入口也不能换键重发。
  function pendingFor(id) {
    try {
      const item = JSON.parse(storage.getItem(key(id)) || 'null');
      return item && typeof item.question === 'string' && typeof item.idempotency_key === 'string' ? item : null;
    } catch { return null; }
  }
  function store(id, value) {
    try { storage.setItem(key(id), JSON.stringify(value)); }
    catch { throw new Error('浏览器无法保存恢复记录，请启用会话存储后再提问'); }
    if (id === state.documentId) state.pending = value;
  }
  const stop = () => { if (timer !== null) unschedule(timer); timer = null; };
  function planPoll(delay = 2500) {
    stop();
    if (isVisible() && activeStatuses.includes(state.job?.status)) timer = schedule(() => poll(), delay);
  }
  const current = (id, token) => id === state.documentId && token === revision;
  function fail(error, fallback) {
    state.notice = (error?.status ? error.message : fallback)
      + (error?.supportId ? `（支持编号：${error.supportId}）` : '');
    state.noticeKind = 'error'; emit();
  }
  function remember(id, job, accepted = false) {
    const prior = pendingFor(id);
    if (!prior) return;
    const expected = prior.mutation === 'retry' ? job.last_retry_key : job.idempotency_key;
    if (!accepted && (prior.uncertain ? expected !== prior.idempotency_key : prior.job_id !== job.id)) return;
    store(id, { ...prior, job_id: job.id, result_id: job.result_id || null, uncertain: false, mutation: null });
  }
  async function acceptJob(job, id, token) {
    if (!current(id, token) || job.document_id !== id) return;
    state.job = job; state.question = job.question || ''; state.pollFailures = 0;
    remember(id, job);
    if (job.result_id && state.result?.id !== job.result_id) {
      const result = await api(`/api/question-results/${encoded(job.result_id)}`);
      if (!current(id, token) || state.job?.id !== job.id) return;
      if (result.document_id !== id || result.job_id !== job.id) throw new Error('回答与当前资料不一致');
      state.result = result;
    }
    emit(); planPoll();
  }
  async function select(id, preferredJob = null) {
    const token = ++revision; stop();
    state.documentId = id; state.loading = true; state.index = null; state.jobs = [];
    state.job = null; state.result = null; state.question = ''; state.notice = '';
    state.pending = id ? pendingFor(id) : null; emit();
    if (!id) { state.loading = false; emit(); return; }
    try {
      try { storage.setItem('docqa.selectedDocument', id); } catch { /* 只读选择不依赖浏览器存储。 */ }
      const [index, payload] = await Promise.all([
        api(`/api/documents/${encoded(id)}/index`), api(`/api/documents/${encoded(id)}/question-jobs`),
      ]);
      if (!current(id, token)) return;
      state.index = index; state.jobs = Array.isArray(payload) ? payload : payload.jobs || [];
      const pending = pendingFor(id);
      const known = pending && state.jobs.find(job => pending.uncertain
        ? (pending.mutation === 'retry' ? job.last_retry_key : job.idempotency_key) === pending.idempotency_key
        : job.id === pending.job_id);
      if (known) remember(id, known);
      const job = state.jobs.find(item => item.id === preferredJob) || known
        || state.jobs.find(item => activeStatuses.includes(item.status)) || state.jobs[0];
      if (job) await acceptJob(job, id, token);
    } catch (error) {
      if (current(id, token)) fail(error, '暂时无法读取资料与历史，请刷新重试；未发起生成');
    } finally { if (current(id, token)) { state.loading = false; emit(); } }
  }
  async function boot() {
    try {
      const [documents, capability] = await Promise.all([api('/api/documents'), api('/api/rag/status')]);
      state.documents = documents; state.configured = Boolean(capability.configured);
      let selected;
      try { selected = storage.getItem('docqa.selectedDocument'); } catch { /* 可继续读取文档。 */ }
      await select(documents.some(doc => doc.id === selected) ? selected : documents[0]?.id || null);
    } catch (error) { state.loading = false; fail(error, '无法读取工作台，请刷新页面重试'); }
  }
  async function open(id) {
    const doc = state.documentId, token = ++revision; stop();
    state.result = null; state.notice = ''; state.loading = true; emit();
    try { await acceptJob(await api(`/api/question-jobs/${encoded(id)}`), doc, token); }
    catch (error) { if (current(doc, token)) fail(error, '暂时无法读取问答记录，请刷新重试'); }
    finally { if (current(doc, token)) { state.loading = false; emit(); } }
  }
  async function poll() {
    stop();
    const id = state.documentId, job = state.job?.id, token = revision;
    if (!job || !isVisible()) return;
    try {
      const next = await api(`/api/question-jobs/${encoded(job)}`);
      if (!current(id, token) || state.job?.id !== job) return;
      state.notice = ''; state.noticeKind = 'info';
      await acceptJob(next, id, token);
      // 终态后同步历史，但不因列表刷新再打开另一份结果。
      if (!activeStatuses.includes(next.status)) {
        const jobs = await api(`/api/documents/${encoded(id)}/question-jobs`);
        if (current(id, token)) { state.jobs = Array.isArray(jobs) ? jobs : jobs.jobs || []; emit(); }
      }
    } catch (error) {
      if (!current(id, token) || state.job?.id !== job) return;
      state.pollFailures += 1;
      state.notice = state.pollFailures > 6 ? '自动查询已暂停，请刷新记录恢复。任务可能仍在执行，未重新生成。'
        : '暂时无法读取进度，将稍后再查。后台任务可能仍在执行，未重新生成。';
      state.noticeKind = 'error'; emit();
      if (state.pollFailures <= 6) planPoll(Math.min(15000, 2500 * 2 ** state.pollFailures));
    }
  }
  async function postPending(pending) {
    if (state.mutating) return;
    const id = state.documentId, token = ++revision; stop();
    state.mutating = true; state.notice = ''; state.question = pending.question; state.result = null; emit();
    let accepted = false;
    try {
      const route = pending.mutation === 'retry' ? `/api/question-jobs/${encoded(pending.job_id)}/retry`
        : `/api/documents/${encoded(id)}/question-jobs`;
      const payload = pending.mutation === 'retry' ? { idempotency_key: pending.idempotency_key }
        : { question: pending.question, idempotency_key: pending.idempotency_key };
      const data = await api(route, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      const job = data.job || data;
      if (!job.id || job.document_id !== id) throw new Error('提交响应不完整');
      accepted = true; remember(id, job, true);
      if (!current(id, token)) return;
      state.jobs = [job, ...state.jobs.filter(item => item.id !== job.id)];
      await acceptJob(job, id, token);
    } catch (error) {
      if (!current(id, token)) return;
      if (accepted) {
        fail(error, '任务已接收，但记录暂时无法读取。请刷新核对，不要重复提问'); planPoll();
      } else if ([400, 401, 403, 404, 409, 413, 422].includes(error.status)) {
        store(id, { ...pending, uncertain: false }); state.job = null;
        fail(error, '提交被拒绝，请检查后重新提问');
      } else {
        state.pending = pending;
        state.notice = '提交结果尚未确认。请先刷新查询已有记录；不会自动重发。确认原提交会复用请求键，可能产生费用。';
        state.noticeKind = 'error';
      }
    } finally { state.mutating = false; emit(); }
  }
  const blocked = () => state.loading || state.mutating || state.pending?.uncertain
    || state.jobs.some(job => activeStatuses.includes(job.status));
  async function submit(question) {
    if (blocked()) return;
    const text = question.trim();
    if (!state.documentId || !text || text.length > 4000) return;
    if (!state.configured || state.index?.status !== 'indexed') return;
    const pending = { question: text, idempotency_key: uuid(), job_id: null, result_id: null, uncertain: true, mutation: null };
    try { store(state.documentId, pending); }
    catch (error) { fail({ status: 1, message: error.message }, ''); return; }
    await postPending(pending);
  }
  async function retry() {
    if (state.mutating || state.pending?.uncertain) return;
    const job = state.job;
    if (!job || !['failed', 'needs_attention'].includes(job.status) || job.requests_used >= job.max_requests) return;
    const pending = { question: job.question, idempotency_key: uuid(), job_id: job.id, uncertain: true, mutation: 'retry' };
    try { store(state.documentId, pending); }
    catch (error) { fail({ status: 1, message: error.message }, ''); return; }
    await postPending(pending);
  }
  async function cancel() {
    if (state.mutating || !activeStatuses.includes(state.job?.status)) return;
    const id = state.documentId, token = revision, job = state.job.id;
    state.mutating = true; emit();
    try {
      const data = await api(`/api/question-jobs/${encoded(job)}/cancel`, { method: 'POST' });
      if (current(id, token)) await acceptJob(data.job || data, id, token);
    } catch (error) { if (current(id, token)) fail(error, '取消状态尚未确认，请刷新记录核对'); }
    finally { state.mutating = false; emit(); }
  }
  return { state, boot, select, open, poll, cancel, retry, blocked,
    refresh: () => select(state.documentId, state.job?.id),
    confirm: () => state.pending?.uncertain && !state.loading ? postPending(state.pending) : Promise.resolve(),
    submit,
    newQuestion() { if (blocked()) return; ++revision; stop(); state.job = null; state.result = null; state.question = ''; state.notice = ''; emit(); },
    visibility() { if (isVisible()) planPoll(0); else stop(); },
    dispose() { ++revision; stop(); },
  };
}
