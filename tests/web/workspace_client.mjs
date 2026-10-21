// 运行真实客户端控制器；只替换 HTTP 边界，验证重复提交、断线与历史恢复的费用边界。
import assert from 'node:assert/strict';
import { createApiClient } from '../../app/web/api-client.js';
import { createQuestionClient } from '../../app/web/question-client.js';

let passed = 0;
const docs = ['a', 'b'].map(id => ({ id, filename: `${id}.txt`, active_parse_version_id: `${id}-v` }));
const job = (overrides = {}) => ({ id: 'job-a', document_id: 'a', question: '原问题', status: 'queued', stage: 'queued',
  requests_used: 0, max_requests: 4, idempotency_key: 'initial-key', ...overrides });
const result = (overrides = {}) => ({ id: 'result-a', job_id: 'job-a', document_id: 'a', answer: { answer: '已保存回答', citations: [] }, ...overrides });
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { resolve, reject, promise }; };
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
function harness(storage = new Map()) {
  const calls = [], timers = new Map(); let counter = 0, custom = null, visible = true;
  const api = createApiClient(async (path, options = {}) => {
    const call = { path, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null };
    calls.push(call);
    let data;
    if (custom) data = await custom(call);
    if (data === undefined) {
      if (path === '/api/documents') data = docs;
      else if (path === '/api/rag/status') data = { configured: true };
      else if (path.endsWith('/index')) data = { status: 'indexed' };
      else if (path.endsWith('/question-jobs')) data = [];
      else throw new Error(`Unexpected ${path}`);
    }
    if (data?.httpError) return { ok: false, status: data.httpError, json: async () => ({ detail: '明确拒绝' }), headers: { get: () => 'req-example' } };
    return { ok: true, status: 200, json: async () => data };
  });
  const client = createQuestionClient({ api, storage: { getItem: key => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value) }, uuid: () => `key-${++counter}`,
    schedule: (fn, delay) => { const id = ++counter; timers.set(id, { fn, delay }); return id; },
    unschedule: id => timers.delete(id), isVisible: () => visible });
  return { client, calls, storage, timers, posts: () => calls.filter(call => call.method === 'POST'),
    route(fn) { custom = fn; }, visible(value) { visible = value; client.visibility(); } };
}
async function test(name, action) { await action(); passed++; console.log(`PASS ${name}`); }

