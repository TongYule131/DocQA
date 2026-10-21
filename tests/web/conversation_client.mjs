// 针对实际会话控制器测试刷新、丢响应和迟到响应；不调用供应商。
import assert from 'node:assert/strict';
import { createConversationClient } from '../../app/web/conversation-client.js';
const clone = value => structuredClone(value);
function fixture({ store = new Map(), request = null } = {}) {
  let counter = 0, nextId = 0;
  const calls = [], conversations = [], timers = new Set();
  const documents = [{ id: 'd1', filename: '资料一' }, { id: 'd2', filename: '资料二' }];
  const storage = { getItem: key => store.get(key) || null, setItem: (key, val) => store.set(key, val), removeItem: key => store.delete(key) };
  async function api(path, options = {}) {
    calls.push([path, options]);
    if (request) { const override = await request(path, options); if (override !== undefined) return override; }
    if (path === '/api/documents') return clone(documents);
    if (path === '/api/rag/status') return { configured: true };
    if (/\/index$/.test(path)) return { status: 'indexed' };
    if (path.startsWith('/api/conversations') && !options.method) {
      const id = path.split('/')[3];
      if (!id) return clone(conversations.filter(c => Boolean(c.archived) === path.includes('archived=true')));
      const c = conversations.find(c => c.id === id); if (!c) throw Object.assign(new Error('会话不存在'), { status: 404 }); return clone(c);
    }
    const body = JSON.parse(options.body || '{}');
    if (path === '/api/conversations' && options.method === 'POST') {
      let c = conversations.find(c => c.request_key === body.idempotency_key);
      if (!c) { c = { id: `c${++nextId}`, document_id: body.document_id, request_key: body.idempotency_key,
        title: '新会话', revision: 0, archived: false, turns: [], memory: { enabled: true, epoch: 0, context: { notes: '', recent_questions: [] } } }; conversations.push(c); }
      return clone(c);
    }
    const c = conversations.find(c => path.includes('/' + c.id));
    if (path.endsWith('/turns')) {
      let t = c.turns.find(t => t.request_key === body.idempotency_key);
      if (!t) { t = { request_key: body.idempotency_key, job: { id: `j${++counter}`, document_id: c.document_id, question: body.question, status: 'queued' } }; c.turns.push(t); c.revision++; }
      return { job: clone(t.job), reused: false };
    }
    if (options.method === 'PATCH') { Object.assign(c, body); if (typeof body.memory_enabled==='boolean') c.memory.enabled=body.memory_enabled; c.revision++; return clone(c); }
    if (path.endsWith('/cancel')) { for (const c of conversations) for (const t of c.turns) t.job.status = 'cancelled'; return {}; }
    throw new Error('Unexpected route ' + path);
  }
  const client = createConversationClient({ api, storage, uuid: () => `key-${++counter}`,
    schedule: fn => { timers.add(fn); return fn; }, unschedule: fn => timers.delete(fn), visible: () => true });
  return { client, calls, conversations, documents, store, storage, timers };
}
const posts = f => f.calls.filter(([, o]) => o.method === 'POST');
{
  const f = fixture(); await f.client.boot();
  assert.equal(posts(f).length, 0); assert.equal(f.client.state.documentId, 'd1');
  await Promise.all([f.client.submit('首次提问'), f.client.submit('重复点击')]);
  assert.equal(posts(f).length, 2); assert.equal(f.conversations.length, 1); assert.equal(f.conversations[0].turns.length, 1);
  assert.equal(f.client.state.pending, null); assert.equal(f.client.state.conversation.turns[0].job.status, 'queued');
  const count = posts(f).length; await f.client.refresh(); await f.client.refresh(); assert.equal(posts(f).length, count);
  f.client.dispose(); assert.equal(f.timers.size, 0);
  console.log('PASS 创建及每轮提交只执行一次，刷新不产生 POST');
}
{
  let lose = true;
  const f = fixture({ request: async (path, o) => { if (path.endsWith('/turns') && o.method === 'POST' && lose) { lose = false; throw new Error('lost response'); } } });
  await f.client.boot(); await f.client.submit('保留原问题');
  const pending = clone(f.client.state.pending), count = posts(f).length;
  await f.client.refresh(); assert.equal(posts(f).length, count); assert.deepEqual(f.client.state.pending, pending);
  await f.client.confirm();
  const bodies = posts(f).filter(([p]) => p.endsWith('/turns')).map(([, o]) => JSON.parse(o.body));
  assert.equal(bodies.length, 2); assert.deepEqual(bodies[0], bodies[1]); assert.equal(bodies[0].question, '保留原问题');
  console.log('PASS 丢失响应保留请求键，仅明确确认原提交才重发');
}
{
  const f = fixture(); await f.client.boot(); await f.client.submit('提交后刷新');
  const c = f.conversations[0], saved = { document_id: 'd1', conversation_id: null, create_key: c.request_key,
    turn_key: c.turns[0].request_key, question: '提交后刷新' };
  f.store.set('docqa.conversation.pending', JSON.stringify(saved));
  const count = posts(f).length;
  await f.client.boot(); assert.equal(posts(f).length, count); assert.equal(f.client.state.pending, null);
  assert.equal(f.client.state.conversation.id, c.id); assert.equal(f.client.state.index.status, 'indexed');
  console.log('PASS 刷新恢复已被接收的原轮次，只读确认并恢复范围');
}
{
  let resolveFirst;
  const f = fixture({ request: async (path) => path === '/api/documents/d1/index' && resolveFirst === undefined
    ? new Promise(resolve => { resolveFirst = resolve; }) : undefined });
  const first = f.client.draft('d1'); await new Promise(resolve => setImmediate(resolve));
  await f.client.draft('d2'); resolveFirst({ status: 'wrong-old-index' }); await first;
  assert.equal(f.client.state.documentId, 'd2'); assert.equal(f.client.state.index.status, 'indexed');
  console.log('PASS 切换资料时迟到索引响应不覆盖当前范围');
}
{
  const f = fixture(); f.storage.setItem = () => { throw new Error('storage denied'); };
  await f.client.boot(); await f.client.submit('不应提交'); assert.equal(posts(f).length, 0); assert.match(f.client.state.notice, /无法保存/);
  console.log('PASS 恢复记录无法保存时阻止收费提交');
}
{
  const f = fixture(); await f.client.boot(); await f.client.submit('第一轮');
  f.conversations[0].turns[0].job.status = 'succeeded'; await f.client.refresh();
  await f.client.submit('继续追问'); assert.equal(f.conversations[0].turns.length, 2);
  assert.equal(posts(f).filter(([p]) => p === '/api/conversations').length, 1);
  await f.client.cancel(f.conversations[0].turns[1].job.id); assert.equal(f.client.state.conversation.turns[1].job.status, 'cancelled');
  console.log('PASS 后续轮次复用同一会话，停止只提交取消');
}
{
  const f = fixture(); await f.client.boot(); await f.client.submit('历史会话');
  f.conversations[0].turns[0].job.status = 'succeeded'; await f.client.refresh();
  const before = posts(f).length; await f.client.update({ archived: true });
  assert.equal(f.client.state.conversations.length, 0); await f.client.archived(true);
  assert.equal(f.client.state.conversations.length, 1); assert.equal(posts(f).length, before);
  assert.equal(Boolean(f.client.blocked()), true);
  console.log('PASS 归档与查看归档不生成，已归档会话禁止新轮次');
}
console.log('7 conversation client scenarios passed');

