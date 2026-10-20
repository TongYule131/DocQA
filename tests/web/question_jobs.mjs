// Offline regression against production app.js. No service, model, or browser is started.
// The transport simulates response loss, refresh, delayed responses, and a hung GET;
// assertions concern request counts and visible outcomes rather than helper internals.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import vm from 'node:vm'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const source = readFileSync(resolve(root, 'app/web/app.js'), 'utf8')
const html = readFileSync(resolve(root, 'app/web/index.html'), 'utf8')
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1])
class Node {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.style = {}
    this._text = ''; this.value = ''; this.hidden = false; this.disabled = false; this.href = ''
    this.classList = { add() {}, remove() {}, contains() { return false } }
  }
  get textContent() { return this.children.length ? this.children.map((item) => item.textContent).join('') : this._text }
  set textContent(value) { this.children = []; this._text = String(value) }
  append(...nodes) { this.children.push(...nodes.map((item) => typeof item === 'string' ? new Text(item) : item)) }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes) }
  querySelector() { return new Node('button') }
  querySelectorAll(tag) { return this.allNodes().filter((item) => item.tagName === tag.toUpperCase()) }
  addEventListener() {}
  setAttribute() {}
  scrollIntoView() {}
  focus() {}
  allNodes() { return this.children.flatMap((item) => [item, ...item.allNodes()]) }
}
class Text extends Node { constructor(text) { super(''); this._text = text } }
const docs = ['doc-a', 'doc-b'].map((id) => ({ id, filename: `${id}.txt`, format: 'txt',
  status: 'parsed', active_parse_version_id: `${id}-v`, chunk_count: 1, size: 100, page_count: 0 }))
const answer = (text = '已存回答。') => ({ answer_id: 'answer-a', status: 'answered',
  answer: text, parse_version_id: 'doc-a-v', index_id: 'index-a', prompt_version: 'offline',
  retrieval: { candidate_count: 8, selected_count: 5 }, timings_ms: { retrieval: 5, generation: 20, total: 25 },
  citations: [], limitations: [], quality_warnings: [], clarification_questions: [] })
const job = (overrides = {}) => ({ id: 'job-a', document_id: 'doc-a', question: '关键数据是什么？',
  status: 'queued', stage: 'queued', requests_used: 0, max_requests: 2,
  parse_version_id: 'doc-a-v', index_id: 'index-a', created_at: '2026-10-18T00:00:00Z', ...overrides })
const result = (overrides = {}) => ({ id: 'result-a', job_id: 'job-a', document_id: 'doc-a',
  answer: answer(), created_at: '2026-10-18T00:00:20Z', ...overrides })
const response = (data) => ({ ok: true, status: 200, json: async () => data })
const tick = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve() }
function deferred() { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }

