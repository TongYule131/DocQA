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
requests.length=0;
const tool={id:'clarify-tool',kind:'tool_parameters',state:'resolving',needed_count:2,
  request_key:'tool-persisted',confirmed_names:['商品，A','商品，A'],fields:[{name:'sku',label:'商品编号'},{name:'warehouse',label:'仓库编号'}]};
for(let i=0;i<2;i++){
 const form=clarificationForm({result:{clarification:tool}},{conversationId:'k:conversation',api,run,refresh});
 const inputs=form.walk().filter(e=>e.tagName==='input');assert.equal(inputs.length,2);
 assert(inputs.every(e=>e.value==='商品，A'&&e.readOnly));
 await form.onsubmit({preventDefault(){}});
}
assert.deepEqual(requests.map(r=>JSON.parse(r.body).request_key),['tool-persisted','tool-persisted']);
assert.deepEqual(JSON.parse(requests[0].body).names,['商品，A','商品，A']);
console.log('PASS clarification UI: persisted confirmation survives refresh, stable request identity, no duplicate scope');