// 临时资料创建、断线恢复与主动准备，均调用实际生产控制器。
{
  let f;
  f = fixture({ request: async (path, o) => {
    if (path === '/api/conversations/temporary' && o.method === 'POST') {
      const c = { id: 'temporary', document_id: 'temporary-doc', request_key: 'temporary/' + o.headers['Idempotency-Key'],
        title: '合成临时资料', turns: [], attachment: { expires_at: '2099-01-01' }, document: { id: 'temporary-doc' }, revision: 0 };
      f.conversations.push(c); return clone(c);
    }
    if (path.endsWith('/attachment/parse')) return {};
    if (path.endsWith('/attachment') && o.method === 'DELETE') { f.conversations.length = 0; return {}; }
  } });
  await f.client.boot();
  await Promise.all([f.client.upload(new File(['synthetic'], 'synthetic.txt')), f.client.upload(new File(['duplicate'], 'other.txt'))]);
  assert.equal(posts(f).length, 1); assert.equal(f.client.state.conversation.id, 'temporary');
  await f.client.prepareAttachment('parse'); await f.client.refresh();
  assert.equal(posts(f).filter(([p]) => p.endsWith('/attachment/parse')).length, 1);
  assert.equal(f.store.has('docqa.attachment.pending'), false);
  await f.client.prepareAttachment('remove'); assert.equal(f.client.state.conversation, null);
  f.client.dispose(); console.log('PASS 临时上传防重复，主动解析，移除仅发送一次');
}
{
  const store = new Map([['docqa.attachment.pending', 'lost-upload']]);
  const f = fixture({ store });
  f.conversations.push({ id: 'accepted-upload', document_id: 'd1', request_key: 'temporary/lost-upload', title: '已接收', turns: [] });
  await f.client.boot();
  assert.equal(posts(f).length, 0); assert.equal(f.client.state.conversation.id, 'accepted-upload');
  assert.equal(store.has('docqa.attachment.pending'), false);
  f.client.dispose(); console.log('PASS 上传响应丢失后只读找回原会话，不重传文件');
}
{
  const f = fixture(); await f.client.boot(); f.storage.setItem = () => { throw new Error('blocked'); };
  await f.client.upload(new File(['synthetic'], 'synthetic.txt'));
  assert.equal(posts(f).length, 0); assert.match(f.client.state.notice, /恢复记录/);
  f.client.dispose(); console.log('PASS 上传恢复键无法保存时不发请求');
}
console.log('10 conversation client scenarios passed');

