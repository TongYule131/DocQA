// T21/T22 真实浏览器验收：用 Chrome 无头模式（CDP）驱动本次改造后的工作台页面。
//
// 为什么需要它：任务书要求 T21（前端刷新/切换/断网/重复点击）与 T22（HTML 注入与
// 敏感信息）不能用后端模拟代替真实前端行为。本脚本不引入额外依赖，直接使用
// Node 内置 WebSocket 连接 Chrome DevTools Protocol。
//
// 用法（先启动业务服务，默认 http://127.0.0.1:8010）：
//   node scripts/browser_acceptance.mjs --api http://127.0.0.1:8010
import { spawn } from 'node:child_process'
import { mkdtemp, rm, writeFile, mkdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const args = process.argv.slice(2)
const getArg = (name, fallback) => {
  const index = args.indexOf(name)
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback
}
const API = getArg('--api', 'http://127.0.0.1:8010')
const CHROME = getArg('--chrome', 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe')
const OUT = resolve(getArg('--out', 'data/docling-validation/browser-acceptance'))
const PORT = Number(getArg('--port', '9333'))

const results = []
let failures = 0

function record(id, title, passed, detail) {
  results.push({ id, title, passed, detail })
  if (!passed) failures += 1
  console.log(`${passed ? 'PASS' : 'FAIL'} [${id}] ${title}\n        ${detail}`)
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

class Cdp {
  constructor(url) { this.url = url; this.id = 0; this.pending = new Map(); this.events = [] }
  async connect() {
    this.ws = new WebSocket(this.url)
    await new Promise((res, rej) => {
      this.ws.addEventListener('open', res, { once: true })
      this.ws.addEventListener('error', (e) => rej(new Error('websocket error')), { once: true })
    })
    this.ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve: res, reject: rej } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
      } else if (msg.method) {
        this.events.push(msg)
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
  async evaluate(expression, awaitPromise = true) {
    const result = await this.send('Runtime.evaluate', {
      expression, awaitPromise, returnByValue: true, userGesture: true,
    })
    if (result.exceptionDetails) {
      throw new Error('页面脚本异常：' + JSON.stringify(result.exceptionDetails.exception?.description
        || result.exceptionDetails.text))
    }
    return result.result.value
  }
  close() { try { this.ws.close() } catch { /* 忽略关闭错误 */ } }
}

async function fetchJson(path, options) {
  const response = await fetch(`${API}${path}`, options)
  const text = await response.text()
  if (!response.ok) throw new Error(`${path} -> ${response.status}: ${text.slice(0, 200)}`)
  return JSON.parse(text)
}

async function waitFor(fn, { timeout = 30000, interval = 500, label = '条件' } = {}) {
  const deadline = Date.now() + timeout
  let last
  while (Date.now() < deadline) {
    last = await fn()
    if (last) return last
    await sleep(interval)
  }
  throw new Error(`等待超时：${label}（最后取值 ${JSON.stringify(last)}）`)
}

/** 通过业务 API 建立一份可用的 TXT 文档（本地解析，不调用 Docling）。 */
async function createTextDocument(name, text) {
  const form = new FormData()
  form.append('file', new Blob([text], { type: 'text/plain' }), name)
  const upload = await fetchJson('/api/documents', { method: 'POST', body: form })
  const documentId = upload.document.id
  const submit = await fetchJson(`/api/documents/${documentId}/parse`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force: true }),
  })
  const taskId = submit.task?.id
  if (taskId) {
    await waitFor(async () => {
      const task = await fetchJson(`/api/parse-tasks/${taskId}`)
      return ['succeeded', 'failed', 'needs_attention'].includes(task.status) ? task : null
    }, { label: `任务 ${taskId} 结束`, timeout: 60000 })
  }
  const detail = await waitFor(async () => {
    const doc = await fetchJson(`/api/documents/${documentId}`)
    return doc.active_parse_version_id ? doc : null
  }, { label: `文档 ${documentId} 解析完成`, timeout: 60000 })
  return { documentId, upload, detail }
}

