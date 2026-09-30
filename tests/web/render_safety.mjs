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
  'question', 'ask', 'summary', 'extract', 'parse', 'reparse', 'refresh', 'test-model',
  'test-embedding', 'build-index', 'rebuild-index', 'search-query', 'search', 'stop-task',
  'retry-task', 'upload-form', 'question-form', 'search-form', 'file',
  // 分析任务（摘要 / 信息提取）面板容器。
  'analysis-plan', 'analysis-plan-panel', 'analysis-status', 'analysis-job', 'analysis-results',
  'analysis-stop', 'analysis-retry', 'analysis-regenerate']
const elements = new Map(containerIds.map((id) => [id, new Node('div')]))
elements.get('upload-form').querySelector = () => new Node('button')
elements.get('answer-panel').hidden = true

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

const failed = results.filter((item) => !item.passed)
console.log(`\n离线渲染安全测试：${results.length - failed.length}/${results.length} 通过`)
process.exit(failed.length ? 1 : 0)
