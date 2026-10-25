import { node, sourceLocationsText, contextCovered } from './answer-view.js';

// SSE只订阅已有任务。切换页面、重连和游标重放不发送生成POST。
export function createAnswerStream({source='question',changed=()=>{},finished=()=>{},
  eventSource=typeof EventSource==='undefined'?null:url=>new EventSource(url)}={}) {
  const entries=new Map();let revision=0,paused=false;
  const terminal=status=>!['queued','running'].includes(status);
  function update(){revision++;changed();}
  function open(entry) {
    if(!eventSource||paused||entry.done||entry.connection)return;
    const url=`/api/${source==='knowledge'?'kb-tasks':'question-jobs'}/${encodeURIComponent(entry.id)}/events?after=${entry.cursor}`;
    const connection=eventSource(url);entry.connection=connection;
    const current=()=>entries.get(entry.id)===entry&&entry.connection===connection;
    connection.addEventListener('update',event=>{
      if(!current())return;
      try {
        const item=JSON.parse(event.data);
        if(!Number.isSafeInteger(item.id)||item.id<=entry.cursor)return;
        entry.cursor=item.id;
        if(item.kind==='reset'){entry.blocks=[];entry.attempt=item.attempt;}
        else if(item.kind==='block'&&item.attempt===entry.attempt&&item.payload?.draft===true&&item.payload.validation_scope==='single_fact')entry.blocks.push(item.payload);
        entry.reconnecting=false;update();
      }catch{entry.invalid=true;connection.close();entry.done=true;update();}
    });
    connection.addEventListener('state',event=>{
      if(!current())return;
      try {
        const state=JSON.parse(event.data);entry.status=state.status;
        if(terminal(state.status)){connection.close();entry.connection=null;entry.done=true;finished(entry.id);}
        update();
      }catch{connection.close();entry.done=true;entry.invalid=true;update();}
    });
    connection.addEventListener('denied',()=>{
      if(!current())return;
      entry.blocks=[];entry.denied=true;entry.done=true;connection.close();entry.connection=null;update();finished(entry.id);
    });
    connection.onerror=()=>{if(current()){entry.reconnecting=true;update();}};
  }
  return {
    get revision(){return revision;},
    watch(jobs) {
      const keep=new Set();
      for(const job of jobs||[]) {
        if(!job?.streaming||job.result_id||job.result||job.status==='succeeded')continue;
        keep.add(job.id);
        let entry=entries.get(job.id);
        if(!entry){entry={id:job.id,cursor:0,attempt:0,blocks:[],status:job.status,lastJobStatus:job.status,connection:null,done:false};entries.set(job.id,entry);}
        // 显式重试沿用任务ID，新attempt必须重新订阅，旧草稿由服务端reset替换。
        if(entry.done&&terminal(entry.lastJobStatus)&&!terminal(job.status)){entry.done=false;entry.invalid=false;entry.denied=false;}
        entry.lastJobStatus=job.status;if(!entry.done)entry.status=job.status;open(entry);
      }
      for(const [id,entry] of entries)if(!keep.has(id)){entry.connection?.close();entries.delete(id);}
    },
    render(container,job,openCitation) {
      const entry=entries.get(job.id);if(!entry||entry.denied)return;
      if(!entry.blocks.length)return;
      const section=node('section','','stream-preview');
      const incomplete=terminal(job.status)||terminal(entry.status)||entry.invalid;
      section.append(node('p',incomplete?'生成未完成，以下片段不作为最终回答。':entry.reconnecting?'连接中断，正在恢复已保存片段…':'生成中 · 片段已核对原文，完成后统一复验。','stream-label'));
      const labels={subject:'对象',period:'期间',unit:'单位',scope:'范围',condition:'限定',role:'用途'};
      const paragraphs=new Map();
      for(const block of entry.blocks) {
        const plan=block.version==='validated-fact-stream-v2' ? block.presentation_block : null;
        const grouped=plan && Number.isSafeInteger(plan.index) && plan.index>=0 && plan.index<12 && plan.kind==='paragraph';
        let parent=section;
        if(grouped) {
          if(!paragraphs.has(plan.index)){const paragraph=node('div','','stream-paragraph');paragraphs.set(plan.index,paragraph);section.append(paragraph);}
          parent=paragraphs.get(plan.index);
        }
        const fact=block.fact,article=node(grouped?'span':'div','','stream-fact');
        const extra=(fact.context||[]).filter(c=>typeof c.text==='string'&&!contextCovered(c.text,fact.text)).map(c=>(labels[c.kind]||'条件')+'：'+c.text);
        article.append(node(grouped?'span':'p',(fact.claim_type==='inference'?'【结构推断】':'')+String(fact.text)+(extra.length?'（'+extra.join('；')+'）':'')));
        const sources=node(grouped?'span':'div','','stream-sources');
        for(const citation of block.citations||[]) {
          const location=sourceLocationsText(citation.sources)||'来源位置未提供';
          const b=node('button',String(citation.reference_id),'reference');b.type='button';
          b.setAttribute('aria-label',`查看引用 ${citation.reference_id}，${location}`);b.title=location;
          b.onclick=()=>openCitation(citation,b);sources.append(b);
        }
        article.append(sources);parent.append(article);
      }
      container.append(section);
    },
    pause(value=true){paused=value;for(const entry of entries.values()){if(value){entry.connection?.close();entry.connection=null;}else open(entry);}},
    dispose(){for(const entry of entries.values())entry.connection?.close();entries.clear();}
  };
}