{
  let expired = false;
  const f = fixture({ request: async (p, o) => { if (expired && p === '/api/conversations/c-expired' && !o.method) throw Object.assign(new Error('临时资料已到期'), {status:410}); } });
  f.conversations.push({id:'c-expired',document_id:'d1',title:'临时会话',turns:[],attachment:{expires_at:'2099-01-01T00:00:00Z'}});
  await f.client.boot(); await f.client.open('c-expired'); assert.ok(f.timers.size);
  expired = true; await f.client.refresh(); assert.equal(f.client.state.conversation,null); assert.match(f.client.state.notice,/到期/);
  assert.equal(posts(f).length,0); f.client.dispose(); console.log('PASS 到期或撤权后清除当前页面旧回答，只读检查不计费');
}
console.log('11 conversation client scenarios passed');

{
  const f=fixture();await f.client.boot();
  const count=f.calls.length;f.client.setDraftMemory(false);assert.equal(f.calls.length,count);
  assert.equal(f.client.state.draftMemoryEnabled,false);await f.client.submit('关闭记忆后提问');
  const writes=f.calls.filter(([,o])=>['POST','PATCH'].includes(o.method));
  assert.deepEqual(writes.map(([p,o])=>[p,o.method]),[['/api/conversations','POST'],['/api/conversations/c1','PATCH'],['/api/conversations/c1/turns','POST']]);
  assert.equal(f.client.state.conversation.memory.enabled,false);
  assert.equal(posts(f).length,2);f.client.dispose();console.log('PASS 首轮前关闭记忆，无模型调用，提问前完成服务端设置');
}
{
  let lose=true;
  const f=fixture({request:async(path,o)=>{
    if(o.method==='PATCH'&&lose){lose=false;const c=f.conversations.find(c=>path.endsWith(c.id));c.memory.enabled=false;c.revision++;throw new Error('synthetic response loss');}
  }});
  await f.client.boot();f.client.setDraftMemory(false);await f.client.submit('保留关闭设置');
  assert.ok(f.client.state.pending);assert.equal(f.conversations[0].turns.length,0);
  await f.client.refresh();await f.client.confirm();
  assert.equal(f.calls.filter(([,o])=>o.method==='PATCH').length,1);
  assert.equal(f.conversations[0].turns.length,1);assert.equal(f.client.state.conversation.memory.enabled,false);
  f.client.dispose();console.log('PASS 记忆开关响应丢失后明确恢复，不重复修改或生成');
}
console.log('13 conversation client scenarios passed');
