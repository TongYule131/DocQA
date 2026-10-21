// 真实会话传输桥的行为验证；HTTP 和页面边界合成，不创建真实账号或调用模型。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const script = await readFile(new URL('../../app/web/auth-bridge.js', import.meta.url), 'utf8');
async function boot(session, initial = {}, handler = null) {
  const storage = { ...initial };
  Object.defineProperties(storage, {
    getItem: { value: key => storage[key] ?? null }, setItem: { value: (key, value) => { storage[key] = value; } },
    removeItem: { value: key => { delete storage[key]; } },
  });
  const calls = [], navigations = [], adminLinks = [{ hidden: false }], logout = { hidden: true }, notice = {};
  const document = {
    body: { dataset: {}, prepend() {} },
    querySelectorAll: selector => selector === '[data-admin-only]' ? adminLinks : selector === '[data-logout]' ? [logout] : [],
    querySelector: () => null, getElementById: () => notice, createElement: () => ({}),
  };
  const location = { pathname: '/app', assign: path => navigations.push(path), replace: path => navigations.push(path) };
  const context = { window: {}, Headers, document, location, sessionStorage: storage,
    fetch: async (path, options = {}) => {
      calls.push({ path, options });
      if (path === '/api/auth/me') return new Response(JSON.stringify(session));
      return handler ? handler(path, options) : new Response('{}');
    } };
  vm.runInNewContext(script, context); await context.window.docqaAuthReady;
  return { ...context, storage, calls, navigations, adminLinks, logout, notice };
}

const local = await boot({ team_mode: false, user: null, csrf_token: '' }, { 'docqa.question.doc': 'pending' });
assert.equal(local.storage['docqa.question.doc'], 'pending');
assert.equal(local.calls.length, 1); assert.equal(local.calls[0].path, '/api/auth/me');
assert.equal(local.document.body.dataset.role, 'admin');
console.log('PASS 首次本地升级保留旧问答恢复键，页面启动仅 GET');

const user = { id: 'alice', role: 'customer', display_name: '合成客户' };
const team = await boot({ team_mode: true, user, csrf_token: 'synthetic-csrf' }, {
  'docqa.identity': 'bob', 'docqa.question.doc': 'other-user-pending', 'unrelated': 'keep',
});
assert.equal(team.storage['docqa.question.doc'], undefined); assert.equal(team.storage.unrelated, 'keep');
assert.equal(team.storage['docqa.identity'], 'alice'); assert.equal(team.adminLinks[0].hidden, true);
assert.ok(!JSON.stringify(team.storage).includes('synthetic-csrf'));
console.log('PASS 切换用户清除旧恢复范围，保留无关存储，CSRF 不进入网页存储');

await team.window.docqaFetch('/api/documents', { method: 'GET' });
await team.window.docqaFetch('/api/documents/doc/question-jobs', { method: 'POST', body: 'synthetic' });
assert.equal(team.calls.at(-2).options.headers.has('X-CSRF-Token'), false);
assert.equal(team.calls.at(-1).options.headers.get('X-CSRF-Token'), 'synthetic-csrf');
assert.equal(team.calls.filter(call => call.options.method === 'POST').length, 1);
await assert.rejects(team.window.docqaFetch('https://example.invalid'), /请求地址/);
console.log('PASS 变更请求带 CSRF，读取不带；无自动 POST 或跨站 token 传输');

const expired = await boot({ team_mode: true, user, csrf_token: 'synthetic' }, {}, () => new Response('{}', { status: 401 }));
await expired.window.docqaFetch('/api/documents');
assert.deepEqual(expired.navigations, ['/login']);
assert.equal(expired.calls.length, 2);
console.log('PASS 失效会话转登录，不自动认证或重新提交业务请求');

await team.logout.onclick();
assert.equal(team.calls.at(-1).path, '/api/auth/logout');
assert.equal(team.calls.at(-1).options.method, 'POST');
assert.equal(team.navigations.at(-1), '/login');
console.log('PASS 退出登录只提交一次显式请求');

const anonymous = await boot({ team_mode: true, user: null, csrf_token: '' });
assert.equal(anonymous.navigations[0], '/login');
assert.equal(anonymous.adminLinks[0].hidden, true);
console.log('PASS 未登录客户进入登录页，管理入口保持隐藏');
console.log('6 product auth scenarios passed');
