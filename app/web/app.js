// 工作台交互：集中管理当前文档、解析任务轮询、预览与检索状态。
//
// 本阶段要点（对应任务书工程包 F）：
// - “解析文档”与“重新解析”明确区分；提交后显示排队/执行阶段/完成/失败，不阻塞整页；
// - 每 2—3 秒查询任务，终态停止；断网显示连接状态并有限退避，不显示虚构百分比；
// - 刷新页面后从服务端恢复任务；切换文档时清理旧定时器，过期响应不得覆盖新选中文档；
// - 重新解析期间继续显示旧内容与检索；失败时同时显示新任务错误与“原有版本仍可用”；
// - 当前预览版本与检索版本不同时明确提示，并可查看命中的旧版本来源；
// - 所有用户内容与上游错误一律用 textContent 渲染，不使用 innerHTML。
const $ = (id) => document.getElementById(id);

// 文档状态：status 表示“内容是否可用”，task_status 表示“本次任务进展”，两者分开显示。
const statusLabels = { uploaded: '待解析', parsing: '解析中', parsed: '已解析', failed: '解析失败' };
const taskLabels = {
  queued: '已排队，等待 worker 领取', running: '正在执行', succeeded: '本次解析成功',
  failed: '本次解析失败', needs_attention: '需要人工确认',
};
const stageLabels = {
  queued: '排队中', submitting: '提交到解析服务', waiting_upstream: '等待解析服务返回',
  fetching_result: '领取解析结果', normalizing: '规范化结构', chunking: '结构化分块',
  publishing: '写入解析版本', done: '已完成',
};
const qualityLabels = { ok: '质量正常', warnings: '有质量告警', invalid: '结构无效，未发布' };
const indexLabels = {
  pending: '未建立索引', indexing: '正在建立索引', indexed: '索引可用',
  failed: '索引失败', stale: '模型或原文已变更，请重建索引',
};

// 页面状态保存在内存中；实际文档、任务与内容以服务端返回的数据为准。
let selectedId = sessionStorage.getItem('docqa.selectedDocument');
let documents = [];
let busy = false;
let modelConfigured = false;
let embeddingConfigured = false;
let indexState = 'pending';
let currentTaskId = null;
let pollTimer = null;
let pollDelay = 2500;          // 起始轮询间隔 2.5 秒，处于要求的 2—3 秒区间。
let pollFailures = 0;
let activeVersionId = null;    // 当前预览使用的解析版本。
let indexVersionId = null;     // 当前可用索引绑定的解析版本。
let searchMeta = null;
let requestSeq = 0;            // 请求序号：用于丢弃切换文档后才返回的过期响应。

