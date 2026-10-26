// 工作台交互：集中管理当前文档、解析任务轮询、预览与检索状态，以及单文档问答。
//
// RAG 问答阶段要点（对应任务书工程包 E）：
// - 问答框只在“文档可用 + 有效索引 + 两个模型已配置 + 当前没有问答请求”时启用；
// - 提问后显示“正在检索资料并生成回答”，非流式不伪造 token 输出或进度百分比；
// - 成功与失败分支都校验请求序号与当前选中文档，迟到响应不得覆盖新状态；
// - 答案、澄清问题、依据不足提示与引用卡片全部用 createElement/textContent 渲染，
//   只支持有限 Markdown（标题/列表/加粗），不解析任意 HTML、图片或外链；
// - 问答持久化为任务：只读轮询、历史与刷新不产生生成调用；提交不明时保留原请求键。
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
const qualityLabels = { ok: '质量正常', warnings: '有质量限制或告警', invalid: '结构无效，未发布' };
const indexLabels = {
  pending: '未建立索引', indexing: '正在建立索引', indexed: '索引可用',
  failed: '索引失败', stale: '模型或原文已变更，请重建索引',
};

// 页面状态保存在内存中；实际文档、任务与内容以服务端返回的数据为准。
let selectedId = (window.location ? new URLSearchParams(window.location.search).get('document') : null)
  || sessionStorage.getItem('docqa.selectedDocument');
let documents = [];
let busy = false;
let modelConfigured = false;
let embeddingConfigured = false;
let ragConfigured = false;      // 两个在线模型是否都已配置（只代表配置存在）。
let analysisConfigured = false; // 生成模型是否已配置（分析任务只需要这一个）。
let analysisJobState = null;    // 最近一次查询到的分析任务状态（不伪造进度）。
let indexState = 'pending';
let currentTaskId = null;
let pollTimer = null;
let pollDelay = 2500;          // 起始轮询间隔 2.5 秒，处于要求的 2—3 秒区间。
let pollFailures = 0;
let activeVersionId = null;    // 当前预览使用的解析版本。
let indexVersionId = null;     // 当前可用索引绑定的解析版本。
let searchMeta = null;
let requestSeq = 0;            // 请求序号：用于丢弃切换文档后才返回的过期响应。
let answerBusy = false;        // 仅当前文档的问答变更请求在途；不占用工作台全局 busy。
let answerWaitTimer = null;    // 等待计时器：只显示真实等待时间，不伪造进度。
let answerStartedAt = 0;
let answerPayload = null;
let questionJobId = null;
let questionJobState = null;
let questionJobs = [];
let questionResult = null;
let questionPollTimer = null;
let questionPollDelay = 2500;
let questionPollFailures = 0;
let questionMutationSeq = 0;
let questionViewSeq = 0;       // 同文档手动刷新或历史重开也使旧查询失效。
const questionActiveStatuses = ['queued', 'running', 'cancel_requested'];
// 验收记录：每次真正发出的问答请求及其结果，用于证明“每个有效提交只对应一次请求”。
let askRequestCount = 0;
const askRequests = [];

// 服务状态单独查询：不经过生成、解析或外部模型连接测试入口。
let runtimePollTimer = null;
let runtimeQuery = null;
let runtimePollFailures = 0;
let runtimeCheckedAt = null;

// ---------------------------------------------------------------------------
// 分析任务（摘要 / 信息提取）页面状态
//
// 关键约束（对应任务书 §8）：
// - 用户先看到分析范围、解析版本与请求数上界，再明确发起生成；
// - 刷新、轮询、查看历史与导出都不得触发生成；
// - 任务状态与结果历史来自服务端持久化数据，页面不伪造进度或 token 流。
// ---------------------------------------------------------------------------
let analysisKind = 'extraction';   // 当前查看／发起的分析类型。
let analysisPlan = null;           // 最近一次“查看分析范围”的结果（零外部调用）。
let analysisJobId = null;
let analysisPollTimer = null;
let analysisPollDelay = 2500;
let analysisPollFailures = 0;
let analysisResults = [];          // 结果历史（不含正文）。
let activeResultId = null;         // 当前展示的结果。
let activeResultPayload = null;    // 当前已读取的可信结果，历史列表刷新后继续展示。
let lastResultKind = null;         // 最近一次结果类型，供“重新生成”使用。
let analysisRequestCount = 0;      // 页面发起的生成提交次数（用于断言不重复提交）。
let extractionSettingsRevision = 0; // 丢弃设置变更前的迟到规划响应。

