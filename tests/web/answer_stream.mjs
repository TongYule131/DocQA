import assert from 'node:assert/strict';
import { createAnswerStream } from '../../app/web/answer-stream.js';
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.text='';}
  set textContent(value){this.text=value;this.children=[];}get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  append(...items){for(const item of items){item.remove?.();item.parentNode=this;this.children.push(item);}}walk(){return [this,...this.children.flatMap(c=>c.walk())];}
  remove(){if(this.parentNode){this.parentNode.children=this.parentNode.children.filter(c=>c!==this);this.parentNode=null;}}
  replaceChildren(...items){for(const child of this.children)child.parentNode=null;this.children=[];this.text='';this.append(...items);}
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

const general=createAnswerStream({eventSource:url=>new Source(url)});general.watch([running]);
const generalSource=connections.at(-1);
generalSource.fire('update',{id:1,attempt:1,kind:'reset',payload:{version:'general-answer-stream-v1'}});
generalSource.fire('update',{id:2,attempt:1,kind:'block',payload:{version:'general-answer-stream-v1',draft:true,validation_scope:'general_text',text_delta:'普通 <img src=x> [1]'}});
root=new Element('div');general.render(root,running,()=>{throw Error('general has no citations');});
assert.match(root.textContent,/通用回答/);assert.match(root.textContent,/<img src=x> \[1\]/);
assert(!root.walk().some(e=>['img','script','button'].includes(e.tagName)));
root=new Element('div');general.render(root,failed,()=>{});assert.match(root.textContent,/未完成/);
generalSource.fire('denied',{status:401});root=new Element('div');general.render(root,running,()=>{});assert.equal(root.children.length,0);
general.dispose();
console.log('general stream drafts are plain text, separate from cited facts, and disappear after denial');

// 已收到的普通草稿分帧绘制；后续片段保留同一DOM，不逐帧重绘历史。
const frames=new Map();let frameId=0,outerPaints=0,scrollPaints=0,stamp=0;
const requestFrame=callback=>{frames.set(++frameId,callback);return frameId;};
const cancelFrame=id=>frames.delete(id);
function tick(){stamp+=100;const pending=[...frames];frames.clear();for(const [,callback] of pending)callback();}
const animatedRoot=new Element('div');
let animatedJob=running;
const animated=createAnswerStream({eventSource:url=>new Source(url),frame:requestFrame,cancelFrame,now:()=>stamp,reducedMotion:()=>false,
  changed:()=>{outerPaints++;animated.render(animatedRoot,animatedJob,()=>{});},beforePaint:()=>true,painted:follow=>{assert.equal(follow,true);scrollPaints++;}});
animated.watch([running]);const animatedSource=connections.at(-1);
animatedSource.fire('update',{id:1,attempt:1,kind:'reset',payload:{}});
const draft=text=>({version:'general-answer-stream-v1',draft:true,validation_scope:'general_text',text_delta:text});
const initial='普通草稿😀与转义文本 <img src=x>。'.repeat(3);
animatedSource.fire('update',{id:2,attempt:1,kind:'block',payload:draft(initial)});
const section=animatedRoot.children[0],textElement=section.walk().find(e=>e.className==='general-stream-text');
assert.equal(textElement.textContent,'');tick();
assert(textElement.textContent.length>0&&textElement.textContent.length<initial.length);
assert(!/[\uD800-\uDBFF]$/.test(textElement.textContent));
const paintsAfterFirst=outerPaints,revisionAfterFirst=animated.revision;
animatedSource.fire('update',{id:3,attempt:1,kind:'block',payload:draft('末尾。')});
assert.equal(outerPaints,paintsAfterFirst);assert.equal(animated.revision,revisionAfterFirst);
assert.equal(animatedRoot.children[0],section);assert.equal(section.walk().find(e=>e.className==='general-stream-text'),textElement);
for(let i=0;frames.size&&i<100;i++)tick();
assert.equal(textElement.textContent,initial+'末尾。');assert(scrollPaints>2);assert.equal(section.walk().some(e=>e.tagName==='img'),false);
animatedSource.fire('update',{id:4,attempt:1,kind:'block',payload:draft('成功前尾文。')});
animatedSource.fire('state',{status:'succeeded',attempt:1});
animatedJob={...running,status:'succeeded',result:{response_kind:'general',answer:initial+'末尾。成功前尾文。'}};
animated.watch([animatedJob]);animated.render(animatedRoot,animatedJob,()=>{});
assert(animated.presenting(animatedJob));assert.notEqual(textElement.textContent,animatedJob.result.answer);
assert.match(section.textContent,/已生成，正在显示/);assert(section.walk().some(e=>e.tagName==='button'&&e.textContent==='立即显示全文'));
for(let i=0;frames.size&&i<100;i++)tick();
assert.equal(textElement.textContent,animatedJob.result.answer);assert(!section.textContent.includes('未完成'));assert.equal(frames.size,0);assert(!animated.presenting(animatedJob));
animated.dispose();