async function api(path, options = {}) {
  // 统一 API 前缀与错误转换，让操作入口只处理成功数据或错误提示。
  const response = await fetch(`/api${path}`, options);
  let data = null;
  try { data = await response.json(); } catch (error) { data = null; }
  if (!response.ok) {
    const detail = data && typeof data.detail === 'string' ? data.detail : '请求失败，请稍后重试';
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return data;
}

function setMessage(text, kind = 'info') {
  // 页面提示统一出口；内容始终作为纯文本写入。
  const node = $('message');
  node.textContent = text || '';
  node.dataset.kind = kind;
}

function controls() {
  // 请求期间禁止重复操作；解析按钮在任务进行中禁用，避免重复提交。
  const doc = documents.find((item) => item.id === selectedId);
  const taskActive = doc && ['queued', 'running'].includes(doc.task_status);
  const hasVersion = Boolean(doc && doc.active_parse_version_id);
  $('parse').disabled = busy || !doc || taskActive || hasVersion;
  $('reparse').disabled = busy || !doc || taskActive;
  // 本阶段尚未提供生成能力，不能因解析完成就让占位按钮变成可用。
  for (const id of ['summary', 'extract', 'question', 'ask']) $(id).disabled = true;
  $('upload-form').querySelector('button').disabled = busy;
  $('refresh').disabled = busy;
  $('test-model').disabled = busy || !modelConfigured;
  $('test-embedding').disabled = busy || !embeddingConfigured;
  $('build-index').disabled = busy || !embeddingConfigured || !hasVersion
    || ['indexed', 'indexing'].includes(indexState);
  $('rebuild-index').disabled = busy || !embeddingConfigured || !hasVersion || indexState === 'indexing';
  for (const id of ['search-query', 'search']) $(id).disabled = busy || !embeddingConfigured || indexState !== 'indexed';
  $('stop-task').disabled = busy || !currentTaskId || !taskActive;
  for (const button of $('documents').querySelectorAll('button')) button.disabled = busy;
}

async function run(action) {
  // 所有交互共用忙碌状态和错误提示，失败后也必须恢复控件。
  if (busy) return;
  busy = true;
  setMessage('');
  controls();
  try { await action(); }
  catch (error) { setMessage(error.message, 'error'); }
  finally { busy = false; controls(); }
}

function renderDocuments() {
  // 每次根据最新列表重建按钮，并突出当前选中的文档。
  $('documents').replaceChildren();
  if (!documents.length) {
    const empty = document.createElement('p');
    empty.className = 'muted'; empty.textContent = '暂无文档。上传第一份资料开始使用。';
    $('documents').append(empty);
  }
  for (const doc of documents) {
    const button = document.createElement('button');
    button.className = `document-item ${doc.id === selectedId ? 'active' : ''}`;
    // 使用 textContent 显示用户内容，避免文件名被解释为 HTML。
    button.textContent = doc.filename;
    const meta = document.createElement('small');
    const format = doc.format ? doc.format.toUpperCase() : '格式未知';
    const task = doc.task_status && ['queued', 'running'].includes(doc.task_status)
      ? ` · ${taskLabels[doc.task_status]}` : '';
    meta.textContent = `${format} · ${statusLabels[doc.status]} · ${(doc.size / 1024).toFixed(1)} KB${task}`;
    button.append(meta);
    button.onclick = () => run(() => select(doc.id));
    $('documents').append(button);
  }
}

function stopPolling() {
  // 切换文档或任务终态时必须清理旧定时器，避免旧响应覆盖新选中文档。
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

async function select(id) {
  // 切换文档：先停止旧轮询并清空旧结果，再加载该文档的版本与内容。
  stopPolling();
  selectedId = id;
  sessionStorage.setItem('docqa.selectedDocument', id);
  currentTaskId = null;
  activeVersionId = null;
  indexVersionId = null;
  searchMeta = null;
  pollFailures = 0;
  pollDelay = 2500;
  const token = ++requestSeq;
  // 连接错误属于此前选中的任务，不能带到另一份已成功的文档上。
  $('connection-status').textContent = '';
  $('search-results').replaceChildren();
  $('task-status').textContent = '';
  $('version-status').textContent = '';
  $('quality-panel').replaceChildren();
  renderDocuments();
  const doc = documents.find((item) => item.id === id);
  $('document-title').textContent = doc.filename;
  const format = doc.format ? doc.format.toUpperCase() : '格式未知';
  $('document-meta').textContent = `${format} · ${statusLabels[doc.status]}`
    + ` · ${doc.page_count} 页 · ${doc.chunk_count} 个分块`
    + (doc.format_source ? ` · 格式依据：${doc.format_source}` : '')
    + (doc.error ? ` · ${doc.error}` : '');
  // 重新解析期间必须继续显示旧内容：旧版本仍可查看与检索。
  if (doc.active_parse_version_id && doc.index_version_mismatch) {
    setMessage('当前可用索引绑定的是历史解析版本，检索仍使用旧版本；如需检索最新版本请重建索引。', 'info');
  }
  await refreshIndex();
  if (token !== requestSeq) return;   // 期间又切换了文档，丢弃本次结果。
  await loadContent(id, null, token);
  if (token !== requestSeq) return;
  // 刷新后从服务端恢复仍在进行的任务。
  if (doc.latest_task_id && ['queued', 'running'].includes(doc.task_status)) {
    currentTaskId = doc.latest_task_id;
    pollDelay = 2500;
    schedulePoll(0);
  } else if (doc.latest_task_id) {
    currentTaskId = doc.latest_task_id;
    await renderTask(await api(`/parse-tasks/${doc.latest_task_id}`), token);
  }
}

async function loadContent(documentId, versionId, token = requestSeq, offset = 0) {
  // 读取指定解析版本的预览内容；分页参数固定为较大的页大小，避免一次取回过多内容。
  if (offset === 0) {
    $('chunks').replaceChildren();
    $('content-summary').textContent = '';
  }
  const query = `?limit=300&offset=${offset}` + (versionId ? `&version_id=${encodeURIComponent(versionId)}` : '');
  let data = null;
  try {
    data = await api(`/documents/${documentId}/content${query}`);
  } catch (error) {
    if (token !== undefined && token !== requestSeq) return;
    if (error.status === 404) {
      $('chunks').textContent = '尚无解析内容。点击“解析文档”创建解析任务。';
      return;
    }
    throw error;
  }
  if (token !== undefined && token !== requestSeq) return;
  activeVersionId = data.version.id;
  renderVersion(data);
  renderBlocks(data.blocks);
  renderWarnings(data.version.warnings);
  $('content-summary').textContent =
    `版本 ${data.version.id} · ${data.version.block_count} 个结构块（已显示 ${offset + data.returned}）`
    + ` · 解析器 ${data.version.parser_name}`
    + (data.version.is_legacy ? ' · 旧库迁移版本（未重新解析）' : '');
  if (offset + data.returned < data.total && data.returned > 0) {
    const more = document.createElement('button');
    more.textContent = '加载后续内容';
    more.onclick = () => run(async () => {
      await loadContent(documentId, data.version.id, token, offset + data.returned);
      more.remove();
    });
    $('chunks').append(more);
  }
}

function renderVersion(data) {
  const version = data.version;
  const parts = [];
  if (!data.is_active_version) parts.push('正在查看历史版本（不是当前预览版本）');
  if (version.quality_status) parts.push(qualityLabels[version.quality_status] || version.quality_status);
  if (version.quality_summary) parts.push(version.quality_summary);
  $('version-status').textContent = parts.join(' · ');
}

function renderBlocks(blocks) {
  // 结构块渲染：表格用结构化表格元素，正文用段落；全部使用 textContent。
  if (!blocks.length) {
    $('chunks').textContent = '该版本没有可显示的结构块。';
    return;
  }
  for (const block of blocks) {
    const article = document.createElement('article');
    article.className = 'chunk';
    const label = document.createElement('small');
    const sourceLabels = (block.sources || []).map((source) => sourceLabel(source));
    label.textContent = `${blockTypeLabel(block.block_type)} · 块 ${block.order_index + 1}`
      + (block.heading_path ? ` · ${block.heading_path}` : '')
      + (sourceLabels.length ? ` · ${sourceLabels.join(' / ')}` : ' · 无来源记录');
    article.append(label);
    if (block.table && block.table.cells) {
      article.append(renderTable(block.table));
    } else {
      const body = document.createElement('p');
      body.textContent = block.text || '（该块没有文本内容）';
      article.append(body);
    }
    if (block.table && block.table.formulas) article.append(renderFormulas(block.table.formulas));
    $('chunks').append(article);
    appendOriginalLinks(article, block.document_id, block.sources || []);
  }
}

function blockTypeLabel(type) {
  const labels = {
    title: '标题', section_header: '章节标题', paragraph: '正文', list_item: '列表项',
    table: '表格', document_index: '目录（识别为表格）', caption: '图表标题', footnote: '脚注',
    formula: '公式', code: '代码', reference: '引用', picture: '图片',
    page_header: '页眉', page_footer: '页脚',
  };
  return labels[type] || type;
}

function sourceLabel(source) {
  // 来源标签由后端生成，前端只做纯文本展示，避免自行编造页码。
  const parts = [];
  if (source.format === 'pdf' && source.page) parts.push(`第 ${source.page} 页`);
  if (source.format === 'xlsx') parts.push(source.sheet_name ? `工作表 ${source.sheet_name}` : '工作表未知');
  if (source.format === 'xlsx' && source.cell_range) parts.push(`单元格 ${source.cell_range}`);
  if (source.format === 'docx') {
    if (source.section_path) parts.push(`章节 ${source.section_path}`);
    if (source.table_no) parts.push(`表格 ${source.table_no}`);
    parts.push('DOCX 无真实页码');
  }
  if (source.format === 'txt') parts.push(source.page ? `逻辑页 ${source.page}` : '逻辑页未知');
  if (source.bbox) parts.push('含坐标框');
  if (!parts.length) parts.push(source.format || '来源未知');
  return parts.join(' · ');
}

function renderTable(table) {
  // 表格结构渲染：合并单元格用 colspan/rowspan 表达，空单元格与合并占位可区分。
  const wrapper = document.createElement('div');
  wrapper.className = 'table-wrap';
  const element = document.createElement('table');
  const rows = table.num_rows || 0;
  const cols = table.num_cols || 0;
  const cells = table.cells || [];
  const byRow = new Map();
  for (const cell of cells) {
    if (typeof cell.row !== 'number') continue;
    if (!byRow.has(cell.row)) byRow.set(cell.row, []);
    byRow.get(cell.row).push(cell);
  }
  for (let rowIndex = 0; rowIndex < rows; rowIndex += 1) {
    const tr = document.createElement('tr');
    const rowCells = (byRow.get(rowIndex) || []).slice().sort((a, b) => a.col - b.col);
    for (const cell of rowCells) {
      const td = document.createElement(cell.column_header ? 'th' : 'td');
      if (cell.col_span > 1) td.colSpan = cell.col_span;
      if (cell.row_span > 1) td.rowSpan = cell.row_span;
      td.textContent = cell.text === '' ? '（空）' : cell.text;
      if (cell.cell) td.title = `单元格 ${cell.cell}`;
      tr.append(td);
    }
    if (cols && rowCells.length === 0) {
      const td = document.createElement('td');
      td.colSpan = cols; td.textContent = '（空行）';
      tr.append(td);
    }
    element.append(tr);
  }
  wrapper.append(element);
  if (table.cell_range) {
    const note = document.createElement('small');
    note.className = 'muted';
    note.textContent = `工作表 ${table.sheet_name || '未知'} · 单元格范围 ${table.cell_range}`;
    wrapper.append(note);
  }
  return wrapper;
}

function renderFormulas(formulas) {
  // 公式展示：保留表达式与文件保存的缓存值；缓存缺失时明确说明，不显示为 0。
  const list = document.createElement('ul');
  list.className = 'formula-list';
  for (const [coordinate, info] of Object.entries(formulas)) {
    const item = document.createElement('li');
    const cached = info.has_cache ? String(info.cached_value) : '缺失（不能当作 0）';
    item.textContent = `${coordinate} = ${info.formula} · 文件保存的缓存值：${cached}`;
    list.append(item);
  }
  return list;
}

function renderWarnings(warnings) {
  // 质量告警按严重程度展示；没有告警时明确说明，不假装内容已验证。
  $('quality-panel').replaceChildren();
  if (!warnings || !warnings.length) {
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = '当前版本没有质量告警；这不表示内容已与原件逐字核对。';
    $('quality-panel').append(note);
    return;
  }
  for (const warning of warnings) {
    const item = document.createElement('div');
    item.className = `warning warning-${warning.severity}`;
    const title = document.createElement('strong');
    title.textContent = `${warning.severity === 'error' ? '错误' : warning.severity === 'warning' ? '告警' : '提示'} · ${warning.code}`;
    const text = document.createElement('p');
    text.textContent = warning.message
      + (warning.page ? `（第 ${warning.page} 页）` : '')
      + (warning.sheet_name ? `（工作表 ${warning.sheet_name}）` : '')
      + (warning.detail ? ` · ${warning.detail}` : '');
    item.append(title, text);
    $('quality-panel').append(item);
  }
}

async function refresh() {
  // 重新获取服务端状态；当前选择仍有效时，同时刷新详情。
  const refreshToken = requestSeq;
  const model = await api('/model/status');
  modelConfigured = model.configured;
  $('model-status').textContent = `${model.model} · ${model.configured ? '已配置，尚未测试连接' : '未配置密钥，请填写本地 .env 并重启服务'}`;
  const embedding = await api('/embedding/status');
  embeddingConfigured = embedding.configured;
  $('embedding-status').textContent = `${embedding.model} · ${embedding.configured ? '已配置，尚未测试连接' : '请填写 EMBEDDING_API_KEY 并重启服务'}`;
  $('embedding-gateway').textContent = `网关：${embedding.base_url}`;
  await refreshParsingStatus();
  const latestDocuments = await api('/documents');
  if (refreshToken !== requestSeq) return;
  documents = latestDocuments;
  renderDocuments();
  if (selectedId && documents.some((doc) => doc.id === selectedId)) await select(selectedId);
}

async function refreshParsingStatus() {
  // 解析服务可达性单独探测：可达不代表模型已预热或内容质量合格。
  try {
    const status = await api('/parsing/status');
    $('parsing-status').textContent = status.reachable
      ? `解析服务可响应 · ${status.base_url} · OCR ${status.ocr_engine}/${status.ocr_lang} · 表格 ${status.table_mode} · 页数上限 ${status.max_pdf_pages}`
      : `解析服务不可达 · ${status.base_url} · ${status.detail}`;
    $('parsing-status').dataset.kind = status.reachable ? 'ok' : 'error';
  } catch (error) {
    $('parsing-status').textContent = '无法读取解析服务状态';
    $('parsing-status').dataset.kind = 'error';
  }
}

$('upload-form').onsubmit = (event) => {
  // 阻止浏览器默认提交，以局部更新保留工作台状态。
  event.preventDefault();
  run(async () => {
    // 由浏览器生成 multipart 边界，不手动设置上传请求的 Content-Type。
    const data = new FormData($('upload-form'));
    const result = await api('/documents', { method: 'POST', body: data });
    selectedId = result.document.id;
    await refresh();
    $('upload-form').reset();
    setMessage(`上传成功：识别为 ${result.format.toUpperCase()}（依据：${result.format_source}）。${result.format_note}`, 'ok');
  });
};
$('refresh').onclick = () => run(refresh);

async function refreshIndex() {
  // 索引状态包含绑定版本与“是否对应当前预览版本”，两者不一致时必须提示。
  const id = selectedId;
  const token = requestSeq;
  const state = await api(`/documents/${id}/index`);
  if (id !== selectedId || token !== requestSeq) return;
  indexState = state.status;
  indexVersionId = state.parse_version_id;
  const doc = documents.find((item) => item.id === selectedId);
  activeVersionId = doc ? doc.active_parse_version_id : null;
  let text = indexLabels[state.status] || state.status;
  if (state.dimension) text += ` · ${state.dimension} 维 · ${state.chunk_count} 个分块`;
  if (state.error) text += ` · ${state.error}`;
  const attempt = state.attempts && state.attempts[0];
  if (state.status === 'indexed' && attempt && attempt.status === 'failed') {
    text += ' · 最近一次新索引构建失败，原索引仍可检索';
  }
  if (state.status === 'indexed' && attempt && attempt.status === 'indexing') {
    text += ' · 新索引构建中，原索引仍可检索';
  }
  if (state.status === 'indexed' && doc && state.parse_version_id && state.parse_version_id !== doc.active_parse_version_id) {
    text += ' · 注意：索引绑定的是历史解析版本，检索仍使用旧版本';
  }
  $('index-status').textContent = text;
}

$('test-embedding').onclick = () => run(async () => {
  $('embedding-status').textContent = '正在测试向量连接…';
  try {
    const result = await api('/embedding/test', { method: 'POST' });
    $('embedding-status').textContent = `${result.model} · 连接成功 · ${result.dimension} 维`;
  } catch (error) {
    $('embedding-status').textContent = '向量连接测试失败';
    throw error;
  }
});

async function buildIndex(rebuild = false) {
  // 建索引是收费操作，只能由用户主动触发；页面加载与轮询都不会调用。
  $('index-status').textContent = '正在向量化文档，请稍候…';
  $('search-results').replaceChildren();
  try {
    await api(`/documents/${selectedId}/index?rebuild=${rebuild}`, { method: 'POST' });
  } finally {
    await refreshIndex();
  }
}
$('build-index').onclick = () => run(() => buildIndex());
$('rebuild-index').onclick = () => run(() => buildIndex(true));

$('search-form').onsubmit = (event) => {
  event.preventDefault();
  run(async () => {
    $('search-results').replaceChildren();
    const data = await api(`/documents/${selectedId}/search`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: $('search-query').value.trim(), top_k: 5 }),
    });
    searchMeta = data;
    renderSearch(data);
  });
};

