// 网页渲染的离线安全测试：直接执行 app/web/app.js，验证引用渲染与 XSS 边界。
//
// 为什么需要它：R23/R24 的可复核证据必须来自真实浏览器（见
// scripts/rag_browser_acceptance.mjs），但“模型文本/引述/文件名里的 HTML 不执行”
// 这一条同时需要能在离线回归中稳定复现，因此这里用最小 DOM 桩加载真实页面脚本，
// 断言它只创建文本节点，绝不产生可执行的脚本或图片节点。
//
// 用法：node tests/web/render_safety.mjs
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import { randomUUID } from 'node:crypto'

const here = dirname(fileURLToPath(import.meta.url))
const appJs = readFileSync(resolve(here, '../../app/web/app.js'), 'utf8')

const results = []
function check(id, title, passed, detail) {
  results.push({ id, title, passed, detail })
  console.log(`${passed ? 'PASS' : 'FAIL'} [${id}] ${title}\n        ${detail}`)
}

// --- 最小 DOM 桩 ----------------------------------------------------------
class Node {
  constructor(tag) {
    this.tagName = tag ? tag.toUpperCase() : ''
    this.children = []
    this.attributes = {}
    this.dataset = {}
    this.style = {}
    this.classList = {
      _set: new Set(),
      add: (value) => this.classList._set.add(value),
      remove: (value) => this.classList._set.delete(value),
      contains: (value) => this.classList._set.has(value),
    }
    this._text = ''
    this.hidden = false
    this.disabled = false
    this.value = ''
    this.href = ''
    this.onclick = null
  }
  get textContent() {
    if (this.children.length === 0) return this._text
    return this.children.map((child) => child.textContent).join('')
  }
  set textContent(value) {
    // 与浏览器一致：赋值文本内容会清空子节点，且不会创建任何元素节点。
    this.children = []
    this._text = String(value)
  }
  append(...nodes) {
    for (const node of nodes) {
      this.children.push(typeof node === 'string' ? new TextNode(node) : node)
    }
  }
  appendChild(node) { this.append(node); return node }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes) }
  querySelector() { return null }
  querySelectorAll() { return [] }
  addEventListener() { /* 桩：页面脚本注册的监听在离线测试中不需要触发 */ }
  setAttribute(name, value) { this.attributes[name] = String(value) }
  getAttribute(name) { return this.attributes[name] }
  scrollIntoView() { /* 桩 */ }
  focus() { /* 桩 */ }
  // 递归收集整棵子树，便于断言节点类型。
  allNodes() {
    const out = []
    for (const child of this.children) {
      out.push(child)
      if (typeof child.allNodes === 'function') out.push(...child.allNodes())
    }
    return out
  }
}
class TextNode extends Node {
  constructor(text) { super(''); this._text = String(text); this.isText = true }
}
const documentStub = {
  createElement: (tag) => new Node(tag),
  createTextNode: (text) => new TextNode(text),
  getElementById: (id) => elements.get(id) || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  hidden: false,
  createDocumentFragment: () => new Node('fragment'),
}

const containerIds = ['message', 'connection-status', 'documents', 'document-title', 'document-meta',
  'task-status', 'version-status', 'content-summary', 'quality-panel', 'index-status',
  'search-results', 'chunks', 'model-status', 'embedding-status', 'embedding-gateway',
  'parsing-status', 'answer-panel', 'answer-status', 'answer-meta', 'answer-waiting',
  'answer-body', 'answer-questions', 'answer-warnings', 'answer-limitations', 'answer-citations',
  'answer-actions', 'question-job', 'question-history', 'question-refresh', 'question-cancel',
  'question-retry', 'question-confirm',
  'question', 'ask', 'summary', 'extract', 'parse', 'reparse', 'refresh', 'test-model',
  'test-embedding', 'build-index', 'rebuild-index', 'search-query', 'search', 'stop-task',
  'retry-task', 'upload-form', 'question-form', 'search-form', 'file',
  // 分析任务（摘要 / 信息提取）面板容器。
  'analysis-plan', 'analysis-plan-panel', 'analysis-status', 'analysis-job', 'analysis-results',
  'analysis-stop', 'analysis-retry', 'analysis-regenerate',
  'extraction-metrics', 'extraction-highlights',
  'runtime-summary', 'runtime-schema', 'runtime-roles', 'runtime-checked', 'refresh-runtime']
const elements = new Map(containerIds.map((id) => [id, new Node('div')]))
elements.get('upload-form').querySelector = () => new Node('button')
elements.get('answer-panel').hidden = true
elements.get('extraction-highlights').checked = true

const sessionValues = new Map()
const sandbox = {
  document: documentStub,
  window: {},
  sessionStorage: { getItem: (key) => sessionValues.get(key) || null,
    setItem: (key, value) => sessionValues.set(key, value),
    removeItem: (key) => sessionValues.delete(key) },
  crypto: { randomUUID },
  fetch: async () => { throw new Error('离线渲染测试不应发起网络请求') },
  setTimeout: (fn) => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  console,
  JSON,
  Number,
  String,
  Object,
  Date,
  Math,
  RegExp,
  Promise,
  AbortController,
  Event: class { constructor(type) { this.type = type } },
  FormData: class {},
  encodeURIComponent,
}
sandbox.window = sandbox
sandbox.globalThis = sandbox
const context = vm.createContext(sandbox)
// 页面脚本以 `run(refresh)` 结尾会立即发起请求：这里先替换 fetch 为空实现，
// 断言重点在渲染函数，而不是启动流程。
vm.runInContext(appJs, context, { filename: 'app.js' })

