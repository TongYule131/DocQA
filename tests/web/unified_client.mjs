import assert from 'node:assert/strict';
import {createUnifiedClient} from '../../app/web/unified-client.js';
const data=new Map(),storage={getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)};
let created=null,submitted=null,failCreate=true,failTurn=true;const calls=[];let counter=0;
const api=async(path,options={})=>{calls.push([path,options.method||'GET']);const body=options.body?JSON.parse(options.body):null;
  if(path==='/api/chat/conversations'){created||={id:'k:one',request_key:body.request_key,memory_enabled:true,revision:0,turns:[],submissions:[]};if(failCreate){failCreate=false;throw new Error('lost create response');}return created;}
  if(path.endsWith('/turns')){submitted||={request_key:body.request_key};created.submissions=[submitted];if(failTurn){failTurn=false;throw new Error('lost turn response');}return submitted;}
  return created;
};
let client=createUnifiedClient({api,storage,key:'alice',uuid:()=>String(++counter)});
client.prepare({baseId:'private',documentIds:['doc'],question:'synthetic question'});
await assert.rejects(()=>client.confirm(),/lost create/);const saved=JSON.parse(data.get('alice'));
client=createUnifiedClient({api,storage,key:'alice'});calls.length=0;await client.recover([created]);assert(calls.every(c=>c[1]==='GET'));assert.equal(client.pending.conversationId,'k:one');
await assert.rejects(()=>client.confirm(),/lost turn/);assert.equal(client.pending.turnKey,saved.turnKey);
client=createUnifiedClient({api,storage,key:'alice'});calls.length=0;const detail=await client.recover([created]);assert.equal(detail.id,'k:one');assert.equal(client.pending,null);assert(calls.every(c=>c[1]==='GET'));
assert.equal(createUnifiedClient({api,storage,key:'bob'}).pending,null);
let posts=0;const broken=createUnifiedClient({api:async()=>posts++,storage:{getItem:()=>null,setItem:()=>{throw new Error('storage disabled');}},key:'broken'});assert.throws(()=>broken.prepare({baseId:'b',question:'q'}),/storage disabled/);assert.equal(posts,0);
console.log('unified client: lost create/turn recovery, read-only polling, isolation, storage failure passed');

const ordinaryCalls=[];
const ordinary=createUnifiedClient({api:async(path,options)=>{const body=JSON.parse(options.body);ordinaryCalls.push({path,body});return path.endsWith('/turns')?{task_id:'fresh-task'}:{id:'k:general'};},storage,key:'ordinary'});
ordinary.prepare({baseId:null,documentIds:[],resourceMode:'none',question:'通用问题'});await ordinary.confirm();
assert.equal(ordinaryCalls.length,2);assert.equal(ordinaryCalls[0].body.base_id,null);
assert(ordinaryCalls.every(c=>c.body.resource_mode==='none'));
assert.equal(ordinary.lastTaskId,'fresh-task');
console.log('ordinary conversation uses explicit none, never legacy empty-array all');