const deniedRoot=new Element('div');
const deniedAnimation=createAnswerStream({eventSource:url=>new Source(url),frame:requestFrame,cancelFrame,now:()=>stamp,reducedMotion:()=>false,
  changed:()=>deniedAnimation.render(deniedRoot,running,()=>{})});
deniedAnimation.watch([running]);const deniedSource=connections.at(-1);
deniedSource.fire('update',{id:1,attempt:1,kind:'reset',payload:{}});
deniedSource.fire('update',{id:2,attempt:1,kind:'block',payload:draft('不能在撤权后继续显示的草稿')});
assert(frames.size>0);deniedSource.fire('denied',{status:403});tick();assert.equal(deniedRoot.textContent,'');assert.equal(frames.size,0);
deniedAnimation.dispose();

const resetRoot=new Element('div');
const retryAnimation=createAnswerStream({eventSource:url=>new Source(url),frame:requestFrame,cancelFrame,reducedMotion:()=>true,
  changed:()=>retryAnimation.render(resetRoot,running,()=>{})});
retryAnimation.watch([running]);const retrySource=connections.at(-1);
retrySource.fire('update',{id:1,attempt:1,kind:'reset',payload:{}});
retrySource.fire('update',{id:2,attempt:1,kind:'block',payload:draft('减少动态效果时直接显示已收到正文')});
assert.match(resetRoot.textContent,/直接显示已收到正文/);assert.equal(frames.size,0);
retrySource.fire('state',{status:'running',attempt:2});assert.equal(resetRoot.textContent,'');
retrySource.fire('update',{id:3,attempt:1,kind:'reset',payload:{}});
retrySource.fire('update',{id:4,attempt:1,kind:'block',payload:draft('旧轮回放不能使attempt倒退')});assert.equal(resetRoot.textContent,'');
retrySource.fire('update',{id:5,attempt:2,kind:'reset',payload:{}});
retrySource.fire('update',{id:6,attempt:2,kind:'block',payload:draft('只显示新轮草稿')});assert.match(resetRoot.textContent,/只显示新轮草稿/);
retryAnimation.dispose();
console.log('frame delivery preserves text nodes, Unicode, tail text, reduced motion, attempt reset and revocation');

// 可见的桌面宿主也可能不派发rAF；64ms兜底仍更新收到的文本，撤权取消两条调度。
const savedRaf=globalThis.requestAnimationFrame,savedCancel=globalThis.cancelAnimationFrame;
let dormantRaf=0;
globalThis.requestAnimationFrame=()=>++dormantRaf;globalThis.cancelAnimationFrame=()=>{};
const fallbackRoot=new Element('div');
const fallback=createAnswerStream({eventSource:url=>new Source(url),reducedMotion:()=>false,
  changed:()=>fallback.render(fallbackRoot,running,()=>{})});
fallback.watch([running]);const fallbackSource=connections.at(-1);
fallbackSource.fire('update',{id:1,attempt:1,kind:'reset',payload:{}});
fallbackSource.fire('update',{id:2,attempt:1,kind:'block',payload:draft('宿主节流时只显示已收到的文字。'.repeat(6))});
const fallbackText=fallbackRoot.walk().find(e=>e.className==='general-stream-text');
assert.equal(fallbackText.textContent,'');await new Promise(resolve=>setTimeout(resolve,90));
assert(fallbackText.textContent.length>0);fallbackSource.fire('denied',{status:403});
await new Promise(resolve=>setTimeout(resolve,90));assert.equal(fallbackRoot.textContent,'');fallback.dispose();
if(savedRaf===undefined)delete globalThis.requestAnimationFrame;else globalThis.requestAnimationFrame=savedRaf;
if(savedCancel===undefined)delete globalThis.cancelAnimationFrame;else globalThis.cancelAnimationFrame=savedCancel;
console.log('throttled host frames fall back without retaining revoked drafts');