async function api(path, options = {}) {
  // 统一 API 前缀与错误转换，让操作入口只处理成功数据或错误提示。
  const response = await (window.docqaFetch || fetch)(`/api${path}`, options);
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
  // 分析能力只需要“有合格解析版本 + 生成模型已配置”，不依赖 embedding 索引。
  const analysisActive = analysisJobId && analysisJobState
    && ['queued', 'running'].includes(analysisJobState.status);
  for (const id of ['summary', 'extract', 'analysis-plan']) {
    $(id).disabled = busy || !hasVersion || !analysisConfigured;
  }
  for (const id of ['extraction-metrics', 'extraction-highlights']) $(id).disabled = busy;
  $('analysis-stop').disabled = busy || !analysisActive;
  $('analysis-retry').hidden = !(analysisJobState
    && ['failed', 'needs_attention'].includes(analysisJobState.status));
  $('analysis-retry').disabled = busy || !analysisJobState
    || analysisJobState.requests_used >= analysisJobState.max_requests;
  $('analysis-regenerate').hidden = !hasVersion || !analysisConfigured;
  $('upload-form').querySelector('button').disabled = busy;
  $('refresh').disabled = busy;
  $('test-model').disabled = busy || !modelConfigured;
  $('test-embedding').disabled = busy || !embeddingConfigured;
  $('build-index').disabled = busy || !embeddingConfigured || !hasVersion
    || ['indexed', 'indexing'].includes(indexState);
  $('rebuild-index').disabled = busy || !embeddingConfigured || !hasVersion || indexState === 'indexing';
  for (const id of ['search-query', 'search']) $(id).disabled = busy || !embeddingConfigured || indexState !== 'indexed';
  // 问答需要：有效索引 + 两个模型已配置 + 没有正在进行的问答请求。
  const pendingQuestion = readQuestionSession(selectedId);
  const questionActive = questionJobState && questionActiveStatuses.includes(questionJobState.status);
  for (const id of ['question', 'ask']) {
    $(id).disabled = busy || answerBusy || questionActive || Boolean(pendingQuestion?.uncertain)
      || !hasVersion || indexState !== 'indexed' || !ragConfigured;
  }
  $('question-cancel').disabled = answerBusy || !questionActive || Boolean(questionJobState?.cancel_requested);
  $('question-cancel').hidden = !questionActive;
  $('question-retry').hidden = !questionJobState
    || !['failed', 'needs_attention'].includes(questionJobState.status);
  $('question-retry').disabled = answerBusy || Boolean(pendingQuestion?.uncertain) || !questionJobState
    || questionJobState.requests_used >= questionJobState.max_requests;
  $('question-confirm').hidden = !pendingQuestion?.uncertain;
  $('question-confirm').disabled = answerBusy;
  $('question-refresh').disabled = answerBusy || !doc;
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

async function select(id, preserveAnswer = false) {
  // 切换文档：先停止旧轮询并清空旧结果，再加载该文档的版本与内容。
  // 同一文档的普通刷新保留正在核对的结果；真正切文档必须同时隔离结果和类型。
  const preservedResult = preserveAnswer && selectedId === id ? activeResultPayload : null;
  stopPolling();
  stopAnalysisPolling();
  stopQuestionPolling();
  stopAnswerTimer();
  answerBusy = false;
  questionMutationSeq += 1;
  questionViewSeq += 1;
  selectedId = id;
  sessionStorage.setItem('docqa.selectedDocument', id);
  currentTaskId = null;
  activeVersionId = null;
  indexVersionId = null;
  searchMeta = null;
  pollFailures = 0;
  pollDelay = 2500;
  // 分析状态属于单份文档：切换文档时不沿用上一份的范围、任务与结果。
  analysisPlan = null;
  analysisJobId = null;
  analysisJobState = null;
  analysisResults = [];
  activeResultPayload = preservedResult;
  activeResultId = preservedResult ? preservedResult.id : null;
  lastResultKind = preservedResult ? preservedResult.kind : null;
  analysisPollFailures = 0;
  analysisPollDelay = 2500;
  questionJobId = null;
  questionJobState = null;
  questionJobs = [];
  questionPollFailures = 0;
  questionPollDelay = 2500;
  const preservedQuestionResult = preserveAnswer && questionResult?.document_id === id ? questionResult : null;
  questionResult = preservedQuestionResult;
  const token = ++requestSeq;
  // 连接错误属于此前选中的任务，不能带到另一份已成功的文档上。
  $('connection-status').textContent = '';
  $('search-results').replaceChildren();
  $('task-status').textContent = '';
  $('version-status').textContent = '';
  $('quality-panel').replaceChildren();
  $('analysis-plan-panel').replaceChildren();
  $('analysis-results').replaceChildren();
  $('analysis-job').replaceChildren();
  // 历史答案按文档隔离，刷新从服务端恢复；不把上一文档的结果带到新文档。
  if (!preservedQuestionResult) resetAnswer();
  $('question-job').replaceChildren();
  $('question-history').replaceChildren();
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
  // 刷新后从服务端恢复分析历史与仍在进行的分析任务（普通刷新不清空正在查看的结果）。
  if (token !== requestSeq) return;
  await loadAnalysisResults();
  if (token !== requestSeq) return;
  const jobs = await api(`/documents/${id}/analysis-jobs`);
  if (token !== requestSeq) return;
  const activeJob = jobs.find((job) => ['queued', 'running'].includes(job.status));
  if (activeJob) {
    analysisJobId = activeJob.id;
    renderAnalysisJob(activeJob);
    analysisPollDelay = 2500;
    scheduleAnalysisPoll(0);
  } else if (jobs.length) {
    analysisJobId = jobs[0].id;
    renderAnalysisJob(jobs[0]);
  } else {
    $('analysis-job').replaceChildren();
  }
  if (token !== requestSeq) return;
  await restoreQuestions();
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
  if (data.content_mode === 'legacy_chunks') {
    renderLegacyChunks(data.legacy_chunks || []);
  } else {
    renderBlocks(data.blocks);
  }
  renderWarnings(data.version.warnings, data.version);
  $('content-summary').textContent =
    `版本 ${data.version.id} · ${data.total} 个${data.content_mode === 'legacy_chunks' ? '旧分块' : '结构块'}（已显示 ${offset + data.returned}）`
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

function renderLegacyChunks(chunks) {
  // 旧逻辑页不冒充 PDF 物理页，也不补造结构化来源或放宽摘要/提取前置要求。
  if (!chunks.length) {
    $('chunks').textContent = '该旧版本没有可显示的分块，请查看原件。';
    return;
  }
  const note = document.createElement('p');
  note.className = 'muted';
  note.textContent = '旧版本仅保留原始分块，缺少可靠结构化来源；如需摘要或信息提取，请明确重新解析。';
  $('chunks').append(note);
  for (const chunk of chunks) {
    const article = document.createElement('article');
    article.className = 'chunk';
    const label = document.createElement('small');
    label.textContent = `旧分块 ${chunk.id}` + (chunk.page ? ` · 旧逻辑页 ${chunk.page}` : '');
    const body = document.createElement('p');
    body.textContent = chunk.text;
    article.append(label, body);
    $('chunks').append(article);
  }
}

function blockTypeLabel(type) {
  const labels = {
    title: '标题', section_header: '章节标题', paragraph: '正文', list_item: '列表项', markdown_table: 'Markdown表格',
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
  const grid = new Map();
  for (const cell of cells) {
    if (!Number.isInteger(cell.row) || !Number.isInteger(cell.col)) continue;
    const height = cell.row_span || 1;
    const width = cell.col_span || 1;
    for (let r = cell.row; r < Math.min(rows, cell.row + height); r += 1) {
      for (let c = cell.col; c < Math.min(cols, cell.col + width); c += 1) {
        grid.set(`${r}:${c}`, r === cell.row && c === cell.col ? cell : null);
      }
    }
  }
  for (let rowIndex = 0; rowIndex < rows; rowIndex += 1) {
    const tr = document.createElement('tr');
    for (let colIndex = 0; colIndex < cols; colIndex += 1) {
      const key = `${rowIndex}:${colIndex}`;
      const cell = grid.get(key);
      if (grid.has(key) && cell === null) continue; // 真正的合并占位，不额外生成单元格。
      if (!cell) {
        const empty = document.createElement('td');
        empty.textContent = '（空）';
        tr.append(empty);
        continue;
      }
      const td = document.createElement(cell.column_header ? 'th' : 'td');
      if (cell.col_span > 1) td.colSpan = cell.col_span;
      if (cell.row_span > 1) td.rowSpan = cell.row_span;
      td.textContent = cell.text === '' ? '（空）' : cell.text;
      if (cell.cell) td.title = `单元格 ${cell.cell}`;
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

function qualityWarningMessage(warning) {
  // 旧不可变解析仍保存原提示；展示时解释其含义，不修改历史记录。
  const message = warning.message || '';
  if (warning.code === 'ocr_limitation' && !message.includes('不表示检测到OCR失败')) {
    return `${message}（通用能力提示：尚未逐页验证准确性，不表示检测到OCR失败。）`;
  }
  return message;
}

function renderWarnings(warnings, version = null) {
  // legacy 可能只有版本级限制，没有独立告警条目，不能据此宣称没有质量问题。
  $('quality-panel').replaceChildren();
  if (!warnings || !warnings.length) {
    const note = document.createElement('p');
    note.className = 'muted';
    const limited = version && (version.is_legacy
      || ['warnings', 'invalid'].includes(version.quality_status));
    note.textContent = limited
      ? '该版本存在质量限制；未记录独立告警条目。请结合上方版本说明及原件核对。'
      : '未记录独立质量告警条目；这不表示内容已与原件逐字核对。';
    $('quality-panel').append(note);
    return;
  }
  for (const warning of warnings) {
    const item = document.createElement('div');
    item.className = `warning warning-${warning.severity}`;
    const title = document.createElement('strong');
    title.textContent = `${warning.severity === 'error' ? '错误' : warning.severity === 'warning' ? '告警' : '提示'} · ${warning.code}`;
    const text = document.createElement('p');
    text.textContent = qualityWarningMessage(warning)
      + (warning.page ? `（第 ${warning.page} 页）` : '')
      + (warning.sheet_name ? `（工作表 ${warning.sheet_name}）` : '')
      + (warning.detail ? ` · ${warning.detail}` : '');
    item.append(title, text);
    $('quality-panel').append(item);
  }
}

const runtimeRoleLabels = { web: '网页服务', 'parse-worker': '解析 worker', 'analysis-worker': '分析 worker' };

function runtimeRoleReady(role) {
  // 缺字段必须保留“未知”，不能把进程存活或已配置推断为兼容且就绪。
  return role && ['running', 'ready', 'code_match', 'schema_match', 'data_dir_match']
    .every((key) => role[key] === true);
}

function renderRuntimeStatus(status) {
  const roles = [status && status.web, ...['parse-worker', 'analysis-worker'].map((name) =>
    status && Array.isArray(status.workers) ? status.workers.find((role) => role && role.role === name) : null)];
  const allReady = roles.every(runtimeRoleReady);
  const summary = $('runtime-summary');
  summary.textContent = status ? (allReady
    ? '网页、解析 worker 与分析 worker 均已就绪，代码、数据目录和数据库结构匹配。'
    : '部分本地角色尚未确认就绪，请查看各角色状态；需要时整体重启本地服务。')
    : '尚未取得当前服务状态，无法确认各角色是否就绪。';
  summary.dataset.kind = status ? (allReady ? 'ok' : 'error') : 'unknown';
  const schemaVersion = status && Number.isInteger(status.schema_version) ? status.schema_version : '未知';
  const expectedVersion = status && Number.isInteger(status.expected_schema_version) ? status.expected_schema_version : '未知';
  $('runtime-schema').textContent = `数据库结构版本：实际 ${schemaVersion} · 代码要求 ${expectedVersion}`;
  const container = $('runtime-roles');
  container.replaceChildren();
  for (const [index, name] of Object.keys(runtimeRoleLabels).entries()) {
    const role = roles[index];
    const card = document.createElement('article');
    card.className = 'runtime-role';
    card.dataset.role = name;
    const title = document.createElement('h3');
    title.textContent = runtimeRoleLabels[name];
    const state = document.createElement('p');
    state.className = 'runtime-state';
    state.textContent = runtimeRoleReady(role) ? '已就绪' : role && role.running === false ? '未运行'
      : role && role.running === true ? '运行中，尚未确认就绪' : '状态未知';
    state.dataset.kind = runtimeRoleReady(role) ? 'ok' : role ? 'error' : 'unknown';
    const facts = document.createElement('dl');
    for (const [field, label, yes, no] of [
      ['running', '进程', '运行中', '未运行'], ['ready', '就绪记录', '已就绪', '未就绪'],
      ['code_match', '代码', '匹配', '不匹配'], ['schema_match', '数据库结构', '匹配', '不匹配'],
      ['data_dir_match', '数据目录', '匹配', '不匹配'],
    ]) {
      const term = document.createElement('dt');
      term.textContent = label;
      const value = document.createElement('dd');
      const actual = role && role[field];
      value.textContent = actual === true ? yes : actual === false ? no : '未知';
      value.dataset.kind = actual === true ? 'ok' : actual === false ? 'error' : 'unknown';
      facts.append(term, value);
    }
    // 仅展示协议中的布尔结论；不复制原始路径、指纹或后端异常到页面。
    card.append(title, state, facts);
    container.append(card);
  }
}

function stopRuntimePolling() {
  if (runtimePollTimer !== null) clearTimeout(runtimePollTimer);
  runtimePollTimer = null;
}

function scheduleRuntimePoll() {
  stopRuntimePolling();
  if (document.hidden) return;
  runtimePollTimer = setTimeout(() => {
    runtimePollTimer = null;
    refreshRuntimeStatus();
  }, Math.min(60000, 15000 * (2 ** runtimePollFailures)));
}

async function refreshRuntimeStatus() {
  // 自动查询与手动刷新复用在途 GET，避免累积请求；失败后不保留旧“就绪”结论。
  if (runtimeQuery) return runtimeQuery;
  stopRuntimePolling();
  $('refresh-runtime').disabled = true;
  const controller = new AbortController();
  // 本地连接挂起也必须及时撤下旧就绪结论，随后允许重新查询。
  const timeout = setTimeout(() => controller.abort(), 10000);
  runtimeQuery = (async () => {
    try {
      const status = await api('/runtime/status', { cache: 'no-store', signal: controller.signal });
      renderRuntimeStatus(status);
      runtimePollFailures = 0;
      runtimeCheckedAt = new Date().toLocaleTimeString('zh-CN', { hour12: false });
      $('runtime-checked').textContent = `最近检查：${runtimeCheckedAt}`;
    } catch (_) {
      runtimePollFailures += 1;
      renderRuntimeStatus(null);
      $('runtime-summary').textContent = '无法读取当前服务状态；请检查本地服务，页面将间隔重试。';
      $('runtime-summary').dataset.kind = 'error';
      $('runtime-checked').textContent = runtimeCheckedAt
        ? `上次成功检查：${runtimeCheckedAt}（已过期，当前状态未知）` : '尚无成功检查记录。';
    } finally {
      clearTimeout(timeout);
      runtimeQuery = null;
      $('refresh-runtime').disabled = false;
      scheduleRuntimePoll();
    }
  })();
  return runtimeQuery;
}

$('refresh-runtime').onclick = () => refreshRuntimeStatus();

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
  // 问答能力状态只读配置与预算，不会触发任何收费调用。
  try {
    const rag = await api('/rag/status');
    ragConfigured = rag.configured;
    if (!$('question').disabled) $('question').placeholder = '这份文档的核心结论是什么？';
    if (!ragConfigured && !embedding.configured) {
      $('question').placeholder = '请先配置检索与回答模型后重启服务';
    }
  } catch (error) {
    ragConfigured = false;
  }
  // 分析能力状态：只报告配置与预算，不调用任何收费接口。
  try {
    const analysis = await api('/analysis/status');
    analysisConfigured = analysis.configured;
    $('analysis-status').textContent = analysis.configured
      ? '分析能力已配置；点击“查看分析范围”先确认范围与请求上界。'
      : `尚未配置生成模型：${analysis.note}`;
  } catch (error) {
    analysisConfigured = false;
  }
  await refreshParsingStatus();
  const latestDocuments = await api('/documents');
  if (refreshToken !== requestSeq) return;
  documents = latestDocuments;
  renderDocuments();
  if (selectedId && documents.some((doc) => doc.id === selectedId)) {
    // 解析终态的后台刷新不能使当前提问的请求序号失效，也不能清空其等待状态。
    if (answerBusy) { await refreshIndex(); controls(); return; }
    await select(selectedId, true);
  }
}

// ---------------------------------------------------------------------------
// 分析任务：规划、提交、轮询、结果历史与导出
// ---------------------------------------------------------------------------
const analysisKindLabels = { extraction: '信息提取', summary: '文档摘要' };
const analysisStatusLabels = {
  queued: '已排队，等待分析 worker 领取', running: '正在执行', succeeded: '已完成',
  failed: '执行失败', cancelled: '已取消', needs_attention: '需要人工确认',
};
const analysisStageLabels = {
  queued: '排队中', planning: '规划输入', generating: '按批次生成与校验',
  reducing: '汇总中间结果', validating: '校验输出', saving: '保存结果', done: '已完成',
};

function stopAnalysisPolling() {
  if (analysisPollTimer !== null) {
    clearTimeout(analysisPollTimer);
    analysisPollTimer = null;
  }
}

function scheduleAnalysisPoll(delay) {
  // 与解析轮询同样只在任务处于活动状态时调度；终态立即停止。
  stopAnalysisPolling();
  analysisPollTimer = setTimeout(pollAnalysisJob, delay === undefined ? analysisPollDelay : delay);
}

const pendingAnalysisKeys = new Map();
function extractionOptions() {
  const lines = $('extraction-metrics').value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (lines.length > 12) throw new Error('需要核对的指标最多 12 项，请减少后重新查看范围。');
  const requiredMetrics = lines.map((line, index) => {
    const fields = line.split('|').map((field) => field.trim());
    if (fields.length > 4 || !fields[0]) {
      throw new Error(`第 ${index + 1} 行请按“指标名称 | 对象 | 期间 | 口径”填写，后面三项可留空。`);
    }
    return { name: fields[0], subject: fields[1] || null, period: fields[2] || null,
      scope: fields[3] || null };
  });
  const includeHighlights = $('extraction-highlights').checked;
  if (!requiredMetrics.length && !includeHighlights) {
    throw new Error('请填写需要核对的指标，或勾选“同时自动补充文档重点”。');
  }
  return { required_metrics: requiredMetrics, include_highlights: includeHighlights };
}

function extractionSettingsChanged() {
  // 设置变更后，旧范围与旧请求键不再代表本次操作；只读历史结果继续可查看。
  extractionSettingsRevision += 1;
  analysisKind = 'extraction';
  if (analysisPlan && analysisPlan.kind === 'extraction') {
    for (const regenerate of [false, true]) {
      const slot = pendingAnalysisSlot('extract', { ...analysisPlan, regenerate });
      pendingAnalysisKeys.delete(slot);
      try { sessionStorage.removeItem(slot); } catch (_) { /* 存储不可用时仍撤下旧计划。 */ }
    }
  }
  const prefix = `docqa-analysis:${selectedId}:extract:`;
  for (const slot of pendingAnalysisKeys.keys()) {
    if (!slot.startsWith(prefix)) continue;
    pendingAnalysisKeys.delete(slot);
    try { sessionStorage.removeItem(slot); } catch (_) { /* 只清理当前文档的提取请求键。 */ }
  }
  analysisPlan = null;
  $('analysis-plan-panel').replaceChildren();
  $('analysis-status').textContent = '提取设置已修改。请重新查看分析范围，再明确发起生成。';
}

function metricLabel(metric) {
  return [metric.name, metric.subject && `对象：${metric.subject}`,
    metric.period && `期间：${metric.period}`, metric.scope && `口径：${metric.scope}`]
    .filter(Boolean).join(' · ');
}

function extractionItemOrigin(extraction, item, matchedItemIds) {
  if (!extraction.metric_coverage) return null;
  if (matchedItemIds.has(item.item_id)) return '指定指标匹配项';
  return extraction.options?.include_highlights === false
    ? '其他提取项（未匹配指定指标）' : '自动补充重点';
}

function sameExtractionOptions(left, right) {
  if (!left || !right || left.include_highlights !== right.include_highlights) return false;
  const fields = ['name', 'subject', 'period', 'scope'];
  return left.required_metrics.length === right.required_metrics.length
    && left.required_metrics.every((metric, index) => fields.every((field) =>
      (metric[field] || null) === (right.required_metrics[index][field] || null)));
}

function pendingAnalysisSlot(action, body, documentId = selectedId) {
  return `docqa-analysis:${documentId}:${action}:${body.plan_fingerprint}:${Boolean(body.regenerate)}`;
}
function analysisPlanPayload(regenerate = false) {
  // 提交时固定解析版本与计划指纹：计划变化时后端会返回 409 并要求重新查看范围。
  if (!analysisPlan) return {};
  const body = {
    parse_version_id: analysisPlan.parse_version_id,
    plan_fingerprint: analysisPlan.plan_fingerprint,
    regenerate,
  };
  if (analysisPlan.kind === 'extraction') body.extraction_options = analysisPlan.extraction_options;
  const slot = pendingAnalysisSlot(analysisPlan.kind === 'summary' ? 'summary' : 'extract', body);
  let key = pendingAnalysisKeys.get(slot);
  try { key = key || sessionStorage.getItem(slot); } catch (_) { /* 禁用存储时保留本页幂等键。 */ }
  key = key || `ui-${crypto.randomUUID()}`;
  pendingAnalysisKeys.set(slot, key);
  try { sessionStorage.setItem(slot, key); } catch (_) { /* 存储不可用不触发重发。 */ }
  // 收到服务端确认前（包括断网/刷新），同一次操作始终重用原键。
  return { ...body, idempotency_key: key };
}

async function loadAnalysisPlan(kind) {
  // 只做本地输入规划：零外部调用；展示版本、范围、批次与请求数上界。
  analysisKind = kind || analysisKind;
  const plannedKind = analysisKind;
  const documentId = selectedId;
  const token = requestSeq;
  const settingsRevision = extractionSettingsRevision;
  const options = plannedKind === 'extraction' ? extractionOptions() : null;
  const plan = await api(`/documents/${documentId}/analysis-plan?kind=${plannedKind}`,
    { method: 'POST', ...(options ? { headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ extraction_options: options }) } : {}) });
  if (token !== requestSeq || documentId !== selectedId || plannedKind !== analysisKind
      || (options && settingsRevision !== extractionSettingsRevision)) return;
  analysisPlan = plan;
  renderAnalysisPlan(plan);
}

function renderAnalysisPlan(plan) {
  const container = $('analysis-plan-panel');
  container.replaceChildren();
  const head = document.createElement('p');
  head.className = 'muted';
  head.textContent = `${analysisKindLabels[plan.kind]} · 解析版本 ${plan.parse_version_id}`
    + (plan.is_active_version ? '（当前预览版本）' : '（历史解析版本）')
    + ` · Prompt ${plan.prompt_version}`;
  container.append(head);

  if (plan.kind === 'extraction' && plan.extraction_options) {
    const options = plan.extraction_options;
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = options.required_metrics.length
      ? `逐项核对 ${options.required_metrics.length} 个指标`
        + (options.include_highlights ? '，并自动补充文档重点。' : '。')
      : '自动挑选文档重点；本次没有指定逐项核对的指标。';
    container.append(note);
    if (options.required_metrics.length) {
      const metrics = document.createElement('ul');
      metrics.className = 'plan-batches';
      for (const metric of options.required_metrics) {
        const item = document.createElement('li');
        item.textContent = metricLabel(metric);
        metrics.append(item);
      }
      container.append(metrics);
    }
  }

  const limits = plan.limits || {};
  $('analysis-status').textContent =
    `可分析单元 ${plan.coverage.planned_units}/${plan.coverage.total_units}`
    + ` · 共 ${plan.coverage.batch_count} 个输入批次`
    + (plan.reduce_required ? ' · 需要 1 次汇总调用' : ' · 单批直接完成')
    + ` · 预计最多 ${plan.request_upper_bound} 次生成调用`
    + `（任务上限 ${limits.max_requests} 次）`
    + (plan.executable ? '' : ' · 本次不可执行');

  const list = document.createElement('ul');
  list.className = 'plan-batches';
  for (const batch of plan.batches) {
    const item = document.createElement('li');
    item.textContent = `批次 ${batch.order_index + 1}：${batch.unit_count} 个单元 · `
      + `${batch.chars} 字符 · 消息 ${batch.message_chars} 字符`;
    list.append(item);
  }
  container.append(list);

  if (plan.coverage.excluded_units) {
    const excluded = document.createElement('p');
    excluded.className = 'muted';
    const reasons = Object.entries(plan.coverage.excluded_reasons || {})
      .map(([reason, count]) => `${reason} ×${count}`).join('；');
    excluded.textContent = `未参与分析的非正文单元 ${plan.coverage.excluded_units} 个：${reasons}`;
    container.append(excluded);
  }
  const limitsNote = document.createElement('p');
  limitsNote.className = 'muted';
  limitsNote.textContent = `预算口径：单批 ${limits.batch_max_chars} 字符 · 汇总 ${limits.reduce_max_chars} 字符`
    + ` · 输入上限 ${limits.input_max_chars} 字符 · 全文上限 ${limits.max_document_chars} 字符`
    + '（字符不是 token）';
  container.append(limitsNote);
  for (const text of plan.limitations || []) {
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = `说明：${text}`;
    container.append(note);
  }
}

function renderAnalysisJob(job) {
  // 任务状态、阶段与预算；失败或待确认时明确说明旧结果仍然可读。
  analysisJobState = job || null;
  const container = $('analysis-job');
  container.replaceChildren();
  if (!job) return;
  const line = document.createElement('p');
  line.textContent = `${analysisKindLabels[job.kind] || job.kind} · `
    + `${analysisStatusLabels[job.status] || job.status} · `
    + `${analysisStageLabels[job.stage] || job.stage}`
    + ` · 已用 ${job.requests_used}/${job.max_requests} 次生成调用`
    + ` · 已完成步骤 ${job.steps_completed}/${job.steps_total}`;
  container.append(line);
  if (job.error_message) {
    const error = document.createElement('p');
    error.className = job.status === 'succeeded' ? 'muted' : 'error';
    error.textContent = `${job.error_code || ''} · ${job.error_message}`;
    container.append(error);
  }
  const coverage = job.coverage;
  if (coverage) {
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = `覆盖：已处理 ${coverage.processed_units}/${coverage.total_units} 个输入单元`
      + ` · 完成批次 ${coverage.batch_completed}/${coverage.batch_total}`
      + (coverage.reduce_completed ? ' · 已执行汇总' : '')
      + (coverage.complete ? ' · 输入处理完整' : ' · 输入处理不完整（未处理内容不会被包装成全文结果）');
    container.append(note);
  }
  if (job.status === 'needs_attention') {
    const note = document.createElement('p');
    note.className = 'error';
    note.textContent = '存在未确认的生成调用：系统不会自动重发；请核实后明确重试，重试可能重复计费。';
    container.append(note);
  }
  controls();
}

async function pollAnalysisJob() {
  // 轮询只查询状态；迟到的响应不得覆盖新选中的文档。
  if (!analysisJobId || !selectedId) { stopAnalysisPolling(); return; }
  const jobId = analysisJobId;
  const documentId = selectedId;
  const token = requestSeq;
  try {
    const job = await api(`/analysis-jobs/${jobId}`);
    if (token !== requestSeq || documentId !== selectedId || jobId !== analysisJobId) return;
    analysisPollFailures = 0;
    analysisPollDelay = 2500;
    renderAnalysisJob(job);
    if (['queued', 'running'].includes(job.status)) {
      scheduleAnalysisPoll();
      return;
    }
    stopAnalysisPolling();
    await loadAnalysisResults();
  } catch (error) {
    if (token !== requestSeq || documentId !== selectedId || jobId !== analysisJobId) return;
    analysisPollFailures += 1;
    if (analysisPollFailures > 6) {
      stopAnalysisPolling();
      $('connection-status').textContent =
        '多次无法读取分析任务状态，已停止自动刷新；请点击“刷新”恢复。';
      return;
    }
    analysisPollDelay = Math.min(15000, analysisPollDelay * 2);
    scheduleAnalysisPoll();
  }
}

async function loadAnalysisResults() {
  // 结果历史只读；不会重新生成，也不会清空当前正在查看的结果。
  const documentId = selectedId;
  const token = requestSeq;
  const results = await api(`/documents/${documentId}/analysis-results`);
  if (token !== requestSeq || documentId !== selectedId) return;
  analysisResults = results;
  renderAnalysisResultList();
}

function renderAnalysisResultList() {
  const container = $('analysis-results');
  container.replaceChildren();
  const viewedSummary = activeResultPayload && activeResultPayload.document_id === selectedId
    ? analysisResults.find((item) => item.id === activeResultPayload.id) : null;
  if (activeResultPayload && !viewedSummary) {
    activeResultPayload = null;
    activeResultId = null;
  }
  if (!analysisResults.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '该文档还没有持久化的分析结果。';
    container.append(empty);
    return;
  }
  const title = document.createElement('h4');
  title.textContent = '历史结果';
  container.append(title);
  for (const item of analysisResults) {
    const row = document.createElement('div');
    row.className = 'result-row';
    const label = document.createElement('span');
    label.textContent = `${analysisKindLabels[item.kind] || item.kind} · `
      + `${item.created_at.slice(0, 19).replace('T', ' ')} · 版本 ${item.parse_version_id}`
      + (item.is_active_version ? '（当前预览版本）' : '（历史解析版本）')
      + (item.complete ? ' · 输入处理完整' : ' · 输入处理不完整')
      + ` · ${item.requests_used} 次调用`;
    row.append(label);
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'subtle';
    open.textContent = '查看结果';
    // 查看已存结果不会重新生成，也不弹出计费步骤。
    open.onclick = () => run(() => openResult(item.id));
    row.append(open);
    container.append(row);
  }
  if (viewedSummary) {
    // 正文不可变，但“当前／历史版本”标记随预览版本变化，从最新历史列表更新。
    activeResultPayload = { ...activeResultPayload, is_active_version: viewedSummary.is_active_version };
    renderAnalysisResult(activeResultPayload, true);
  }
}

async function openResult(resultId) {
  const documentId = selectedId;
  const token = requestSeq;
  const result = await api(`/analysis-results/${resultId}`);
  if (token !== requestSeq || documentId !== selectedId) return;
  activeResultId = result.id;
  activeResultPayload = result;
  lastResultKind = result.kind;
  analysisKind = result.kind;
  renderAnalysisResult(result);
}

function renderAnalysisResult(result, append = false) {
  // 由已校验结构渲染；全部使用 textContent，模型或文件名里的 HTML 只作为文字。
  const container = $('analysis-results');
  const panel = document.createElement('section');
  panel.className = 'analysis-result';

  const head = document.createElement('h4');
  head.textContent = `${analysisKindLabels[result.kind] || result.kind}结果 · ${result.created_at.slice(0, 19).replace('T', ' ')}`;
  panel.append(head);

  const meta = document.createElement('p');
  meta.className = 'muted';
  meta.textContent = `解析版本 ${result.parse_version_id}`
    + (result.is_active_version ? '（当前预览版本）' : '（历史解析版本）')
    + ` · Prompt ${result.prompt_version} · 协议 ${result.protocol_version}`
    + ` · 本次生成调用 ${result.requests_used} 次`
    + (result.parse_parser_name ? ` · 解析器 ${result.parse_parser_name}` : '');
  panel.append(meta);

  const coverage = result.coverage || {};
  const coverageNote = document.createElement('p');
  coverageNote.className = 'muted';
  coverageNote.textContent = `覆盖：已处理 ${coverage.processed_units}/${coverage.total_units} 个输入单元`
    + ` · 完成批次 ${coverage.batch_completed}/${coverage.batch_total}`
    + (coverage.reduce_completed ? ' · 已执行汇总' : '')
    + (coverage.complete ? ' · 本次已处理该解析版本的完整合格输入' : ' · 输入处理不完整')
    + (coverage.excluded_units ? ` · 另有 ${coverage.excluded_units} 个非正文单元未参与分析` : '')
    + ` · 最终引用回落原文 ${coverage.resolved_original_refs} 条`
    + (result.extraction ? '。输入处理完整不表示所有事实均已提取。' : '');
  panel.append(coverageNote);

  if (result.summary) {
    const topic = document.createElement('div');
    const topicTitle = document.createElement('strong');
    topicTitle.textContent = '主题概述';
    const topicText = document.createElement('p');
    topicText.textContent = result.summary.topic_overview || '（无）';
    topic.append(topicTitle, topicText);
    panel.append(topic);
    panel.append(renderPointSection('主要内容与结论', result.summary.main_points));
    if (result.summary.exceptions.length) {
      panel.append(renderPointSection('例外、冲突与限制', result.summary.exceptions));
    } else {
      const note = document.createElement('p');
      note.className = 'muted';
      note.textContent = '本次结果没有单列的例外条目。';
      panel.append(note);
    }
  }
  if (result.extraction) {
    if (result.extraction.metric_coverage) {
      panel.append(renderMetricCoverage(result.extraction.metric_coverage));
    }
    const matchedItemIds = new Set((result.extraction.metric_coverage?.metrics || [])
      .filter((metric) => metric.status === 'matched').flatMap((metric) => metric.item_ids || []));
    const labels = { data: '数据', conclusion: '结论', viewpoint: '观点' };
    for (const kind of ['data', 'conclusion', 'viewpoint']) {
      const group = result.extraction.items.filter((item) => item.kind === kind);
      const section = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = labels[kind];
      section.append(title);
      if (!group.length) {
        const note = document.createElement('p');
        note.className = 'muted';
        const status = result.extraction.sections[kind];
        note.textContent = status === 'none'
          ? '已覆盖本次输入，但没有该类可提取内容（不是模型失败）。'
          : '该类内容未处理。';
        section.append(note);
      }
      for (const item of group) {
        section.append(renderExtractionItem(item,
          extractionItemOrigin(result.extraction, item, matchedItemIds)));
      }
      panel.append(section);
    }
  }

  if (result.limitations && result.limitations.length) {
    const title = document.createElement('strong');
    title.textContent = '适用边界与限制';
    const list = document.createElement('ul');
    for (const text of result.limitations) {
      const item = document.createElement('li');
      item.textContent = text;
      list.append(item);
    }
    panel.append(title, list);
  }
  if (result.quality_warnings && result.quality_warnings.length) {
    const title = document.createElement('strong');
    title.textContent = '解析质量提示（继续生效）';
    const list = document.createElement('ul');
    for (const warning of result.quality_warnings) {
      const item = document.createElement('li');
      item.textContent = `${warning.severity === 'error' ? '错误' : warning.severity === 'warning' ? '告警' : '提示'}`
        + ` · ${warning.code} · ${qualityWarningMessage(warning)}`;
      list.append(item);
    }
    panel.append(title, list);
  }

  panel.append(renderResultCitations(result));

  const actions = document.createElement('div');
  actions.className = 'actions';
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'subtle';
  copy.textContent = '复制可读内容';
  // 复制内容由同一份已校验结构生成，不包含原始模型响应或模型思考。
  copy.onclick = () => run(async () => {
    const text = resultToText(result);
    try {
      await navigator.clipboard.writeText(text);
      setMessage('已复制结果文本（含引用与限制）。', 'ok');
    } catch (error) {
      setMessage(`浏览器拒绝写入剪贴板：${error.message}；可改用导出文件。`, 'error');
    }
  });
  actions.append(copy);
  for (const [format, label] of [['markdown', '导出 Markdown'], ['json', '导出 JSON']]) {
    const link = document.createElement('a');
    link.href = `/api/analysis-results/${encodeURIComponent(result.id)}/export?format=${format}`;
    link.textContent = label;
    actions.append(link);
  }
  panel.append(actions);
  if (append) container.append(panel);
  else container.replaceChildren(panel);
}

function renderMetricCoverage(coverage) {
  const section = document.createElement('div');
  section.className = 'metric-coverage';
  section.dataset.status = coverage.status;
  const title = document.createElement('strong');
  title.textContent = '指标匹配核对（不代表全部语义已验收）';
  const note = document.createElement('p');
  note.className = 'muted';
  note.textContent = coverage.status === 'not_requested'
    ? '本次未指定指标，展示自动挑选的重点；不承诺穷举所有事实。'
    : `逐字字段匹配 ${coverage.matched_count}/${coverage.required_count} 项`
      + (coverage.status === 'complete' ? '，指定指标均有匹配项。' : '，尚有指标未确认。')
      + ' 每项只核对所填写的条件；未填写的对象、期间或口径不表示所有记录都已提取。';
  section.append(title, note);
  const list = document.createElement('ul');
  for (const metric of coverage.metrics || []) {
    const item = document.createElement('li');
    item.textContent = `${metric.status === 'matched' ? '已匹配' : '未确认'} · ${metricLabel(metric)}`
      + (metric.reason ? ` · ${metric.reason}` : '');
    list.append(item);
  }
  if (list.children.length) section.append(list);
  return section;
}

function renderExtractionItem(item, origin = null) {
  const row = document.createElement('div');
  row.className = 'extraction-item';
  if (origin) {
    const label = document.createElement('small');
    label.className = 'extraction-origin';
    label.textContent = origin;
    row.append(label);
  }
  const content = document.createElement('p');
  const text = document.createElement('span');
  text.textContent = item.content;
  content.append(text);
  appendRefButtons(content, item.refs, 'result-citation');
  row.append(content);
  const details = [];
  if (item.name) details.push(`名称：${item.name}`);
  if (item.value_text) details.push(`数值：${item.value_text}`);
  if (item.unit) details.push(`单位：${item.unit}`);
  if (item.period) details.push(`时间：${item.period}`);
  if (item.subject) details.push(`主体：${item.subject}`);
  if (item.scope) details.push(`口径/条件：${item.scope}`);
  if (details.length) {
    const note = document.createElement('small');
    note.className = 'muted';
    note.textContent = details.join(' · ');
    row.append(note);
  }
  return row;
}

function renderPointSection(title, points) {
  const section = document.createElement('div');
  const head = document.createElement('strong');
  head.textContent = title;
  section.append(head);
  const list = document.createElement('ul');
  for (const point of points) {
    const item = document.createElement('li');
    const text = document.createElement('span');
    text.textContent = point.text;
    item.append(text);
    appendRefButtons(item, point.refs, 'result-citation');
    list.append(item);
  }
  section.append(list);
  return section;
}

function renderResultCitations(result) {
  const container = document.createElement('div');
  container.className = 'citations';
  const citations = (result.summary && result.summary.citations)
    || (result.extraction && result.extraction.citations) || [];
  if (!citations.length) return container;
  const title = document.createElement('h4');
  title.textContent = '引用与来源（回到原文）';
  container.append(title);
  for (const citation of citations) {
    const card = document.createElement('article');
    card.className = 'citation';
    card.id = `result-citation-${citation.reference_id}`;
    const head = document.createElement('div');
    const label = document.createElement('strong');
    label.textContent = `[${citation.reference_id}]`;
    const location = document.createElement('span');
    location.className = 'muted';
    location.textContent = (citation.sources || []).map(sourceText).join(' / ')
      || '来源定位信息有限';
    head.append(label, document.createTextNode(' '), location);
    const quote = document.createElement('blockquote');
    quote.textContent = citation.quote;
    card.append(head, quote);
    const info = document.createElement('p');
    info.className = 'muted';
    info.textContent = `解析版本 ${result.parse_version_id}`
      + (citation.block_type ? ` · 块类型 ${citation.block_type}` : '')
      + (citation.char_start !== null && citation.char_start !== undefined
        ? ` · 引述位于该单元第 ${citation.char_start}-${citation.char_end} 字符` : '')
      + ' · 仅声明连续子串位置，不声称已实现字符级高亮';
    card.append(info);
    const actions = document.createElement('div');
    actions.className = 'actions';
    const preview = document.createElement('button');
    preview.type = 'button';
    preview.className = 'subtle';
    preview.textContent = '查看引用版本原文';
    preview.onclick = () => run(() => loadContent(result.document_id, result.parse_version_id));
    actions.append(preview);
    const pages = (citation.sources || [])
      .filter((source) => source.format === 'pdf' && Number.isInteger(source.page) && source.page > 0)
      .map((source) => source.page);
    const link = document.createElement('a');
    link.href = `/api/documents/${encodeURIComponent(result.document_id)}/original`
      + (pages.length ? `#page=${pages[0]}` : '');
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = pages.length ? `查看原件第 ${pages[0]} 页` : '下载原件核对';
    actions.append(link);
    card.append(actions);
    container.append(card);
  }
  return container;
}

function resultToText(result) {
  // 复制用文本：只含已校验结构与限制，便于粘贴到文档或邮件中核对。
  const lines = [];
  lines.push(`${analysisKindLabels[result.kind] || result.kind}（解析版本 ${result.parse_version_id}，`
    + `生成时间 ${result.created_at}）`);
  if (result.coverage) {
    lines.push(`输入处理：${result.coverage.processed_units}/${result.coverage.total_units} 个单元`
      + (result.coverage.complete ? '，输入处理完整。' : '，输入处理不完整。')
      + (result.extraction ? '不表示所有事实均已提取。' : ''));
  }
  if (result.summary) {
    lines.push(`主题概述：${result.summary.topic_overview}`);
    lines.push('主要内容与结论：');
    for (const point of result.summary.main_points) {
      lines.push(`- ${point.text}${point.refs.map((ref) => `[${ref}]`).join('')}`);
    }
    if (result.summary.exceptions.length) {
      lines.push('例外、冲突与限制：');
      for (const point of result.summary.exceptions) {
        lines.push(`- ${point.text}${point.refs.map((ref) => `[${ref}]`).join('')}`);
      }
    }
  }
  if (result.extraction) {
    const metricCoverage = result.extraction.metric_coverage;
    const matchedItemIds = new Set((metricCoverage?.metrics || [])
      .filter((metric) => metric.status === 'matched').flatMap((metric) => metric.item_ids || []));
    if (metricCoverage) {
      lines.push('指标匹配核对（不代表全部语义已验收）：');
      lines.push(metricCoverage.status === 'not_requested'
        ? '未指定指标，自动挑选重点；不承诺穷举所有事实。'
        : `逐字字段匹配 ${metricCoverage.matched_count}/${metricCoverage.required_count} 项；`
          + '仅核对已填写条件，未填写对象、期间或口径不表示穷举全部记录。');
      for (const metric of metricCoverage.metrics || []) {
        lines.push(`- ${metric.status === 'matched' ? '已匹配' : '未确认'} · ${metricLabel(metric)}`
          + (metric.reason ? ` · ${metric.reason}` : ''));
      }
    }
    const labels = { data: '数据', conclusion: '结论', viewpoint: '观点' };
    for (const kind of ['data', 'conclusion', 'viewpoint']) {
      lines.push(`${labels[kind]}：`);
      const group = result.extraction.items.filter((item) => item.kind === kind);
      if (!group.length) {
        lines.push(result.extraction.sections[kind] === 'none'
          ? '- （已覆盖输入，但没有该类可提取内容）' : '- （未处理）');
        continue;
      }
      for (const item of group) {
        const originLabel = extractionItemOrigin(result.extraction, item, matchedItemIds);
        const origin = originLabel ? `【${originLabel}】` : '';
        lines.push(`- ${origin}${item.content}${item.refs.map((ref) => `[${ref}]`).join('')}`);
      }
    }
  }
  const citations = (result.summary && result.summary.citations)
    || (result.extraction && result.extraction.citations) || [];
  if (citations.length) {
    lines.push('引用：');
    for (const citation of citations) {
      lines.push(`[${citation.reference_id}] ${citation.quote}`);
    }
  }
  if (result.limitations.length) {
    lines.push('适用边界：');
    for (const text of result.limitations) lines.push(`- ${text}`);
  }
  return lines.join('\n');
}

async function refreshParsingStatus() {  // 解析服务可达性单独探测：可达不代表模型已预热或内容质量合格。
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

// ---------------------------------------------------------------------------
// 单文档 RAG 问答：安全渲染、引用卡片、状态与竞态处理
// ---------------------------------------------------------------------------
const answerStatusLabels = {
  answered: '已根据引用片段回答',
  partial: '已部分回答，请核对尚缺少的依据',
  clarification_needed: '需要补充条件后才能回答',
  insufficient_evidence: '当前片段依据不足',
};

function resetAnswer() {
  // 切换文档或开始新提问时清理旧结果，绝不让上一份文档的答案残留。
  answerPayload = null;
  $('answer-body').replaceChildren();
  $('answer-questions').replaceChildren();
  $('answer-warnings').replaceChildren();
  $('answer-limitations').replaceChildren();
  $('answer-citations').replaceChildren();
  $('answer-actions').replaceChildren();
  $('answer-status').textContent = '等待提问';
  $('answer-meta').textContent = '';
  $('answer-panel').hidden = true;
  $('answer-waiting').hidden = true;
}

function stopAnswerTimer() {
  if (answerWaitTimer !== null) {
    clearInterval(answerWaitTimer);
    answerWaitTimer = null;
  }
}

function startAnswerTimer() {
  // 只显示真实等待时间；非流式回答没有可展示的进度百分比。
  stopAnswerTimer();
  answerStartedAt = Date.now();
  $('answer-waiting').hidden = false;
  $('answer-waiting').textContent = '正在检索资料并生成回答…已等待 0 秒';
  answerWaitTimer = setInterval(() => {
    const seconds = Math.floor((Date.now() - answerStartedAt) / 1000);
    $('answer-waiting').textContent = `正在检索资料并生成回答…已等待 ${seconds} 秒`;
  }, 1000);
}

// 有限 Markdown 渲染：只识别标题、无序/有序列表与 **加粗**，其余一律作为纯文本。
// 不使用 innerHTML，因此模型或资料里的 <script>、<img onerror>、javascript: 只能以文字出现。
// 行尾的 [n] / [n][m] 是后端按已校验 refs 生成的引用标记，必须渲染成可点击按钮。
const REF_MARKER = /(?:\[(\d+)\])+\s*$/;

function splitRefs(text) {
  // 拆出结尾的引用标记；正文中其他位置的方括号一律按文字保留。
  const match = REF_MARKER.exec(text);
  if (!match) return { text: text, refs: [] };
  const refs = [...match[0].matchAll(/\[(\d+)\]/g)].map((item) => Number(item[1]));
  return { text: text.slice(0, match.index).replace(/\s+$/, ''), refs: refs };
}

function appendRefButtons(container, refs, prefix = 'citation') {
  for (const ref of refs) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ref';
    button.textContent = `[${ref}]`;
    button.title = `查看第 ${ref} 条引用`;
    button.onclick = () => focusCitation(ref, prefix);
    container.append(button);
  }
}

function appendInline(container, text) {
  // 行内渲染：先分离引用标记，再在正文部分识别 **加粗**。
  const parts = splitRefs(text);
  const pattern = /\*\*([^*]+)\*\*/g;
  let cursor = 0;
  let match = pattern.exec(parts.text);
  while (match !== null) {
    if (match.index > cursor) {
      container.append(document.createTextNode(parts.text.slice(cursor, match.index)));
    }
    const strong = document.createElement('strong');
    strong.textContent = match[1];
    container.append(strong);
    cursor = match.index + match[0].length;
    match = pattern.exec(parts.text);
  }
  if (cursor < parts.text.length) {
    container.append(document.createTextNode(parts.text.slice(cursor)));
  }
  appendRefButtons(container, parts.refs);
}

function appendFact(container, fact) {
  // 事实后紧跟引用标记按钮，编号来自后端已校验的 refs（不是模型自写文本）。
  const item = document.createElement('li');
  appendInline(item, fact.text);
  appendRefButtons(item, fact.refs);
  container.append(item);
}

function renderAnswerMarkdown(markdown) {
  // 逐行解析受限 Markdown；不支持的语法按原文显示，不做任何 HTML 解释。
  const body = $('answer-body');
  body.replaceChildren();
  const lines = String(markdown || '').replace(/\r\n/g, '\n').split('\n');
  let list = null;
  let listKind = null;
  const flush = () => { list = null; listKind = null; };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (!line.trim()) { flush(); continue; }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    const bullet = /^[-*]\s+(.*)$/.exec(line);
    const ordered = /^(\d+)[.、]\s*(.*)$/.exec(line);
    if (heading) {
      flush();
      const element = document.createElement('h4');
      appendInline(element, heading[2]);
      body.append(element);
    } else if (bullet) {
      if (listKind !== 'ul') { list = document.createElement('ul'); listKind = 'ul'; body.append(list); }
      const item = document.createElement('li');
      appendInline(item, bullet[1]);
      list.append(item);
    } else if (ordered) {
      if (listKind !== 'ol') { list = document.createElement('ol'); listKind = 'ol'; body.append(list); }
      const item = document.createElement('li');
      appendInline(item, ordered[2]);
      list.append(item);
    } else {
      flush();
      const paragraph = document.createElement('p');
      appendInline(paragraph, line);
      body.append(paragraph);
    }
  }
}

function sourceText(source) {
  // 来源标签由后端契约生成（含单位与“无真实页码”说明），前端不自行编造页码。
  const parts = [];
  if (source.format === 'pdf' && source.page) parts.push(`第 ${source.page} 页`);
  if (source.format === 'pdf' && source.bbox) parts.push('含坐标框');
  if (source.format === 'docx') {
    if (source.section_path) parts.push(`章节 ${source.section_path}`);
    if (source.table_no) parts.push(`表格 ${source.table_no}`);
    parts.push('DOCX 无真实页码');
  }
  if (source.format === 'xlsx') {
    parts.push(source.sheet_name ? `工作表 ${source.sheet_name}` : '工作表未知');
    if (source.cell_range) parts.push(`单元格 ${source.cell_range}`);
  }
  if (source.format === 'txt') {
    parts.push(source.page ? `逻辑页 ${source.page}` : '逻辑页未知');
    if (source.line_start) parts.push(`第 ${source.line_start}-${source.line_end} 行`);
  }
  if (!parts.length) parts.push('来源定位信息有限');
  return parts.join(' · ');
}

function renderCitations(citations, meta) {
  const container = $('answer-citations');
  container.replaceChildren();
  if (!citations.length) return;
  const title = document.createElement('h4');
  title.textContent = '引用与来源';
  container.append(title);
  for (const citation of citations) {
    const card = document.createElement('article');
    card.className = 'citation';
    card.id = `citation-${citation.reference_id}`;
    const head = document.createElement('div');
    const label = document.createElement('strong');
    label.textContent = `[${citation.reference_id}]`;
    head.append(label);
    const location = document.createElement('span');
    location.className = 'muted';
    location.textContent = (citation.sources || []).map(sourceText).join(' / ') || '来源定位信息有限';
    head.append(document.createTextNode(' '), location);
    const quote = document.createElement('blockquote');
    quote.textContent = citation.quote;
    card.append(head, quote);
    const structure = citation.table_structure;
    if (structure && structure.version === 'table-coordinate-evidence-v1' && structure.status === 'available') {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = '查看表格行列结构';
      const note = document.createElement('p');
      note.className = 'muted';
      note.textContent = '以下是模型收到的整块行列，可能超出本条引述范围。空白不等于零；合并覆盖格另作标记。期间、单位和脚注范围仍须核对原文。';
      const grid = document.createElement('pre');
      const covered = new Set((structure.covered_cells || []).map((cell) => `${cell[0]},${cell[1]}`));
      grid.textContent = (structure.rows || []).slice(0, 400).map((row) =>
        `表内第 ${row.row} 行：${(row.values || []).map((value, col) => covered.has(`${row.row},${col + 1}`)
          ? '↳（合并覆盖）' : value === '' ? '（空）' : String(value)).join(' | ')}`
      ).join('\n');
      details.append(summary, note, grid);
      for (const formula of (structure.formulas || []).slice(0, 400)) {
        const line = document.createElement('p');
        line.textContent = `${formula.cell}：${formula.cache_status === 'missing' ? '公式缓存缺失（不能当作0）'
          : `文件缓存值 ${formula.cached_value}（未验证公式正确性）`}`;
        details.append(line);
      }
      for (const footnote of (structure.footnotes || []).slice(0, 400)) {
        const line = document.createElement('p');
        line.textContent = `表格注释（适用范围未确认）：${footnote.text}`;
        details.append(line);
      }
      card.append(details);
    }

    const info = document.createElement('p');
    info.className = 'muted';
    info.textContent = `解析版本 ${citation.parse_version_id || '未知'}`
      + ` · 片段 ${citation.chunk_id}`
      + (meta && meta.is_old_version ? ' · 本次回答依据历史解析版本' : '');
    card.append(info);
    if (citation.evidence_id) {
      const identity = document.createElement('p');
      identity.className = 'muted';
      identity.textContent = `证据ID ${citation.evidence_id}`
        + ` · 解析原文范围 [${citation.char_start}, ${citation.char_end})`
        + ' · 原文精确匹配；语义支持待复核。页码/坐标为分块来源区域。';
      card.append(identity);
    }

    const actions = document.createElement('div');
    actions.className = 'actions';
    // 版本级预览入口：已有接口只支持版本级预览，不声称已精确高亮对应字符。
    const preview = document.createElement('button');
    preview.type = 'button';
    preview.className = 'subtle';
    preview.textContent = '查看引用版本';
    preview.onclick = () => run(() => loadContent(citation.document_id, citation.parse_version_id));
    actions.append(preview);
    if (citation.verification === 'exact_match' && citation.offset_basis === 'chunk_crlf_lf_unicode_codepoints') {
      const excerpt = document.createElement('button');
      excerpt.type = 'button';
      excerpt.className = 'subtle';
      excerpt.textContent = '展开并高亮解析原文';
      const region = document.createElement('pre');
      region.hidden = true;
      excerpt.onclick = () => run(async () => {
        if (region.dataset.loaded === 'true') { region.hidden = !region.hidden; return; }
        const chunks = await api(`/documents/${encodeURIComponent(citation.document_id)}/chunks`
          + `?version_id=${encodeURIComponent(citation.parse_version_id)}`);
        const chunk = chunks.find((item) => item.id === citation.chunk_id);
        if (!chunk || chunk.document_id !== citation.document_id || chunk.parse_version_id !== citation.parse_version_id) {
          throw new Error('对应版本的原文片段不可用');
        }
        // 后端偏移为Unicode码点；不能直接用JS的UTF-16下标，否则emoji后会错位。
        const chars = Array.from(String(chunk.text).replace(/\r\n/g, '\n'));
        const start = citation.char_start, end = citation.char_end;
        if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > chars.length
          || chars.slice(start, end).join('') !== citation.quote) {
          throw new Error('引述范围与原文不一致，未显示高亮');
        }
        const mark = document.createElement('mark');
        mark.textContent = citation.quote;
        region.replaceChildren(document.createTextNode(chars.slice(0, start).join('')), mark,
          document.createTextNode(chars.slice(end).join('')));
        region.dataset.loaded = 'true';
        region.hidden = false;
      });
      actions.append(excerpt);
      card.append(region);
    }
    const pages = (citation.sources || [])
      .filter((source) => source.format === 'pdf' && Number.isInteger(source.page) && source.page > 0)
      .map((source) => source.page);
    const link = document.createElement('a');
    link.href = `/api/documents/${encodeURIComponent(citation.document_id)}/original`
      + (pages.length ? `#page=${pages[0]}` : '');
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = pages.length ? `查看原件第 ${pages[0]} 页` : '下载原件核对';
    actions.append(link);
    card.append(actions);
    container.append(card);
  }
}

function focusCitation(ref, prefix = 'citation') {
  // 点击 [n] 定位到对应引用卡片；不改变任何后端状态。
  const card = document.getElementById(`${prefix}-${ref}`);
  if (!card) return;
  card.scrollIntoView({ block: 'center' });
  card.classList.add('citation-active');
  setTimeout(() => card.classList.remove('citation-active'), 1200);
}

function renderAnswer(payload) {
  // 渲染已校验的响应结构；不使用 JSON.stringify 代替产品展示。
  answerPayload = payload;
  $('answer-panel').hidden = false;
  $('answer-waiting').hidden = true;
  $('answer-status').textContent = answerStatusLabels[payload.status] || payload.status;
  const retrieval = payload.retrieval || {};
  const timings = payload.timings_ms || {};
  $('answer-meta').textContent =
    `解析版本 ${payload.parse_version_id || '未知'} · 索引 ${payload.index_id || '未知'}`
    + ` · 候选 ${retrieval.candidate_count} / 送入模型 ${retrieval.selected_count} 段`
    + (retrieval.truncated ? ' · 证据集合不完整（受字符预算限制）' : '')
    + ` · 检索 ${timings.retrieval} ms · 生成 ${timings.generation} ms · 合计 ${timings.total} ms`
    + ` · Prompt ${payload.prompt_version}`;

  renderAnswerMarkdown(payload.answer);
  if (Array.isArray(payload.answer_parts) && payload.answer_parts.length) {
    const title = document.createElement('h4');
    title.textContent = '问题覆盖';
    const list = document.createElement('ul');
    const labels = { answered: '已回答', partial: '部分回答', missing_evidence: '缺少依据', needs_clarification: '需要澄清' };
    for (const part of payload.answer_parts) {
      const item = document.createElement('li');
      item.textContent = `${labels[part.state] || '待核对'}：${part.text}`
        + (part.missing_detail ? `（${part.missing_detail}）` : '');
      list.append(item);
    }
    $('answer-body').append(title, list);
  }

  const questions = $('answer-questions');
  questions.replaceChildren();
  if (payload.clarification_questions && payload.clarification_questions.length) {
    const title = document.createElement('h4');
    title.textContent = '需要您补充的条件';
    const hint = document.createElement('p');
    hint.className = 'muted';
    // 历史用于重开核对，每轮仍须提交完整问题，不隐式拼接上下文。
    hint.textContent = '请把条件补进一个完整问题后重新提问，'
      + '例如“原问题 + 补充条件”；上一轮的回答不会被当作事实再次发送。';
    const list = document.createElement('ol');
    for (const question of payload.clarification_questions) {
      const item = document.createElement('li');
      item.textContent = question;
      list.append(item);
    }
    const template = document.createElement('button');
    template.type = 'button';
    template.className = 'subtle';
    template.textContent = '填入“原问题 + 补充信息”模板';
    template.onclick = () => {
      $('question').value = `${$('question').value.trim()}（补充信息：${payload.clarification_questions.join('；')}）`;
      $('question').focus();
    };
    questions.append(title, hint, list, template);
  }

  const warnings = $('answer-warnings');
  warnings.replaceChildren();
  if (payload.quality_warnings && payload.quality_warnings.length) {
    const title = document.createElement('h4');
    title.textContent = '本次回答的质量提示';
    warnings.append(title);
    for (const warning of payload.quality_warnings) {
      const item = document.createElement('div');
      item.className = `warning warning-${warning.severity}`;
      const head = document.createElement('strong');
      head.textContent = `${warning.severity === 'error' ? '错误' : warning.severity === 'warning' ? '告警' : '提示'} · ${warning.code}`;
      const text = document.createElement('p');
      text.textContent = qualityWarningMessage(warning)
        + (warning.page ? `（第 ${warning.page} 页）` : '')
        + (warning.sheet_name ? `（工作表 ${warning.sheet_name}）` : '')
        + (warning.detail ? ` · ${warning.detail}` : '');
      item.append(head, text);
      warnings.append(item);
    }
  }

  const limitations = $('answer-limitations');
  limitations.replaceChildren();
  if (payload.limitations && payload.limitations.length) {
    const title = document.createElement('h4');
    title.textContent = '本次回答的适用边界';
    const list = document.createElement('ul');
    for (const text of payload.limitations) {
      const item = document.createElement('li');
      item.textContent = text;
      list.append(item);
    }
    limitations.append(title, list);
  }

  renderCitations(payload.citations || [], payload);
}

const questionStatusLabels = {
  queued: '已排队，等待 worker 领取', running: '正在执行', succeeded: '已完成',
  failed: '执行失败', needs_attention: '需要确认', cancelled: '已取消',
  cancel_requested: '已请求取消，等待当前阶段结束',
};
const questionStageLabels = {
  queued: '排队中', preparing: '准备问答', retrieving: '检索证据', retrieval: '检索证据',
  evidence_ready: '证据已固定',
  generating: '生成回答', validating: '校验回答与引用', saving: '保存结果', done: '已完成',
  cancelled: '已取消', failed: '已停止',
};

function questionSessionKey(documentId) { return `docqa.question.${documentId}`; }
function readQuestionSession(documentId) {
  if (!documentId) return null;
  try {
    const value = JSON.parse(sessionStorage.getItem(questionSessionKey(documentId)) || 'null');
    return value && typeof value === 'object' && typeof value.idempotency_key === 'string'
      && typeof value.question === 'string' ? value : null;
  } catch (error) { return null; }
}
function writeQuestionSession(documentId, value) {
  // 在提交前保存原问题与请求键；服务端结果不明时刷新也不能偷偷换键重发。
  try { sessionStorage.setItem(questionSessionKey(documentId), JSON.stringify(value)); }
  catch (error) {
    throw new Error('浏览器无法保存问答恢复记录，请允许本页使用会话存储后再提交。');
  }
}
function stopQuestionPolling() {
  if (questionPollTimer !== null) clearTimeout(questionPollTimer);
  questionPollTimer = null;
}
function scheduleQuestionPoll(delay = questionPollDelay) {
  stopQuestionPolling();
  if (!document.hidden) questionPollTimer = setTimeout(pollQuestionJob, delay);
}
async function questionApi(path, options = {}) {
  // GET 与短提交连接都设超时；超时仅中断网页连接，不代表服务端任务已取消。
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try { return await api(path, { ...options, signal: controller.signal, cache: 'no-store' }); }
  finally { clearTimeout(timeout); }
}
function questionViewMatches(documentId, token, jobId = questionJobId, viewSeq = questionViewSeq) {
  return documentId === selectedId && token === requestSeq && jobId === questionJobId
    && viewSeq === questionViewSeq;
}
function unwrapQuestionJob(data) { return data?.job || data; }
function renderQuestionPending(pending) {
  const line = document.createElement('p');
  line.className = 'error';
  line.textContent = `提交结果尚未确认。原问题：${pending.question}。`
    + '请先刷新问答任务查询已有记录；页面不会自动重发，也不能据此判断是否已经计费。'
    + '确认原提交会复用同一个请求键；若服务器尚未收到原请求，这将创建任务并可能产生费用。';
  $('question-job').replaceChildren(line);
  controls();
}
function renderQuestionJob(job) {
  questionJobState = job;
  const panel = $('question-job');
  panel.replaceChildren();
  if (!job) { controls(); return; }
  const line = document.createElement('p');
  const status = job.cancel_requested && questionActiveStatuses.includes(job.status)
    ? '已请求取消，等待当前阶段结束' : questionStatusLabels[job.status] || job.status;
  const stage = job.status === 'cancelled' ? 'cancelled' : job.stage;
  line.textContent = `任务 ${job.id} · ${status}`
    + ` · ${questionStageLabels[stage] || stage || '等待阶段信息'}`
    + ` · 已用 ${job.requests_used ?? 0}/${job.max_requests ?? '—'} 次模型请求`;
  panel.append(line);
  if (questionActiveStatuses.includes(job.status)) {
    const start = Date.parse(job.status === 'queued' ? job.created_at : (job.started_at || job.created_at));
    if (Number.isFinite(start)) {
      const elapsed = document.createElement('p');
      elapsed.className = 'muted';
      const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
      elapsed.textContent = `${job.status === 'queued' ? '排队' : '本次执行'}已等待 ${seconds} 秒。`;
      if (job.stage === 'generating' && seconds >= 30 && !job.cancel_requested) {
        elapsed.textContent += '生成可能需要较长时间，可稍后从问答历史查看；刷新不会重复提交。';
      }
      panel.append(elapsed);
    }
  }
  if (job.stage_detail) {
    const detail = document.createElement('p');
    detail.className = 'muted';
    detail.textContent = job.stage_detail;
    panel.append(detail);
  }
  if (job.question) {
    const question = document.createElement('p');
    question.textContent = `问题：${job.question}`;
    panel.append(question);
  }
  const meta = document.createElement('p');
  meta.className = 'muted';
  meta.textContent = `解析版本 ${job.parse_version_id || '待绑定'} · 索引 ${job.index_id || '待绑定'}`
    + (job.request_id ? ` · 请求编号 ${job.request_id}` : '')
    + (job.updated_at ? ` · 更新 ${job.updated_at}` : '')
    + '。任务在后台执行，可切换文档或稍后刷新。';
  panel.append(meta);
  if (job.error_code || job.error_message) {
    const error = document.createElement('p');
    error.className = 'error';
    error.textContent = `${job.error_code || '任务提示'} · ${job.error_message || '请核对任务状态。'}`;
    panel.append(error);
  }
  if (job.status === 'needs_attention') {
    const note = document.createElement('p');
    note.className = 'error';
    note.textContent = '上游请求结果尚未确认，系统不会自动重发。明确重试可能重复计费；已有结果仍可查看。';
    panel.append(note);
  }
  controls();
}
function renderQuestionHistory() {
  const panel = $('question-history');
  panel.replaceChildren();
  const title = document.createElement('h4');
  title.textContent = '问答历史';
  panel.append(title);
  if (!questionJobs.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '该文档还没有已记录的问答任务。';
    panel.append(empty);
  }
  for (const job of questionJobs) {
    const row = document.createElement('div');
    row.className = 'result-row';
    const label = document.createElement('span');
    label.textContent = `${job.question || '问答任务'} · ${questionStatusLabels[job.status] || job.status}`
      + ` · ${job.created_at || ''} · ${job.id}`;
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'subtle';
    open.textContent = job.result_id ? '查看已存回答' : '查看任务';
    const documentId = selectedId;
    open.onclick = () => {
      if (documentId !== selectedId || answerBusy) return;
      openQuestionJob(job.id).catch((error) => {
        if (documentId === selectedId) setMessage('无法读取问答记录，请刷新后重试。', 'error');
      });
    };
    row.append(label, open);
    panel.append(row);
  }
}
async function loadQuestionHistory(documentId = selectedId, token = requestSeq, viewSeq = questionViewSeq) {
  const jobs = await questionApi(`/documents/${encodeURIComponent(documentId)}/question-jobs`);
  if (documentId !== selectedId || token !== requestSeq || viewSeq !== questionViewSeq) return null;
  questionJobs = Array.isArray(jobs) ? jobs : (jobs.jobs || []);
  renderQuestionHistory();
  return questionJobs;
}
function saveKnownQuestionJob(documentId, job, pending = readQuestionSession(documentId), acceptedResponse = false) {
  const current = readQuestionSession(documentId);
  if (!pending || !current || current.idempotency_key !== pending.idempotency_key) return;
  if (!acceptedResponse) {
    if (pending.uncertain) {
      const knownKey = pending.mutation === 'retry' ? job.last_retry_key : job.idempotency_key;
      if (knownKey !== pending.idempotency_key) return;
    } else if (pending.job_id !== job.id) return;
  }
  writeQuestionSession(documentId, { ...pending, job_id: job.id, uncertain: false,
    result_id: job.result_id || null, mutation: null });
}
async function openQuestionResult(resultId, documentId = selectedId, token = requestSeq,
  jobId = questionJobId, viewSeq = questionViewSeq) {
  const result = await questionApi(`/question-results/${encodeURIComponent(resultId)}`);
  if (!questionViewMatches(documentId, token, jobId, viewSeq) || result.document_id !== documentId) return;
  questionResult = result;
  renderAnswer(result.answer);
  const actions = $('answer-actions');
  actions.replaceChildren();
  const label = document.createElement('span');
  label.className = 'muted';
  label.textContent = `已保存回答 ${result.id}` + (result.created_at ? ` · ${result.created_at}` : '');
  actions.append(label);
  for (const [format, text] of [['markdown', '导出 Markdown'], ['json', '导出 JSON']]) {
    const link = document.createElement('a');
    link.href = `/api/question-results/${encodeURIComponent(result.id)}/export?format=${format}`;
    link.textContent = text;
    actions.append(link);
  }
  const pending = readQuestionSession(documentId);
  if (pending?.job_id === result.job_id) {
    writeQuestionSession(documentId, { ...pending, result_id: result.id });
  }
}
async function openQuestionJob(jobId) {
  const documentId = selectedId;
  const token = requestSeq;
  const viewSeq = ++questionViewSeq;
  stopQuestionPolling();
  questionJobId = jobId;
  const job = unwrapQuestionJob(await questionApi(`/question-jobs/${encodeURIComponent(jobId)}`));
  if (!questionViewMatches(documentId, token, jobId, viewSeq) || job.document_id !== documentId) return;
  renderQuestionJob(job);
  if (job.result_id) await openQuestionResult(job.result_id, documentId, token, jobId, viewSeq);
  if (!questionViewMatches(documentId, token, jobId, viewSeq)) return;
  if (questionActiveStatuses.includes(job.status)) scheduleQuestionPoll(0);
}
async function restoreQuestions() {
  const documentId = selectedId;
  const token = requestSeq;
  const viewSeq = ++questionViewSeq;
  stopQuestionPolling();
  const pending = readQuestionSession(documentId);
  try {
    const jobs = await loadQuestionHistory(documentId, token, viewSeq);
    if (!jobs || documentId !== selectedId || token !== requestSeq || viewSeq !== questionViewSeq) return;
    const matched = pending && jobs.find((job) => pending.mutation === 'retry'
      ? job.last_retry_key === pending.idempotency_key
      : job.id === pending.job_id || job.idempotency_key === pending.idempotency_key);
    if (matched) saveKnownQuestionJob(documentId, matched, pending);
    if (pending?.uncertain && !matched) { renderQuestionPending(pending); return; }
    const job = matched || jobs.find((item) => questionActiveStatuses.includes(item.status)) || jobs[0];
    if (job) {
      questionJobId = job.id;
      renderQuestionJob(job);
      if (questionActiveStatuses.includes(job.status)) scheduleQuestionPoll(0);
      // 普通刷新保留用户正在核对的回答，其余情况恢复已保存回答。
      if (!questionResult && job.result_id) await openQuestionResult(job.result_id, documentId, token, job.id, viewSeq);
      else if (questionResult) await openQuestionResult(questionResult.id, documentId, token, job.id, viewSeq);
    } else controls();
  } catch (error) {
    if (documentId !== selectedId || token !== requestSeq || viewSeq !== questionViewSeq) return;
    $('question-job').textContent = '无法读取问答任务与历史，请点击“刷新问答任务与历史”重试；未发起生成。';
    if (pending?.uncertain) renderQuestionPending(pending);
    controls();
  }
}
async function pollQuestionJob() {
  if (!questionJobId || !selectedId || document.hidden) { stopQuestionPolling(); return; }
  const jobId = questionJobId;
  const documentId = selectedId;
  const token = requestSeq;
  const viewSeq = questionViewSeq;
  try {
    const job = unwrapQuestionJob(await questionApi(`/question-jobs/${encodeURIComponent(jobId)}`));
    if (!questionViewMatches(documentId, token, jobId, viewSeq) || job.document_id !== documentId) return;
    renderQuestionJob(job);
    saveKnownQuestionJob(documentId, job);
    if (questionActiveStatuses.includes(job.status)) {
      questionPollFailures = 0;
      questionPollDelay = 2500;
      scheduleQuestionPoll();
      return;
    }
    stopQuestionPolling();
    if (job.result_id) await openQuestionResult(job.result_id, documentId, token, jobId, viewSeq);
    if (!questionViewMatches(documentId, token, jobId, viewSeq)) return;
    await loadQuestionHistory(documentId, token, viewSeq);
    questionPollFailures = 0;
    questionPollDelay = 2500;
  } catch (error) {
    if (!questionViewMatches(documentId, token, jobId, viewSeq)) return;
    questionPollFailures += 1;
    const note = document.createElement('p');
    note.className = 'error';
    note.textContent = questionPollFailures > 6
      ? '多次无法读取问答状态，已暂停自动查询。请刷新问答任务恢复；任务可能仍在后台执行，页面未重新生成。'
      : '暂时无法读取问答状态，将稍后重查。任务可能仍在后台执行，页面不会重新生成。';
    $('question-job').replaceChildren(note);
    if (questionPollFailures > 6) { stopQuestionPolling(); return; }
    questionPollDelay = Math.min(15000, questionPollDelay * 2);
    scheduleQuestionPoll();
  }
}
async function submitQuestionPending(pending) {
  const documentId = selectedId;
  const token = requestSeq;
  const mutation = ++questionMutationSeq;
  const viewSeq = ++questionViewSeq;
  answerBusy = true;
  controls();
  askRequestCount += 1;
  const record = { documentId, question: pending.question, seq: token, status: 'pending' };
  askRequests.push(record);
  const note = document.createElement('p');
  note.textContent = '正在提交问答任务，任务编号将在服务器接收后显示。';
  $('question-job').replaceChildren(note);
  let accepted = false;
  try {
    const path = pending.mutation === 'retry'
      ? `/question-jobs/${encodeURIComponent(pending.job_id)}/retry`
      : `/documents/${encodeURIComponent(documentId)}/question-jobs`;
    const data = await questionApi(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pending.mutation === 'retry'
        ? { idempotency_key: pending.idempotency_key }
        : { question: pending.question, idempotency_key: pending.idempotency_key }),
    });
    const job = unwrapQuestionJob(data);
    if (!job?.id || job.document_id !== documentId) throw new Error('任务响应不完整');
    accepted = true;
    saveKnownQuestionJob(documentId, job, pending, true);
    record.status = 'ok';
    if (documentId !== selectedId || token !== requestSeq || mutation !== questionMutationSeq) {
      record.status = 'stale'; return;
    }
    questionJobId = job.id;
    renderQuestionJob(job);
    await loadQuestionHistory(documentId, token, viewSeq);
    if (!questionViewMatches(documentId, token, job.id, viewSeq)) return;
    if (job.result_id) await openQuestionResult(job.result_id, documentId, token, job.id, viewSeq);
    if (!questionViewMatches(documentId, token, job.id, viewSeq)) return;
    if (questionActiveStatuses.includes(job.status)) scheduleQuestionPoll(0);
  } catch (error) {
    record.status = accepted ? 'ok' : 'uncertain';
    if (documentId !== selectedId || token !== requestSeq || mutation !== questionMutationSeq) return;
    if (accepted) {
      setMessage('任务已接收，暂时无法刷新记录。请刷新问答任务核对；页面不会重新生成。', 'error');
      scheduleQuestionPoll(0);
      return;
    }
    if ([400, 422].includes(error.status)) {
      // 服务端明确拒绝的参数错误允许修改后重新提交；网络不明不能走此分支。
      const current = readQuestionSession(documentId);
      if (current?.idempotency_key === pending.idempotency_key) {
        writeQuestionSession(documentId, { ...current, uncertain: false });
      }
      record.status = 'rejected';
      const rejected = document.createElement('p');
      rejected.className = 'error';
      rejected.textContent = `提交被服务器拒绝：${error.message}。请检查条件后重新提交。`;
      $('question-job').replaceChildren(rejected);
      return;
    }
    // 无论网络超时还是响应丢失，都只能先查询已有任务；不能自动再次 POST。
    renderQuestionPending(readQuestionSession(documentId) || pending);
    await restoreQuestions();
  } finally {
    if (documentId === selectedId && token === requestSeq && mutation === questionMutationSeq) {
      answerBusy = false;
      controls();
    }
  }
}
async function askQuestion() {
  const documentId = selectedId;
  const question = $('question').value.trim();
  if (!documentId) { setMessage('请先选择一份文档', 'error'); return; }
  if (!question) { setMessage('请输入问题后再提问', 'error'); return; }
  if (busy || answerBusy || questionActiveStatuses.includes(questionJobState?.status)) return;
  const existing = readQuestionSession(documentId);
  if (existing?.uncertain) { renderQuestionPending(existing); return; }
  const pending = { question, idempotency_key: crypto.randomUUID(), job_id: null,
    result_id: null, uncertain: true, mutation: null };
  try { writeQuestionSession(documentId, pending); }
  catch (error) { setMessage(error.message, 'error'); return; }
  questionResult = null;
  questionJobState = null;
  questionJobId = null;
  stopQuestionPolling();
  resetAnswer();
  setMessage('');
  await submitQuestionPending(pending);
}