async function harness(storage = new Map()) {
  const elements = new Map(ids.map((id) => [id, new Node()]))
  elements.get('extraction-highlights').checked = true
  const timers = new Map(); let timerId = 0
  const requests = []
  let route = null
  const visibility = []
  const document = { hidden: false, getElementById: (id) => elements.get(id),
    createElement: (tag) => new Node(tag), createTextNode: (text) => new Text(text),
    createDocumentFragment: () => new Node('fragment'), querySelector: () => null,
    querySelectorAll: () => [], addEventListener: (event, fn) => { if (event === 'visibilitychange') visibility.push(fn) } }
  const base = (url) => {
    if (url === '/api/documents') return []
    if (url.endsWith('/status')) return { configured: true, model: 'offline', note: '' }
    if (url.endsWith('/index')) return { status: 'indexed', parse_version_id: `${url.split('/')[3]}-v` }
    if (url.includes('/content?')) return { version: { id: `${url.split('/')[3]}-v`,
      parser_name: 'offline', quality_status: 'ok', warnings: [] }, total: 0, returned: 0,
      blocks: [], is_active_version: true }
    if (url.endsWith('/analysis-jobs') || url.endsWith('/analysis-results')) return []
    throw new Error(`Unexpected offline request: ${url}`)
  }
  const sandbox = { document, window: {}, crypto: { randomUUID }, AbortController,
    sessionStorage: { getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    fetch: async (url, options = {}) => {
      const entry = { url, options, body: options.body ? JSON.parse(options.body) : null }
      requests.push(entry)
      if (route && /question-(jobs|results)/.test(url)) return route(entry)
      return response(base(url))
    },
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id },
    clearTimeout: (id) => timers.delete(id), setInterval: () => 0, clearInterval() {},
    console, Event: class { constructor(type) { this.type = type } }, FormData: class {},
    encodeURIComponent, Date, JSON, String, Number, Object, Math, RegExp, Promise,
  }
  sandbox.window = sandbox; sandbox.globalThis = sandbox
  const context = vm.createContext(sandbox)
  const evaluate = (code) => vm.runInContext(code, context)
  evaluate(source)
  for (let i = 0; i < 500 && evaluate('busy'); i += 1) await Promise.resolve()
  assert.equal(evaluate('busy'), false, 'initial read-only page load must settle before scenario')
  evaluate(`documents=${JSON.stringify(docs)}; selectedId='doc-a'; busy=false;
    ragConfigured=true; embeddingConfigured=true; analysisConfigured=true; indexState='indexed'; controls();`)
  requests.length = 0
  return { elements, timers, requests, storage, sandbox, document, evaluate,
    hooks: sandbox.__docqaTest, setRoute(fn) { route = fn },
    async click(id) { await elements.get(id).onclick(); await tick() },
    async ask(text = '关键数据是什么？') { elements.get('question').value = text; await evaluate('askQuestion()'); await tick() },
    changeVisibility(hidden) { document.hidden = hidden; visibility.forEach((fn) => fn()) },
    posts: () => requests.filter((entry) => entry.options.method === 'POST'),
  }
}

const tests = []
async function test(name, fn) {
  try { await fn(); console.log(`PASS ${name}`); tests.push(true) }
  catch (error) { console.error(`FAIL ${name}\n${error.stack}`); tests.push(false) }
}

await test('single submit, duplicate Enter, immediate ID/stage, and no global workbench lock', async () => {
  const h = await harness(); const pending = deferred(); let saved
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      assert.ok(h.storage.has('docqa.question.doc-a'), 'restore key must precede transport')
      saved = job({ idempotency_key: request.body.idempotency_key }); return pending.promise
    }
    return response([saved])
  })
  h.elements.get('question').value = '关键数据是什么？'
  const first = h.evaluate('askQuestion()')
  await h.evaluate('askQuestion()')
  assert.equal(h.posts().length, 1)
  assert.equal(h.hooks.questionState().busy, false)
  assert.equal(h.hooks.questionState().answerBusy, true)
  pending.resolve(response({ job: saved, reused: false })); await first
  assert.match(h.elements.get('question-job').textContent, /job-a.*排队/)
  assert.equal(h.hooks.questionState().answerBusy, false)
  assert.equal(h.elements.get('refresh').disabled, false)
  assert.equal(h.hooks.questionState().uncertain, false)
})

await test('poll, history reopening, and export links use only GET and preserve answer safety', async () => {
  const h = await harness(); let saved
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return response({ job: saved })
    }
    if (request.url.endsWith('/question-jobs')) return response([saved])
    if (request.url.includes('/question-results/')) return response(result({ answer: answer('<img src=x onerror=alert(1)> 95.3%') }))
    return response(saved)
  })
  await h.ask(); const count = h.posts().length
  saved = { ...saved, status: 'succeeded', stage: 'done', result_id: 'result-a' }
  await h.evaluate('pollQuestionJob()')
  assert.equal(h.hooks.questionState().polling, 0)
  assert.match(h.elements.get('answer-body').textContent, /95.3%/)
  const nodes = h.elements.get('answer-panel').allNodes()
  assert.equal(nodes.some((node) => ['IMG', 'SCRIPT', 'SVG'].includes(node.tagName)), false)
  assert.deepEqual(h.elements.get('answer-actions').allNodes().filter((node) => node.tagName === 'A').map((node) => node.href), [
    '/api/question-results/result-a/export?format=markdown', '/api/question-results/result-a/export?format=json'])
  await h.evaluate("openQuestionJob('job-a')")
  await h.click('question-refresh')
  assert.equal(h.posts().length, count)
})

