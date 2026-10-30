// 只在显式发送/确认时POST；恢复和轮询只GET，同一用户独立保存未确认的请求键。
export function createUnifiedClient({api,storage,key,uuid=()=>crypto.randomUUID()}) {
  let pending=null,lastTaskId=null;
  try{pending=JSON.parse(storage.getItem(key)||'null');}catch{}
  function save(value){if(value)storage.setItem(key,JSON.stringify(value));else storage.removeItem(key);pending=value;}
  const json=(body)=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return {
    get pending(){return pending;},
    get lastTaskId(){return lastTaskId;},
    prepare({conversationId=null,baseId,documentIds=[],resourceMode=null,question,memoryEnabled=true}){
      if(pending)throw new Error('请先确认上次提交');
      save({conversationId,baseId,documentIds,resourceMode,question,memoryEnabled,createKey:uuid(),turnKey:uuid()});
    },
    async confirm(){
      if(!pending)return null;
      if(!pending.conversationId){const c=await api('/api/chat/conversations',json({base_id:pending.baseId,document_ids:pending.documentIds,request_key:pending.createKey,...(pending.resourceMode?{resource_mode:pending.resourceMode}:{})}));save({...pending,conversationId:c.id});}
      const id=pending.conversationId;
      if(!pending.memoryEnabled){const c=await api('/api/chat/conversations/'+encodeURIComponent(id));if(c.memory_enabled&&!c.turns.length)await api('/api/chat/conversations/'+encodeURIComponent(id),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:c.revision,memory_enabled:false})});}
      const submitted=await api('/api/chat/conversations/'+encodeURIComponent(id)+'/turns',json({question:pending.question,document_ids:pending.documentIds,request_key:pending.turnKey,...(pending.resourceMode?{resource_mode:pending.resourceMode}:{})}));
      lastTaskId=typeof submitted?.task_id==='string'?submitted.task_id:null;
      save(null);return id;
    },
    async recover(history){
      if(!pending)return null;
      if(!pending.conversationId){const c=history.find(c=>c.request_key===pending.createKey);if(c)save({...pending,conversationId:c.id});}
      if(!pending.conversationId)return null;
      const c=await api('/api/chat/conversations/'+encodeURIComponent(pending.conversationId));
      if(c.submissions.some(s=>s.request_key===pending.turnKey))save(null);
      return c;
    }
  };
}