$('question-refresh').onclick = async () => {
  if (answerBusy) return;
  stopQuestionPolling();
  questionPollFailures = 0;
  questionPollDelay = 2500;
  await restoreQuestions();
};
$('question-confirm').onclick = async () => {
  if (answerBusy) return;
  const pending = readQuestionSession(selectedId);
  if (pending?.uncertain) await submitQuestionPending(pending);
};
$('question-retry').onclick = async () => {
  if (answerBusy || !questionJobState || !['failed', 'needs_attention'].includes(questionJobState.status)
    || readQuestionSession(selectedId)?.uncertain
    || questionJobState.requests_used >= questionJobState.max_requests) return;
  const pending = { question: questionJobState.question || '', idempotency_key: crypto.randomUUID(),
    job_id: questionJobState.id, result_id: null, uncertain: true, mutation: 'retry' };
  try { writeQuestionSession(selectedId, pending); }
  catch (error) { setMessage(error.message, 'error'); return; }
  await submitQuestionPending(pending);
};
$('question-cancel').onclick = async () => {
  if (answerBusy || !questionJobId || !questionActiveStatuses.includes(questionJobState?.status)
    || questionJobState?.cancel_requested) return;
  const documentId = selectedId;
  const token = requestSeq;
  const jobId = questionJobId;
  const mutation = ++questionMutationSeq;
  const viewSeq = ++questionViewSeq;
  answerBusy = true;
  controls();
  try {
    const job = unwrapQuestionJob(await questionApi(`/question-jobs/${encodeURIComponent(jobId)}/cancel`,
      { method: 'POST' }));
    if (!questionViewMatches(documentId, token, jobId, viewSeq) || mutation !== questionMutationSeq) return;
    renderQuestionJob(job);
    setMessage('已请求取消问答任务；已经发出的上游请求仍可能计费。', 'info');
    scheduleQuestionPoll(0);
  } catch (error) {
    if (!questionViewMatches(documentId, token, jobId, viewSeq) || mutation !== questionMutationSeq) return;
    setMessage('取消请求结果尚未确认，请刷新问答任务核对；已经发出的请求仍可能计费。', 'error');
    scheduleQuestionPoll(0);
  } finally {
    if (questionViewMatches(documentId, token, jobId) && mutation === questionMutationSeq) {
      answerBusy = false;
      controls();
    }
  }
};

