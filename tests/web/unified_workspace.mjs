// 执行真实页面控制器，覆盖异步刷新后的资料选择和每轮提交范围；HTTP/SSE为合成边界。
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {node,renderAnswer,sourceLocationsText,answerTextForDisplay} from '../../app/web/answer-view.js';
import {createUnifiedClient} from '../../app/web/unified-client.js';

class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.attributes={};this.dataset={};this.style={};this.value='';this.text='';this.open=false;this.checked=false;this.disabled=false;this.hidden=false;this.scrollTop=0;this.scrollHeight=100;this.clientHeight=100;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.text='';this.children=[];this.append(...items);}
  set textContent(value){this.text=String(value);this.children=[];}
  get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  setAttribute(key,value){this.attributes[key]=value;}
  walk(){return [this,...this.children.flatMap(c=>c.walk())];}
}
const html=await readFile(new URL('../../app/web/unified-chat.html',import.meta.url),'utf8');
const script=(await readFile(new URL('../../app/web/unified-chat.js',import.meta.url),'utf8')).replace(/^import .*;\r?\n/gm,'');
const ids=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element()]));
ids.get('archived').checked=false;
const document={hidden:false,getElementById:id=>ids.get(id),createElement:tag=>new Element(tag),createTextNode:text=>{const el=new Element('#text');el.textContent=text;return el;},addEventListener(){},querySelectorAll:selector=>{
  assert.equal(selector,'#document-options input, #document-options button');
  return ids.get('document-options').walk().filter(el=>['input','button'].includes(el.tagName));
}};
globalThis.document=document;
const base={id:'base',name:'合成知识库',revision:1,active_snapshot_id:'v1',documents:[{id:'one',filename:'第一份.txt',ready:true},{id:'two',filename:'第二份.txt',ready:true},{id:'pending',filename:'准备中的资料.txt',ready:false}]};
let detail={id:'k:one',base_id:'base',title:'长问题'.repeat(400),memory_enabled:true,memory_notes:'',revision:0,archived:false,document_ids:['one'],turns:[],submissions:[]};
const calls=[],storage=new Map();
let holdRefresh=null;
const api=async(path,options={})=>{
  calls.push({path,method:options.method||'GET',body:options.body?JSON.parse(options.body):null});
  if(path==='/api/chat/scopes'){if(holdRefresh)await holdRefresh;return [structuredClone(base)];}
  if(path.startsWith('/api/chat/conversations?'))return [{id:detail.id,title:detail.title}];
  if(path==='/api/ingestions?base_id=base')return [];
  if(path.endsWith('/turns'))return {id:'submission'};
  if(path.startsWith('/api/chat/conversations/'))return structuredClone(detail);
  throw new Error('Unexpected path '+path);
};
const context={document,node,renderAnswer,sourceLocationsText,answerTextForDisplay,createUnifiedClient,
  createApiClient:()=>api,encoded:encodeURIComponent,URLSearchParams,location:{search:'?chat=k%3Aone'},
  createAnswerStream:()=>({revision:0,watch(){},render(){},pause(){},dispose(){}}),
  window:{docqaAuthReady:Promise.resolve({user:{id:'synthetic',role:'customer'}}),addEventListener(){}},
  sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
  setTimeout:()=>1,clearTimeout(){},navigator:{clipboard:{writeText:async()=>{}}},Option:class extends Element {constructor(text,value){super('option');this.textContent=text;this.value=value;}},
};
await vm.runInNewContext('(async()=>{'+script+'})()',context);
const inputs=()=>ids.get('document-options').walk().filter(el=>el.tagName==='input');
const allButton=()=>ids.get('document-options').walk().find(el=>el.tagName==='button');
assert.equal(inputs().length,3);
await ids.get('chat-refresh').onclick();
assert(inputs().every(el=>!el.disabled),'异步刷新结束后必须重新启用资料选择');
assert.equal(allButton().disabled,false);
assert.equal(calls.filter(c=>c.method==='POST').length,0,'刷新与选择资料不能提交问题');

inputs()[1].checked=true;inputs()[1].onchange();
assert.equal(ids.get('scope-caption').textContent,'已选择 2 份资料');
ids.get('kb-input').value='只按所选文档回答';
ids.get('kb-form').onsubmit({preventDefault(){}});
for(let n=0;n<60;n++)await Promise.resolve();
assert.deepEqual(calls.find(c=>c.path.endsWith('/turns')).body.document_ids,['one','two']);
await allButton().onclick();
assert.deepEqual(inputs().map(el=>el.checked),[true,true,false],'全选必须勾上所有可用资料，准备中的资料不属于可用范围');
assert(inputs().every(el=>!el.disabled));
assert.equal(ids.get('scope-caption').textContent,'全部可用资料（2 份）');