function renderSearch(data) {
  // 检索结果显式标注使用的索引与解析版本；旧版本必须提示，并允许查看命中版本原文。
  const note = document.createElement('p');
  note.className = 'muted';
  note.textContent = `索引 ${data.index_id || '未知'} · 解析版本 ${data.parse_version_id}`
    + (data.is_legacy ? ' · 旧库迁移索引' : '')
    + ` · ${data.message}`;
  $('search-results').append(note);
  if (data.is_old_version) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    button.textContent = `查看命中版本（${data.parse_version_id}）原文`;
    button.onclick = () => run(() => loadContent(selectedId, data.parse_version_id));
    $('search-results').append(button);
  }
  if (!data.results.length) {
    const empty = document.createElement('p');
    empty.textContent = '没有可用的检索片段。';
    $('search-results').append(empty);
    return;
  }
  for (const hit of data.results) {
    const article = document.createElement('article');
    article.className = 'chunk';
    const label = document.createElement('small');
    const sourceLabels = (hit.sources || []).map((source) => sourceLabel(source));
    label.textContent = `相似度 ${hit.score.toFixed(3)} · 分块类型 ${hit.chunk_type || '未知'}`
      + (hit.heading_path ? ` · ${hit.heading_path}` : '')
      + (sourceLabels.length ? ` · ${sourceLabels.join(' / ')}` : ' · 无来源记录');
    const text = document.createElement('p');
    text.textContent = hit.text;
    article.append(label, text);
    appendOriginalLinks(article, hit.document_id, hit.sources || []);
    $('search-results').append(article);
  }
}