const hooks = sandbox.window.__docqaTest
const renderAnswerMarkdown = hooks.renderAnswerMarkdown
const renderCitations = hooks.renderCitations
const renderAnalysisResult = hooks.renderAnalysisResult

// --- R24 渲染安全 ---------------------------------------------------------
// 静态检查只作为附加证据：主要依据是下面基于真实 DOM 桩的节点断言。
// 只扫描可执行代码：先去掉整行注释，避免把“说明不使用 innerHTML”的注释算作命中。
const codeOnly = appJs.split('\n')
  .filter((line) => !line.trim().startsWith('//'))
  .join('\n')
const usesInnerHtml = /innerHTML|outerHTML|insertAdjacentHTML|document\.write/.test(codeOnly)
check('R24-1', '页面脚本（去注释后）不使用 innerHTML/outerHTML/document.write',
  !usesInnerHtml,
  usesInnerHtml ? '发现 innerHTML 类 API，需要人工复核' : '未使用 innerHTML/outerHTML/document.write')

const xssVectors = [
  '<script>window.__xss=1</script>',
  '<img src=x onerror="window.__xss=1">',
  '<a href="javascript:window.__xss=1">点击</a>',
  '![远程图片](https://example.invalid/a.png)',
  '# <svg/onload=window.__xss=1>',
  '- <iframe src="https://example.invalid"></iframe>',
]
for (const [index, vector] of xssVectors.entries()) {
  renderAnswerMarkdown(vector)
  const nodes = elements.get('answer-body').allNodes()
  // 只关心可执行或会产生网络请求的节点类型；标题/段落/列表本身是安全的。
  const bad = nodes.filter((node) => ['SCRIPT', 'IMG', 'IFRAME', 'SVG', 'A'].includes(node.tagName))
  check(`R24-2.${index + 1}`, `恶意模型文本不产生可执行节点：${vector.slice(0, 24)}`,
    bad.length === 0 && nodes.some((node) => node.isText),
    `危险节点：${bad.map((n) => n.tagName).join(',') || '无'}；`
    + `文本节点数：${nodes.filter((n) => n.isText).length}`)
}

// 后端按已校验 refs 生成的 [n] 标记必须渲染为可点击按钮，而不是纯文本。
renderAnswerMarkdown('## 结论\n- 全书内容分为21个部分。[1]\n- 第二部分收录统计表格。[1][2]\n')
const refButtons = elements.get('answer-body').allNodes().filter((node) => node.tagName === 'BUTTON')
check('R24-6', '逐事实引用标记渲染为可点击按钮',
  refButtons.length === 3 && refButtons.every((node) => node.className === 'ref')
    && refButtons.map((node) => node.textContent).join('') === '[1][1][2]',
  `引用按钮：${refButtons.map((node) => node.textContent).join('') || '无'}`)
const bodyText = elements.get('answer-body').textContent
const markerTextNodes = elements.get('answer-body').allNodes()
  .filter((node) => node.isText && /\[\d+\]/.test(node.textContent))
check('R24-7', '引用标记只以按钮形式出现，正文中没有重复文字标记',
  markerTextNodes.length === 0,
  `残留文本标记节点数=${markerTextNodes.length}；按钮文本=${refButtons.map((n) => n.textContent).join('')}`)

const payload = {
  is_old_version: false,
  citations: [{
    reference_id: 1,
    document_id: 'doc-1',
    chunk_id: 'c1',
    parse_version_id: 'v-a',
    page: 2,
    quote: '<script>window.__xss=1</script>原文引述',
    sources: [{ format: 'pdf', page: 2 }],
  }],
}
renderCitations(payload.citations, payload)
const citationNodes = elements.get('answer-citations').allNodes()
check('R24-3', '引述中的 HTML 只作为文字显示',
  citationNodes.filter((node) => ['SCRIPT', 'IMG'].includes(node.tagName)).length === 0,
  `引述文本：${elements.get('answer-citations').textContent.slice(0, 60)}`)

const linkNodes = citationNodes.filter((node) => node.tagName === 'A')
check('R24-4', '引用卡片只生成受控的原件/预览入口',
  linkNodes.length === 1 && String(linkNodes[0].href).startsWith('/api/documents/doc-1/original'),
  `链接：${linkNodes.map((node) => node.href).join(',')}`)

const openedLinks = linkNodes.filter((node) => node.target === '_blank')
check('R24-5', '外部打开均带 noopener', openedLinks.every((node) => node.rel === 'noopener noreferrer'),
  `target=_blank 数量 ${openedLinks.length}`)

const tableCitation = structuredClone(payload.citations[0])
tableCitation.table_structure = { version: 'table-coordinate-evidence-v1', status: 'available',
  rows: [{ row: 1, values: ['项目', '金额'] }, { row: 4, values: ['<img src=x onerror=alert(1)>', '0'] },
    { row: 5, values: ['补助', ''] }],
  formulas: [{ cell: 'B6', cache_status: 'missing', cached_value: null },
    { cell: 'C6', cache_status: 'saved', cached_value: 0 }],
  footnotes: [{ text: '<script>假指令</script>部分项目未经审计', applicability: 'unresolved' }] }
renderCitations([tableCitation], payload)
const tableCard = elements.get('answer-citations')
check('TABLE-UI-1', '表格行列仅作辅助，保留真实行号、空白和零',
  tableCard.textContent.includes('表内第 4 行：<img') && tableCard.textContent.includes('补助 | （空）')
    && tableCard.textContent.includes('可能超出本条引述范围') && tableCard.allNodes().some(n => n.tagName === 'DETAILS'),
  tableCard.textContent.slice(0, 220))
