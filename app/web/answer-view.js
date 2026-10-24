// 模型内容、文件名和引文都作为不可信文本处理，不解释 HTML 或外部资源。
export function node(tag, text = '', className = '') {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}
export function locationText(source) {
  if (source.format === 'pdf') {
    if (!source.page) return 'PDF 页码未提供';
    const mapping=source.pdf_page_label;
    if (!mapping) return `第 ${source.page} 页`;
    const physical=`PDF 第 ${source.page} 页`;
    if (['footer_observed','corroborated'].includes(mapping.status) && mapping.printed_label) return `${physical} · 印刷页码 ${mapping.printed_label}（页脚提取）`;
    if (mapping.status==='catalog_only' && mapping.catalog_label) return `${physical} · PDF 标签 ${mapping.catalog_label}（印刷页码未核对）`;
    return physical;
  }
  if (source.format === 'xlsx') return [source.sheet_name && `工作表 ${source.sheet_name}`, source.cell_range].filter(Boolean).join(' · ') || '工作表位置未提供';
  if (source.format === 'docx') return [source.section_path, source.table_no && `表格 ${source.table_no}`, 'DOCX 无真实页码'].filter(Boolean).join(' · ');
  return [source.page && `逻辑页 ${source.page}`, source.line_start && `第 ${source.line_start}–${source.line_end || source.line_start} 行`].filter(Boolean).join(' · ') || '位置有限';
}
// 同一页可能有多个坐标框；只合并展示标签，保留原始来源和引用身份用于高亮。
export function sourceLocationsText(sources = []) {
  return [...new Set(sources.map(locationText))].join(' / ');
}
function inline(parent, text, citations, open) {
  const pattern = /(\*\*([^*\n]+)\*\*|\[(\d+)\])/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(cursor, match.index)));
    if (match[2]) parent.append(node('strong', match[2]));
    else if (citations.has(Number(match[3]))) {
      const citation = citations.get(Number(match[3]));
      const button = node('button', match[3], 'reference');
      button.type = 'button'; button.setAttribute('aria-label', `查看引用 ${match[3]}`);
      button.onclick = () => open(citation, button); parent.append(button);
    } else parent.append(document.createTextNode(match[0]));
    cursor = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(cursor)));
}
export function answerTextForDisplay(answer) {
  let text=String(answer.answer || '');
  const labels={direct:'原文直述',synthesis:'多处综合',inference:'结构推断'};
  const contexts={subject:'对象',period:'期间',unit:'单位',scope:'范围',condition:'限定',role:'用途'};
  for(const fact of [...(answer.conclusion || []),...(answer.explanation || [])]) {
    if(typeof fact.text!=='string' || !labels[fact.claim_type])continue;
    const extra=(fact.context || []).filter(c=>typeof c.text==='string' && contexts[c.kind] && !fact.text.includes(c.text)).map(c=>contexts[c.kind]+'：'+c.text);
    const content=fact.text+(extra.length?'（'+extra.join('；')+'）':'');
    const status=fact.support_review?.status;
    const note=status==='needs_review'?'（证据规则检查：需复核；语义待复核）':status==='checks_passed'?'（证据规则检查通过；语义待复核）':'';
    const decorated='【'+labels[fact.claim_type]+'】'+content+note;
    // 仅转换能由结构化事实完整重建的旧展示串；不对任意正文做正则清洗。
    const display=(fact.claim_type==='inference'?'【结构推断】':'')+content;
    if(text.includes(decorated))text=text.split(decorated).join(display);
  }
  return text;
}
export function renderAnswer(container, answer, openCitation) {
  container.replaceChildren();
  if (answer.response_kind === 'system') { container.append(node('p', String(answer.answer))); return; }
  const citations = new Map((answer.citations || []).map(citation => [citation.reference_id, citation]));
  let list = null, kind = '';
  for (const line of answerTextForDisplay(answer).split('\n')) {
    if (!line.trim()) { list = null; kind = ''; continue; }
    const heading = /^(#{1,4})\s+(.+)/.exec(line);
    const bullet = /^\s*[-*]\s+(.+)/.exec(line), ordered = /^\s*\d+[.)、]\s+(.+)/.exec(line);
    let parent;
    if (heading) { parent = node('h3'); container.append(parent); inline(parent, heading[2], citations, openCitation); list = null; }
    else if (bullet || ordered) {
      const next = bullet ? 'ul' : 'ol';
      if (!list || kind !== next) { list = node(next); kind = next; container.append(list); }
      parent = node('li'); list.append(parent); inline(parent, (bullet || ordered)[1], citations, openCitation);
    } else { list = null; kind = ''; parent = node('p'); container.append(parent); inline(parent, line, citations, openCitation); }
  }
  if (answer.clarification_questions?.length) {
    const question = node('section', '', 'answer-note'); question.append(node('h4', '需要补充的条件'));
    answer.clarification_questions.forEach(text => question.append(node('p', String(text)))); container.append(question);
  }
  const facts = [...(answer.conclusion || []), ...(answer.explanation || [])];
  const needsReview = facts.some(fact => fact.claim_type === 'inference' || fact.support_review?.status === 'needs_review');
  const notes = node('details', '', 'answer-details');
  notes.append(node('summary', needsReview ? '回答说明 · 含推断或待复核内容' : '适用范围与引用说明'));
  if (answer.is_old_version) notes.append(node('p', '这份回答依据历史资料版本生成，请结合当前资料核对。', 'review-note'));
  const sections = [
    ['尚缺少的依据', answer.missing_points || []],
    ['适用范围与限制', answer.limitations || []],
  ];
  for (const [title, values] of sections) {
    if (!values.length) continue;
    const section = node('section', '', 'answer-note'); section.append(node('h4', title));
    const list = node('ul'); values.forEach(text => list.append(node('li', String(text)))); section.append(list); notes.append(section);
  }
  if (answer.answer_parts?.length) {
    const details = node('section', '', 'coverage-note'); details.append(node('h4', '各个问题的回答范围'));
    const labels = { answered: '已回答', partial: '部分回答', missing_evidence: '依据不足', needs_clarification: '待补充条件' };
    for (const part of answer.answer_parts) details.append(node('p', `${labels[part.state] || '待核对'}：${part.text}${part.missing_detail ? ` · ${part.missing_detail}` : ''}`));
    notes.append(details);
  }
  if (answer.quality_warnings?.length) {
    const section = node('section', '', 'answer-note'); section.append(node('h4', '资料使用提示'));
    for (const warning of answer.quality_warnings) section.append(node('p', warning.message || warning.detail || warning.code || '资料存在解析限制，请核对原件'));
    notes.append(section);
  }
  if (needsReview) {
    notes.append(node('p', '本回答含推断或待复核内容，请结合原文判断。', 'review-note'));
  }
  if (facts.some(f=>typeof f.text==='string')) {
    const audit=node('section','','answer-note');audit.append(node('h4','论断与校验'));
    for(const f of facts) {
      if(typeof f.text!=='string')continue;
      const kind={direct:'原文直述',synthesis:'多处综合',inference:'结构推断'}[f.claim_type] || '未分类';
      const status=f.support_review?.status==='checks_passed'?'证据规则检查通过；语义未自动认证':f.support_review?.status==='needs_review'?'需复核；语义未自动认证':'未提供自动校验状态';
      audit.append(node('p',`${kind} · ${f.text}（${status}）`));
    }
    notes.append(audit);
  }
  if (citations.size) {
    const sourceDetails = node('details', '', 'answer-details source-details'); sourceDetails.append(node('summary', `引用来源（${citations.size}）`));
    const sources = node('div', '', 'source-list');
    for (const citation of citations.values()) {
      const label = sourceLocationsText(citation.sources);
      const button = node('button', `[${citation.reference_id}] ${label || '查看原文'}`, 'source-chip');
      button.type = 'button'; button.onclick = () => openCitation(citation, button); sources.append(button);
    }
    sourceDetails.append(sources); container.append(sourceDetails);
  }
  if (citations.size || facts.length) notes.append(node('p', '来源可回溯与语义正确是不同检查，请保留必要的人工复核。', 'answer-footnote'));
  if (notes.children.length > 1) container.append(notes);
}