await test('browser reload restores active task using server history and saved request key without POST', async () => {
  const storage = new Map(); const first = await harness(storage); let saved
  first.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return response({ job: saved })
    }
    return response([saved])
  })
  await first.ask()
  const second = await harness(storage)
  second.setRoute(() => response([saved]))
  await second.evaluate('restoreQuestions()')
  assert.equal(second.hooks.questionState().jobId, saved.id)
  assert.equal(second.hooks.questionState().polling, 1)
  assert.equal(second.posts().length, 0)
})

await test('lost POST response resolves by GET history without automatic resubmission', async () => {
  const h = await harness(); let saved
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key })
      throw new Error('Response lost after server commit')
    }
    return response([saved])
  })
  await h.ask()
  assert.equal(h.posts().length, 1)
  assert.equal(h.hooks.questionState().jobId, saved.id)
  assert.equal(h.hooks.questionState().uncertain, false)
})

await test('unconfirmed submission survives reload; only explicit confirmation repeats original key and question', async () => {
  const storage = new Map(); const first = await harness(storage)
  first.setRoute((request) => {
    if (request.options.method === 'POST') throw new Error('connection lost')
    return response([])
  })
  await first.ask('原问题')
  const original = first.posts()[0].body
  assert.match(first.elements.get('question-job').textContent, /尚未确认/)
  assert.equal(first.hooks.questionState().uncertain, true)
  const second = await harness(storage); let saved
  second.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ question: request.body.question, idempotency_key: request.body.idempotency_key })
      return response({ job: saved })
    }
    return response(saved ? [saved] : [])
  })
  await second.evaluate('restoreQuestions()')
  await second.ask('修改后的问题不能偷换原提交')
  assert.equal(second.posts().length, 0)
  await second.click('question-confirm')
  assert.equal(second.posts().length, 1)
  assert.deepEqual(second.posts()[0].body, original)
})

await test('accepted submit followed by history GET failure keeps known task instead of claiming lost submit', async () => {
  const h = await harness(); let saved
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return response({ job: saved })
    }
    throw new Error('history unavailable')
  })
  await h.ask()
  assert.equal(h.hooks.questionState().uncertain, false)
  assert.equal(h.hooks.questionState().jobId, 'job-a')
  assert.match(h.elements.get('message').textContent, /已接收/)
  assert.equal(h.posts().length, 1)
})

await test('explicit validation rejection permits correction; missing local restore storage blocks POST', async () => {
  const h = await harness()
  h.setRoute(() => ({ ok: false, status: 422, json: async () => ({ detail: '问题超出输入限制' }) }))
  await h.ask()
  assert.equal(h.hooks.questionState().uncertain, false)
  assert.match(h.elements.get('question-job').textContent, /被服务器拒绝/)
  assert.equal(h.elements.get('ask').disabled, false)
  const count = h.posts().length
  h.sandbox.sessionStorage.setItem = () => { throw new Error('Storage denied') }
  await h.ask('修改后的问题')
  assert.equal(h.posts().length, count)
  assert.match(h.elements.get('message').textContent, /无法保存问答恢复记录/)
})

await test('switching document during submission discards late UI response while saving original task for recovery', async () => {
  const h = await harness(); const late = deferred(); let saved
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return late.promise
    }
    return response(request.url.includes('/doc-b/') ? [] : [saved])
  })
  h.elements.get('question').value = '文档A问题'; const asking = h.evaluate('askQuestion()')
  await h.evaluate("select('doc-b')")
  late.resolve(response({ job: saved })); await asking
  assert.equal(h.hooks.state().selectedId, 'doc-b')
  assert.equal(h.hooks.questionState().jobId, null)
  assert.doesNotMatch(h.elements.get('question-job').textContent, /job-a/)
  const persisted = JSON.parse(h.storage.get('docqa.question.doc-a'))
  assert.equal(persisted.job_id, 'job-a')
  assert.equal(persisted.uncertain, false)
  assert.equal(h.hooks.questionState().answerBusy, false)
})