check('TABLE-UI-2', '公式缺缓存与缓存零分别显示，脚注范围保留未确认',
  tableCard.textContent.includes('B6：公式缓存缺失') && tableCard.textContent.includes('C6：文件缓存值 0')
    && tableCard.textContent.includes('表格注释（适用范围未确认）'), tableCard.textContent.slice(-220))
check('TABLE-UI-3', '辅助表格和脚注中的 HTML 只作文字',
  !tableCard.allNodes().some(n => ['IMG', 'SCRIPT'].includes(n.tagName)), '所有不可信值均使用textContent')
renderCitations(payload.citations, payload)
check('TABLE-UI-4', '旧结果无结构字段时保持引用卡片兼容',
  !elements.get('answer-citations').allNodes().some(n => n.tagName === 'DETAILS'), '不补造旧表结构')

// --- 分析结果渲染安全（摘要 / 提取） --------------------------------------
// 与问答共用同一套受限渲染：模型文本、引述与文件名里的 HTML 只能作为文字出现。
const analysisResult = {
  id: 'res_test',
  job_id: 'job_test',
  document_id: 'doc-1',
  parse_version_id: 'v-a',
  kind: 'extraction',
  is_active_version: true,
  coverage: {
    total_units: 3, processed_units: 3, unresolved_units: 0, unresolved_reasons: {},
    excluded_units: 1, excluded_reasons: { '页眉属于版面信息，不作为事实依据': 1 },
    batch_total: 1, batch_completed: 1, reduce_completed: false, complete: true,
    resolved_original_refs: 1, unresolved_original_refs: 0,
  },
  quality_warnings: [],
  limitations: ['<img src=x onerror="window.__xss=1"> 只是文本'],
  prompt_version: 'docqa-extract-v1',
  protocol_version: 'docqa-extract-protocol-v1',
  model_signature: 'sig',
  parse_parser_name: 'synthetic',
  parse_quality_status: 'warnings',
  requests_used: 1,
  created_at: '2026-09-29T00:00:00+00:00',
  extraction: {
    items: [{
      item_id: 'b1-i1', kind: 'data',
      content: '<script>window.__xss=1</script> 收入 1.2 亿元',
      name: '<svg/onload=window.__xss=1>', value_text: '1.2 亿元', unit: '亿元',
      period: '<a href="javascript:window.__xss=1">2024 年</a>', subject: null, scope: null,
      refs: [1],
    }],
    sections: { data: 'present', conclusion: 'none', viewpoint: 'none' },
    citations: [{
      reference_id: 1, block_id: 'v-a-b1', source_index: 0, block_type: 'paragraph',
      quote: '<iframe src="https://example.invalid"></iframe>原文引述',
      sources: [{ format: 'pdf', page: 2 }], char_start: 3, char_end: 12,
    }],
  },
  summary: null,
}
renderAnalysisResult(analysisResult)
const analysisNodes = elements.get('analysis-results').allNodes()
const dangerousNodes = analysisNodes.filter(
  (node) => ['SCRIPT', 'IMG', 'IFRAME', 'SVG'].includes(node.tagName))
check('R24-8', '分析结果中的恶意文本不产生可执行或外链节点',
  dangerousNodes.length === 0,
  `危险节点：${dangerousNodes.map((n) => n.tagName).join(',') || '无'}`)

const analysisRefButtons = elements.get('analysis-results').allNodes()
  .filter((node) => node.tagName === 'BUTTON' && node.className === 'ref')
check('R24-9', '分析结果逐条引用渲染为可点击引用标记',
  analysisRefButtons.length === 1 && analysisRefButtons[0].textContent === '[1]',
  `引用按钮：${analysisRefButtons.map((node) => node.textContent).join('') || '无'}`)

const analysisLinks = analysisNodes.filter((node) => node.tagName === 'A')
check('R24-10', '分析结果的链接只指向受控导出与原件接口',
  analysisLinks.length === 3
    && analysisLinks.every((node) => String(node.href).startsWith('/api/')),
  `链接：${analysisLinks.map((node) => node.href).join(',')}`)
check('R24-11', '分析结果的导出链接指向导出接口且带有结果 ID',
  analysisLinks.some((node) => String(node.href).includes('/export?format=markdown'))
    && analysisLinks.some((node) => String(node.href).includes('/export?format=json')),
  `导出链接：${analysisLinks.map((node) => node.href).join(',')}`)

const analysisText = hooks.resultToText(analysisResult)
check('R24-12', '复制文本含引用与限制，且不包含原始模型响应字段',
  analysisText.includes('[1]') && analysisText.includes('适用边界')
    && !analysisText.includes('reasoning'),
  `文本长度 ${analysisText.length}`)

// --- 汇总 -----------------------------------------------------------------
// 未确认提交必须跨本页重试与刷新复用原键；重新生成与普通提交不能混为一份载荷。
const pendingCheck = vm.runInContext(`
  selectedId = 'offline-doc';
  analysisPlan = {kind:'summary',parse_version_id:'v-1',plan_fingerprint:'plan-1'};
  const firstPending = analysisPlanPayload();
  const repeatedPending = analysisPlanPayload();
  pendingAnalysisKeys.clear();
  const afterPageReload = analysisPlanPayload();
  const regeneratedPending = analysisPlanPayload(true);
  ({same:firstPending.idempotency_key===repeatedPending.idempotency_key,
    retained:firstPending.idempotency_key===afterPageReload.idempotency_key,
    distinct:firstPending.idempotency_key!==regeneratedPending.idempotency_key});
`, context)
check('A06-UI-1', '响应未确认时重复操作与刷新保留幂等键',
  pendingCheck.same && pendingCheck.retained, JSON.stringify(pendingCheck))
