import assert from 'node:assert/strict';
import { createAnswerStream } from '../../app/web/answer-stream.js';
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.text='';}
  set textContent(value){this.text=value;this.children=[];}get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  append(...items){this.children.push(...items);}walk(){return [this,...this.children.flatMap(c=>c.walk())];}
  setAttribute(name,value){this[name]=value;}
}
globalThis.document={createElement:tag=>new Element(tag)};
const connections=[],done=[];let paints=0;
class Source {
  constructor(url){this.url=url;this.events={};this.closed=false;connections.push(this);}
  addEventListener(name,callback){this.events[name]=callback;}close(){this.closed=true;}
  fire(name,data){this.events[name]?.({data:JSON.stringify(data)});}
}
const running={id:'private task',streaming:true,status:'running'},failed={...running,status:'failed'};
const manager=createAnswerStream({eventSource:url=>new Source(url),changed:()=>{paints++;manager.watch([running]);},finished:id=>done.push(id)});
manager.watch([running]);assert.equal(connections.length,1);assert.match(connections[0].url,/private%20task\/events\?after=0/);
const first=connections[0],fact={text:'合成 <img src=x onerror=alert(1)>',claim_type:'direct',context:[{kind:'condition',text:'保留包装'}]};
const block={version:'validated-fact-stream-v1',draft:true,validation_scope:'single_fact',fact,citations:[{reference_id:1,sources:[{format:'pdf',page:3}]}]};
first.fire('update',{id:1,attempt:1,kind:'reset',payload:{}});
first.fire('update',{id:2,attempt:1,kind:'block',payload:block});first.fire('update',{id:2,attempt:1,kind:'block',payload:block});
let root=new Element('div'),clicked=null;manager.render(root,running,c=>clicked=c);
assert.equal(root.walk().filter(e=>e.className==='stream-fact').length,1);assert.match(root.textContent,/生成中/);
assert.match(root.textContent,/限定：保留包装/);assert.equal(root.walk().some(e=>e.tagName==='img'),false);
root.walk().find(e=>e.tagName==='button').onclick();assert.equal(clicked.reference_id,1);
first.fire('state',{status:'failed',attempt:1});assert.equal(done.length,1);assert.equal(connections.length,1);assert.ok(first.closed);
// 旧的running DTO暂未刷新时，终止事件不能导致无限重连。
manager.watch([running]);assert.equal(connections.length,1);
root=new Element('div');manager.render(root,failed,()=>{});assert.match(root.textContent,/生成未完成/);
manager.watch([failed]);manager.watch([{...running,status:'queued'}]);assert.equal(connections.length,2);assert.match(connections[1].url,/after=2$/);
connections[1].fire('update',{id:3,attempt:2,kind:'reset',payload:{}});
root=new Element('div');manager.render(root,running,()=>{});assert.equal(root.children.length,0);
manager.pause();assert.ok(connections[1].closed);manager.pause(false);assert.equal(connections.length,3);
connections[2].fire('update',{id:4,attempt:2,kind:'block',payload:block});
connections[2].fire('denied',{status:403});root=new Element('div');manager.render(root,running,()=>{});assert.equal(root.children.length,0);
manager.watch([]);first.fire('update',{id:5,attempt:1,kind:'block',payload:block});assert.ok(paints>=4);
manager.dispose();
// v2仅组合已校验句，逐句引用仍可点开；旧版本忽略编排元数据。
const grouped=createAnswerStream({eventSource:url=>new Source(url)});
grouped.watch([running]);const stream=connections.at(-1);
stream.fire('update',{id:1,attempt:1,kind:'reset',payload:{}});
for(let i=0;i<2;i++)stream.fire('update',{id:i+2,attempt:1,kind:'block',payload:{...block,version:'validated-fact-stream-v2',presentation_block:{index:0,kind:'paragraph'}}});
root=new Element('div');grouped.render(root,running,()=>{});
assert.equal(root.walk().filter(e=>e.className==='stream-paragraph').length,1);
assert.equal(root.walk().filter(e=>e.className==='stream-fact').length,2);
assert.equal(root.walk().filter(e=>e.tagName==='button').length,2);
stream.fire('update',{id:4,attempt:1,kind:'block',payload:{...block,presentation_block:{index:0,kind:'paragraph'}}});
stream.fire('update',{id:5,attempt:1,kind:'block',payload:{...block,version:'validated-fact-stream-v2',presentation_block:{index:999,kind:'paragraph'}}});
root=new Element('div');grouped.render(root,running,()=>{});
assert.equal(root.walk().filter(e=>e.className==='stream-paragraph').length,1);
assert.equal(root.walk().filter(e=>e.className==='stream-fact'&&e.tagName==='div').length,2);
grouped.dispose();
// OCR排版空白不能令已覆盖限定重现；未覆盖的否定仍须显示，逐条引用保持可点。
const polished=createAnswerStream({eventSource:url=>new Source(url)});polished.watch([running]);const polishedSource=connections.at(-1);
polishedSource.fire('update',{id:1,attempt:1,kind:'reset',payload:{}});
polishedSource.fire('update',{id:2,attempt:1,kind:'block',payload:{...block,
  fact:{text:'61.8%的教师存在迁移障碍。',claim_type:'direct',context:[{kind:'scope',text:' 61.8% 的教师存在迁移障碍 '},{kind:'condition',text:'仅限样本，未经审计'}]}}});
root=new Element('div');polished.render(root,running,()=>{});
assert.equal((root.textContent.match(/61\.8/g)||[]).length,1);assert.match(root.textContent,/仅限样本，未经审计/);
const reference=root.walk().find(e=>e.tagName==='button');assert.equal(reference.textContent,'1');assert.match(reference['aria-label'],/引用 1.*第 3 页/);assert.equal(reference.title,'第 3 页');
polished.dispose();
console.log('12 answer stream client scenarios passed');