function appendOriginalLinks(container, documentId, sources) {
  // 页码只对 PDF 使用，其他格式下载原件；地址始终由受控 API 路径构造。
  const pages = [...new Set(sources.filter(s => s.format === 'pdf' && Number.isInteger(s.page) && s.page > 0).map(s => s.page))];
  for (const page of pages.length ? pages : [null]) {
    const link = document.createElement('a');
    link.href = `/api/documents/${encodeURIComponent(documentId)}/original` + (page ? `#page=${page}` : '');
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = page ? `查看原件第 ${page} 页` : '下载原件核对';
    container.append(link, document.createTextNode(' '));
  }
}

// 不在页面加载时自动调用模型；仅点击后发送固定测试消息。
$('test-model').onclick = () => run(async () => {
  $('model-status').textContent = '正在连接 DeepSeek，请稍候…';
  try {
    const result = await api('/model/test', { method: 'POST' });
    $('model-status').textContent = `${result.model} · 连接成功`;
    setMessage(`模型回复：${result.answer}`, 'ok');
  } catch (error) {
    $('model-status').textContent = '连接测试失败';
    throw error;
  }
});

async function submitParse(force) {
  // 解析与重新解析共用提交逻辑，区别只在 force。
  setMessage('');
  const body = JSON.stringify({ force: force });
  const data = await api(`/documents/${selectedId}/parse`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body,
  });
  currentTaskId = data.task ? data.task.id : null;
  pollDelay = 2500;
  pollFailures = 0;
  await refresh();
  if (!currentTaskId) {
    setMessage(data.message, 'ok');
    return;
  }
  setMessage(data.message + (data.reused ? '（复用了已有任务）' : ''), 'ok');
  schedulePoll(0);
}