await test('late status response and late result response cannot replace another document', async () => {
  const h = await harness(); const lateStatus = deferred(); const lateResult = deferred(); let saved
  let phase = 'submit'
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return response({ job: saved })
    }
    if (request.url.endsWith('/question-jobs')) return response(request.url.includes('/doc-b/') ? [] : [saved])
    if (request.url.includes('/question-results/')) return lateResult.promise
    return phase === 'late-status' ? lateStatus.promise : response(saved)
  })
  await h.ask(); phase = 'late-status'
  const polling = h.evaluate('pollQuestionJob()')
  await h.evaluate("select('doc-b')")
  lateStatus.resolve(response({ ...saved, status: 'succeeded', result_id: 'result-a' })); await polling
  assert.equal(h.hooks.questionState().jobId, null)
  assert.equal(h.requests.some((entry) => entry.url.includes('/question-results/')), false)
  phase = 'submit'; saved = { ...saved, status: 'succeeded', stage: 'done', result_id: 'result-a' }
  const switchingBack = h.evaluate("select('doc-a')")
  await tick()
  await h.evaluate("select('doc-b')")
  lateResult.resolve(response(result())); await switchingBack
  assert.equal(h.hooks.questionState().resultId, null)
  assert.equal(h.elements.get('answer-panel').hidden, true)
})

await test('same-document history selection invalidates previous job poll response', async () => {
  const h = await harness(); let saved; const late = deferred(); let pollBlocked = false
  const historical = job({ id: 'job-old', status: 'succeeded', result_id: 'result-old' })
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return response({ job: saved })
    }
    if (request.url.endsWith('/question-jobs')) return response([saved, historical])
    if (request.url.endsWith('/question-jobs/job-old')) return response(historical)
    if (request.url.includes('/question-results/')) return response(result({ id: 'result-old', job_id: 'job-old', answer: answer('历史回答') }))
    return pollBlocked ? late.promise : response(saved)
  })
  await h.ask(); pollBlocked = true
  const polling = h.evaluate('pollQuestionJob()')
  await h.evaluate("openQuestionJob('job-old')")
  late.resolve(response({ ...saved, stage: 'generating', status: 'running' })); await polling
  assert.equal(h.hooks.questionState().jobId, 'job-old')
  assert.match(h.elements.get('answer-body').textContent, /历史回答/)
  assert.doesNotMatch(h.elements.get('question-job').textContent, /job-a/)
})

await test('manual refresh of same job discards an older in-flight status response', async () => {
  const h = await harness(); let saved; let blocked = false; const late = deferred()
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return response({ job: saved })
    }
    if (request.url.endsWith('/question-jobs')) return response([saved])
    return blocked ? late.promise : response(saved)
  })
  await h.ask(); blocked = true
  const oldPoll = h.evaluate('pollQuestionJob()')
  saved = { ...saved, status: 'failed', stage: 'validating', error_message: '当前已校验失败' }
  await h.click('question-refresh')
  late.resolve(response({ ...saved, status: 'running', stage: 'generating', error_message: null })); await oldPoll
  assert.equal(h.hooks.questionState().jobStatus, 'failed')
  assert.match(h.elements.get('question-job').textContent, /当前已校验失败/)
  assert.equal(h.hooks.questionState().polling, 0)
})

