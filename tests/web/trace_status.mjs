// 运行真实链路页面控制器：列表、总状态与节点必须同时保留可读状态及颜色身份。
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {node,locationText} from '../../app/web/answer-view.js';

class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.text='';this.value='';this.dataset={};this.style={setProperty(){}};this.classList={add(){}};}
  append(...items){this.children.push(...items);}
  addEventListener(){}
  replaceChildren(...items){this.text='';this.children=[];this.append(...items);}
  set textContent(value){this.text=String(value);this.children=[];}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  walk(){return [this,...this.children.flatMap(c=>c.walk())];}
}
const html=await readFile(new URL('../../app/web/knowledge-admin.html',import.meta.url),'utf8');
const script=(await readFile(new URL('../../app/web/knowledge-admin.js',import.meta.url),'utf8')).replace(/^import .*;\r?\n/gm,'');
const ids=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element()]));
const document={getElementById:id=>ids.get(id),createElement:tag=>new Element(tag),querySelectorAll:()=>[],querySelector:()=>new Element()};
globalThis.document=document;
const calls=[];
const api=async(path,options={})=>{
  assert(!options.method || options.method==='GET');calls.push(path);
  if(path.startsWith('/api/admin/traces?'))return {items:[{id:'failed',name:'合成失败',status:'failed'},{id:'ok',name:'合成成功',status:'succeeded'}],total:2,stats:{succeeded:1,failed:1,running:0,success_rate:50},window:'合成窗口'};
  if(path==='/api/admin/traces/failed')return {id:'failed',name:'合成失败',status:'failed',error_code:'output_numeric_context_missing',elapsed_ms:30,
    retrieval_agent:{stop_reason:'no_new_evidence',admitted_count:4,steps:[{round:1,action:'read_source',state:'no_new_evidence',before:{visible_chars:30},after:{visible_chars:30},requests_before:1,requests_after:2}]},
    calls:[{sequence_no:1,role:'query_planning',delivery_status:'sent',usage:{total_tokens:50},elapsed_ms:10},
           {sequence_no:2,role:'generation',delivery_status:'uncertain',usage:null,elapsed_ms:20}],
    diagnostics:[{kind:'retrieval',attempt:'1',admitted_count:4,context_chars:1200,drop_count:0,math_evidence:{total:2,incomplete_count:1,blocked_count:1}},
      {kind:'validation',attempt:'1',reason:'numeric_context_missing',location:{category:'explanation',ordinal:1,numeric_count:2}}],spans:[
    {id:'one',name:'查询',status:'succeeded',kind:'EMBEDDING',offset_ms:0,elapsed_ms:10},
    {id:'two',name:'生成',status:'failed',kind:'GENERATION',offset_ms:10,elapsed_ms:20,error_code:'output_numeric_context_missing'},
    {id:'three',name:'未执行',status:'cancelled',kind:'OTHER',offset_ms:30,elapsed_ms:0},
  ],timing_note:'实际时序',content_policy:'仅元数据'};
  throw new Error(path);
};
const context={document,node,locationText,createApiClient:()=>api,encoded:encodeURIComponent,
  location:{pathname:'/admin/traces',search:'',href:'http://example.invalid/admin/traces'},
  window:{docqaAuthReady:Promise.resolve(),addEventListener(){}},URL,URLSearchParams,
  history:{replaceState(){}},setTimeout:()=>1,clearTimeout(){},console};
await vm.runInNewContext('(async()=>{'+script+'})()',context);
const rows=ids.get('trace-rows').children;
assert.equal(rows[0].dataset.status,'failed');assert.equal(rows[1].dataset.status,'succeeded');
assert.equal(rows[0].children[4].textContent,'失败');assert.equal(rows[1].children[4].textContent,'成功');
assert(rows[0].children[4].children[0].className.includes('trace-status failed'));
await rows[0].children.at(-1).children[0].onclick();
const detail=ids.get('trace-detail').walk();
assert(detail.some(n=>n.className==='trace-error-summary' && n.textContent.includes('output_numeric_context_missing')));
const nodeRows=detail.filter(n=>n.className==='trace-row');
assert.deepEqual(nodeRows.map(n=>n.dataset.status),['succeeded','failed','cancelled']);
assert.equal(nodeRows[1].children[0].textContent,'生成','节点名称不混入执行状态');
assert.equal(nodeRows[1].children[2].textContent,'失败');
assert.equal(nodeRows[2].children[2].textContent,'已取消');
assert(nodeRows.every(row=>row.children.length===5),'类型、状态、时间线及耗时各有独立单元格');
const diagnostic=detail.find(n=>n.tagName==='details' && n.className==='trace-diagnostics');
assert(diagnostic && !diagnostic.open,'诊断默认收起，不占用链路页面');
assert(diagnostic.textContent.includes('依据第 2 条校验失败'));
assert(diagnostic.textContent.includes('装入 4 项证据'));
assert(diagnostic.textContent.includes('数学证据组 2 个，未完整装入 1 个'));
assert(diagnostic.textContent.includes('不可读数学片段 1 个'));
assert(ids.get('trace-detail').textContent.includes('没有新增原文'));
assert(ids.get('trace-detail').textContent.includes('实报 50 token'));
assert(ids.get('trace-detail').textContent.includes('token用量未知'));
assert.equal(calls.length,2,'列表和详情只读，不调用模型或创建任务');
console.log('PASS trace status: list, summary, node labels, cancelled state and read-only requests');