await test('页面与历史恢复零 POST，持久化答案直接读取', async () => {
  const h = harness(); h.route(call => call.path.endsWith('/question-jobs') ? [job({ status: 'succeeded', result_id: 'result-a' })]
    : call.path.includes('/question-results/') ? result() : undefined);
  await h.client.boot(); assert.equal(h.client.state.result.answer.answer, '已保存回答'); assert.equal(h.posts().length, 0); h.client.dispose();
});
await test('快速重复发送只产生一次 POST', async () => {
  const h = harness(), pending = deferred(); await h.client.boot();
  h.route(call => call.method === 'POST' ? pending.promise : undefined);
  const first = h.client.submit('原问题'); await tick(); await h.client.submit('第二次点击');
  assert.equal(h.posts().length, 1); pending.resolve({ job: job({ idempotency_key: h.posts()[0].body.idempotency_key }) }); await first;
  assert.equal(h.client.state.job.id, 'job-a'); h.client.dispose();
});
await test('响应丢失后刷新或重开只查询，不自动重发', async () => {
  const h = harness(); await h.client.boot(); let accepted;
  h.route(call => { if (call.method === 'POST') { accepted = job({ idempotency_key: call.body.idempotency_key }); throw new Error('response lost'); } });
  await h.client.submit('原问题'); assert.equal(h.client.state.pending.uncertain, true); assert.equal(h.posts().length, 1);
  const reopened = harness(h.storage); reopened.route(call => call.path.endsWith('/question-jobs') ? [accepted] : undefined);
  await reopened.client.boot(); assert.equal(reopened.posts().length, 0); assert.equal(reopened.client.state.pending.uncertain, false);
  assert.equal(reopened.client.state.job.id, accepted.id); h.client.dispose(); reopened.client.dispose();
});
await test('确认原提交保留原问题和请求键', async () => {
  const h = harness(); await h.client.boot(); h.route(call => { if (call.method === 'POST') throw new Error('offline'); });
  await h.client.submit('原问题'); const original = h.posts()[0].body;
  await h.client.submit('用户改过的草稿'); assert.equal(h.posts().length, 1);
  h.route(call => call.method === 'POST' ? { job: job({ idempotency_key: original.idempotency_key }) } : undefined);
  await h.client.confirm(); assert.equal(h.posts().length, 2); assert.deepEqual(h.posts()[1].body, original); h.client.dispose();
});
await test('明确参数拒绝解除不确定状态，并显示支持编号', async () => {
  const h = harness(); await h.client.boot(); h.route(call => call.method === 'POST' ? { httpError: 422 } : undefined);
  await h.client.submit('原问题'); assert.equal(h.client.state.pending.uncertain, false);
  assert.match(h.client.state.notice, /req-example/); assert.equal(h.posts().length, 1); h.client.dispose();
});
await test('切换文档后迟到列表不能覆盖新范围', async () => {
  const h = harness(); await h.client.boot(); const late = deferred();
  h.route(call => call.path === '/api/documents/a/question-jobs' ? late.promise : undefined);
  const older = h.client.select('a'); await h.client.select('b'); late.resolve([job()]); await older;
  assert.equal(h.client.state.documentId, 'b'); assert.equal(h.client.state.jobs.length, 0); assert.equal(h.posts().length, 0); h.client.dispose();
});
await test('同文档历史切换也丢弃迟到结果', async () => {
  const h = harness(); await h.client.boot(); const late = deferred();
  h.route(call => call.path === '/api/question-jobs/job-a' ? job({ status: 'succeeded', result_id: 'result-a' })
    : call.path === '/api/question-results/result-a' ? late.promise
      : call.path === '/api/question-jobs/job-b' ? job({ id: 'job-b', status: 'succeeded', result_id: 'result-b' })
        : call.path === '/api/question-results/result-b' ? result({ id: 'result-b', job_id: 'job-b' }) : undefined);
  const old = h.client.open('job-a'); await tick(); await h.client.open('job-b'); late.resolve(result()); await old;
  assert.equal(h.client.state.result.id, 'result-b'); assert.equal(h.posts().length, 0); h.client.dispose();
});
await test('隐藏页暂停查询，恢复页面不产生 POST', async () => {
  const h = harness(); h.route(call => call.path.endsWith('/question-jobs') ? [job()] : call.path === '/api/question-jobs/job-a' ? job() : undefined);
  await h.client.boot(); assert.equal(h.timers.size, 1); h.visible(false); assert.equal(h.timers.size, 0);
  const count = h.calls.length; await h.client.poll(); assert.equal(h.calls.length, count);
  h.visible(true); assert.equal(h.timers.size, 1); assert.equal(h.posts().length, 0); h.client.dispose();
});
await test('取消仅调用取消接口，不启动另一个生成任务', async () => {
  const h = harness(); h.route(call => call.path.endsWith('/question-jobs') ? [job()] : call.path.endsWith('/cancel') ? job({ status: 'cancelled' }) : undefined);
  await h.client.boot(); await h.client.cancel(); assert.equal(h.client.state.job.status, 'cancelled');
  assert.equal(h.posts().length, 1); assert.match(h.posts()[0].path, /\/cancel$/); h.client.dispose();
});
await test('存储失败时不发送收费提交', async () => {
  const h = harness(); await h.client.boot(); h.storage.set = () => { throw new Error('blocked'); };
  await h.client.submit('原问题'); assert.equal(h.posts().length, 0); assert.match(h.client.state.notice, /恢复记录/); h.client.dispose();
});
await test('读取错误归属结果被拒绝', async () => {
  const h = harness(); h.route(call => call.path.endsWith('/question-jobs') ? [job({ status: 'succeeded', result_id: 'result-a' })]
    : call.path.includes('/question-results/') ? result({ document_id: 'b' }) : undefined);
  await h.client.boot(); assert.equal(h.client.state.result, null); assert.equal(h.posts().length, 0); assert.match(h.client.state.notice, /刷新/); h.client.dispose();
});
await test('重试响应丢失按 last_retry_key 恢复，不重置任务预算', async () => {
  const h = harness(); let record = job({ status: 'failed', requests_used: 2 });
  h.route(call => call.path.endsWith('/question-jobs') ? [record] : undefined); await h.client.boot();
  h.route(call => { if (call.path.endsWith('/retry')) { record = { ...record, status: 'queued', last_retry_key: call.body.idempotency_key }; throw new Error('response lost'); } });
  await h.client.retry(); assert.equal(h.client.state.pending.mutation, 'retry'); assert.equal(h.posts().length, 1);
  const second = harness(h.storage); second.route(call => call.path.endsWith('/question-jobs') ? [record] : undefined);
  await second.client.boot(); assert.equal(second.client.state.pending.uncertain, false); assert.equal(second.client.state.job.requests_used, 2);
  assert.equal(second.posts().length, 0); h.client.dispose(); second.client.dispose();
});
await test('断线超过上限暂停查询，不变成模型重试', async () => {
  const h = harness(); h.route(call => call.path.endsWith('/question-jobs') ? [job()] : undefined); await h.client.boot();
  h.route(call => { if (call.path === '/api/question-jobs/job-a') throw new Error('offline'); });
  for (let i = 0; i < 7; i++) await h.client.poll();
  assert.equal(h.timers.size, 0); assert.match(h.client.state.notice, /暂停/); assert.equal(h.posts().length, 0); h.client.dispose();
});
console.log(`${passed} workspace client scenarios passed`);