await test('hung status GET aborts after 10 seconds and backs off; repeated failures never create POST', async () => {
  const h = await harness(); let saved; let hung = false
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return response({ job: saved })
    }
    if (request.url.endsWith('/question-jobs')) return response([saved])
    if (hung) return new Promise((resolve, reject) => request.options.signal.addEventListener('abort', () => reject(new Error('timeout'))))
    return response(saved)
  })
  await h.ask(); hung = true
  const polling = h.evaluate('pollQuestionJob()')
  const timeout = [...h.timers.values()].find((entry) => entry.delay === 10000)
  assert.ok(timeout); timeout.fn(); await polling
  assert.equal(h.hooks.questionState().pollDelay, 5000)
  const count = h.posts().length
  h.setRoute(() => { throw new Error('offline') })
  for (let i = 0; i < 6; i += 1) await h.evaluate('pollQuestionJob()')
  assert.equal(h.hooks.questionState().polling, 0)
  assert.match(h.elements.get('question-job').textContent, /已暂停自动查询/)
  assert.equal(h.posts().length, count)
})

await test('retry is explicit, respects cumulative request cap, and uses idempotent retry payload', async () => {
  const h = await harness(); let saved = job({ status: 'failed', stage: 'failed', requests_used: 1 })
  h.setRoute((request) => {
    if (request.url.endsWith('/retry')) {
      assert.deepEqual(Object.keys(request.body), ['idempotency_key'])
      saved = { ...saved, status: 'queued', stage: 'queued', last_retry_key: request.body.idempotency_key }
      return response({ job: saved })
    }
    return response(request.url.endsWith('/question-jobs') ? [saved] : saved)
  })
  await h.evaluate('restoreQuestions()')
  assert.equal(h.posts().length, 0)
  await h.click('question-retry')
  assert.equal(h.posts().length, 1)
  assert.match(h.posts()[0].url, /job-a\/retry$/)
  assert.equal(h.hooks.questionState().uncertain, false)
  saved = { ...saved, status: 'failed', requests_used: 2 }
  await h.evaluate('pollQuestionJob()')
  assert.equal(h.elements.get('question-retry').disabled, true)
  await h.click('question-retry')
  assert.equal(h.posts().length, 1)
})

await test('unconfirmed retry cannot be mistaken for an old failed job or assigned a new retry key', async () => {
  const h = await harness(); const saved = job({ status: 'needs_attention', stage: 'generating', requests_used: 1 })
  h.setRoute((request) => {
    if (request.url.endsWith('/retry')) throw new Error('response lost')
    return response(request.url.endsWith('/question-jobs') ? [saved] : saved)
  })
  await h.evaluate('restoreQuestions()'); await h.click('question-retry')
  assert.equal(h.hooks.questionState().uncertain, true)
  const key = h.hooks.questionState().pendingKey
  await h.click('question-retry')
  assert.equal(h.posts().length, 1)
  await h.click('question-confirm')
  assert.equal(h.posts().length, 2)
  assert.equal(h.posts()[1].body.idempotency_key, key)
})

await test('cancel reports possible charge and polls existing task without regeneration', async () => {
  const h = await harness(); let saved
  h.setRoute((request) => {
    if (request.url.endsWith('/cancel')) { saved = { ...saved, status: 'cancelled', stage: 'cancelled' }; return response(saved) }
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return response({ job: saved })
    }
    return response(request.url.endsWith('/question-jobs') ? [saved] : saved)
  })
  await h.ask(); await h.click('question-cancel')
  assert.equal(h.posts().length, 2)
  assert.match(h.elements.get('message').textContent, /仍可能计费/)
  await h.evaluate('pollQuestionJob()')
  assert.equal(h.hooks.questionState().jobStatus, 'cancelled')
  assert.equal(h.hooks.questionState().polling, 0)
})

await test('visibility pause and resume only query the active job', async () => {
  const h = await harness(); let saved
  h.setRoute((request) => {
    if (request.options.method === 'POST') {
      saved = job({ idempotency_key: request.body.idempotency_key }); return response({ job: saved })
    }
    return response(request.url.endsWith('/question-jobs') ? [saved] : saved)
  })
  await h.ask(); const count = h.posts().length
  h.changeVisibility(true)
  assert.equal(h.hooks.questionState().polling, 0)
  h.changeVisibility(false); await tick()
  assert.equal(h.hooks.questionState().polling, 1)
  assert.equal(h.posts().length, count)
})