// 所有数据、完成事件和最终GET同时返回，仍要显示可见进度；历史和手动全文均不延迟。
const burstRoot=new Element('div');let burstJob={...running,id:'burst'},burstDone=0;
const burst=createAnswerStream({eventSource:url=>new Source(url),frame:requestFrame,cancelFrame,now:()=>stamp,reducedMotion:()=>false,
  changed:()=>burst.render(burstRoot,burstJob,()=>{}),finished:()=>burstDone++});
burst.watch([burstJob]);const burstSource=connections.at(-1),burstText='这条三十五字左右的合成回答😀需要连续展开，不能在完成事件后直接跳成全文。';
burstSource.fire('update',{id:1,attempt:1,kind:'reset',payload:{}});
burstSource.fire('update',{id:2,attempt:1,kind:'block',payload:draft(burstText)});
burstSource.fire('state',{status:'succeeded',attempt:1});
burstJob={...burstJob,status:'succeeded',result:{response_kind:'general',answer:burstText}};
burst.watch([burstJob]);burst.render(burstRoot,burstJob,()=>{});
assert.equal(burstDone,1);assert(burst.presenting(burstJob));tick();tick();
const burstParagraph=burstRoot.walk().find(e=>e.className==='general-stream-text');
assert(burstParagraph.textContent.length>0&&burstParagraph.textContent.length<burstText.length);
assert(!/[\uD800-\uDBFF]$/.test(burstParagraph.textContent));
const networkBeforeSkip=connections.length;burstRoot.walk().find(e=>e.tagName==='button'&&e.textContent==='立即显示全文').onclick();
assert.equal(burstParagraph.textContent,burstText);assert.equal(connections.length,networkBeforeSkip);assert(!burst.presenting(burstJob));burst.dispose();
const history=createAnswerStream({eventSource:url=>new Source(url),frame:requestFrame,cancelFrame,reducedMotion:()=>false});
history.watch([burstJob]);assert(!history.presenting(burstJob));assert.equal(connections.length,networkBeforeSkip);history.dispose();

const fastRoot=new Element('div');
const fast=createAnswerStream({eventSource:url=>new Source(url),frame:requestFrame,cancelFrame,now:()=>stamp,reducedMotion:()=>false,
  changed:()=>fast.render(fastRoot,burstJob,()=>{})});
fast.track('burst');fast.watch([burstJob]);fast.render(fastRoot,burstJob,()=>{});
assert(fast.presenting(burstJob));assert.equal(connections.length,networkBeforeSkip);fast.showAll('burst');fast.dispose();

// 文档快批次同样逐步显示；完整限定与引用先保留，最终结果到达不能跳过队列。
const docRoot=new Element('div');let docJob={...running,id:'document-burst'};
const documentBurst=createAnswerStream({eventSource:url=>new Source(url),frame:requestFrame,cancelFrame,now:()=>stamp,reducedMotion:()=>false,
  changed:()=>documentBurst.render(docRoot,docJob,()=>{})});
documentBurst.watch([docJob]);const docSource=connections.at(-1);
const docFact={...fact,text:'只有出示凭证并通过核验后才可受理。'.repeat(40),context:[{kind:'condition',text:'必须先核验凭证'}],refs:[1]};
docSource.fire('update',{id:1,attempt:1,kind:'reset',payload:{}});
docSource.fire('update',{id:2,attempt:1,kind:'block',payload:{...block,fact:docFact}});
docSource.fire('state',{status:'succeeded',attempt:1});
docJob={...docJob,status:'succeeded',result:{status:'partial',conclusion:[docFact],explanation:[],citations:block.citations,missing_points:['缺少比较另一方的证据']}};
documentBurst.watch([docJob]);documentBurst.render(docRoot,docJob,()=>{});tick();
const article=docRoot.walk().find(e=>e.className==='stream-fact'),docParagraph=article.walk().find(e=>e.tagName==='p');
assert(docParagraph.textContent.length>0&&docParagraph.textContent.length<docFact.text.length);assert.match(article.textContent,/必须先核验凭证/);
assert.match(docRoot.textContent,/缺少比较另一方的证据/);
assert(article.walk().some(e=>e.tagName==='button'&&e.textContent==='1'));assert(documentBurst.presenting(docJob));
for(let i=0;frames.size&&i<70;i++)tick();
assert.equal(docParagraph.textContent,docFact.text);assert(!documentBurst.presenting(docJob));documentBurst.dispose();
console.log('burst completion, fresh fast results, cited text, full-text skip and historical reads preserve display pacing without new requests');