check('A06-UI-2', '明确重新生成使用独立请求键', pendingCheck.distinct, JSON.stringify(pendingCheck))

elements.get('chunks').replaceChildren()
hooks.renderLegacyChunks([{id:'synthetic-legacy',page:1,text:'<img src=x onerror=alert(1)>旧文档正文'}])
const legacyPanel = elements.get('chunks')
check('CORE-UI-1', '旧版本分块可见，原文 HTML 只作为文本',
  legacyPanel.textContent.includes('旧文档正文')
    && legacyPanel.allNodes().every((node) => !['IMG','SCRIPT','IFRAME'].includes(node.tagName)),
  legacyPanel.textContent)
check('CORE-UI-2', '旧版本说明来源限制，旧逻辑页不冒充物理页',
  legacyPanel.textContent.includes('缺少可靠结构化来源')
    && legacyPanel.textContent.includes('旧逻辑页 1'), legacyPanel.textContent)

// 等待页面初始的离线查询结束，避免其失败回调覆盖后续显式渲染断言。
await hooks.refreshRuntimeStatus()
hooks.renderWarnings([], { is_legacy: true, quality_status: 'warnings' })
check('QUALITY-UI-1', '旧版本没有独立告警时仍显示质量限制',
  elements.get('quality-panel').textContent.includes('存在质量限制'),
  elements.get('quality-panel').textContent)
hooks.renderWarnings([], { is_legacy: false, quality_status: 'ok' })
check('QUALITY-UI-2', '空告警列表不表示已逐字核对',
  elements.get('quality-panel').textContent.includes('不表示内容已与原件逐字核对'),
  elements.get('quality-panel').textContent)
const originalOCRWarning = { code: 'ocr_limitation', severity: 'info', scope: 'document',
  message: '扫描或混合PDF识别可能有错字。' }
hooks.renderWarnings([originalOCRWarning])
check('QUALITY-UI-3', '旧OCR通用提示解释为未验证，不误称识别失败且不改历史对象',
  elements.get('quality-panel').textContent.includes('不表示检测到OCR失败')
    && originalOCRWarning.message === '扫描或混合PDF识别可能有错字。',
  elements.get('quality-panel').textContent)

const readyRole = (role) => ({ role, running: true, ready: true, code_match: true,
  schema_match: true, data_dir_match: true })
const readyRuntime = { web: readyRole('web'), workers: [readyRole('parse-worker'),
  readyRole('analysis-worker')], schema_version: 4, expected_schema_version: 4 }
hooks.renderRuntimeStatus(readyRuntime)
check('RUNTIME-UI-1', '全部角色匹配且就绪时显示可用状态',
  elements.get('runtime-summary').dataset.kind === 'ok'
    && elements.get('runtime-roles').children.length === 3,
  elements.get('runtime-summary').textContent)
hooks.renderRuntimeStatus({ ...readyRuntime, workers: [] })
check('RUNTIME-UI-2', '缺失 worker 不继承旧就绪结论',
  elements.get('runtime-summary').dataset.kind === 'error'
    && elements.get('runtime-roles').textContent.includes('状态未知'),
  elements.get('runtime-summary').textContent)
hooks.renderRuntimeStatus({ ...readyRuntime, workers: [readyRole('parse-worker'),
  { ...readyRole('analysis-worker'), running: false, ready: false }] })
check('RUNTIME-UI-3', '退出的 worker 显示未运行',
  elements.get('runtime-summary').dataset.kind === 'error'
    && elements.get('runtime-roles').textContent.includes('未运行'),
  elements.get('runtime-summary').textContent)
hooks.renderRuntimeStatus({ ...readyRuntime, workers: [{ ...readyRole('parse-worker'),
  code_match: false, ready: false }, readyRole('analysis-worker')] })
check('RUNTIME-UI-4', '版本不匹配不显示全部就绪',
  elements.get('runtime-summary').dataset.kind === 'error'
    && elements.get('runtime-roles').textContent.includes('不匹配'),
  elements.get('runtime-summary').textContent)
hooks.renderRuntimeStatus({ ...readyRuntime, web: { detail: '<img src=x onerror=alert(1)>',
  data_dir: 'private-path', code_fingerprint: 'private-fingerprint' } })
check('RUNTIME-UI-5', '未知字段不被推断为就绪，私有字段与 HTML 不展示',
  elements.get('runtime-summary').dataset.kind === 'error'
    && !elements.get('runtime-roles').textContent.includes('private-')
    && elements.get('runtime-roles').allNodes().every((node) => !['IMG','SCRIPT'].includes(node.tagName)),
  elements.get('runtime-summary').textContent)

const runtimeRequests = []
let releaseRuntime
sandbox.fetch = (url, options) => {
  runtimeRequests.push({ url, options })
  return new Promise((resolve) => { releaseRuntime = resolve })
}
const firstRefresh = hooks.refreshRuntimeStatus()
const secondRefresh = hooks.refreshRuntimeStatus()
check('RUNTIME-UI-6', '手动与自动刷新复用同一个只读在途请求',
  runtimeRequests.length === 1 && runtimeRequests[0].url === '/api/runtime/status'
    && !runtimeRequests[0].options.method && runtimeRequests[0].options.cache === 'no-store'
    && runtimeRequests[0].options.signal instanceof AbortSignal,
  `GET 请求数=${runtimeRequests.length}`)