await test('fact conditions and part coverage display safely and clear with the answer', async () => {
  const h = await harness()
  const value = { status: 'answered', answer: '- 收入1280万元（期间：2025年；限定：未经审计）[1]', citations: [],
    answer_parts: [{part_id:'p1', state:'answered', text:'<img src=x onerror=alert(1)>金额口径？', missing_detail:''}] }
  h.evaluate(`renderAnswer(${JSON.stringify(value)})`)
  const body = h.elements.get('answer-body')
  assert.match(body.textContent, /未经审计/)
  assert.match(body.textContent, /问题覆盖/)
  assert.match(body.textContent, /<img/)
  assert.equal(body.allNodes().filter(n => ['IMG','SCRIPT','IFRAME'].includes(n.tagName)).length, 0)
  assert.equal(h.posts().length, 0)
  h.evaluate('resetAnswer()')
  assert.equal(body.textContent, '')
})

await test('partially answered single part keeps its concrete gap visible without a new request', async () => {
  const h = await harness()
  const value = { status:'partial', answer:'- 收入已列明。[1]', citations:[],
    answer_parts:[{part_id:'p1',state:'partial',text:'收入与成本是多少？',missing_detail:'成本范围<img src=x>尚未给出。'}] }
  h.evaluate(`renderAnswer(${JSON.stringify(value)})`)
  const body = h.elements.get('answer-body')
  assert.match(body.textContent,/部分回答/)
  assert.match(body.textContent,/成本范围<img src=x>尚未给出/)
  assert.equal(body.allNodes().filter(n=>n.tagName==='IMG').length,0)
  assert.equal(h.requests.length,0)
})

await test('legacy answers remain visible without invented coverage or extra calls', async () => {
  const h = await harness()
  h.evaluate(`renderAnswer(${JSON.stringify({status:'answered',answer:'- 旧答案正文。[1]',citations:[]})})`)
  assert.match(h.elements.get('answer-body').textContent, /旧答案正文/)
  assert.doesNotMatch(h.elements.get('answer-body').textContent, /问题覆盖/)
  assert.equal(h.requests.length, 0)
})

await test('long generation shows elapsed time and safe refresh guidance without any request', async () => {
  const h = await harness()
  h.sandbox.Date = class extends Date { static now() { return Date.parse('2026-10-19T08:00:45Z') } }
  const running = job({status:'running',stage:'generating',started_at:'2026-10-19T08:00:00Z'})
  h.evaluate(`renderQuestionJob(${JSON.stringify(running)})`)
  assert.match(h.elements.get('question-job').textContent,/本次执行已等待 45 秒/)
  assert.match(h.elements.get('question-job').textContent,/刷新不会重复提交/)
  h.evaluate(`renderQuestionJob(${JSON.stringify({...running,status:'succeeded',stage:'done'})})`)
  assert.doesNotMatch(h.elements.get('question-job').textContent,/已等待|生成可能需要/)
  assert.equal(h.requests.length,0)
})

await test('queued, invalid and future timestamps do not produce misleading negative or NaN waits', async () => {
  const h = await harness()
  h.sandbox.Date = class extends Date { static now() { return Date.parse('2026-10-19T08:00:00Z') } }
  h.evaluate(`renderQuestionJob(${JSON.stringify(job({created_at:'2026-10-19T08:01:00Z'}))})`)
  assert.match(h.elements.get('question-job').textContent,/排队已等待 0 秒/)
  h.evaluate(`renderQuestionJob(${JSON.stringify(job({created_at:'invalid'}))})`)
  assert.doesNotMatch(h.elements.get('question-job').textContent,/已等待|NaN/)
  assert.equal(h.requests.length,0)
})

console.log(`\nQuestion job UI: ${tests.filter(Boolean).length}/${tests.length} passed`)
process.exitCode = tests.every(Boolean) ? 0 : 1
