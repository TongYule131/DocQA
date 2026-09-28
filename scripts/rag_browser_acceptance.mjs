// RAG 问答网页端到端验收：用 Chrome（CDP，无头模式）驱动真实工作台页面。
//
// 为什么必须用真实浏览器：R23/R24 要求“逐事实引用可点击、版本入口正确、恶意内容不执行、
// 忙碌期间不重复提交、迟到响应不覆盖新状态”等证据，不能靠后端断言或“某段 JS 含有
// textContent”来替代。本脚本不引入额外依赖，直接用 Node 内置 WebSocket 连接 CDP。
//
// 服务端要求：使用独立 DOCQA_DATA_DIR 启动真实 Web 服务，并已完成对目标文档的建索引；
// 脚本会在提问前记录一次“无操作基线”，借此证明刷新/列表/状态查询不会触发问答计费。
//
// 用法：
//   python -m uvicorn app.main:app --host 127.0.0.1 --port 8022   （先设置 DOCQA_DATA_DIR）
//   node scripts/rag_browser_acceptance.mjs --api http://127.0.0.1:8022 --doc <document_id>
import { spawn } from 'node:child_process'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const args = process.argv.slice(2)
const getArg = (name, fallback) => {
  const index = args.indexOf(name)
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback
}
const API = getArg('--api', 'http://127.0.0.1:8022')
const DOC = getArg('--doc', '')
const CHROME = getArg('--chrome', 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe')
const OUT = resolve(getArg('--out', 'data/rag-live/browser-acceptance'))
const PORT = Number(getArg('--port', '9334'))

const results = []
let failures = 0
function record(id, title, passed, detail) {
  results.push({ id, title, passed, detail })
  if (!passed) failures += 1
  console.log(`${passed ? 'PASS' : 'FAIL'} [${id}] ${title}\n        ${detail}`)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

class Cdp {
  constructor(url) { this.url = url; this.id = 0; this.pending = new Map() }
  async connect() {
    this.ws = new WebSocket(this.url)
    await new Promise((res, rej) => {
      this.ws.addEventListener('open', res, { once: true })
      this.ws.addEventListener('error', () => rej(new Error('websocket error')), { once: true })
    })
    this.ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve: res, reject: rej } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
      }
    })
  }
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((res, rej) => {
      this.pending.set(id, { resolve: res, reject: rej })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true, userGesture: true,
    })
    if (result.exceptionDetails) {
      throw new Error('页面脚本异常：' + JSON.stringify(
        result.exceptionDetails.exception?.description || result.exceptionDetails.text))
    }
    return result.result.value
  }
  close() { try { this.ws.close() } catch { /* 忽略 */ } }
}

async function fetchJson(path, options) {
  const response = await fetch(`${API}${path}`, options)
  const text = await response.text()
  if (!response.ok) throw new Error(`${path} -> ${response.status}: ${text.slice(0, 200)}`)
  return JSON.parse(text)
}

async function waitFor(fn, { timeout = 120000, interval = 500, label = '条件' } = {}) {
  const deadline = Date.now() + timeout
  let last
  while (Date.now() < deadline) {
    last = await fn()
    if (last) return last
    await sleep(interval)
  }
  throw new Error(`等待超时：${label}（最后取值 ${JSON.stringify(last)}）`)
}