$('parse').onclick = () => run(() => submitParse(false));
$('reparse').onclick = () => run(() => submitParse(true));

function schedulePoll(delay) {
  // 统一调度：每次先清理旧定时器，避免多个轮询叠加。
  stopPolling();
  pollTimer = setTimeout(pollTask, delay === undefined ? pollDelay : delay);
}

async function pollTask() {
  // 轮询任务：终态停止；网络异常时显示连接状态并有限退避，不显示虚构进度。
  if (!currentTaskId || !selectedId) { stopPolling(); return; }
  const taskId = currentTaskId;
  const documentId = selectedId;
  const token = requestSeq;
  try {
    const task = await api(`/parse-tasks/${taskId}`);
    // 先确认请求仍属于当前文档和任务，再修改连接提示与退避计数。
    if (token !== requestSeq || documentId !== selectedId || taskId !== currentTaskId) return;
    pollFailures = 0;
    pollDelay = 2500;
    $('connection-status').textContent = '';
    await renderTask(task, token);
    if (token !== requestSeq || documentId !== selectedId || taskId !== currentTaskId) return;
    if (['queued', 'running'].includes(task.status)) {
      schedulePoll();
    } else {
      stopPolling();
      await refresh();
    }
  } catch (error) {
    if (token !== requestSeq || documentId !== selectedId || taskId !== currentTaskId) return;
    pollFailures += 1;
    $('connection-status').textContent =
      `与服务器连接异常（第 ${pollFailures} 次）：${error.message}；将有限退避后重试`;
    // 有限退避：最长 15 秒，超过 6 次后停止轮询并提示手动刷新。
    if (pollFailures > 6) {
      stopPolling();
      $('connection-status').textContent =
        '多次无法连接服务器，已停止自动刷新；请检查网络后点击“刷新”恢复。';
      return;
    }
    pollDelay = Math.min(15000, pollDelay * 2);
    schedulePoll();
  } finally {
    // 后台轮询不经过 run()；终态刷新后也必须重新计算按钮状态。
    controls();
  }
}

