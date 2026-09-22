// 工作台交互：集中管理当前文档、请求状态及各操作入口。
const $ = (id) => document.getElementById(id);
const statusLabels = { uploaded: '待解析', parsing: '解析中', parsed: '已解析', failed: '解析失败' };
// 页面状态保存在内存中；实际文档与分块以服务端返回的数据为准。
let selectedId = null;
let documents = [];
let busy = false;
let modelConfigured = false;
let embeddingConfigured = false;
let indexState = 'pending';
const indexLabels = { pending: '未建立索引', indexing: '正在建立索引', indexed: '索引可用', failed: '索引失败', stale: '模型或原文已变更，请重建索引' };

async function api(path, options = {}) {
  // 统一 API 前缀与错误转换，让操作入口只处理成功数据或错误提示。
  const response = await fetch(`/api${path}`, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求参数不正确');
  return data;
}

function controls() {
  // 请求期间禁止重复操作；智能入口只校验解析状态，模型可用性由后端判断。
  const doc = documents.find((item) => item.id === selectedId);
  $('parse').disabled = busy || !doc || ['parsed', 'parsing'].includes(doc.status);
  for (const id of ['summary', 'extract', 'question', 'ask']) $(id).disabled = busy || doc?.status !== 'parsed';
  $('upload-form').querySelector('button').disabled = busy;
  $('refresh').disabled = busy;
  $('test-model').disabled = busy || !modelConfigured;
  $('test-embedding').disabled = busy || !embeddingConfigured;
  $('build-index').disabled = busy || !embeddingConfigured || doc?.status !== 'parsed' || ['indexed', 'indexing'].includes(indexState);
  $('rebuild-index').disabled = busy || !embeddingConfigured || doc?.status !== 'parsed' || indexState === 'indexing';
  for (const id of ['search-query', 'search']) $(id).disabled = busy || !embeddingConfigured || indexState !== 'indexed';
  for (const button of $('documents').querySelectorAll('button')) button.disabled = busy;
}

async function run(action) {
  // 所有交互共用忙碌状态和错误提示，失败后也必须恢复控件。
  if (busy) return;
  busy = true;
  $('message').textContent = '';
  controls();
  try { await action(); }
  catch (error) { $('message').textContent = error.message; }
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
    meta.textContent = `${statusLabels[doc.status]} · ${(doc.size / 1024).toFixed(1)} KB`;
    button.append(meta);
    button.onclick = () => run(() => select(doc.id));
    $('documents').append(button);
  }
}

async function select(id) {
  // 切换时清空旧结果，随后加载该文档的来源片段。
  selectedId = id;
  indexState = 'pending';
  $('search-results').replaceChildren();
  renderDocuments();
  const doc = documents.find((item) => item.id === id);
  $('document-title').textContent = doc.filename;
  $('document-meta').textContent = `${statusLabels[doc.status]} · ${doc.page_count} 页 · ${doc.chunk_count} 个分块${doc.error ? ` · ${doc.error}` : ''}`;
  $('result').hidden = true;
  $('chunks').replaceChildren();
  await refreshIndex();
  const chunks = await api(`/documents/${id}/chunks`);
  if (!chunks.length) $('chunks').textContent = '尚无解析内容。点击“解析文档”生成文本分块。';
  for (const chunk of chunks) {
    const article = document.createElement('article'); article.className = 'chunk';
    const label = document.createElement('small'); label.textContent = `第 ${chunk.page} 页 · 来源片段`;
    // 原文也作为纯文本呈现，文档中即便包含标签也不会被执行。
    const body = document.createElement('p'); body.textContent = chunk.text;
    article.append(label, body); $('chunks').append(article);
  }
}

async function refresh() {
  // 重新获取服务端状态；当前选择仍有效时，同时刷新详情。
  const model = await api('/model/status');
  modelConfigured = model.configured;
  $('model-status').textContent = `${model.model} · ${model.configured ? '已配置，尚未测试连接' : '未配置密钥，请填写本地 .env 并重启服务'}`;
  const embedding = await api('/embedding/status');
  embeddingConfigured = embedding.configured;
  $('embedding-status').textContent = `${embedding.model} · ${embedding.configured ? '已配置，尚未测试连接' : '请填写 EMBEDDING_API_KEY 并重启服务'}`;
  $('embedding-gateway').textContent = `网关：${embedding.base_url}`;
  documents = await api('/documents');
  renderDocuments();
  if (selectedId && documents.some((doc) => doc.id === selectedId)) await select(selectedId);
}

$('upload-form').onsubmit = (event) => {
  // 阻止浏览器默认提交，以局部更新保留工作台状态。
  event.preventDefault();
  run(async () => {
    // 由浏览器生成 multipart 边界，不手动设置上传请求的 Content-Type。
    const data = new FormData($('upload-form'));
    const doc = await api('/documents', { method: 'POST', body: data });
    selectedId = doc.id;
    await refresh();
    $('upload-form').reset();
    $('message').textContent = '上传成功，可以开始解析。';
  });
};
$('refresh').onclick = () => run(refresh);

async function refreshIndex() {
  const state = await api(`/documents/${selectedId}/index`);
  indexState = state.status;
  $('index-status').textContent = `${indexLabels[state.status]}${state.dimension ? ` · ${state.dimension} 维 · ${state.chunk_count} 个分块` : ''}${state.error ? ` · ${state.error}` : ''}`;
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
    // 相似度只用于排序，不显示为“可信度”；所有原文均使用纯文本渲染。
    for (const hit of data.results) {
      const article = document.createElement('article'); article.className = 'chunk';
      const label = document.createElement('small'); label.textContent = `第 ${hit.page} 页 · 相似度 ${hit.score.toFixed(3)}`;
      const text = document.createElement('p'); text.textContent = hit.text;
      article.append(label, text); $('search-results').append(article);
    }
    if (!data.results.length) $('search-results').textContent = '没有可用的检索片段。';
  });
};
// 不在页面加载时自动调用模型；仅点击后发送固定测试消息。
$('test-model').onclick = () => run(async () => {
  $('model-status').textContent = '正在连接 DeepSeek，请稍候…';
  try {
    const result = await api('/model/test', { method: 'POST' });
    $('model-status').textContent = `${result.model} · 连接成功`;
    $('message').textContent = `模型回复：${result.answer}`;
  } catch (error) {
    $('model-status').textContent = '连接测试失败';
    throw error;
  }
});
$('parse').onclick = () => run(async () => {
  // 即使解析失败也重新读取状态，确保页面能显示服务端记录的错误。
  try { await api(`/documents/${selectedId}/parse`, { method: 'POST' }); }
  finally { await refresh(); }
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
// 首次打开页面时加载文档列表。
run(refresh);