releaseRuntime({ ok: true, json: async () => readyRuntime })
await Promise.all([firstRefresh, secondRefresh])
sandbox.fetch = async () => { throw new Error('连接中断') }
await hooks.refreshRuntimeStatus()
check('RUNTIME-UI-7', '查询失败撤下旧就绪状态并解除刷新按钮',
  elements.get('runtime-summary').dataset.kind === 'error'
    && !elements.get('runtime-roles').textContent.includes('已就绪')
    && elements.get('runtime-checked').textContent.includes('已过期')
    && !elements.get('refresh-runtime').disabled,
  elements.get('runtime-checked').textContent)

// 模拟挂起连接：触发生产超时回调，确认请求取消并恢复查询能力。
const callbacks = []
sandbox.setTimeout = (fn, delay) => { callbacks.push({ fn, delay }); return callbacks.length }
sandbox.fetch = (url, options) => new Promise((resolve, reject) => {
  options.signal.addEventListener('abort', () => reject(new Error('请求超时')))
})
const hungRefresh = hooks.refreshRuntimeStatus()
callbacks.find((entry) => entry.delay === 10000).fn()
await hungRefresh
check('RUNTIME-UI-8', '状态请求超时后取消连接并允许重试',
  !elements.get('refresh-runtime').disabled
    && elements.get('runtime-summary').dataset.kind === 'error',
  elements.get('runtime-summary').textContent)

// 混合提取：用户必须先看到当前设置的计划，再明确提交生成。
const hybridRequests = []
const hybridOptions = { required_metrics: [
  { name: '主营业务收入', subject: '华东', period: '2024年度', scope: null },
  { name: '其他业务收入', subject: '华东', period: '2024年度', scope: null },
], include_highlights: true }
let planNumber = 0
const makePlan = (kind, options) => ({
  document_id: 'hybrid-doc', parse_version_id: 'hybrid-v', kind,
  is_active_version: true, prompt_version: 'synthetic', plan_fingerprint: `hybrid-plan-${++planNumber}`,
  extraction_options: kind === 'extraction' ? options : null, executable: true,
  coverage: { planned_units: 1, total_units: 1, batch_count: 1, excluded_units: 0 },
  batches: [{ order_index: 0, unit_count: 1, chars: 100, message_chars: 300 }],
  limits: { max_requests: 2, batch_max_chars: 1000, reduce_max_chars: 1000,
    input_max_chars: 2000, max_document_chars: 2000 }, request_upper_bound: 1,
  reduce_required: false, limitations: [],
})
sandbox.setTimeout = () => 0
sandbox.fetch = async (url, options = {}) => {
  hybridRequests.push({ url, options, body: options.body ? JSON.parse(options.body) : null })
  if (url.includes('/analysis-plan?')) {
    const kind = url.endsWith('kind=summary') ? 'summary' : 'extraction'
    return { ok: true, json: async () => makePlan(kind, options.body
      ? JSON.parse(options.body).extraction_options : null) }
  }
  if (url.endsWith('/analysis-results')) return { ok: true, json: async () => [] }
  if (url.endsWith('/extract') || url.endsWith('/summary')) return { ok: true, json: async () => ({
    message: '已创建任务', reused: false,
    job: { id: 'hybrid-job', kind: url.endsWith('/summary') ? 'summary' : 'extraction',
      status: 'queued', stage: 'queued', requests_used: 0, max_requests: 2,
      steps_completed: 0, steps_total: 1 },
  }) }
  throw new Error(`未预期的离线请求 ${url}`)
}
vm.runInContext(`selectedId='hybrid-doc'; documents=[{id:'hybrid-doc',active_parse_version_id:'hybrid-v'}];
  busy=false; analysisConfigured=true; analysisPlan=null; analysisKind='extraction';`, context)
elements.get('extraction-metrics').value = ' 主营业务收入 | 华东 | 2024年度\n其他业务收入 | 华东 | 2024年度 '
elements.get('extraction-highlights').checked = true
elements.get('extraction-metrics').oninput()
await elements.get('extract').onclick()
check('HYBRID-UI-1', '首次点击只规划并展示指标，不创建收费生成任务',
  hybridRequests.length === 1 && hybridRequests[0].url.includes('/analysis-plan?kind=extraction')
    && JSON.stringify(hybridRequests[0].body.extraction_options) === JSON.stringify(hybridOptions)
    && elements.get('analysis-plan-panel').textContent.includes('其他业务收入')
    && hooks.analysisState().submitCount === 0,
  JSON.stringify(hybridRequests))
const pendingHybrid = vm.runInContext('analysisPlanPayload()', context)
await elements.get('extract').onclick()
const firstExtract = hybridRequests.find((request) => request.url.endsWith('/extract'))
check('HYBRID-UI-2', '再次点击提交与已展示计划完全相同的指标及幂等键',
  firstExtract && firstExtract.body.idempotency_key === pendingHybrid.idempotency_key
    && JSON.stringify(firstExtract.body.extraction_options) === JSON.stringify(hybridOptions)
    && firstExtract.body.plan_fingerprint === 'hybrid-plan-1', JSON.stringify(firstExtract))
const unconfirmed = vm.runInContext('analysisPlanPayload()', context)
elements.get('extraction-metrics').value = '净利润 | 华东 || 同比口径'
elements.get('extraction-highlights').checked = false
elements.get('extraction-metrics').oninput()
check('HYBRID-UI-3', '修改设置立即撤下旧计划并清理原请求键',
  !hooks.analysisState().hasPlan && elements.get('analysis-plan-panel').textContent === ''
    && ![...sessionValues.values()].includes(unconfirmed.idempotency_key),
  JSON.stringify(hooks.analysisState()))
