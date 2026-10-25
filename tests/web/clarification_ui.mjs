import assert from 'node:assert/strict';
import {clarificationForm} from '../../app/web/clarification-ui.js';
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.value='';}
 set textContent(value){this.text=String(value);}
 append(...items){this.children.push(...items);}
 setAttribute(key,value){this[key]=value;}
 walk(){return [this,...this.children.flatMap(c=>c.walk())];}
}
globalThis.document={createElement:tag=>new Element(tag)};
const requests=[],api=async(path,options)=>{requests.push({path,...options});},run=work=>work(),refresh=async()=>{};
const value={id:'clarify-task',state:'resolving',needed_count:2,request_key:'persisted-key',confirmed_names:['对象甲','对象乙']};
for(let i=0;i<2;i++){
 const form=clarificationForm({result:{clarification:value}},{conversationId:'k:conversation',api,run,refresh});
 assert(form);const input=form.walk().find(e=>e.tagName==='textarea');assert.equal(input.value,'对象甲\n对象乙');assert.equal(input.readOnly,true);
 form.onsubmit({preventDefault(){}});await new Promise(resolve=>setImmediate(resolve));
}
assert.deepEqual(requests.map(r=>JSON.parse(r.body).request_key),['persisted-key','persisted-key']);
assert.deepEqual(JSON.parse(requests[0].body).names,['对象甲','对象乙']);
assert.equal(clarificationForm({result:{clarification:{...value,state:'resolved'}}},{conversationId:'k:conversation',api,run,refresh}),null);
console.log('PASS clarification UI: persisted confirmation survives refresh, stable request identity, no duplicate scope');
