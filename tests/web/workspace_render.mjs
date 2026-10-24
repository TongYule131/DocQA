// 对真实客户渲染模块执行恶意内容与引用导航检查，不依赖浏览器或外部服务。
import assert from 'node:assert/strict';
import { renderAnswer, locationText, sourceLocationsText, answerTextForDisplay } from '../../app/web/answer-view.js';
class Element {
  constructor(tag, text = '') { this.tagName = tag; this.children = []; this.text = text; this.attributes = {}; }
  set textContent(text) { this.children = []; this.text = text; }
  get textContent() { return this.text + this.children.map(item => item.textContent).join(''); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = []; this.text = ''; this.append(...items); }
  setAttribute(key, value) { this.attributes[key] = value; }
  walk() { return [this, ...this.children.flatMap(item => item.walk())]; }
}
globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => new Element('#text', text) };
const root = new Element('div'); let opened = null;
const answer = { answer: '# 标题\n**关键事实** <img src="https://example.invalid/track" onerror="alert(1)"> [1] [99]\n- [外链](javascript:alert(1))',
  citations: [{ reference_id: 1, quote: '<script>bad()</script>', sources: [{ format: 'docx', section_path: '第一章' }] }],
  limitations: ['<iframe>仅为文本</iframe>'], clarification_questions: ['需要哪个期间？'],
  answer_parts: [{ state: 'partial', text: '覆盖测试', missing_detail: '缺少后半段' }],
  quality_warnings: [{ message: '<svg>资料提示</svg>' }],
  conclusion: [{ claim_type: 'inference' }], explanation: [] };
renderAnswer(root, answer, citation => { opened = citation.reference_id; });
assert.equal(root.walk().some(item => ['img', 'script', 'iframe', 'svg', 'a'].includes(item.tagName)), false);
assert.match(root.textContent, /<img/); assert.match(root.textContent, /\[99\]/);
const reference = root.walk().find(item => item.tagName === 'button' && item.className === 'reference');
reference.onclick(); assert.equal(opened, 1);
assert.match(root.textContent, /需要哪个期间/); assert.match(root.textContent, /缺少后半段/);
assert.match(root.textContent, /推断或待复核/); assert.match(root.textContent, /语义正确是不同检查/);
const disclosures=root.walk().filter(item=>item.tagName==='details');
assert.ok(disclosures.length>=2);
assert.ok(disclosures.every(item=>!item.open));
const notes=root.walk().find(item=>item.className==='answer-details');
assert.match(notes.textContent,/适用范围与限制/);assert.match(notes.textContent,/资料使用提示/);
assert.equal(root.children.some(item=>['answer-note','review-note'].includes(item.className)),true); // 需要用户补充的条件仍可直接看见。
const repeatedSources=[{format:'pdf',page:1,bbox:[0,0,1,1]},{format:'pdf',page:1,bbox:[1,1,2,2]},{format:'pdf',page:2}];
assert.equal(sourceLocationsText(repeatedSources),'第 1 页 / 第 2 页');
assert.equal(repeatedSources.length,3); // 展示去重不能删除实际坐标。
assert.equal(locationText({format:'pdf',page:2,pdf_page_label:{status:'footer_observed',printed_label:'19'}}),'PDF 第 2 页 · 印刷页码 19（页脚提取）');
assert.equal(locationText({format:'pdf',page:2,pdf_page_label:{status:'catalog_only',catalog_label:'iv'}}),'PDF 第 2 页 · PDF 标签 iv（印刷页码未核对）');
assert.equal(locationText({format:'pdf',page:2,pdf_page_label:{status:'conflict',catalog_label:'19',printed_label:'20'}}),'PDF 第 2 页');
const fact={text:'允许退款。',claim_type:'direct',context:[{kind:'condition',text:'签收后七天内'}],support_review:{status:'checks_passed'}};
const original='【原文直述】允许退款。（限定：签收后七天内）（证据规则检查通过；语义待复核）[1]';
assert.equal(answerTextForDisplay({answer:original,conclusion:[fact]}),'允许退款。（限定：签收后七天内）[1]');
assert.equal(original,'【原文直述】允许退款。（限定：签收后七天内）（证据规则检查通过；语义待复核）[1]');
assert.equal(answerTextForDisplay({answer:'文档原句包含（证据规则检查通过；语义待复核）',conclusion:[]}), '文档原句包含（证据规则检查通过；语义待复核）');
assert.equal(answerTextForDisplay({answer:'【结构推断】据此推断。',conclusion:[{text:'据此推断。',claim_type:'inference'}]}),'【结构推断】据此推断。');
assert.match(locationText({ format: 'docx', section_path: '说明' }), /无真实页码/);
assert.match(locationText({ format: 'xlsx', sheet_name: '统计', cell_range: 'A1:C3' }), /统计.*A1:C3/);
renderAnswer(root, { answer: '另一份回答', citations: [] }, () => {});
assert.equal(root.walk().some(item => item.tagName === 'button'), false);
assert.doesNotMatch(root.textContent, /关键事实/);
renderAnswer(root,{response_kind:'system',answer:'你好 <img src=x onerror=alert(1)>',citations:[]},()=>{});
assert.equal(root.children.length,1);
assert.equal(root.children[0].tagName,'p');
assert.equal(root.textContent,'你好 <img src=x onerror=alert(1)>');
assert.equal(root.walk().some(item=>['details','img','a'].includes(item.tagName)),false);
console.log('8 workspace rendering checks passed');