const beforeModifiedClick = hybridRequests.length
await elements.get('extract').onclick()
const modifiedPlan = hybridRequests[beforeModifiedClick]
const newPending = vm.runInContext('analysisPlanPayload()', context)
check('HYBRID-UI-4', '新设置须先重新规划，清空中间条件仍保留正确口径位置',
  hybridRequests.length === beforeModifiedClick + 1
    && modifiedPlan.body.extraction_options.required_metrics[0].period === null
    && modifiedPlan.body.extraction_options.required_metrics[0].scope === '同比口径'
    && modifiedPlan.body.extraction_options.include_highlights === false
    && newPending.idempotency_key !== unconfirmed.idempotency_key,
  JSON.stringify(modifiedPlan))
elements.get('extraction-metrics').value = ''
elements.get('extraction-metrics').oninput()
const beforeInvalidClick = hybridRequests.length
await elements.get('extract').onclick()
check('HYBRID-UI-5', '既未指定指标也未启用重点时在本地阻止请求',
  hybridRequests.length === beforeInvalidClick && elements.get('message').dataset.kind === 'error',
  elements.get('message').textContent)
elements.get('extraction-metrics').value = Array.from({length: 13}, (_, i) => `指标${i}`).join('\n')
elements.get('extraction-metrics').oninput()
await elements.get('extract').onclick()
check('HYBRID-UI-6', '超出12项不创建规划或生成请求',
  hybridRequests.length === beforeInvalidClick && elements.get('message').textContent.includes('最多 12 项'),
  elements.get('message').textContent)
await elements.get('summary').onclick()
await elements.get('summary').onclick()
const summaryPlanRequest = hybridRequests.find((request) => request.url.endsWith('kind=summary'))
const summarySubmitRequest = hybridRequests.find((request) => request.url.endsWith('/summary'))
check('HYBRID-UI-7', '摘要不受提取设置限制，规划和提交都不携带提取选项',
  summaryPlanRequest && !summaryPlanRequest.body
    && summarySubmitRequest && !('extraction_options' in summarySubmitRequest.body),
  JSON.stringify({ plan: summaryPlanRequest, submit: summarySubmitRequest }))
elements.get('extraction-metrics').value = ''
elements.get('extraction-highlights').checked = true
elements.get('extraction-metrics').oninput()
const beforeAutomaticPlan = hybridRequests.length
await elements.get('extract').onclick()
check('HYBRID-UI-13', '不指定指标时采用自动重点模式并先展示范围',
  hybridRequests.length === beforeAutomaticPlan + 1
    && hybridRequests[beforeAutomaticPlan].body.extraction_options.required_metrics.length === 0
    && hybridRequests[beforeAutomaticPlan].body.extraction_options.include_highlights
    && elements.get('analysis-plan-panel').textContent.includes('自动挑选文档重点'),
  elements.get('analysis-plan-panel').textContent)
const hybridFetch = sandbox.fetch
let releaseOldPlan
sandbox.fetch = (url, options) => new Promise((resolve) => { releaseOldPlan = () => resolve({
  ok: true, json: async () => makePlan('extraction', JSON.parse(options.body).extraction_options),
}) })
elements.get('extraction-metrics').value = '旧收入'
elements.get('extraction-metrics').oninput()
const latePlan = vm.runInContext("loadAnalysisPlan('extraction')", context)
elements.get('extraction-metrics').value = '新收入'
elements.get('extraction-metrics').oninput()
releaseOldPlan()
await latePlan
check('HYBRID-UI-14', '设置修改前的迟到规划响应不能重新展示旧计划',
  !hooks.analysisState().hasPlan && elements.get('analysis-plan-panel').textContent === '',
  JSON.stringify(hooks.analysisState()))
sandbox.fetch = hybridFetch

const hybridResult = JSON.parse(JSON.stringify(analysisResult))
hybridResult.extraction.options = hybridOptions
hybridResult.extraction.items.push({ ...hybridResult.extraction.items[0], item_id: 'b1-i2',
  name: '净利润', content: '净利润98.7万元', value_text: '98.7' })
hybridResult.extraction.metric_coverage = { status: 'incomplete', required_count: 2, matched_count: 1,
  metrics: [{ metric_id: 'm1', ...hybridOptions.required_metrics[0], status: 'matched',
    item_ids: ['b1-i1'], reason: '已匹配指定字段' },
  { metric_id: 'm2', name: '<img src=x onerror=alert(1)>', subject: '华东', period: '2024年度',
    scope: null, status: 'unresolved', item_ids: [], reason: '结果尚未包含匹配的字段，不能确认' }] }
renderAnalysisResult(hybridResult)
const hybridPanel = elements.get('analysis-results')
check('HYBRID-UI-8', '输入处理完整与指标未确认分开展示，匹配项和补充项清楚区分',
  hybridPanel.textContent.includes('输入处理完整不表示所有事实均已提取')
    && hybridPanel.textContent.includes('指标匹配核对（不代表全部语义已验收）')
    && hybridPanel.textContent.includes('1/2') && hybridPanel.textContent.includes('未确认')
    && hybridPanel.textContent.includes('指定指标匹配项')
    && hybridPanel.textContent.includes('自动补充重点'), hybridPanel.textContent.slice(0, 750))
check('HYBRID-UI-9', '指标名称与未确认原因继续按纯文本渲染',
  hybridPanel.allNodes().every((node) => !['IMG','SCRIPT','SVG','IFRAME'].includes(node.tagName)),
  '指标 HTML 保留为文本，无可执行节点')
const hybridCopy = hooks.resultToText(hybridResult)
check('HYBRID-UI-10', '复制文本保留输入完整性、指标核对边界和提取项来源',
  hybridCopy.includes('输入处理完整') && hybridCopy.includes('不代表全部语义')
    && hybridCopy.includes('未确认') && hybridCopy.includes('【指定指标匹配项】')
    && hybridCopy.includes('【自动补充重点】'), hybridCopy.slice(0, 700))
