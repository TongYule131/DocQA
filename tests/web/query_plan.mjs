// 执行后台测试台控制器，验证展示实际策略/预算而不是把规则分值显示成语义概率。
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {node} from '../../app/web/answer-view.js';

class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.text='';this.value='';}
  append(...items){this.children.push(...items);}
  before(){}
  setAttribute(name,value){this[name]=value;}
  replaceChildren(...items){this.text='';this.children=[];this.append(...items);}
  set textContent(value){this.text=String(value);this.children=[];}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  walk(){return [this,...this.children.flatMap(c=>c.walk())];}
}
const document={getElementById:()=>new Element(),createElement:tag=>new Element(tag)};
globalThis.document=document;
const calls=[];
const api=async(path,options)=>{
  calls.push({path,options});assert.equal(path,'/api/admin/intents/test');
  assert.equal(options.method,'POST');
  return {results:[{question:'核对公式与正文是否一致',decision:{code:'knowledge-default',name:'知识检索',kind:'KB',method:'fallback',reason:'自动任务策略',candidates:[],configuration:'signature',version:'intent-rules-v2'},
    query_plan:{task_type:'formula_check',name:'公式与正文核对',effects:['同时保留公式及解释正文'],required_evidence:['原始公式及不等式','解释正文']},
    retrieval_budget:{recall_limit:8,candidate_limit:64,context_limit:12,context_chars:24000},availability:{ready:false,reason:'请先选择获授权的知识库'}}]};
};
const source=(await readFile(new URL('../../app/web/intent-test.js',import.meta.url),'utf8')).replace(/^import .*;\r?\n/gm,'').replace('export function','function');
const view=vm.runInNewContext(source+'\ninstallIntentTest()', {document,node,createApiClient:()=>api});
const fields=view.section.walk(),form=fields.find(e=>e.tagName==='form');
fields.find(e=>e.id==='intent-test-questions').value='核对公式与正文是否一致';
fields.find(e=>e.id==='test-scope').value='knowledge';
await form.onsubmit({preventDefault(){}});
assert.equal(calls.length,1);
assert.equal(JSON.parse(calls[0].options.body).questions.length,1);
assert(view.section.textContent.includes('公式与正文核对'));
assert(view.section.textContent.includes('候选 64 / 最终最多 12 块'));
assert(view.section.textContent.includes('请先选择获授权的知识库'));
assert(view.section.textContent.includes('模型／工具请求 0 次'));
assert(!view.section.textContent.includes('100%'));
console.log('PASS query plan UI: actual task strategy, evidence needs, budget, unavailable scope, zero supplier calls');