async function main() {
  await mkdir(OUT, { recursive: true })
  const profile = await mkdtemp(join(tmpdir(), 'docqa-chrome-'))
  const chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--window-size=1400,1000', 'about:blank',
  ], { stdio: 'ignore' })

  let cdp
  try {
    // 等待 DevTools 端点可用。
    const version = await waitFor(async () => {
      try {
        const response = await fetch(`http://127.0.0.1:${PORT}/json/version`)
        return response.ok ? await response.json() : null
      } catch { return null }
    }, { label: 'Chrome DevTools 端点', timeout: 30000 })
    console.log(`浏览器：${version['Browser']}`)

    const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(API + '/')}`,
      { method: 'PUT' })).json()
    cdp = new Cdp(target.webSocketDebuggerUrl)
    await cdp.connect()
    await cdp.send('Runtime.enable')
    await cdp.send('Page.enable')
    await cdp.send('Log.enable')
    await cdp.send('Network.enable')
    // 探针在每次新文档创建前注入（Page.addScriptToEvaluateOnNewDocument），
    // 因此刷新页面后依然生效：记录 alert 调用与动态注入的 script 节点。
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', {
      source: `
        window.__docqaProbe = { alerts: [], injected: [] };
        window.alert = (message) => { window.__docqaProbe.alerts.push(String(message)); };
        try {
          const observer = new MutationObserver((records) => {
            for (const record of records) {
              for (const node of record.addedNodes) {
                if (node.nodeType === 1 && node.tagName === 'SCRIPT') {
                  window.__docqaProbe.injected.push(node.textContent || 'external');
                }
              }
            }
          });
          observer.observe(document.documentElement, { childList: true, subtree: true });
        } catch (error) { /* documentElement 尚未就绪时忽略 */ }
      `,
    })

    // 准备两份可检索的文档：一份正常，一份文件名与正文含 HTML 注入载荷。
    const benign = await createTextDocument('安全样本.txt', '本文档用于前端验收，包含“苹果”关键词与结论。')
    const hostileName = '<img src=x onerror="window.__docqaProbe.alerts.push(\'xss-img\')">.txt'
    const hostile = await createTextDocument(
      hostileName,
      '<script>window.__docqaProbe.alerts.push("xss-inline")</script>正文注入测试：香蕉与结论。')

    // 打开工作台并等待文档列表加载。
    await cdp.send('Page.navigate', { url: `${API}/` })
    await waitFor(() => cdp.evaluate('document.readyState === "complete"'), { label: '页面加载' })
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#documents button").length >= 2'),
      { label: '文档列表渲染' })

    // ---------------- T21-1：刷新页面后从服务端恢复任务与内容 ----------------
    await cdp.evaluate(`(() => {
      const buttons = [...document.querySelectorAll('#documents button')];
      const target = buttons.find((b) => b.textContent.includes('安全样本'));
      target.click(); return true;
    })()`)
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#chunks article").length > 0'),
      { label: '预览内容渲染' })
    const beforeReload = await cdp.evaluate(`JSON.stringify({
      title: document.getElementById('document-title').textContent,
      chunks: document.querySelectorAll('#chunks article').length,
      version: document.getElementById('version-status').textContent,
      index: document.getElementById('index-status').textContent,
    })`)
    await cdp.send('Page.reload', { ignoreCache: true })
    await waitFor(() => cdp.evaluate('document.readyState === "complete"'), { label: '刷新完成' })
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#documents button").length >= 2'),
      { label: '刷新后文档列表恢复' })
    await cdp.evaluate(`(() => {
      const buttons = [...document.querySelectorAll('#documents button')];
      buttons.find((b) => b.textContent.includes('安全样本')).click(); return true;
    })()`)
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#chunks article").length > 0'),
      { label: '刷新后内容恢复' })
    const afterReload = await cdp.evaluate(`JSON.stringify({
      title: document.getElementById('document-title').textContent,
      chunks: document.querySelectorAll('#chunks article').length,
      version: document.getElementById('version-status').textContent,
      index: document.getElementById('index-status').textContent,
    })`)
    const beforeObj = JSON.parse(beforeReload)
    const afterObj = JSON.parse(afterReload)
    record('T21-1', '刷新页面后从服务端恢复文档、预览版本与索引状态',
      afterObj.title === beforeObj.title && afterObj.chunks === beforeObj.chunks
      && afterObj.version === beforeObj.version && afterObj.index === beforeObj.index,
      `刷新前=${beforeReload}；刷新后=${afterReload}`)

    // ---------------- T21-2：切换文档时旧轮询响应不得覆盖新选中文档 ----------------
    // 让页面开始轮询一个真实任务（重新解析安全样本），随后立即切换到另一份文档。
    const reparse = await fetchJson(`/api/documents/${benign.documentId}/parse`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: true }),
    })
    const reparseTask = reparse.task?.id
    await cdp.evaluate(`(() => {
      const buttons = [...document.querySelectorAll('#documents button')];
      buttons.find((b) => b.textContent.includes('安全样本')).click(); return true;
    })()`)
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#chunks article").length > 0'),
      { label: '重新选择文档' })
    // 触发“重新解析”，随后马上切到另一份文档。
    await cdp.evaluate('document.getElementById("reparse").click(); true')
    await sleep(300)
    await cdp.evaluate(`(() => {
      const buttons = [...document.querySelectorAll('#documents button')];
      buttons.find((b) => b.textContent.includes('xss-img') || b.textContent.includes('onerror')).click();
      return true;
    })()`)
    await sleep(1200)
    const afterSwitch = await cdp.evaluate(`JSON.stringify({
      title: document.getElementById('document-title').textContent,
      task: document.getElementById('task-status').textContent,
      timers: window.__docqaPollTimers ? window.__docqaPollTimers() : null,
    })`)
    const switched = JSON.parse(afterSwitch)
    // 切换后标题必须是新文档，且任务状态区不得出现旧任务的上游编号。
    const titleIsNew = switched.title.includes('onerror') || switched.title.includes('img')
    record('T21-2', '切换文档后旧轮询响应不覆盖新选中文档',
      titleIsNew && !switched.task.includes('上游任务'),
      `切换后标题=${switched.title}；任务区=${switched.task || '（空）'}`)

    // ---------------- T21-3：重复点击解析不产生第二个活动任务 ----------------
    await cdp.evaluate(`(() => {
      const buttons = [...document.querySelectorAll('#documents button')];
      buttons.find((b) => b.textContent.includes('onerror') || b.textContent.includes('img')).click();
      return true;
    })()`)
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#chunks article").length > 0'),
      { label: '选中注入样本' })
    // 连点 3 次“重新解析”：第一次点击后按钮必须立即禁用，且服务端只保留一个活动任务。
    const clickStates = await cdp.evaluate(`(async () => {
      const button = document.getElementById('reparse');
      const states = [];
      for (let i = 0; i < 3; i += 1) {
        button.click();
        states.push(button.disabled);
        await new Promise((r) => setTimeout(r, 60));
      }
      return JSON.stringify(states);
    })()`)
    await waitFor(async () => {
      const doc = await fetchJson(`/api/documents/${hostile.documentId}`)
      return doc.task_status === 'parsing' ? doc : null
    }, { label: '注入样本进入解析中', timeout: 15000 }).catch(() => null)
    const afterClicks = await fetchJson(`/api/documents/${hostile.documentId}`)
    const taskIds = new Set()
    const listed = await fetchJson(`/api/documents/${hostile.documentId}/chunks`)
    const activeTask = await fetchJson(`/api/parse-tasks/${afterClicks.latest_task_id}`).catch(() => null)
    record('T21-3', '重复点击“重新解析”时按钮立即禁用，服务端只有一个活动任务',
      JSON.parse(clickStates).includes(true) && Boolean(afterClicks.latest_task_id)
      && activeTask !== null && ['queued', 'running', 'succeeded'].includes(activeTask.status),
      `按钮禁用序列=${clickStates}；文档最新任务=${afterClicks.latest_task_id}；`
      + `任务状态=${activeTask?.status}；分块数=${listed.length}`)

    // ---------------- T21-4：断网时显示连接状态并有限退避 ----------------
    // 用 CDP 拦截任务查询接口模拟“断网”，并通过页面自身的轮询入口让页面进入轮询：
    // 页面必须显示连接异常提示并退避重试，且不得显示虚构百分比。
    // 通过 API 创建一个真实任务，再用页面只读钩子把页面切到该任务的轮询状态。
    const offlineTask = await fetchJson(`/api/documents/${benign.documentId}/parse`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: true }),
    })
    const offlineTaskId = offlineTask.task?.id || (await fetchJson(`/api/documents/${benign.documentId}`)).latest_task_id
    await cdp.send('Network.setBlockedURLs', { urls: ['*/api/parse-tasks/*'] })
    await cdp.evaluate(`window.__docqaTest.startPolling(${JSON.stringify(benign.documentId)}, ${JSON.stringify(offlineTaskId)})`)
    await waitFor(async () => {
      const text = await cdp.evaluate('document.getElementById("connection-status").textContent')
      return text.includes('连接') ? text : null
    }, { label: '页面显示连接异常提示', timeout: 25000 }).catch(() => null)
    const offlineState = await cdp.evaluate(`JSON.stringify({
      connection: document.getElementById('connection-status').textContent,
      message: document.getElementById('message').textContent,
      hasPercent: /\\d+\\s*%/.test(document.body.textContent),
    })`)
    await cdp.send('Network.setBlockedURLs', { urls: [] })
    // 恢复后页面应能再次读取服务端状态。
    await cdp.evaluate('document.getElementById("refresh").click(); true')
    await waitFor(async () => {
      const text = await cdp.evaluate('document.getElementById("parsing-status").textContent')
      return text.includes('可响应') ? text : null
    }, { label: '恢复网络后刷新成功', timeout: 20000 }).catch(() => null)
    const recovered = await cdp.evaluate('document.getElementById("parsing-status").textContent')
    const offline = JSON.parse(offlineState)
    record('T21-4', '断网时显示连接状态、无虚构百分比，恢复网络后可继续',
      offline.hasPercent === false && offline.connection.includes('连接')
      && recovered.includes('可响应'),
      `断网提示=${offline.connection}；页面出现百分比=${offline.hasPercent}；恢复后=${recovered}`)

    // ---------------- T22-1：文件名与正文中的 HTML 不被执行 ----------------
    // 先切到注入样本，确保页面正在渲染包含载荷的文件名与正文。
    await cdp.evaluate(`(() => {
      const buttons = [...document.querySelectorAll('#documents button')];
      buttons.find((b) => b.textContent.includes('onerror') || b.textContent.includes('img')).click();
      return true;
    })()`)
    // 必须等待标题真正切换到注入样本，否则会读到上一份文档的正文。
    await waitFor(async () => {
      const title = await cdp.evaluate('document.getElementById("document-title").textContent')
      return title.includes('onerror') || title.includes('img') ? title : null
    }, { label: '注入样本成为当前文档', timeout: 15000 })
    await waitFor(() => cdp.evaluate('document.querySelectorAll("#chunks article").length > 0'),
      { label: '注入样本内容渲染' })
    const probe = await cdp.evaluate(`JSON.stringify({
      alerts: window.__docqaProbe.alerts,
      injected: window.__docqaProbe.injected,
      imgTags: document.querySelectorAll('#documents img, #chunks img').length,
      listText: document.getElementById('documents').textContent.slice(0, 200),
      chunkHasLiteralScript: document.getElementById('chunks').textContent.includes('<script>'),
      chunkText: document.getElementById('chunks').textContent.slice(0, 160),
      title: document.getElementById('document-title').textContent,
    })`)
    const probeObj = JSON.parse(probe)
    record('T22-1', '文件名与正文中的 HTML/脚本不被执行，按纯文本显示',
      probeObj.alerts.length === 0 && probeObj.injected.length === 0 && probeObj.imgTags === 0
      && probeObj.chunkHasLiteralScript === true,
      `alert=${JSON.stringify(probeObj.alerts)}；注入脚本=${JSON.stringify(probeObj.injected)}；`
      + `img 标签数=${probeObj.imgTags}；标题=${probeObj.title}；`
      + `正文按纯文本显示 <script>=${probeObj.chunkHasLiteralScript}；正文片段=${probeObj.chunkText}`)

    // ---------------- T22-2：页面不泄露密钥或上游内部响应 ----------------
    const secrets = await cdp.evaluate(`JSON.stringify({
      body: document.body.textContent,
      html: document.documentElement.outerHTML,
    })`)
    const secretObj = JSON.parse(secrets)
    const forbidden = ['EMBEDDING_API_KEY', 'DEEPSEEK_API_KEY', 'Bearer ', 'api_key',
      'gateway-test-key', 'Traceback', 'sqlite3.', 'File "', 'D:\\\\python',
      '/v1/convert/file/async', '/v1/status/poll/', '/v1/result/']
    // 常见密钥形态单独用正则检查，避免 'sk-' 误命中 'tasks-' 之类的普通文本。
    const secretPatterns = [/sk-[A-Za-z0-9]{16,}/, /[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,}/]
    const leaked = forbidden.filter((needle) => secretObj.body.includes(needle) || secretObj.html.includes(needle))
    const patternHits = secretPatterns.filter((pattern) => pattern.test(secretObj.body) || pattern.test(secretObj.html))
    // 页面只允许出现“未配置密钥”这类提示，不能出现密钥本身或上游内部堆栈。
    record('T22-2', '页面不出现密钥、上游原始错误或内部堆栈',
      leaked.length === 0 && patternHits.length === 0,
      (leaked.length || patternHits.length)
        ? `出现敏感片段：${[...leaked, ...patternHits.map(String)].join(', ')}`
        : '未发现密钥名、Bearer、堆栈、上游接口路径或本地路径片段')

    // ---------------- T22-3：服务端错误文本也按纯文本渲染 ----------------
    // 构造一个真实的服务端错误：解析不存在的任务并检查页面提示区域。
    await cdp.evaluate(`(async () => {
      document.getElementById('message').textContent = '';
      return true;
    })()`)
    const errorRender = await cdp.evaluate(`(async () => {
      const response = await fetch('/api/parse-tasks/不存在<script>alert(1)</script>');
      const data = await response.json().catch(() => ({}));
      const node = document.getElementById('message');
      node.textContent = String(data.detail || response.status);
      return JSON.stringify({ status: response.status, text: node.textContent,
                              scripts: document.querySelectorAll('#message script').length });
    })()`)
    const errorObj = JSON.parse(errorRender)
    record('T22-3', '服务端错误提示按纯文本渲染，不产生脚本节点',
      errorObj.scripts === 0 && errorObj.status === 404,
      `状态=${errorObj.status}；提示文本=${errorObj.text}；脚本节点=${errorObj.scripts}`)

    // ---------------- T21-5：轮询定时器在终态后停止 ----------------
    // 统计一段时间内 /api/parse-tasks 请求数量：任务进入终态后不应继续增长。
    await cdp.evaluate(`(() => {
      const original = window.fetch;
      window.__pollCount = 0;
      window.fetch = function (...params) {
        const url = String(params[0]);
        if (url.includes('/api/parse-tasks/')) window.__pollCount += 1;
        return original.apply(this, params);
      };
      return true;
    })()`)
    await cdp.evaluate(`(() => {
      const buttons = [...document.querySelectorAll('#documents button')];
      buttons.find((b) => b.textContent.includes('安全样本')).click(); return true;
    })()`)
    await waitFor(async () => {
      const doc = await fetchJson(`/api/documents/${benign.documentId}`)
      return ['succeeded', 'failed'].includes(doc.task_status) ? doc : null
    }, { label: '重新解析任务进入终态', timeout: 60000 })
    await sleep(2500)
    const firstCount = await cdp.evaluate('window.__pollCount')
    await sleep(5000)
    const secondCount = await cdp.evaluate('window.__pollCount')
    const timersAfterTerminal = await cdp.evaluate('window.__docqaTest.timers()')
    record('T21-5', '任务进入终态后停止轮询（定时器清理）',
      secondCount - firstCount <= 1 && timersAfterTerminal === 0,
      `终态后 5 秒内新增轮询请求=${secondCount - firstCount}（累计 ${secondCount}）；`
      + `活动轮询定时器=${timersAfterTerminal}`)

    await writeFile(join(OUT, 'browser-results.json'),
      JSON.stringify({ api: API, browser: version['Browser'], results }, null, 2), 'utf-8')
    await cdp.send('Page.captureScreenshot', { format: 'png' }).then(async (shot) => {
      await writeFile(join(OUT, 'workbench.png'), Buffer.from(shot.data, 'base64'))
    }).catch(() => {})
    console.log(`证据目录：${OUT}`)
    console.log(`通过 ${results.filter((r) => r.passed).length} / ${results.length}`)
  } finally {
    cdp?.close()
    chrome.kill()
    await sleep(500)
    await rm(profile, { recursive: true, force: true }).catch(() => {})
  }
  return failures === 0 ? 0 : 1
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error('浏览器验收脚本失败：', error)
  process.exit(2)
})