async function renderTask(task, token) {
  // 展示任务阶段、时间与错误；任务失败时明确说明原有版本是否仍可用。
  if (token !== undefined && token !== requestSeq) return;
  const parts = [taskLabels[task.status] || task.status, stageLabels[task.stage] || task.stage];
  if (task.attempt_count) parts.push(`第 ${task.attempt_count} 次尝试`);
  if (task.upstream_task_id) parts.push(`上游任务 ${task.upstream_task_id}`);
  $('task-status').textContent = parts.join(' · ');
  if (task.error_message) {
    $('task-status').textContent += ` · ${task.error_message}`;
  }
  if (task.status === 'failed' || task.status === 'needs_attention') {
    const doc = documents.find((item) => item.id === selectedId);
    const stillAvailable = doc && doc.active_parse_version_id;
    setMessage(
      `${taskLabels[task.status]}：${task.error_message || '请查看任务详情'}`
      + (stillAvailable ? (indexState === 'indexed'
        ? '。原有解析版本与索引仍然可用，可继续查看与检索。'
        : '。原有解析内容仍可查看；尚无与当前模型兼容的可用索引。')
        : '。该文档尚无可用解析版本。'),
      'error');
    if (token === undefined || token === requestSeq) {
      $('retry-task').hidden = !task.id;
      $('retry-task').dataset.taskId = task.id;
    }
  } else {
    $('retry-task').hidden = true;
  }
  if (task.warnings && task.warnings.length && (token === undefined || token === requestSeq)) {
    renderWarnings(task.warnings);
  }
}

