import { createApiClient, encoded } from './api-client.js';
import { node, renderAnswer, sourceLocationsText, answerTextForDisplay } from './answer-view.js';
import { createAnswerStream } from './answer-stream.js';

const api = createApiClient(), $ = id => document.getElementById(id);
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const active = job => ['queued', 'running'].includes(job.status);
const labels = { queued: '等待处理', routing: '识别问题类型', retrieving: '查找知识库资料', generating: '生成回答', validating: '核对原文引用', done: '已完成' };
let bases = [], historyItems = [], current = null, selectedBase = null, busy = false, pending = null, timer = null, failures = 0, rev = 0, storageKey = '', selectedKey = '', rendered = '', draftMemoryEnabled = true;
const stream=createAnswerStream({source:'knowledge',changed:()=>{rendered='';render();},finished:()=>perform(refresh)});
function notice(text) { $('chat-notice').textContent = text; }
function persist(value) { try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch { throw new Error('浏览器无法保存恢复请求键，请启用会话存储后再提交。'); } pending = value; }
function stop() { clearTimeout(timer); timer = null; }
function poll() { stop(); if (!document.hidden && failures < 5 && current?.turns.some(active)) timer = setTimeout(() => perform(refresh), Math.min(2500 * (failures + 1), 12000)); }
async function perform(work) {
  if (busy) return; busy = true; controls();
  try { await work(); failures = 0; } catch (error) { if ([401,403,404].includes(error.status)) { current = null; historyItems = []; rendered = ''; render(); stop(); } failures++; notice(error.status ? error.message : '连接中断或等待超时。请刷新核对原记录，没有自动重发请求。'); }
  finally { busy = false; controls(); poll(); }
}
function controls() {
  const blocked = busy || Boolean(pending) || !selectedBase || current?.turns.some(active);
  $('kb-send').disabled = Boolean(blocked); $('new-chat').disabled = busy || Boolean(pending); $('kb-picker').disabled = busy || Boolean(pending);
  $('confirm-pending').hidden = !pending; $('confirm-pending').disabled = busy;
  $('kb-memory').disabled = Boolean(blocked); $('clear-memory').disabled = !current || Boolean(blocked);
}
function openSource(citation) {
  const content = $('kb-source-content'); content.replaceChildren();
  content.append(node('h3', citation.filename || '文档原文'), node('p', sourceLocationsText(citation.sources), 'muted'));
  const quote = node('pre', citation.quote, 'source-preview'); content.append(quote);
  const info = node('p', `引用校验：${citation.verification === 'exact_match' ? '原文精确匹配' : '请核对'} · ${citation.evidence_id || ''}`, 'muted-detail'); content.append(info);
  const original = node('a', '打开原件'); original.href = `/api/documents/${encoded(citation.document_id)}/original`; original.target = '_blank'; original.rel = 'noopener'; content.append(original);
  $('kb-source').showModal();
}
function render() {
  const hs = $('kb-history'); hs.replaceChildren();
  for (const item of historyItems) { const b = node('button', item.title, item.id === current?.id ? 'active' : ''); b.type = 'button'; b.onclick = () => perform(async () => { if (pending) { notice('请先确认上次请求，再切换会话。'); return; } current = await api(`/api/kb-conversations/${encoded(item.id)}`); selectedBase = current.base_id; $('kb-picker').value = selectedBase; remember(); rendered = ''; render(); }); hs.append(b); }
  $('chat-title').textContent = current?.title || '知识库对话';
  const base = bases.find(b => b.id === selectedBase); $('chat-scope').textContent = base ? `${base.name} · 已发布 v${base.revision}` : '尚无获授权的发布版知识库';
  const memoryEnabled=current ? Boolean(current.memory_enabled) : draftMemoryEnabled;
  $('kb-memory').setAttribute('aria-checked',String(memoryEnabled)); $('kb-memory').title=memoryEnabled?'会话记忆已开启，点击关闭':'会话记忆已关闭，点击开启';
  stream.watch(current?.turns);
  const signature = JSON.stringify([current?.turns || [],stream.revision]);
  if (signature !== rendered) {
    rendered = signature; const scroll = $('kb-scroll'), atEnd = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 150; const turns = $('kb-turns'); turns.replaceChildren();
    for (const task of current?.turns || []) {
      const turn = node('section', '', 'kb-chat-turn'); turn.append(node('p', task.question, 'kb-user-message'));
      const answer = node('article', '', 'kb-assistant'); answer.append(node('strong', 'DocQA'));
      if (task.result) {
        const body = node('div', '', 'answer-body'); renderAnswer(body, task.result, openSource); answer.append(body);
        const facts = [...(task.result.conclusion || []), ...(task.result.explanation || [])];
        if (facts.length) { const audit = node('details', '', 'audit-details'); audit.append(node('summary', '查看论断与引用校验详情')); facts.forEach(f => audit.append(node('p', `${({ direct: '原文直述', synthesis: '多处综合', inference: '推断' })[f.claim_type] || '待核对'} · ${f.text} · ${f.support_review?.status === 'needs_review' ? '存在待复核风险' : '证据规则检查通过'}；语义未自动认证`))); answer.append(audit); }
        const actions = node('div', '', 'row'), copy = node('button', '复制回答', 'secondary'); copy.type = 'button'; copy.onclick = async () => { try { await navigator.clipboard.writeText(answerTextForDisplay(task.result)); notice('已复制回答'); } catch { notice('无法使用剪贴板，请选择正文复制。'); } }; actions.append(copy); const download = node('a', '导出结果', 'button secondary'); download.href = `/api/kb-tasks/${encoded(task.id)}/export`; actions.append(download); answer.append(actions);
      } else {
        stream.render(answer,task,openSource);
        answer.append(node('p', task.error_message || labels[task.stage] || '正在处理'));
        if (active(task)) { const cancel = node('button', task.cancel_requested ? '正在停止' : '停止后续处理', 'secondary'); cancel.type = 'button'; cancel.disabled = Boolean(task.cancel_requested); cancel.onclick = () => perform(async () => { await api(`/api/kb-tasks/${encoded(task.id)}/cancel`, { method: 'POST' }); await refresh(); }); answer.append(cancel); }
        if (['failed', 'needs_attention'].includes(task.status)) { const retry = node('button', '复用检查点重试', 'secondary'); retry.type = 'button'; retry.onclick = () => perform(async () => { let acknowledged = false; if (task.calls.some(c => c.status === 'uncertain')) { acknowledged = confirm('有未确认的调用，可能已计费。再次执行可能产生额外费用，是否明确重试？'); if (!acknowledged) return; } await api(`/api/kb-tasks/${encoded(task.id)}/retry`, json('POST', { acknowledge_uncertain: acknowledged })); await refresh(); }); answer.append(retry); }
      }
      turn.append(answer); turns.append(turn);
    }
    $('kb-welcome').hidden = Boolean(current?.turns.length); if (atEnd) scroll.scrollTop = scroll.scrollHeight;
  }
  controls();
}
function remember() { try { if (current) sessionStorage.setItem(selectedKey, current.id); else sessionStorage.removeItem(selectedKey); } catch { } }
async function refresh() {
  const version = ++rev;
  const [nextBases, nextHistory] = await Promise.all([api('/api/knowledge-bases'), api('/api/kb-conversations')]);
  if (version !== rev) return; bases = nextBases.filter(b => b.active_snapshot_id); historyItems = nextHistory;
  const picker = $('kb-picker'); picker.replaceChildren();
  for (const base of bases) { const o = node('option', base.name); o.value = base.id; picker.append(o); }
  if (!bases.length) { const o = node('option', '暂无可用知识库'); o.value = ''; picker.append(o); }
  if (!bases.some(b => b.id === selectedBase)) selectedBase = bases.find(b => b.id === new URLSearchParams(location.search).get('base'))?.id || bases[0]?.id || null; picker.value = selectedBase || '';
  if (pending && !pending.conversationId) { const match = historyItems.find(c => c.request_key === pending.createKey); if (match) { pending.conversationId = match.id; persist(pending); } }
  if (pending?.conversationId) current = await api(`/api/kb-conversations/${encoded(pending.conversationId)}`);
  else if (current) current = await api(`/api/kb-conversations/${encoded(current.id)}`);
  if (current) { selectedBase = current.base_id; picker.value = selectedBase; }
  if (pending && current?.turns.some(t => t.request_key === `${pending.conversationId}/${pending.turnKey}`)) { persist(null); $('kb-input').value = ''; notice('已找到原任务，没有重新提交。'); }
  else if (pending) notice('上次提交尚未确认。请先刷新核对，必要时点击“确认原请求”。');
  render(); poll();
}
async function sendPending() {
  if (!pending) return;
  if (!pending.conversationId) { const c = await api('/api/kb-conversations', json('POST', { base_id: pending.baseId, request_key: pending.createKey })); pending.conversationId = c.id; persist(pending); }
  if (pending.newConversation && pending.memoryEnabled === false) {
    const c=await api(`/api/kb-conversations/${encoded(pending.conversationId)}`);
    if (c.memory_enabled && !c.turns.length) await api(`/api/kb-conversations/${encoded(c.id)}/memory`,json('PATCH',{enabled:false,clear:false}));
  }
  await api(`/api/kb-conversations/${encoded(pending.conversationId)}/turns`, json('POST', { question: pending.question, request_key: pending.turnKey }));
  current = await api(`/api/kb-conversations/${encoded(pending.conversationId)}`); remember(); persist(null); $('kb-input').value = ''; notice(''); await refresh();
}
$('kb-form').onsubmit = event => { event.preventDefault(); perform(async () => { if (pending || !selectedBase || current?.turns.some(active)) return; const question = $('kb-input').value.trim(); if (!question) return; persist({ question, baseId: selectedBase, conversationId: current?.id || null, createKey: crypto.randomUUID(), turnKey: crypto.randomUUID(), newConversation:!current, memoryEnabled:current?Boolean(current.memory_enabled):draftMemoryEnabled }); await sendPending(); }); };
$('confirm-pending').onclick = () => perform(sendPending); $('chat-refresh').onclick = () => perform(refresh);
$('kb-input').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); if (!$('kb-send').disabled) $('kb-form').requestSubmit(); } };
$('kb-input').oninput = () => { const input = $('kb-input'); input.style.height = 'auto'; input.style.height = `${Math.max(90, Math.min(input.scrollHeight, 220))}px`; };
$('kb-picker').onchange = () => { stop(); selectedBase = $('kb-picker').value; current = null; remember(); rendered = ''; render(); notice(''); };
$('new-chat').onclick = () => { stop(); current = null; remember(); rendered = ''; render(); notice(''); };
$('kb-memory').onclick = () => perform(async () => { if (!current) draftMemoryEnabled=!draftMemoryEnabled; else current = await api(`/api/kb-conversations/${encoded(current.id)}/memory`, json('PATCH', { enabled: !current.memory_enabled, clear: false })); render(); });
$('clear-memory').onclick = () => perform(async () => { if (!current) return; current = await api(`/api/kb-conversations/${encoded(current.id)}/memory`, json('PATCH', { enabled: false, clear: true })); render(); notice('已清除并关闭记忆。历史记录保留，重新开启后从新轮次开始。'); });
$('close-source').onclick = () => $('kb-source').close(); window.addEventListener('pagehide', stop); document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); else perform(refresh); });
window.addEventListener('pagehide',()=>stream.dispose());document.addEventListener('visibilitychange',()=>stream.pause(document.hidden));
try {
  const session = await window.docqaAuthReady; const owner = session.user?.id || 'local'; storageKey = `docqa.knowledge.pending.${owner}`; selectedKey = `docqa.knowledge.selected.${owner}`;
  $('kb-admin').hidden = Boolean(session.team_mode && session.user?.role !== 'admin');
  try { const stored = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); if (stored && typeof stored.question === 'string' && typeof stored.turnKey === 'string' && typeof stored.createKey === 'string') pending = stored; } catch { }
  historyItems = await api('/api/kb-conversations'); const selected = sessionStorage.getItem(selectedKey); const prior = historyItems.find(c => c.id === selected);
  if (prior) current = await api(`/api/kb-conversations/${encoded(prior.id)}`);
  if (current) selectedBase = current.base_id;
  await refresh();
} catch (error) { notice(error.message || '无法读取知识库，请刷新。'); }