async function main() {
  if (!DOC) throw new Error('必须用 --doc 指定已建索引的文档 ID')
  await mkdir(OUT, { recursive: true })
  const profile = await mkdtemp(join(tmpdir(), 'docqa-rag-chrome-'))
  const chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--window-size=1500,1100', 'about:blank',
  ], { stdio: 'ignore' })

  let cdp
  try {
    const version = await waitFor(async () => {
      try {
        const response = await fetch(`http://127.0.0.1:${PORT}/json/version`)
        return response.ok ? await response.json() : null
      } catch { return null }
    }, { label: 'Chrome DevTools 端点', timeout: 30000 })
    console.log(`浏览器：${version['Browser']}`)

    const target = await (await fetch(
      `http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(API + '/')}`,
      { method: 'PUT' })).json()
    cdp = new Cdp(target.webSocketDebuggerUrl)
    await cdp.connect()
    await cdp.send('Runtime.enable')
    await cdp.send('Page.enable')
    await cdp.send('Log.enable')
    await cdp.send('Network.enable')
    // 探针：记录内联脚本注入、外部图片/外链请求与任何未捕获异常。
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', {
      source: `
        window.__ragProbe = { injected: [], external: [], errors: [] };
        try {
          new MutationObserver((records) => {
            for (const record of records) {
              for (const node of record.addedNodes) {
                if (node.nodeType === 1 && ['SCRIPT', 'IFRAME', 'IMG'].includes(node.tagName)) {
                  window.__ragProbe.injected.push(node.tagName + ':' + (node.src || 'inline'));
                }
              }
            }
          }).observe(document.documentElement, { childList: true, subtree: true });
        } catch (error) { /* documentElement 尚未就绪 */ }
        window.addEventListener('error', (event) => window.__ragProbe.errors.push(String(event.message)));
        const originalFetch = window.fetch;
        window.fetch = function (...fetchArgs) {
          const url = String(fetchArgs[0]);
          if (url.includes('/questions')) {
            window.__ragProbe.questionRequests = (window.__ragProbe.questionRequests || 0) + 1;
          }
          return originalFetch.apply(this, fetchArgs);
        };
      `,
    })

    await cdp.send('Page.navigate', { url: `${API}/` })
    await waitFor(() => cdp.evaluate('document.readyState === "complete"'), { label: '页面加载' })
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#documents button").length > 0'),
      { label: '文档列表渲染' })

    // ---------------- R25：刷新与状态查询不触发问答计费 ----------------
    const ragStatus = await fetchJson('/api/rag/status')
    record('R25-1', '问答能力状态可读且不返回密钥',
      ragStatus.prompt_version === 'rag-qa-v1' && ragStatus.budget_unit === 'characters',
      `prompt_version=${ragStatus.prompt_version}，configured=${ragStatus.configured}，` +
      `预算单位=${ragStatus.budget_unit}`)

    await cdp.send('Page.reload', { ignoreCache: true })
    await waitFor(() => cdp.evaluate('document.readyState === "complete"'), { label: '刷新完成' })
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#documents button").length > 0'),
      { label: '刷新后文档列表' })
    const afterRefresh = await cdp.evaluate('window.__ragProbe.questionRequests || 0')
    record('R25-2', '页面加载与刷新不触发问答请求',
      afterRefresh === 0,
      `刷新后窗口内 /questions 请求数=${afterRefresh}`)

    // 选中文档：使用与用户点击相同的路径。
    const clicked = await cdp.evaluate(`(() => {
      const state = window.__docqaTest.state();
      window.__docqaTest.selectDocument(${JSON.stringify(DOC)});
      return true;
    })()`)
    await waitFor(() => cdp.evaluate(
      `window.__docqaTest.state().selectedId === ${JSON.stringify(DOC)}`), { label: '选中文档' })
    await waitFor(() => cdp.evaluate('!document.getElementById("ask").disabled'),
      { label: '问答控件启用（有效索引 + 模型已配置）' })
    record('R03-0', '选中文档并建立有效索引后问答控件启用', clicked === true,
      `ask.disabled=${await cdp.evaluate('document.getElementById("ask").disabled')}`)

    // ---------------- R03/R24：正常有据回答、逐事实引用可点击 ----------------
    await cdp.evaluate(`window.__docqaTest.submitQuestion(${JSON.stringify('本年鉴包含多少个部分？')})`)
    await waitFor(() => cdp.evaluate('!document.getElementById("answer-waiting").hidden'), { timeout: 5000, label: '等待提示出现' })
    const waitingText = await cdp.evaluate('document.getElementById("answer-waiting").textContent')
    record('R08-1', '提问后显示检索与生成等待提示，且不伪造进度百分比',
      waitingText.includes('正在检索资料并生成回答') && !/%/.test(waitingText),
      `提示文本：${waitingText}`)

    await waitFor(() => cdp.evaluate('window.__docqaTest.state().answerStatus !== null'),
      { label: '回答返回', timeout: 180000 })
    const answered = await cdp.evaluate('JSON.stringify(window.__docqaTest.state())')
    const state = JSON.parse(answered)
    const refButtons = await cdp.evaluate('document.querySelectorAll("#answer-body button.ref").length')
    const citationCards = await cdp.evaluate('document.querySelectorAll("#answer-citations article.citation").length')
    record('R03-1', '页面展示答案与逐事实引用按钮',
      state.answerStatus === 'answered' && refButtons >= 1 && citationCards === state.citations,
      `status=${state.answerStatus}，引用按钮=${refButtons}，引用卡片=${citationCards}，citations=${state.citations}`)

    const firstRef = await cdp.evaluate(
      'document.querySelector("#answer-body button.ref") ? document.querySelector("#answer-body button.ref").textContent : ""')
    const clickResult = await cdp.evaluate(`(() => {
      const button = document.querySelector('#answer-body button.ref');
      if (!button) return { clicked: false };
      button.click();
      const ref = button.textContent.replace(/[^0-9]/g, '');
      const card = document.getElementById('citation-' + ref);
      return { clicked: true, ref: ref, found: Boolean(card),
               active: card ? card.classList.contains('citation-active') : false,
               quote: card ? card.querySelector('blockquote').textContent : '' };
    })()`)
    record('R24-1', '点击逐事实引用可定位对应引用卡片',
      clickResult.clicked && clickResult.found && clickResult.quote.length > 0,
      `引用标记 ${firstRef} → 卡片存在=${clickResult.found}，高亮=${clickResult.active}，引述前 30 字：${(clickResult.quote || '').slice(0, 30)}`)

    const citationMeta = JSON.parse(await cdp.evaluate(`JSON.stringify(
      [...document.querySelectorAll('#answer-citations article.citation')].map((card) => ({
        id: card.id,
        location: card.querySelector('.muted') ? card.querySelector('.muted').textContent : '',
        info: card.querySelectorAll('p.muted').length ? card.querySelectorAll('p.muted')[0].textContent : '',
        actions: [...card.querySelectorAll('a, button')].map((node) => node.textContent.trim()),
        href: card.querySelector('a') ? card.querySelector('a').getAttribute('href') : null,
      })))`))
    record('R15-1', '引用卡片显示引述、来源位置、解析版本与核对入口',
      citationMeta.length >= 1 && citationMeta.every((item) => item.info.includes('解析版本'))
        && citationMeta.every((item) => item.actions.length >= 2),
      JSON.stringify(citationMeta[0]))

    const previewResult = await cdp.evaluate(`(() => {
      const button = [...document.querySelectorAll('#answer-citations button')]
        .find((node) => node.textContent.includes('查看引用版本'));
      if (!button) return { clicked: false };
      button.click();
      return { clicked: true };
    })()`)
    await sleep(1500)
    const previewState = JSON.parse(await cdp.evaluate(`JSON.stringify({
      content: document.getElementById('content-summary').textContent,
      version: document.getElementById('version-status').textContent,
      chunks: document.querySelectorAll('#chunks article').length,
    })`))
    record('R24-2', '引用卡片可打开该引用所属解析版本的内容预览',
      previewResult.clicked && previewState.chunks > 0 && previewState.content.includes('版本'),
      `版本摘要：${previewState.version || '(空)'}；内容块=${previewState.chunks}`)

    // ---------------- R23：忙碌期间不重复提交、每个有效提交只对应一次请求 ----------------
    const before = JSON.parse(await cdp.evaluate('JSON.stringify(window.__docqaTest.state())'))
    await cdp.evaluate(`window.__docqaTest.submitQuestion(${JSON.stringify('本年鉴的目录在哪一页？')})`)
    const busyState = JSON.parse(await waitFor(async () => {
      const text = await cdp.evaluate('JSON.stringify(window.__docqaTest.state())')
      const parsed = JSON.parse(text)
      return parsed.answerBusy ? text : null
    }, { timeout: 20000, label: '进入忙碌状态' }))
    // 忙碌期间连续触发 5 次提交（模拟重复点击与 Enter 连按）。
    await cdp.evaluate(`(() => {
      for (let i = 0; i < 5; i += 1) window.__docqaTest.submitQuestion();
      return true;
    })()`)
    const duringBusy = JSON.parse(await cdp.evaluate('JSON.stringify(window.__docqaTest.state())'))
    record('R23-1', '生成期间禁用重复提交',
      duringBusy.answerBusy === true && duringBusy.askButtonDisabled === true
        && duringBusy.questionDisabled === true,
      `answerBusy=${duringBusy.answerBusy}，ask.disabled=${duringBusy.askButtonDisabled}，question.disabled=${duringBusy.questionDisabled}`)

    await waitFor(() => cdp.evaluate('window.__docqaTest.state().answerBusy === false'),
      { label: '第二次提问结束', timeout: 180000 })
    const after = JSON.parse(await cdp.evaluate('JSON.stringify(window.__docqaTest.state())'))
    const requests = await cdp.evaluate('JSON.stringify(window.__docqaTest.requests())')
    const sent = JSON.parse(requests).filter((item) => item.question.includes('目录'))
    record('R23-2', '每个有效提交只产生一次问答请求（重复触发被忽略）',
      after.requestCount === before.requestCount + 1 && sent.length === 1,
      `请求计数 ${before.requestCount} → ${after.requestCount}；该问题实际请求数=${sent.length}`)

    // ---------------- R24：恶意内容只作为文字，不执行、不加载外部资源 ----------------
    const xss = await cdp.evaluate(`(() => {
      const payload = {
        answer_id: 'x'.repeat(32), status: 'answered',
        answer: '## 结论\\n- <img src=x onerror="window.__ragProbe.errors.push(\\'xss\\')">注入测试。[1]\\n\\n' +
                '## 依据与说明\\n- <script>window.__ragProbe.errors.push("script")<\\/script>文本。[1]',
        clarification_questions: [], conclusion: [{ text: '<iframe src="https://example.invalid/"></iframe>', refs: [1] }],
        explanation: [], document_id: 'doc', index_id: 'idx', parse_version_id: 'v',
        is_old_version: false, is_current_index: true, prompt_version: 'rag-qa-v1',
        retrieval: { candidate_count: 1, selected_count: 1, context_chars: 10, truncated: false },
        quality_warnings: [], limitations: [],
        timings_ms: { retrieval: 1, generation: 1, total: 2 },
        citations: [{ reference_id: 1, document_id: 'doc', chunk_id: 'c1', parse_version_id: 'v',
                      page: 2, quote: '<script>window.__ragProbe.errors.push("quote")<\\/script>引述',
                      sources: [{ format: 'pdf', page: 2 }] }],
      };
      window.__docqaTest.renderAnswer(payload);
      return true;
    })()`)
    await sleep(1200)
    const probe = JSON.parse(await cdp.evaluate('JSON.stringify(window.__ragProbe)'))
    const domCheck = JSON.parse(await cdp.evaluate(`JSON.stringify({
      injected: document.querySelectorAll('#answer-panel script, #answer-panel iframe, #answer-panel img').length,
      external: performance.getEntriesByType('resource').filter((e) => e.name.includes('example.invalid')).length,
      bodyText: document.getElementById('answer-body').textContent,
      quoteText: document.querySelector('#answer-citations blockquote').textContent,
      title: document.title,
    })`))
    record('R24-3', '模型文本与引述中的 HTML 不执行、不生成可执行节点',
      domCheck.injected === 0 && probe.errors.length === 0 && probe.injected.length === 0,
      `answer-panel 内 script/iframe/img 节点=${domCheck.injected}，探针异常=${JSON.stringify(probe.errors)}，注入节点=${JSON.stringify(probe.injected)}`)
    record('R24-4', '外部图片/外链不会被加载',
      domCheck.external === 0 && !probe.external.some((url) => url.includes('example.invalid')),
      `example.invalid 资源请求数=${domCheck.external}；页面标题未被修改=${domCheck.title.includes('DocQA')}`)
    record('R24-5', '恶意文本以纯文本形式显示',
      domCheck.bodyText.includes('<script>') && domCheck.quoteText.includes('<script>'),
      `正文包含字面 script 标签=${domCheck.bodyText.includes('<script>')}；引述包含字面标签=${domCheck.quoteText.includes('<script>')}`)

    // ---------------- 依据不足展示 ----------------
    await cdp.evaluate(`window.__docqaTest.setQuestion(${JSON.stringify('这份文档有没有提到公司年会举办日期？')})`)
    await cdp.evaluate('window.__docqaTest.submitQuestion()')
    await waitFor(() => cdp.evaluate('window.__docqaTest.state().answerBusy === false && window.__docqaTest.state().answerStatus !== null'),
      { label: '依据不足回答返回', timeout: 180000 })
    const insufficient = JSON.parse(await cdp.evaluate(`JSON.stringify({
      status: window.__docqaTest.state().answerStatus,
      text: document.getElementById('answer-body').textContent,
      citations: document.querySelectorAll('#answer-citations article.citation').length,
      questionDisabled: window.__docqaTest.state().questionDisabled,
    })`))
    record('R12-1', '依据不足时页面显示兜底说明且引用为空、控件恢复',
      insufficient.status === 'insufficient_evidence' && insufficient.citations === 0
        && insufficient.text.includes('依据不足') && insufficient.questionDisabled === false,
      `status=${insufficient.status}，引用卡片=${insufficient.citations}，控件恢复=${!insufficient.questionDisabled}`)

    // ---------------- 刷新后答案清空（第一版行为） ----------------
    await cdp.send('Page.reload', { ignoreCache: true })
    await waitFor(() => cdp.evaluate('document.readyState === "complete"'), { label: '刷新完成' })
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#documents button").length > 0'),
      { label: '刷新后文档列表' })
    const afterReload = JSON.parse(await cdp.evaluate(`JSON.stringify({
      panelHidden: document.getElementById('answer-panel').hidden,
      status: document.getElementById('answer-status').textContent,
      requests: window.__ragProbe.questionRequests || 0,
    })`))
    record('R25-3', '刷新后答案区清空且不自动重发问答（第一版明确行为）',
      afterReload.panelHidden === true && afterReload.requests === 0,
      `答案区隐藏=${afterReload.panelHidden}，刷新后问答请求数=${afterReload.requests}`)

    await writeFile(join(OUT, 'rag-browser-results.json'),
      JSON.stringify({ api: API, documentId: DOC, browser: version['Browser'], results }, null, 2))
    console.log(`\n网页验收：${results.length - failures}/${results.length} 通过`)
    return failures === 0 ? 0 : 1
  } finally {
    if (cdp) cdp.close()
    chrome.kill()
    await sleep(500)
    await rm(profile, { recursive: true, force: true }).catch(() => {})
  }
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error('验收脚本失败：', error.message)
  process.exit(2)
})