inputs()[0].checked=false;inputs()[0].onchange();
assert.deepEqual(inputs().map(el=>el.checked),[false,true,false]);
assert.equal(ids.get('scope-caption').textContent,'已选择 1 份资料');
assert.equal(ids.get('kb-send').disabled,false);
inputs()[1].checked=false;inputs()[1].onchange();
assert.equal(ids.get('scope-caption').textContent,'未选择资料');
assert.equal(ids.get('kb-send').disabled,true,'取消全部勾选不能回退到全库检索');
ids.get('kb-input').value='空选不能发送';
ids.get('kb-form').onsubmit({preventDefault(){}});
for(let n=0;n<60;n++)await Promise.resolve();
assert.equal(calls.filter(c=>c.method==='POST').length,1,'空选不得提交为全库问题');
await ids.get('chat-refresh').onclick();
assert.equal(ids.get('scope-caption').textContent,'未选择资料','刷新不能将空选变成全库');

await allButton().onclick();
ids.get('kb-input').value='全部可用资料';
ids.get('kb-form').onsubmit({preventDefault(){}});
for(let n=0;n<60;n++)await Promise.resolve();
assert.deepEqual(calls.filter(c=>c.path.endsWith('/turns'))[1].body.document_ids,[],'保留服务端全部可用快照的兼容协议');

inputs()[2].checked=true;inputs()[2].onchange();
assert.equal(ids.get('scope-caption').textContent,'已选择 3 份资料');
await allButton().onclick();
assert.deepEqual(inputs().map(el=>el.checked),[true,true,false],'全选可用资料必须去掉待处理资料');
base.documents.forEach(d=>d.ready=false);
await ids.get('chat-refresh').onclick();
assert(inputs().every(el=>!el.checked));
assert.equal(ids.get('scope-caption').textContent,'暂无可用资料');
assert.equal(allButton().disabled,true);
assert.equal(ids.get('kb-send').disabled,true);
inputs()[2].checked=true;inputs()[2].onchange();
assert.equal(ids.get('kb-send').disabled,false,'显式选择准备中的资料仍可提交等待问题');
base.documents[0].ready=true;base.documents[1].ready=true;
await ids.get('chat-refresh').onclick();
await allButton().onclick();

let release;holdRefresh=new Promise(resolve=>{release=resolve;});
const refreshing=ids.get('chat-refresh').onclick();
assert(inputs().every(el=>el.disabled));assert.equal(allButton().disabled,true);
release();await refreshing;holdRefresh=null;
assert(inputs().every(el=>!el.disabled));
detail.turns=[{id:'task',source:'knowledge',status:'running',stage:'retrieving'}];
await ids.get('chat-refresh').onclick();
assert(inputs().every(el=>el.disabled));assert.equal(allButton().disabled,true);
detail.turns=[];detail.submissions=[{id:'waiting',status:'waiting',question:'等待资料'}];
await ids.get('chat-refresh').onclick();assert(inputs().every(el=>el.disabled));
detail.submissions=[];await ids.get('chat-refresh').onclick();assert(inputs().every(el=>!el.disabled));

assert.equal(ids.get('chat-title-details').open,false);
assert.equal(ids.get('chat-title-full').textContent,detail.title);
ids.get('chat-title-details').open=true;
await ids.get('chat-refresh').onclick();assert.equal(ids.get('chat-title-details').open,true,'同一会话刷新应保留展开状态');
detail={...detail,id:'k:two',title:'另一段 <img src=x> 长标题'};
await ids.get('kb-history').children[0].onclick();
assert.equal(ids.get('chat-title-details').open,false,'切换会话默认收起标题');
assert.equal(ids.get('chat-title-full').textContent,detail.title);
assert.equal(calls.filter(c=>c.method==='POST').length,2);
detail={...detail,id:'k:all',document_ids:[]};
await ids.get('kb-history').children[0].onclick();
assert.deepEqual(inputs().map(el=>el.checked),[true,true,false],'历史全库范围恢复后仍应展示实际全选');
console.log('unified workspace: visible all-ready selection, explicit empty selection guard, exact selected/all POST, preparing/empty scopes, refresh and busy/running/waiting locks, long title disclosure and conversation switching passed');
