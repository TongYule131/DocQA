import { createApiClient, encoded } from './api-client.js';
import { createConversationClient } from './conversation-client.js';
import { node, renderAnswer, sourceLocationsText, answerTextForDisplay } from './answer-view.js';
import { jobLabels, stageLabels } from './question-client.js';
import { createAnswerStream } from './answer-stream.js';

const $ = id => document.getElementById(id), api = createApiClient();
const controller = createConversationClient({ api, storage: sessionStorage, changed: paint });
const stream = createAnswerStream({changed:()=>{turnsSignature='';paint();},finished:()=>controller.refresh()});
let turnsSignature = '', memorySignature = '', documentsSignature = '', listSignature = '', sourceSequence = 0, previousId = null, sourceFocus = null;
function notice(text) { $('notice').textContent = text; $('notice').hidden = !text; }
function closeMenu() { $('sidebar').classList.remove('mobile-open'); $('menu-toggle').setAttribute('aria-expanded', 'false'); }
function closeSource(restore = true) { ++sourceSequence; $('source-drawer').hidden = true; if (restore) sourceFocus?.focus(); }
function source(citation, focus) {
  sourceFocus = focus;
  const sequence = ++sourceSequence, region = $('source-content'); $('source-drawer').hidden = false; region.replaceChildren();
  const file = controller.state.conversation?.document?.id === citation.document_id ? controller.state.conversation.document : controller.state.documents.find(d => d.id === citation.document_id);
  region.append(node('h3', file?.filename || '引用资料'), node('p', sourceLocationsText(citation.sources), 'source-location'),
    node('blockquote', citation.quote), node('p', `解析版本：${citation.parse_version_id || '未知'}`, 'small-text'));
  const page = citation.sources?.find(s => s.format === 'pdf' && Number.isInteger(s.page) && s.page > 0)?.page;
  const original = node('a', '打开原件核对', 'source-original'); original.href = `/api/documents/${encoded(citation.document_id)}/original` + (page ? `#page=${page}` : '');
  original.target = '_blank'; original.rel = 'noopener noreferrer'; region.append(original);
  const button = node('button', '展开并高亮解析原文', 'secondary'), excerpt = node('pre', '', 'source-excerpt'); excerpt.hidden = true;
  button.type = 'button'; button.onclick = async () => {
    button.disabled = true;
    try {
      const chunks = await api(`/api/documents/${encoded(citation.document_id)}/chunks?version_id=${encoded(citation.parse_version_id)}`);
      if (sequence !== sourceSequence) return;
      const chunk = chunks.find(c => c.id === citation.chunk_id && c.parse_version_id === citation.parse_version_id && c.document_id === citation.document_id);
      const chars = Array.from(String(chunk?.text || '').replace(/\r\n/g, '\n'));
      if (citation.verification !== 'exact_match' || citation.offset_basis !== 'chunk_crlf_lf_unicode_codepoints' || !Number.isInteger(citation.char_start)
        || !Number.isInteger(citation.char_end) || citation.char_start < 0 || citation.char_end > chars.length || citation.char_end <= citation.char_start
        || chars.slice(citation.char_start, citation.char_end).join('') !== citation.quote) throw new Error('引用范围不一致');
      excerpt.replaceChildren(document.createTextNode(chars.slice(0, citation.char_start).join('')), node('mark', citation.quote), document.createTextNode(chars.slice(citation.char_end).join('')));
      button.hidden = true;
    } catch { if (sequence === sourceSequence) excerpt.textContent = '暂时无法核对范围，未显示高亮，请打开原件复核。'; }
    finally { if (sequence === sourceSequence) { excerpt.hidden = false; button.disabled = false; } }
  }; region.append(button, excerpt); $('close-sources').focus();
}
function paint() {
  const s = controller.state, c = s.conversation, doc = c?.document || s.documents.find(d => d.id === s.documentId);
  const id = c?.id || `draft/${s.documentId}`;
  if (previousId !== id) { previousId = id; $('question-input').value = ''; $('question-input').style.height = '84px'; $('memory-panel').hidden = true; closeSource(false); }
  const ds = JSON.stringify(s.documents.map(d => [d.id, d.filename]));
  if (ds !== documentsSignature) { documentsSignature = ds; $('document-picker').replaceChildren(); if (!s.documents.length) $('document-picker').append(new Option('暂无可用资料', '')); s.documents.forEach(d => $('document-picker').append(new Option(d.filename, d.id))); }
  $('document-picker').value = s.documentId || ''; $('document-picker').disabled = Boolean(c) || s.mutating || Boolean(s.pending);
  $('workspace-title').textContent = c?.title || '新会话'; $('scope-label').textContent = doc ? `资料：${doc.filename}` : '暂无资料，请联系管理员或上传准备';
  $('document-hint').textContent = c ? '会话固定当前资料，换资料请新建会话' : s.index?.status === 'indexed' ? '资料已准备，可以提问' : '请先完成资料解析与索引';
  const blocked = controller.blocked();
  $('attachment-upload').hidden = Boolean(c);
  $('attachment-submit').disabled = s.loading || s.mutating || Boolean(s.pending);
  $('attachment-file').disabled = $('attachment-submit').disabled;
  $('attachment-panel').hidden = !c?.attachment;
  if (c?.attachment) {
    $('attachment-name').textContent = `临时资料：${doc.filename}`;
    $('attachment-state').textContent = `到期时间：${new Date(c.attachment.expires_at).toLocaleString()} · ${doc.task_status === 'running' || doc.task_status === 'queued' ? '解析中，请稍候' : doc.task_status === 'failed' || doc.task_status === 'needs_attention' ? '解析未成功；请移除后重新上传，未自动重试' : s.index?.status === 'indexed' ? '已准备好，可以提问' : doc.active_parse_version_id ? '解析完成，请主动建立索引' : '上传完成，请主动解析'}。到期后历史回答及来源也停止读取。`;
    $('attachment-parse').disabled = Boolean(blocked) || Boolean(doc.active_parse_version_id) || ['queued', 'running'].includes(doc.task_status);
    $('attachment-index').disabled = Boolean(blocked) || !doc.active_parse_version_id || ['indexed', 'indexing'].includes(s.index?.status);
    $('attachment-remove').disabled = s.mutating || Boolean(s.pending);
  }
  $('question-input').disabled = Boolean(blocked) || !s.configured || s.index?.status !== 'indexed';
  $('send').disabled = $('question-input').disabled || !$('question-input').value.trim(); $('input-count').textContent = `${$('question-input').value.length} / 4000`;
  $('new-conversation').disabled = s.mutating || Boolean(s.pending); $('refresh').disabled = s.mutating;
  $('confirm').hidden = !s.pending; $('confirm').disabled = s.mutating || s.loading;
  $('memory-toggle').disabled = !c; $('archive').disabled = !c || s.mutating || Boolean(s.pending); $('archive').textContent = c?.archived ? '恢复会话' : '归档';
  const memoryEnabled=c ? c.memory.enabled : s.draftMemoryEnabled;
  $('memory-status').setAttribute('aria-checked',String(Boolean(memoryEnabled))); $('memory-status').disabled=Boolean(blocked);
  $('memory-status').title=memoryEnabled ? '会话记忆已开启，点击关闭' : '会话记忆已关闭，点击开启';
  $('open-attachment').disabled=s.loading || s.mutating || Boolean(s.pending);
  if (c?.attachment && $('attachment-dialog').open) $('attachment-dialog').close();
  notice(s.notice || (s.mutating ? '正在处理本次操作…' : s.attachmentPending ? '临时上传尚未确认。请刷新核对；没有记录时，重新选择原文件确认原上传。' : s.pending ? `提交尚未确认：${s.pending.question}。请先刷新查询原记录。` : ''));
  const search = $('history-search').value.trim().toLowerCase(), ls = JSON.stringify([s.conversations, c?.id, search]);
  if (ls !== listSignature) { listSignature = ls; $('history-list').replaceChildren();
    const items = s.conversations.filter(item => item.title.toLowerCase().includes(search));
    if (!items.length) $('history-list').append(node('p', '暂无匹配会话', 'small-text'));
    for (const item of items) { const b = node('button', '', 'history-item' + (item.id === c?.id ? ' active' : '')); b.type = 'button'; b.append(node('span', item.title), node('small', s.documents.find(d => d.id === item.document_id)?.filename || '资料会话')); b.onclick = () => { controller.open(item.id); closeMenu(); }; $('history-list').append(b); }
  }
  stream.watch(c?.turns?.map(t=>t.job));
  const ts = JSON.stringify([id,stream.revision, c?.turns?.map(t => [t.job.id, t.job.status, t.job.stage, t.job.error_message, t.result?.id, t.job.cancel_requested])]);
  if (ts !== turnsSignature) {
    const scroll = $('chat-scroll'), atEnd = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140;
    turnsSignature = ts; $('turn-list').replaceChildren(); $('welcome').hidden = Boolean(c?.turns?.length);
    for (const turn of c?.turns || []) {
      const job = turn.job, article = node('section', '', 'turn'); article.append(node('p', job.question, 'user-question'));
      const assistant = node('article', '', 'assistant'), content = node('div', '', 'assistant-content'); assistant.append(node('span', 'D', 'assistant-mark'), content);
      const heading = node('div', '', 'answer-heading'); heading.append(node('strong', 'DocQA'), node('span', jobLabels[job.status] || job.status)); content.append(heading);
      if (turn.result) {
        const answer = node('div', '', 'answer-body'); renderAnswer(answer, turn.result.answer, source); content.append(answer);
        const actions = node('div', '', 'answer-actions'), copy = node('button', '复制回答', 'text-button'); copy.type = 'button';
        copy.onclick = async () => { try { await navigator.clipboard.writeText(answerTextForDisplay(turn.result.answer)); notice('已复制回答。'); } catch { notice('浏览器限制了剪贴板，请选择正文复制。'); } }; actions.append(copy);
        for (const [format, label] of [['markdown', '导出 Markdown'], ['json', '导出 JSON']]) { const a = node('a', label); a.href = `/api/question-results/${encoded(turn.result.id)}/export?format=${format}`; actions.append(a); } content.append(actions);
      } else {
        stream.render(content,job,source);
        content.append(node('p', job.error_message || (job.cancel_requested ? '正在停止后续处理；在途请求可能仍计费。' : stageLabels[job.stage] || '正在处理'), 'stage-status'));
        if (['queued', 'running'].includes(job.status)) { const stop = node('button', '停止后续处理', 'secondary'); stop.type = 'button'; stop.disabled = s.mutating || job.cancel_requested; stop.onclick = () => controller.cancel(job.id); content.append(stop); }
      }
      article.append(assistant); $('turn-list').append(article);
    }
    if (atEnd) scroll.scrollTop = scroll.scrollHeight;
  }
  if (c) {
    const ms = `${c.id}/${c.memory.epoch}`;
    if (ms !== memorySignature) { memorySignature = ms; $('memory-enabled').checked = c.memory.enabled; $('memory-notes').value = c.memory_notes; $('conversation-title').value = c.title; }
    $('remembered-questions').textContent = '当前可带入的提问：' + (c.memory.context.recent_questions.join(' / ') || '无');
    for (const name of ['memory-enabled', 'memory-notes', 'memory-save', 'memory-clear']) $(name).disabled = Boolean(blocked);
  }
  $('return-latest').hidden = !c?.turns?.length || $('chat-scroll').scrollHeight - $('chat-scroll').scrollTop - $('chat-scroll').clientHeight < 140;
}
function input() { const el = $('question-input'); el.style.height = 'auto'; el.style.height = `${Math.min(210, Math.max(84, el.scrollHeight))}px`; paint(); }
$('question-form').onsubmit = async e => { e.preventDefault(); const text = $('question-input').value; await controller.submit(text); if (!controller.state.pending && !controller.state.notice) { $('question-input').value = ''; input(); } };
$('question-input').oninput = input; $('question-input').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); $('question-form').requestSubmit(); } };
$('document-picker').onchange = () => controller.draft($('document-picker').value); $('new-conversation').onclick = () => { controller.draft(); closeMenu(); };
$('refresh').onclick = controller.refresh; $('confirm').onclick = controller.confirm; $('history-search').oninput = paint;
$('attachment-upload').onsubmit = e => { e.preventDefault(); controller.upload($('attachment-file').files[0]); };
$('open-attachment').onclick = async () => { if (controller.state.conversation) await controller.draft(); if (!controller.state.pending) $('attachment-dialog').showModal(); };
$('close-attachment').onclick = () => { if (!controller.state.mutating) $('attachment-dialog').close(); };
$('memory-status').onclick = () => { const c=controller.state.conversation; if (c) controller.update({memory_enabled:!c.memory.enabled}); else controller.setDraftMemory(!controller.state.draftMemoryEnabled); };
$('attachment-parse').onclick = () => controller.prepareAttachment('parse');
$('attachment-index').onclick = () => controller.prepareAttachment('index');
$('attachment-remove').onclick = () => controller.prepareAttachment('remove');
$('archived-list').onchange = () => controller.archived($('archived-list').checked);
$('archive').onclick = () => controller.update({ archived: !controller.state.conversation.archived });
$('memory-toggle').onclick = () => { $('memory-panel').hidden = !$('memory-panel').hidden; }; $('memory-close').onclick = () => { $('memory-panel').hidden = true; };
$('memory-form').onsubmit = e => { e.preventDefault(); controller.update({ memory_enabled: $('memory-enabled').checked, memory_notes: $('memory-notes').value }); };
$('memory-clear').onclick = () => controller.update({ clear_memory: true });
$('rename-form').onsubmit = e => { e.preventDefault(); controller.update({ title: $('conversation-title').value.trim() }); };
$('menu-toggle').onclick = () => { $('sidebar').classList.toggle('mobile-open'); $('menu-toggle').setAttribute('aria-expanded', String($('sidebar').classList.contains('mobile-open'))); };
$('close-sources').onclick = () => closeSource();
$('chat-scroll').addEventListener('scroll', () => { $('return-latest').hidden = $('chat-scroll').scrollHeight - $('chat-scroll').scrollTop - $('chat-scroll').clientHeight < 140; }, { passive: true });
$('return-latest').onclick = () => $('chat-scroll').scrollTo({ top: $('chat-scroll').scrollHeight, behavior: 'smooth' });
document.addEventListener('visibilitychange', controller.visibility); window.addEventListener('pagehide', controller.dispose);
document.addEventListener('visibilitychange',()=>stream.pause(document.hidden));window.addEventListener('pagehide',()=>stream.dispose());
document.addEventListener('keydown', e => { if (e.key === 'Escape') { if (!$('source-drawer').hidden) closeSource(); $('memory-panel').hidden = true; closeMenu(); } });
controller.boot();