$('retry-task').onclick = () => run(async () => {
  // 明确重试：新建一次尝试，保留原失败记录；不会自动重投上游。
  const taskId = $('retry-task').dataset.taskId;
  if (!taskId) return;
  const task = await api(`/parse-tasks/${taskId}/retry`, { method: 'POST' });
  currentTaskId = task.id;
  pollDelay = 2500;
  pollFailures = 0;
  setMessage(task.upstream_task_id ? '已恢复等待原有上游任务，不重新上传。'
    : '已创建新的解析尝试；原失败记录保留，不确定提交的重试可能产生重复转换。', 'ok');
  await refresh();
  schedulePoll(0);
});

$('stop-task').onclick = () => run(async () => {
  // 停止等待：只停止本次等待，上游任务编号保留，可稍后恢复。
  if (!currentTaskId) return;
  await api(`/parse-tasks/${currentTaskId}/cancel`, { method: 'POST' });
  setMessage('已请求停止；上游任务编号已保留，可在稍后恢复继续。', 'ok');
  await refresh();
});

async function intelligence(action, body) {
  // 共用摘要、提取和问答调用流程；尚未接入时由 run 显示后端 503 提示。
  const result = await api(`/documents/${selectedId}/${action}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  $('result').textContent = JSON.stringify(result, null, 2);
  $('result').hidden = false;
}
$('summary').onclick = () => run(() => intelligence('summary'));
$('extract').onclick = () => run(() => intelligence('extract'));
$('question-form').onsubmit = (event) => {
  event.preventDefault();
  // 去除首尾空白后提交；后端仍会校验空白问题和长度限制。
  run(() => intelligence('questions', { question: $('question').value.trim() }));
};

// 页面隐藏时暂停轮询，重新可见时立即补一次查询，避免后台堆积请求。
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling();
  } else if (currentTaskId) {
    schedulePoll(0);
  }
});

// 自动化验收（T21/T22）专用只读钩子：只暴露读取定时器数量与“开始轮询指定任务”的能力，
// 便于在真实浏览器中断言“终态后停止轮询”和“断网时的连接提示”，不改变页面行为。
window.__docqaTest = {
  timers: () => (pollTimer === null ? 0 : 1),
  startPolling: (documentId, taskId) => {
    selectedId = documentId;
    currentTaskId = taskId;
    pollDelay = 2500;
    pollFailures = 0;
    schedulePoll(0);
    return true;
  },
};

// 首次打开页面时加载文档列表。
run(refresh);
