import { createApiClient, encoded } from './api-client.js';
import { createQuestionClient, activeStatuses, jobLabels, stageLabels } from './question-client.js';
import { node, renderAnswer, sourceLocationsText, answerTextForDisplay } from './answer-view.js';
import { createAnswerStream } from './answer-stream.js';

const $ = id => document.getElementById(id), api = createApiClient();
let renderedResult = null, renderedQuestion = '', sourceSequence = 0, lastSourceFocus = null;
let historySignature = '', documentSignature = '', previousDocument = null;
const controller = createQuestionClient({ api, storage: sessionStorage, changed: paint,
  isVisible: () => !document.hidden });
const stream=createAnswerStream({changed:()=>paint(controller.state),finished:()=>controller.refresh()});

function announce(text, kind = 'info') {
  $('notice').textContent = text; $('notice').hidden = !text; $('notice').dataset.kind = kind;
}
function closeSources(restoreFocus = true) {
  ++sourceSequence; $('source-drawer').hidden = true;
  if (restoreFocus) lastSourceFocus?.focus();
}
function paint(state) {
  if (previousDocument !== state.documentId) {
    previousDocument = state.documentId; closeSources(false); renderedResult = null;
    $('question-input').value = ''; $('question-input').style.height = '84px'; $('input-count').textContent = '0 / 4000';
    $('history-search').value = ''; $('answer-body').replaceChildren();
  }
  const doc = state.documents.find(item => item.id === state.documentId);
  const signature = JSON.stringify(state.documents.map(item => [item.id, item.filename]));
  if (documentSignature !== signature) {
    documentSignature = signature; $('document-picker').replaceChildren();
    if (!state.documents.length) $('document-picker').append(new Option('暂无资料', ''));
    state.documents.forEach(item => $('document-picker').append(new Option(item.filename, item.id)));
  }
  $('document-picker').value = state.documentId || '';
  $('workspace-title').textContent = doc?.filename || '文档问答';
  $('scope-label').textContent = doc ? `回答范围：${doc.filename}` : '尚未选择资料';
  $('tools-link').href = $('composer-tools').href = '/tools' + (doc ? `?document=${encoded(doc.id)}` : '');
  const isCustomer = window.docqaSession?.team_mode && window.docqaSession?.user?.role === 'customer';
  const readiness = !doc ? isCustomer ? '暂无可用资料，请联系管理员分配知识库' : '暂无资料，请上传并准备文档'
    : state.loading ? '正在读取资料与历史…' : !state.configured ? '问答服务尚未配置，请联系管理员'
    : state.index?.status !== 'indexed' ? isCustomer ? '资料正在更新，请联系管理员确认发布状态' : '资料尚未准备完成，请上传或检查解析与索引' : '资料已准备，可以提问';
  $('document-hint').textContent = readiness;
  $('welcome-description').textContent = doc ? `${readiness}。每次提问独立处理，事实依据来自本次检索原文。`
    : isCustomer ? '管理员分配知识库后，即可选择其中的资料提问。' : '点击左侧“上传 / 准备文档”添加资料。解析、检查并建立索引后即可提问。';
  const uncertain = Boolean(state.pending?.uncertain);
  const blocked = controller.blocked();
  $('question-input').disabled = !doc || state.loading || state.mutating || uncertain || !state.configured || state.index?.status !== 'indexed';
  $('send-question').disabled = blocked || $('question-input').disabled || !$('question-input').value.trim();
  $('new-question').disabled = blocked; $('refresh-history').disabled = state.mutating || state.loading;
  $('cancel-question').hidden = !activeStatuses.includes(state.job?.status);
  $('cancel-question').disabled = state.mutating || Boolean(state.job?.cancel_requested);
  $('confirm-question').hidden = !uncertain; $('confirm-question').disabled = state.mutating || state.loading;
  $('retry-question').hidden = !['failed', 'needs_attention'].includes(state.job?.status);
  $('retry-question').disabled = state.mutating || uncertain || state.job?.requests_used >= state.job?.max_requests;
  const pendingNotice = uncertain ? `提交状态尚未确认。原问题：${state.pending.question}。请先刷新查询已有记录；确认原提交会复用请求键，可能计费。` : '';
  announce(state.notice || pendingNotice, state.notice ? state.noticeKind : uncertain ? 'error' : 'info');
  const hasQuestion = Boolean(state.question);
  $('welcome').hidden = hasQuestion; $('conversation').hidden = !hasQuestion;
  if (state.question !== renderedQuestion) {
    renderedQuestion = state.question; $('user-question').textContent = state.question;
    $('answer-body').replaceChildren(); renderedResult = null; closeSources(false);
  }
  $('job-status').textContent = state.mutating && !state.job ? '正在提交' : jobLabels[state.job?.status] || '';
  const job = state.job;
  $('stage-status').textContent = job?.error_message || (uncertain ? '等待提交确认' : job?.cancel_requested
    ? '已请求停止后续处理；已在途的请求仍可能计费。' : activeStatuses.includes(job?.status)
      ? `${stageLabels[job.stage] || '正在处理'}，可稍后从历史查看。` : '');
  stream.watch(state.job?[state.job]:[]);
  if (state.result && state.result.id !== renderedResult) {
    renderAnswer($('answer-body'), state.result.answer, openSource); renderedResult = state.result.id;
  }
  if (!state.result && renderedResult !== null) { renderedResult = null; $('answer-body').replaceChildren(); closeSources(false); }
  if(!state.result){$('answer-body').replaceChildren();stream.render($('answer-body'),state.job||{},openSource);}
  $('answer-actions').hidden = !state.result;
  $('show-sources').disabled = !state.result?.answer.citations?.length;
  if (state.result) {
    for (const format of ['markdown', 'json']) $(`export-${format}`).href = `/api/question-results/${encoded(state.result.id)}/export?format=${format}`;
  }
  const historyKey = JSON.stringify([state.documentId, state.job?.id, state.jobs.map(item => [item.id, item.question, item.status])]);
  if (historySignature !== historyKey) { historySignature = historyKey; paintHistory(); }
  updateScrollControl();
}
function updateScrollControl() {
  const scroll = $('chat-scroll');
  $('return-latest').hidden = !controller.state.question || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 130;
}
function paintHistory() {
  const state = controller.state, list = $('history-list'), query = $('history-search').value.trim().toLowerCase();
  list.replaceChildren();
  const jobs = state.jobs.filter(item => String(item.question).toLowerCase().includes(query));
  if (!jobs.length) list.append(node('p', query ? '没有匹配的问答' : '当前文档暂无问答', 'small-text'));
  for (const job of jobs) {
    const button = node('button', '', 'history-item' + (state.job?.id === job.id ? ' active' : ''));
    button.type = 'button'; button.append(node('span', job.question || '问答记录'), node('small', jobLabels[job.status] || '查看记录'));
    button.onclick = () => { controller.open(job.id); closeMenu(); }; list.append(button);
  }
}
function openSource(citation, focus) {
  const sequence = ++sourceSequence; lastSourceFocus = focus; $('source-drawer').hidden = false;
  const container = $('source-content'); container.replaceChildren();
  const doc = controller.state.documents.find(item => item.id === citation.document_id);
  container.append(node('h3', doc?.filename || '引用资料'), node('p', sourceLocationsText(citation.sources), 'source-location'), node('blockquote', citation.quote));
  container.append(node('p', '该原文范围可回溯，语义支持仍需结合回答核对。', 'small-text'));
  const metadata = node('details'), summary = node('summary', '来源身份与版本'); metadata.append(summary,
    node('p', `解析版本：${citation.parse_version_id || '未知'}`), node('p', `证据标识：${citation.evidence_id || citation.chunk_id || '未知'}`));
  container.append(metadata);
  const original = node('a', '打开原件核对', 'source-original');
  const page = citation.sources?.find(item => item.format === 'pdf' && Number.isInteger(item.page) && item.page > 0)?.page;
  original.href = `/api/documents/${encoded(citation.document_id)}/original` + (page ? `#page=${page}` : '');
  original.target = '_blank'; original.rel = 'noopener noreferrer'; container.append(original);
  if (citation.verification === 'exact_match' && citation.offset_basis === 'chunk_crlf_lf_unicode_codepoints') {
    const expand = node('button', '展开并高亮解析原文', 'secondary'), region = node('pre', '', 'source-excerpt'); region.hidden = true;
    expand.type = 'button'; expand.onclick = async () => {
      expand.disabled = true;
      try {
        const chunks = await api(`/api/documents/${encoded(citation.document_id)}/chunks?version_id=${encoded(citation.parse_version_id)}`);
        if (sequence !== sourceSequence) return;
        const chunk = chunks.find(item => item.id === citation.chunk_id && item.document_id === citation.document_id && item.parse_version_id === citation.parse_version_id);
        const chars = Array.from(String(chunk?.text || '').replace(/\r\n/g, '\n'));
        if (!Number.isInteger(citation.char_start) || !Number.isInteger(citation.char_end) || citation.char_start < 0 || citation.char_end > chars.length
          || citation.char_end <= citation.char_start || chars.slice(citation.char_start, citation.char_end).join('') !== citation.quote) throw new Error('范围不一致');
        region.replaceChildren(document.createTextNode(chars.slice(0, citation.char_start).join('')), node('mark', citation.quote), document.createTextNode(chars.slice(citation.char_end).join('')));
        region.hidden = false; expand.hidden = true;
      } catch { if (sequence === sourceSequence) { region.textContent = '暂时无法核对该范围，未显示高亮，请打开原件复核。'; region.hidden = false; } }
      finally { if (sequence === sourceSequence) expand.disabled = false; }
    };
    container.append(expand, region);
  }
  $('close-sources').focus();
}
function closeMenu() { $('sidebar').classList.remove('mobile-open'); $('menu-toggle').setAttribute('aria-expanded', 'false'); }
function inputChanged() {
  const input = $('question-input'); input.style.height = 'auto'; input.style.height = `${Math.min(Math.max(input.scrollHeight, 84), 210)}px`;
  $('input-count').textContent = `${input.value.length} / 4000`; paint(controller.state);
}
$('question-form').onsubmit = event => { event.preventDefault(); controller.submit($('question-input').value); };
$('question-input').addEventListener('input', inputChanged);
$('question-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault(); $('question-form').requestSubmit();
  }
});
$('document-picker').onchange = () => { controller.select($('document-picker').value); closeMenu(); };
$('refresh-history').onclick = () => controller.refresh(); $('history-search').oninput = paintHistory;
$('new-question').onclick = () => { controller.newQuestion(); $('question-input').value = ''; inputChanged(); $('question-input').focus(); closeMenu(); };
$('cancel-question').onclick = () => controller.cancel(); $('confirm-question').onclick = () => controller.confirm(); $('retry-question').onclick = () => controller.retry();
$('show-sources').onclick = () => { const citation = controller.state.result?.answer.citations?.[0]; if (citation) openSource(citation, $('show-sources')); };
$('close-sources').onclick = () => closeSources();
$('menu-toggle').onclick = () => { $('sidebar').classList.toggle('mobile-open'); $('menu-toggle').setAttribute('aria-expanded', String($('sidebar').classList.contains('mobile-open'))); };
$('copy-answer').onclick = async () => {
  try { await navigator.clipboard.writeText(answerTextForDisplay(controller.state.result?.answer || {})); announce('已复制回答。'); }
  catch { announce('浏览器限制了剪贴板，请选择正文手动复制。', 'error'); }
};
document.querySelectorAll('[data-prompt]').forEach(button => button.onclick = () => { $('question-input').value = button.dataset.prompt; inputChanged(); $('question-input').focus(); });
document.addEventListener('visibilitychange', () => controller.visibility());
document.addEventListener('visibilitychange',()=>stream.pause(document.hidden));window.addEventListener('pagehide',()=>stream.dispose());
$('chat-scroll').addEventListener('scroll', updateScrollControl, { passive: true });
$('return-latest').onclick = () => { const scroll = $('chat-scroll'); scroll.scrollTo({ top: scroll.scrollHeight, behavior: 'smooth' }); };
document.addEventListener('keydown', event => { if (event.key === 'Escape') { if (!$('source-drawer').hidden) closeSources(); else closeMenu(); } });
window.addEventListener('pagehide', () => controller.dispose());
controller.boot();