async function intelligence(action, body) {
  // 摘要与信息提取已改为任务式接口：提交后由独立 worker 执行，页面轮询真实状态。
  // action 取 'summary' 或 'extract'；提取设置固定为已展示计划中的规范设置。
  analysisRequestCount += 1;
  const slot = pendingAnalysisSlot(action, body);
  const result = await api(`/documents/${selectedId}/${action}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  pendingAnalysisKeys.delete(slot);
  try { sessionStorage.removeItem(slot); } catch (_) { /* 只在明确收到响应后释放键。 */ }
  analysisJobId = result.job ? result.job.id : null;
  renderAnalysisJob(result.job);
  setMessage(result.message + (result.reused ? '（复用了已有任务或结果）' : ''), 'ok');
  await loadAnalysisResults();
  scheduleAnalysisPoll(0);
}

$('analysis-plan').onclick = () => run(() => loadAnalysisPlan(analysisKind));
$('extraction-metrics').oninput = extractionSettingsChanged;
$('extraction-highlights').onchange = extractionSettingsChanged;
async function ensureVisibleAnalysisPlan(kind) {
  const doc = documents.find((item) => item.id === selectedId);
  if (!analysisPlan || analysisPlan.kind !== kind || analysisPlan.document_id !== selectedId
      || analysisPlan.parse_version_id !== (doc && doc.active_parse_version_id)
      || (kind === 'extraction'
        && !sameExtractionOptions(analysisPlan.extraction_options, extractionOptions()))) {
    await loadAnalysisPlan(kind);
    setMessage('已展示分析范围与请求上界。确认后再次点击生成按钮；本次尚未创建生成任务。', 'info');
    return false;
  }
  if (!analysisPlan.executable) {
    setMessage('当前计划不可执行，请先处理显示的限制。', 'error');
    return false;
  }
  return true;
}
$('summary').onclick = () => run(async () => {
  analysisKind = 'summary';
  if (!await ensureVisibleAnalysisPlan('summary')) return;
  const body = analysisPlanPayload();
  await intelligence('summary', body);
});
$('extract').onclick = () => run(async () => {
  analysisKind = 'extraction';
  if (!await ensureVisibleAnalysisPlan('extraction')) return;
  const body = analysisPlanPayload();
  await intelligence('extract', body);
});
$('analysis-stop').onclick = () => run(async () => {
  if (!analysisJobId) return;
  const result = await api(`/analysis-jobs/${analysisJobId}/cancel`, { method: 'POST' });
  setMessage(result.message, 'ok');
  renderAnalysisJob(await api(`/analysis-jobs/${analysisJobId}`));
  await loadAnalysisResults();
});
$('analysis-retry').onclick = () => run(async () => {
  if (!analysisJobId) return;
  // 明确重试：预算不重置，已校验步骤直接复用；页面提示可能重复计费。
  const job = await api(`/analysis-jobs/${analysisJobId}/retry`, { method: 'POST' });
  setMessage('已提交重试：只做未完成的批次，已校验步骤复用；失败与不确定调用仍占用预算，'
    + '可能重复计费。', 'ok');
  renderAnalysisJob(job);
  scheduleAnalysisPoll(0);
});
$('analysis-regenerate').onclick = () => run(async () => {
  // 明确重新生成：使用新幂等键，产生新的结果版本，旧结果继续可读。
  const kind = lastResultKind || analysisKind || 'extraction';
  analysisKind = kind;
  if (!await ensureVisibleAnalysisPlan(kind)) return;
  const body = analysisPlanPayload(true);
  await intelligence(kind === 'summary' ? 'summary' : 'extract', body);
});
$('question-form').onsubmit = (event) => {
  // 阻止浏览器默认提交；问答提交期间重复触发会被 askQuestion 直接忽略。
  event.preventDefault();
  askQuestion();
};

// 页面隐藏时暂停轮询，重新可见时立即补一次查询，避免后台堆积请求。
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling();
    stopRuntimePolling();
    stopQuestionPolling();
  } else if (currentTaskId) {
    schedulePoll(0);
  }
  if (!document.hidden) refreshRuntimeStatus();
  if (!document.hidden && questionJobId && questionActiveStatuses.includes(questionJobState?.status)) {
    scheduleQuestionPoll(0);
  }
});

// 自动化验收专用只读钩子：只暴露读取状态与“触发一次真实用户操作”的能力，
// 便于在真实浏览器中断言“终态停止轮询”“断网提示”“重复提交被忽略”“迟到响应被丢弃”，
// 不改变正常页面行为，也不允许直接改写内部状态。
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
  // 问答相关只读状态：用于断言每个有效提交只对应一次请求、迟到响应被丢弃。
  state: () => ({
    selectedId,
    requestSeq,
    answerBusy,
    answerId: answerPayload ? answerPayload.answer_id : null,
    answerStatus: answerPayload ? answerPayload.status : null,
    citations: answerPayload ? answerPayload.citations.length : 0,
    questionDisabled: $('question').disabled,
    askButtonDisabled: $('ask').disabled,
    answerPanelHidden: $('answer-panel').hidden,
    waitingVisible: !$('answer-waiting').hidden,
    requestCount: askRequestCount,
    lastRequest: askRequests.length ? askRequests[askRequests.length - 1] : null,
  }),
  questionState: () => ({
    jobId: questionJobId, jobStatus: questionJobState?.status || null,
    resultId: questionResult?.id || null, historyCount: questionJobs.length,
    polling: questionPollTimer === null ? 0 : 1, pollDelay: questionPollDelay,
    pollFailures: questionPollFailures, uncertain: Boolean(readQuestionSession(selectedId)?.uncertain),
    pendingKey: readQuestionSession(selectedId)?.idempotency_key || null,
    busy, answerBusy,
  }),
  requests: () => askRequests.map((entry) => ({ documentId: entry.documentId,
                                                question: entry.question, seq: entry.seq })),
  // 渲染函数导出：供离线渲染安全回归（tests/web/render_safety.mjs）与浏览器
  // 验收脚本直接调用**同一份生产渲染代码**，避免测试复制一份实现而失去意义。
  renderAnswerMarkdown: (markdown) => renderAnswerMarkdown(markdown),
  renderCitations: (citations, meta) => renderCitations(citations, meta),
  renderAnswer: (payload) => renderAnswer(payload),
  renderLegacyChunks: (chunks) => renderLegacyChunks(chunks),
  renderWarnings: (warnings, version) => renderWarnings(warnings, version),
  renderRuntimeStatus: (status) => renderRuntimeStatus(status),
  refreshRuntimeStatus: () => refreshRuntimeStatus(),
  // 分析任务的渲染函数导出：同一份生产代码，供离线安全回归与浏览器验收复用。
  renderAnalysisPlan: (plan) => renderAnalysisPlan(plan),
  renderAnalysisJob: (job) => renderAnalysisJob(job),
  renderAnalysisResult: (result) => renderAnalysisResult(result),
  renderAnalysisResultList: () => renderAnalysisResultList(),
  resultToText: (result) => resultToText(result),
  // 分析只读状态：用于断言“刷新与查看历史不触发生成”“重复点击只提交一次”。
  analysisState: () => ({
    kind: analysisKind,
    hasPlan: analysisPlan !== null,
    planFingerprint: analysisPlan ? analysisPlan.plan_fingerprint : null,
    jobId: analysisJobId,
    jobStatus: analysisJobState ? analysisJobState.status : null,
    requestsUsed: analysisJobState ? analysisJobState.requests_used : null,
    maxRequests: analysisJobState ? analysisJobState.max_requests : null,
    resultCount: analysisResults.length,
    activeResultId,
    activeResultKind: lastResultKind,
    submitCount: analysisRequestCount,
    polling: analysisPollTimer === null ? 0 : 1,
    summaryDisabled: $('summary').disabled,
    extractDisabled: $('extract').disabled,
  }),
  // 选择文档必须走与用户点击完全相同的路径，不做任何内部状态注入。
  selectDocument: (documentId) => run(() => select(documentId)),
  setQuestion: (value) => { $('question').value = value; return true; },
  // 触发真实提交（与用户按 Enter / 点击按钮相同的事件路径）。
  submitQuestion: (value) => {
    if (value !== undefined) $('question').value = value;
    $('question-form').dispatchEvent(new Event('submit', { cancelable: true }));
    return true;
  },
};

// 首次打开页面时加载文档列表。
run(refresh);
refreshRuntimeStatus();