hybridResult.extraction.options.include_highlights = false
renderAnalysisResult(hybridResult)
check('HYBRID-UI-11', '关闭自动补充时未命中候选不会被说成自动重点',
  elements.get('analysis-results').textContent.includes('其他提取项（未匹配指定指标）')
    && !elements.get('analysis-results').textContent.includes('自动补充重点'),
  '未命中候选显示其他提取项')
renderAnalysisResult(analysisResult)
check('HYBRID-UI-12', '历史结果没有新匹配记录时继续正常展示',
  !elements.get('analysis-results').textContent.includes('指标匹配核对')
    && elements.get('analysis-results').textContent.includes('收入 1.2 亿元'),
  '旧结果无覆盖字段，原结果及引用仍可读')

const sparseTable = vm.runInContext(`renderTable({num_rows: 2, num_cols: 3, cells: [
  {row: 0, col: 0, row_span: 1, col_span: 2, column_header: true, text: '合并表头'},
  {row: 0, col: 2, text: '第三列'},
  {row: 1, col: 0, text: '首列'}, {row: 1, col: 2, text: '末列'}]})`, context)
const tableRows = sparseTable.allNodes().filter((node) => node.tagName === 'TR')
check('REVIEW-UI-1', '表格预览保留缺列，末列内容不左移',
  tableRows[1].children.length === 3 && tableRows[1].children[1].textContent === '（空）'
    && tableRows[1].children[2].textContent === '末列', tableRows[1].textContent)
const spanningTable = vm.runInContext(`renderTable({num_rows: 2, num_cols: 2, cells: [
  {row: 0, col: 0, row_span: 2, col_span: 2, text: '跨两行两列'}]})`, context)
check('REVIEW-UI-2', '整行被上方合并单元格覆盖时不添加多余空行单元格',
  spanningTable.allNodes().filter((node) => node.tagName === 'TR')[1].children.length === 0,
  spanningTable.textContent)
renderAnalysisResult(analysisResult)
const resultCard = elements.get('analysis-results').allNodes().find((node) => node.id === 'result-citation-1')
elements.set('result-citation-1', resultCard)
const unrelatedCard = new Node('article')
elements.set('citation-1', unrelatedCard)
const analysisRef = elements.get('analysis-results').allNodes().find((node) => node.className === 'ref')
analysisRef.onclick()
check('REVIEW-UI-3', '分析结果引用按钮定位到分析引用，避免跳转问答同号引用',
  resultCard.classList.contains('citation-active') && !unrelatedCard.classList.contains('citation-active'),
  `分析引用高亮=${resultCard.classList.contains('citation-active')}`)

// 普通刷新和任务终态刷新都走生产数据加载路径，不能撤下用户正在核对的结果。
const closureDocuments = ['closure-a', 'closure-b'].map((id) => ({
  id, filename: `${id}.txt`, status: 'parsed', format: 'txt', page_count: 0,
  chunk_count: 1, active_parse_version_id: `${id}-v${id === 'closure-a' ? '2' : ''}`,
}))
const closureResult = { ...JSON.parse(JSON.stringify(analysisResult)), id: 'closure-result',
  document_id: 'closure-a', parse_version_id: 'closure-a-v', kind: 'summary',
  prompt_version: 'closure-summary-synthetic', protocol_version: 'closure-summary-protocol-synthetic',
  summary: { topic_overview: '闭环摘要主体', main_points: [{ text: '收入 1.2 亿元', refs: [1] }],
    exceptions: [], limitations: [], citations: analysisResult.extraction.citations.map((citation) => ({
      ...citation, document_id: 'closure-a', parse_version_id: 'closure-a-v',
    })) },
  extraction: null,
}
const closureRequests = []
const closureSummary = { id: closureResult.id, document_id: 'closure-a', kind: 'summary',
  parse_version_id: 'closure-a-v', created_at: closureResult.created_at, complete: true,
  requests_used: 1, is_active_version: false }
sandbox.closureDocuments = closureDocuments
sandbox.fetch = async (url, options = {}) => {
  closureRequests.push({ url, options })
  let data
  if (url === '/api/documents') data = closureDocuments
  else if (url === '/api/analysis-results/closure-result') data = closureResult
  else if (url.endsWith('/analysis-results')) data = url.includes('/closure-a/') ? [closureSummary] : []
  else if (url.endsWith('/analysis-jobs')) data = []
  else if (url.endsWith('/question-jobs')) data = []
  else if (url === '/api/analysis-jobs/closure-other-job') data = {
    id: 'closure-other-job', kind: 'extraction', status: 'succeeded', stage: 'done',
    requests_used: 1, max_requests: 8, steps_completed: 1, steps_total: 1,
  }
  else if (url.endsWith('/index')) data = { status: 'not_indexed', chunk_count: 0 }
  else if (url.includes('/content?')) {
    const documentId = url.includes('/closure-a/') ? 'closure-a' : 'closure-b'
    data = { document_id: documentId, version: { id: closureDocuments.find((doc) => doc.id === documentId)
      .active_parse_version_id, parser_name: 'local-text-2',
      quality_status: 'ok', warnings: [] }, is_active_version: true, total: 0, returned: 0, blocks: [] }
  }
  else if (url.includes('/analysis-plan?')) {
    const kind = url.endsWith('kind=summary') ? 'summary' : 'extraction'
    data = { ...makePlan(kind, null), document_id: 'closure-b', parse_version_id: 'closure-b-v' }
  }
  else if (url.endsWith('/status')) data = { configured: true, model: 'offline', base_url: 'offline',
    note: '', available: false, backend: 'docling', enabled: false }
  else throw new Error(`未预期的闭环离线请求 ${url}`)
  return { ok: true, json: async () => data }
}
vm.runInContext("documents=closureDocuments; selectedId='closure-a'; busy=false; answerBusy=false;", context)
await vm.runInContext("openResult('closure-result')", context)
await elements.get('refresh').onclick()
const refreshedPanel = elements.get('analysis-results')
check('CLOSURE-UI-1', '普通刷新保留当前摘要、引用与复制导出入口，并更新历史版本标记',
  hooks.analysisState().activeResultId === 'closure-result'
    && refreshedPanel.textContent.includes('闭环摘要主体')
    && refreshedPanel.textContent.includes('历史解析版本')
    && refreshedPanel.textContent.includes('复制可读内容')
    && refreshedPanel.allNodes().some((node) => node.id === 'result-citation-1')
    && refreshedPanel.allNodes().some((node) => node.href.includes('/closure-result/export')),
  JSON.stringify({ state: hooks.analysisState(), text: refreshedPanel.textContent.slice(0, 300) }))
