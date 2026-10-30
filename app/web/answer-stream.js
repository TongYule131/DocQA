import { node, sourceLocationsText, contextCovered } from './answer-view.js';

// 某些桌面宿主会节流rAF，即使页面仍可见；兜底只绘制已收到的文字。
function scheduleFrame(callback){
  const ticket={raf:null,timer:null,done:false};
  const run=()=>{if(ticket.done)return;ticket.done=true;cancelAnimationFrame(ticket.raf);clearTimeout(ticket.timer);callback();};
  ticket.raf=requestAnimationFrame(run);ticket.timer=setTimeout(run,64);return ticket;
}
function cancelScheduledFrame(ticket){if(ticket){ticket.done=true;cancelAnimationFrame(ticket.raf);clearTimeout(ticket.timer);}}
const REVEAL_PER_SECOND=32,MAX_REVEAL_MS=6000;

// SSE只订阅已有任务。增量更新已收到的草稿，不重建历史或发送生成POST。
export function createAnswerStream({source='question',changed=()=>{},finished=()=>{},beforePaint=()=>false,painted=()=>{},
  frame=typeof requestAnimationFrame==='undefined'?null:scheduleFrame,
  cancelFrame=cancelScheduledFrame,
  now=()=>performance.now(),
  reducedMotion=()=>Boolean(globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches),
  eventSource=typeof EventSource==='undefined'?null:url=>new EventSource(url)}={}) {
  const entries=new Map(),fresh=new Set();let revision=0,paused=false;
  const terminal=status=>!['queued','running'].includes(status);
  const failed=status=>terminal(status)&&status!=='succeeded';
  function cancelPaint(entry){if(entry.frame!==null&&entry.frame!==undefined)cancelFrame(entry.frame);entry.frame=null;}
  function clearView(entry){cancelPaint(entry);entry.view?.section.remove?.();entry.view=null;entry.shown=0;}
  function reset(entry,attempt){clearView(entry);Object.assign(entry,{blocks:[],text:'',points:[],total:0,attempt,general:null,credit:0,lastTick:0,deadline:0,finalReceived:false,displayNotified:false,skipped:false});}
  function presenting(entry){return Boolean(entry&&!entry.denied&&!entry.invalid&&!entry.skipped&&frame&&!reducedMotion()&&entry.status==='succeeded'&&(!entry.finalReceived||entry.shown<entry.total));}
  function finalContent(entry,result){
    if(entry.finalReceived)return;
    const general=result.response_kind==='general';
    const facts=[...(result.conclusion||[]),...(result.explanation||[])];
    const blocks=general?[{version:'general-answer-stream-v1',draft:true,validation_scope:'general_text',text_delta:result.answer||''}]:facts.map((fact,index)=>{
      const n=(result.presentation?.blocks||[]).findIndex(b=>b.fact_indices?.includes(index));
      return {version:'validated-fact-stream-v2',draft:true,validation_scope:'single_fact',fact,
        citations:(result.citations||[]).filter(c=>(fact.refs||[]).includes(c.reference_id)),
        ...(n<0?{}:{presentation_block:{index:n,kind:result.presentation.blocks[n].kind}})};
    });
    const compatible=general===entry.general&&(general?(result.answer||'').startsWith(entry.text):entry.blocks.every((b,i)=>b.fact?.text===facts[i]?.text));
    if(!compatible)clearView(entry);
    entry.blocks=blocks;entry.general=general;entry.text=general?(result.answer||''):'';entry.points=general?Array.from(entry.text):[];
    entry.total=general?entry.points.length:facts.reduce((n,f)=>n+Array.from(f.text).length,0);
    entry.notices=[...(result.status==='partial'?['当前回答仅部分有据，以下缺口仍未解决。']:[]),
      ...(result.missing_points||[]),...(result.limitations||[]),...(result.clarification_questions||[])].filter(v=>typeof v==='string'&&v);
    entry.finalReceived=true;entry.deadline=now()+Math.min(MAX_REVEAL_MS,Math.max(0,entry.total-entry.shown)/REVEAL_PER_SECOND*1000);
  }
  function paintParts(entry){
    const follow=beforePaint();let remaining=entry.shown;
    for(const [index,part] of entry.view.parts.entries()){
      const count=Math.min(remaining,part.points.length);remaining-=count;
      if(part.count!==count){part.text.textContent=part.points.slice(0,count).join('');part.count=count;}
      if(part.article)part.article.hidden=index>0&&count===0;
      if(part.context)part.context.textContent=count<part.points.length?part.allContext:part.remainingContext;
    }
    painted(follow);
  }
  function paintText(entry){
    if(!entry.view)return;
    const instant=()=>entry.skipped||entry.invalid||failed(entry.status)||failed(entry.lastJobStatus)||paused||reducedMotion()||!frame;
    const paint=()=>{
      entry.frame=null;if(entries.get(entry.id)!==entry||!entry.view)return;
      const stamp=now(),remaining=entry.total-entry.shown;
      if(instant())entry.shown=entry.total;
      else{
        const speed=entry.finalReceived?Math.max(REVEAL_PER_SECOND,remaining/Math.max(1,entry.deadline-stamp)*1000):REVEAL_PER_SECOND;
        entry.credit+=Math.min(100,Math.max(0,stamp-entry.lastTick))*speed/1000;
        const step=Math.min(remaining,Math.floor(entry.credit));entry.credit-=step;entry.shown+=step;
      }
      entry.lastTick=stamp;paintParts(entry);
      if(entry.shown<entry.total&&frame&&!instant())entry.frame=frame(paint);
      else if(entry.done&&entry.finalReceived&&!entry.displayNotified){entry.displayNotified=true;revision++;changed();}
    };
    if(instant()){cancelPaint(entry);paint();}
    else if((entry.frame===null||entry.frame===undefined)&&entry.shown<entry.total){entry.lastTick=now();entry.frame=frame(paint);}
  }
  function appendFact(view,block,openCitation){
    const plan=block.version==='validated-fact-stream-v2'?block.presentation_block:null;
    const grouped=plan&&Number.isSafeInteger(plan.index)&&plan.index>=0&&plan.index<12&&plan.kind==='paragraph';
    let parent=view.section;
    if(grouped){if(!view.paragraphs.has(plan.index)){const p=node('div','','stream-paragraph');view.paragraphs.set(plan.index,p);view.section.append(p);}parent=view.paragraphs.get(plan.index);}
    const labels={subject:'对象',period:'期间',unit:'单位',scope:'范围',condition:'限定',role:'用途'};
    const fact=block.fact,article=node(grouped?'span':'div','','stream-fact');
    const contexts=(fact.context||[]).filter(c=>typeof c.text==='string');
    const describe=items=>items.length?'（'+items.map(c=>(labels[c.kind]||'条件')+'：'+c.text).join('；')+'）':'';
    const text=node(grouped?'span':'p'),context=node('span','','stream-context');
    if(fact.claim_type==='inference')article.append(node('span','【结构推断】'));
    article.append(text,context);
    const sources=node(grouped?'span':'div','','stream-sources');
    for(const citation of block.citations||[]){const location=sourceLocationsText(citation.sources)||'来源位置未提供';const b=node('button',String(citation.reference_id),'reference');b.type='button';
      b.setAttribute('aria-label',`查看引用 ${citation.reference_id}，${location}`);b.title=location;b.onclick=()=>openCitation(citation,b);sources.append(b);}
    article.append(sources);parent.append(article);
    view.parts.push({article,text,context,points:Array.from(String(fact.text)),count:-1,
      allContext:describe(contexts),remainingContext:describe(contexts.filter(c=>!contextCovered(c.text,fact.text)))});
  }
  function syncView(entry){
    const view=entry.view;if(!view)return;
    const incomplete=entry.invalid||failed(entry.status)||failed(entry.lastJobStatus);
    const complete=entry.status==='succeeded'||entry.lastJobStatus==='succeeded';
    if(entry.finalReceived&&!view.noticesAdded){for(const text of entry.notices||[])view.notices.append(node('p',text));view.notices.hidden=!entry.notices?.length;view.noticesAdded=true;}
    view.label.textContent=view.general?(incomplete?'普通回答未完成，以下为生成中草稿。':complete?'回答已生成，正在显示':'通用回答 · 生成中'):
      incomplete?'生成未完成，以下片段不作为最终回答。':complete?'回答已生成，正在显示':entry.reconnecting?'连接中断，正在恢复已保存片段…':'生成中 · 正在展开已核对的内容，限定和引用同步保留；完成后统一复验。';
    if(view.general)view.parts[0].points=entry.points;
    else while(view.processed<entry.blocks.length)appendFact(view,entry.blocks[view.processed++],view.openCitation);
    if(complete&&entry.finalReceived&&presenting(entry)&&!view.skip){view.skip=node('button','立即显示全文','secondary');view.skip.type='button';view.skip.onclick=()=>showAll(entry);view.section.append(view.skip);}
    paintText(entry);
  }
  function showAll(entry){entry.skipped=true;cancelPaint(entry);entry.shown=entry.total;if(entry.view)paintParts(entry);revision++;changed();}
  function update(entry,outer=false){if(entry.view&&!outer)syncView(entry);else{revision++;changed();}}
  function invalid(entry,connection){connection.close();entry.connection=null;entry.invalid=true;entry.done=true;update(entry,true);}
  function open(entry){
    if(!eventSource||paused||entry.done||entry.connection)return;
    const url=`/api/${source==='knowledge'?'kb-tasks':'question-jobs'}/${encodeURIComponent(entry.id)}/events?after=${entry.cursor}`;
    const connection=eventSource(url);entry.connection=connection;
    const current=()=>entries.get(entry.id)===entry&&entry.connection===connection;
    connection.addEventListener('update',event=>{
      if(!current())return;
      try{
        const item=JSON.parse(event.data);if(!Number.isSafeInteger(item.id)||item.id<=entry.cursor)return;
        entry.cursor=item.id;
        if(item.kind==='reset'){if(!Number.isSafeInteger(item.attempt)||item.attempt<entry.attempt)return;reset(entry,item.attempt);entry.reconnecting=false;update(entry,true);return;}
        if(item.kind==='block'&&item.attempt===entry.attempt&&item.payload?.draft===true){
          const b=item.payload,general=b.version==='general-answer-stream-v1'&&b.validation_scope==='general_text'&&typeof b.text_delta==='string';
          const fact=b.validation_scope==='single_fact'&&b.fact&&typeof b.fact.text==='string';
          if(general||fact){if(entry.blocks.length&&general!==(entry.blocks[0].version==='general-answer-stream-v1'))throw Error('mixed stream');
            entry.blocks.push(b);entry.general=general;
            if(general){entry.text+=b.text_delta;entry.points=Array.from(entry.text);entry.total=entry.points.length;}
            else entry.total+=Array.from(b.fact.text).length;}
        }
        entry.reconnecting=false;update(entry);
      }catch{invalid(entry,connection);}
    });
    connection.addEventListener('state',event=>{
      if(!current())return;
      try{
        const state=JSON.parse(event.data);
        if(Number.isSafeInteger(state.attempt)&&state.attempt>entry.attempt)reset(entry,state.attempt);
        entry.status=state.status;
        if(terminal(state.status)){connection.close();entry.connection=null;entry.done=true;syncView(entry);finished(entry.id);}
        update(entry,true);
      }catch{invalid(entry,connection);}
    });
    connection.addEventListener('denied',()=>{
      if(!current())return;reset(entry,entry.attempt);entry.denied=true;entry.done=true;connection.close();entry.connection=null;update(entry,true);finished(entry.id);
    });
    connection.onerror=()=>{if(current()){entry.reconnecting=true;update(entry);}};
  }
  return {
    get revision(){return revision;},
    track(id){if(typeof id==='string'&&id)fresh.add(id);},
    watch(jobs){
      const keep=new Set();
      for(const job of jobs||[]){
        if(!job?.streaming)continue;
        let entry=entries.get(job.id);
        if(!entry&&fresh.has(job.id)){entry={id:job.id,cursor:0,attempt:0,shown:0,view:null,frame:null,status:job.status,lastJobStatus:job.status,connection:null,done:false};reset(entry,0);entries.set(job.id,entry);}
        fresh.delete(job.id);
        if(job.result_id||job.result||job.status==='succeeded'){
          // 只保留本页已观察到的实时任务；刷新／打开历史结果不播放打字效果。
          if(entry&&!entry.denied){entry.status='succeeded';entry.done=true;entry.connection?.close();entry.connection=null;
            if(job.result)finalContent(entry,job.result);
            if(presenting(entry)){keep.add(job.id);entry.lastJobStatus=job.status;continue;}}
          continue;
        }
        keep.add(job.id);
        if(!entry){entry={id:job.id,cursor:0,attempt:0,blocks:[],text:'',points:[],total:0,general:null,credit:0,lastTick:0,deadline:0,finalReceived:false,displayNotified:false,skipped:false,shown:0,view:null,frame:null,status:job.status,lastJobStatus:job.status,connection:null,done:false};entries.set(job.id,entry);}
        if(entry.done&&terminal(entry.lastJobStatus)&&!terminal(job.status)){entry.done=false;entry.invalid=false;entry.denied=false;}
        entry.lastJobStatus=job.status;if(!entry.done)entry.status=job.status;open(entry);
      }
      for(const [id,entry] of entries)if(!keep.has(id)){entry.connection?.close();clearView(entry);entries.delete(id);}
    },
    render(container,job,openCitation){
      const entry=entries.get(job.id);if(!entry||entry.denied||!entry.blocks.length)return;
      if(!entry.view){const section=node('section','','stream-preview'),label=node('p','','stream-label'),notices=node('div','','stream-limitations');notices.hidden=true;section.append(label,notices);
        const general=entry.blocks.every(b=>b.version==='general-answer-stream-v1');
        entry.view={section,label,notices,general,processed:0,paragraphs:new Map(),openCitation,parts:[]};
        if(general){entry.view.text=node('p','','general-stream-text');section.append(entry.view.text);entry.view.parts.push({text:entry.view.text,points:entry.points,count:-1});}
      }
      entry.lastJobStatus=job.status;entry.view.openCitation=openCitation;container.append(entry.view.section);syncView(entry);return true;
    },
    presenting(job){return presenting(entries.get(job.id));},
    showAll(id){const entry=entries.get(id);if(entry)showAll(entry);},
    pause(value=true){paused=value;for(const entry of entries.values()){if(value){entry.connection?.close();entry.connection=null;paintText(entry);}else open(entry);}},
    dispose(){for(const entry of entries.values()){entry.connection?.close();clearView(entry);}entries.clear();fresh.clear();}
  };
}
