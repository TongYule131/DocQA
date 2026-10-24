// 运行真实反馈组件：默认不分享、原键确认、撤回及文本安全；不连接服务或模型。
import assert from 'node:assert/strict';
import {feedbackButton,showMyFeedback} from '../../app/web/feedback-ui.js';
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.attributes={};this.checked=false;this.disabled=false;this.value='';this.open=false;}
  set textContent(value){this.text=String(value);this.children=[];}
  get textContent(){return (this.text||'')+this.children.map(c=>c.textContent).join('');}
  append(...items){this.children.push(...items);}
  setAttribute(key,value){this.attributes[key]=value;}
  showModal(){this.open=true;}
  close(){this.open=false;}
  remove(){this.removed=true;}
  walk(){return [this,...this.children.flatMap(c=>c.walk())];}
}
globalThis.document={createElement:tag=>new Element(tag),body:new Element('body')};
const requests=[],errors=[];let failOnce=true;
const api=async(path,options={})=>{requests.push({path,...options});if(options.method==='POST'&&failOnce){failOnce=false;throw new Error('synthetic lost response');}if(path==='/api/feedback'&&!options.method)return [{id:'own',category:'expression',status:'open',comment:'<img src=x onerror=alert(1)>',share_context:true}];return {id:'saved'};};
const run=async work=>{try{await work();}catch(e){errors.push(e.message);}};
const button=feedbackButton({id:'task',source:'knowledge'},{api,run,notice:()=>{}});button.onclick();
const dialog=document.body.children[0],controls=dialog.walk(),share=controls.find(e=>e.type==='checkbox');
const submit=controls.find(e=>e.tagName==='button'&&e.textContent==='提交反馈');
assert.equal(share.checked,false);await submit.onclick();assert(dialog.open);assert.equal(submit.disabled,false);
await submit.onclick();const submitted=requests.filter(r=>r.method==='POST').map(r=>JSON.parse(r.body));
assert.equal(submitted[0].request_key,submitted[1].request_key);assert.equal(submitted[1].share_context,false);
assert.equal(dialog.removed,true);assert.deepEqual(errors,['synthetic lost response']);
await showMyFeedback(api);const own=document.body.children.at(-1),revoke=own.walk().find(e=>e.tagName==='button'&&e.textContent==='撤回正文分享');
assert(own.textContent.includes('<img src=x onerror=alert(1)>'));assert.equal(own.walk().some(e=>e.tagName==='img'),false);
await revoke.onclick();assert.equal(revoke.disabled,true);assert(requests.some(r=>r.method==='DELETE'&&r.path==='/api/feedback/own/shared-context'));
console.log('feedback UI: private default, stable request key, explicit retry, text rendering and consent revocation passed');