await vm.runInContext("openResult('closure-result')", context)
vm.runInContext("analysisJobId='closure-other-job';", context)
await vm.runInContext('pollAnalysisJob()', context)
check('CLOSURE-UI-2', '另一分析任务结束后刷新历史，保留当前结果且不创建生成请求',
  hooks.analysisState().activeResultId === 'closure-result'
    && elements.get('analysis-results').textContent.includes('闭环摘要主体')
    && closureRequests.every((request) => !request.options.method || request.options.method === 'GET'),
  JSON.stringify(hooks.analysisState()))
await vm.runInContext("select('closure-b')", context)
vm.runInContext("analysisKind='extraction'; analysisPlan=null;", context)
const requestsBeforeRegenerate = closureRequests.length
await elements.get('analysis-regenerate').onclick()
check('CLOSURE-UI-3', '切换文档清空旧结果类型，重新生成使用当前文档所选提取类型',
  hooks.analysisState().activeResultId === null && hooks.analysisState().activeResultKind === null
    && !elements.get('analysis-results').textContent.includes('闭环摘要主体')
    && closureRequests.slice(requestsBeforeRegenerate).some((request) =>
      request.url.endsWith('/closure-b/analysis-plan?kind=extraction')),
  JSON.stringify({ state: hooks.analysisState(), requests: closureRequests.slice(requestsBeforeRegenerate) }))

// v4：稳定身份展示和解析原文高亮。全部通过本地fetch桩，不发送实际请求。
const previousFetch = sandbox.fetch
const evidenceRequests = []
const selectedQuote = '<img src=x onerror=alert(1)>'
const evidenceCitation = { reference_id: 3, document_id: 'citation-doc', parse_version_id: 'citation-v',
  chunk_id: 'citation-c', quote: selectedQuote, evidence_id: 'ev-stable-synthetic',
  verification: 'exact_match', offset_basis: 'chunk_crlf_lf_unicode_codepoints',
  char_start: 1, char_end: 1 + selectedQuote.length, sources: [{ format: 'pdf', page: 7 }] }
sandbox.fetch = async (url, options = {}) => {
  evidenceRequests.push({ url, options })
  return { ok: true, json: async () => [{ id: 'citation-c', document_id: 'citation-doc',
    parse_version_id: 'citation-v', text: `😀${selectedQuote}尾部` }] }
}
renderCitations([evidenceCitation], { is_old_version: true })
const evidencePanel = elements.get('answer-citations')
check('CITATION-V4-1', '来源卡展示稳定ID，并区分原文匹配与语义复核',
  evidencePanel.textContent.includes('ev-stable-synthetic') && evidencePanel.textContent.includes('语义支持待复核'),
  '显示编号不承担稳定身份；不宣称语义已通过')
const expandEvidence = evidencePanel.allNodes().find((node) => node.tagName === 'BUTTON' && node.textContent.includes('展开并高亮'))
await expandEvidence.onclick()
let evidenceMark = evidencePanel.allNodes().find((node) => node.tagName === 'MARK')
check('CITATION-V4-2', 'emoji之后的原文按Unicode码点准确高亮，恶意文字只作为文本',
  evidenceMark?.textContent === selectedQuote && !evidencePanel.allNodes().some((node) => node.tagName === 'IMG'),
  'UTF-16下标不冒充后端Unicode字符偏移')
await expandEvidence.onclick()
check('CITATION-V4-3', '展开引用只GET固定解析版本，重复展开复用本地原文',
  evidenceRequests.length === 1 && evidenceRequests[0].url.includes('version_id=citation-v')
    && (!evidenceRequests[0].options.method || evidenceRequests[0].options.method === 'GET'),
  JSON.stringify(evidenceRequests.map((r) => r.url)))
renderCitations([{ ...evidenceCitation, char_end: evidenceCitation.char_end + 1 }], {})
await elements.get('answer-citations').allNodes().find((node) => node.tagName === 'BUTTON' && node.textContent.includes('展开并高亮')).onclick()
check('CITATION-V4-4', '范围与原文不一致时拒绝高亮',
  !elements.get('answer-citations').allNodes().some((node) => node.tagName === 'MARK'),
  '错误范围不会通过find静默定位到另一处')
sandbox.fetch = previousFetch

const failed = results.filter((item) => !item.passed)
console.log(`\n离线渲染安全测试：${results.length - failed.length}/${results.length} 通过`)
process.exit(failed.length ? 1 : 0)
