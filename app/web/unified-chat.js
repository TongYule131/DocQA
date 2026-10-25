import {createApiClient,encoded} from './api-client.js';
import {node,renderAnswer,sourceLocationsText,answerTextForDisplay} from './answer-view.js';
import {createAnswerStream} from './answer-stream.js';
import {createUnifiedClient} from './unified-client.js';
import {feedbackButton,showMyFeedback} from './feedback-ui.js';
import {clarificationForm} from './clarification-ui.js';

const api=createApiClient(),$=id=>document.getElementById(id),json=(method,body)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const active=t=>['queued','running'].includes(t.status),labels={queued:'等待处理',running:'正在准备',parsing:'解析文档',indexing:'建立问答索引',review:'等待管理员审核',ready:'资料可用',failed:'准备失败',needs_attention:'需要确认',cancelled:'已停止',uploaded:'已保存原件',retrieving:'查找原文',planning:'选择证据与组织回答',generating:'生成回答',validating:'核对引用'};
// null表示全部可用资料；[]表示用户取消了全部勾选，两者不能共用空数组。
let bases=[],historyItems=[],current=null,baseId=null,documentIds=null,jobs=[],busy=false,failures=0,timer=null,storagePrefix='',client=null,draftMemory=true,uploadRecord=null,rendered='';
const streams={knowledge:createAnswerStream({source:'knowledge',changed:()=>renderTurns(),finished:()=>run(refresh)}),question:createAnswerStream({source:'question',changed:()=>renderTurns(),finished:()=>run(refresh)})};
function notice(text){$('chat-notice').textContent=text;}
function remember(){sessionStorage.setItem(storagePrefix+'.selected',current?.id||'');}
function controls(){const blocked=busy||Boolean(client?.pending)||current?.turns.some(active)||current?.submissions.some(s=>s.status==='waiting');$('kb-send').disabled=Boolean(blocked||!baseId||current?.archived||!selectedDocuments().length);$('new-chat').disabled=busy||Boolean(client?.pending);$('kb-picker').disabled=busy||Boolean(client?.pending);$('confirm-pending').hidden=!client?.pending;$('confirm-pending').disabled=busy;$('kb-memory').disabled=Boolean(blocked);$('upload-open').disabled=busy||Boolean(client?.pending);for(const id of ['rename-chat','save-notes','clear-memory','archive-chat'])$(id).disabled=busy||!current||Boolean(client?.pending);$('kb-memory').setAttribute('aria-checked',String(current?Boolean(current.memory_enabled):draftMemory));
  $('entity-memory-tools').disabled=busy||Boolean(client?.pending)||!current;
  // render可能发生在busy期间；请求结束时必须连同“全部资料”重新计算禁用状态。
  for(const control of document.querySelectorAll('#document-options input, #document-options button'))control.disabled=Boolean(blocked||!baseId||current?.archived||(control.dataset.selectAll==='true'&&!readyDocuments().length));
}
function poll(){clearTimeout(timer);if(!document.hidden&&failures<5&&(current?.turns.some(active)||current?.submissions.some(s=>s.status==='waiting')||jobs.some(j=>active(j))))timer=setTimeout(()=>run(refresh),Math.min(1800*(failures+1),10000));}
async function run(work){if(busy)return;busy=true;controls();try{await work();failures=0;}catch(error){failures++;if([401,403,404,410].includes(error.status)){current=null;jobs=[];renderTurns();}notice(error.status?error.message:'连接中断，请先刷新核对原记录，没有自动重发。');}finally{busy=false;controls();poll();}}
function base(){return bases.find(b=>b.id===baseId);}
function readyDocuments(){return (base()?.documents||[]).filter(d=>d.ready).map(d=>d.id);}
function selectedDocuments(){return documentIds===null?readyDocuments():documentIds;}
function restoreDocuments(ids){documentIds=ids?.length?[...ids]:null;}
function scopeCaption(){const count=selectedDocuments().length;$('scope-caption').textContent=documentIds===null?(count?`全部可用资料（${count} 份）`:'暂无可用资料'):(count?`已选择 ${count} 份资料`:'未选择资料');}
function source(citation){const content=$('kb-source-content');content.replaceChildren(node('h3',citation.filename||'文档原文'),node('p',sourceLocationsText(citation.sources),'muted'),node('pre',citation.quote,'source-preview'));const a=node('a','打开原件核对');a.href=`/api/documents/${encoded(citation.document_id)}/original`;a.target='_blank';a.rel='noopener';content.append(a);const details=node('details'),summary=node('summary','引用校验详情');details.append(summary,node('p',`${citation.verification==='exact_match'?'原文精确匹配':'请核对原文'} · ${citation.evidence_id||''}`,'muted-detail'));content.append(details);$('kb-source').showModal();}
function button(label,action){const b=node('button',label,'secondary');b.type='button';b.onclick=()=>run(action);return b;}
function renderTurns(){
  for(const [kind,stream] of Object.entries(streams))stream.watch(current?.turns.filter(t=>t.source===kind));
  const signature=JSON.stringify([current?.id,current?.turns,current?.submissions,streams.knowledge.revision,streams.question.revision]);if(signature===rendered)return;rendered=signature;
  const scroll=$('kb-scroll'),bottom=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<160,container=$('kb-turns');container.replaceChildren();
  for(const task of current?.turns||[]){const turn=node('section','','kb-chat-turn');turn.append(node('p',task.question,'kb-user-message'));const answer=node('article','','kb-assistant');answer.append(node('strong','DocQA'));
    if(task.result){const body=node('div','','answer-body');renderAnswer(body,task.result,source);answer.append(body);const actions=node('div','','row');actions.append(button('复制回答',async()=>{await navigator.clipboard.writeText(answerTextForDisplay(task.result));notice('已复制回答');}));if(task.export_url){const a=node('a','导出结果','button secondary');a.href=task.export_url;actions.append(a);}actions.append(feedbackButton(task,{api,run,notice}));answer.append(actions);}
    else{streams[task.source].render(answer,task,source);answer.append(node('p',task.error_message||labels[task.stage]||labels[task.status]||task.status));if(active(task))answer.append(button(task.cancel_requested?'正在停止':'停止生成',async()=>{await api(`/api/${task.source==='knowledge'?'kb-tasks':'question-jobs'}/${encoded(task.id)}/cancel`,{method:'POST'});await refresh();}));}
    if(!task.result&&task.source==='knowledge'&&['failed','needs_attention'].includes(task.status))answer.append(button('复用检查点重试',async()=>{const ack=task.calls?.some(c=>c.status==='uncertain');if(ack&&!confirm('有未确认调用，可能已计费。核实后是否重试？'))return;await api(`/api/kb-tasks/${encoded(task.id)}/retry`,json('POST',{acknowledge_uncertain:Boolean(ack)}));await refresh();}));
    const clarify=clarificationForm(task,{conversationId:current.id,api,run,refresh});if(clarify)answer.append(clarify);
    turn.append(answer);container.append(turn);
  }
  for(const submission of current?.submissions||[]){if(submission.status==='submitted')continue;const block=node('section','','kb-chat-turn');block.append(node('p',submission.question,'kb-user-message'));const card=node('div','','waiting-card');card.append(node('p',submission.status==='waiting'?'等待所选资料就绪，完成后会自动回答。':submission.error_message||'本次等待已停止'));if(submission.status==='waiting')card.append(button('取消等待',async()=>{await api(`/api/chat/conversations/${encoded(current.id)}/submissions/${encoded(submission.id)}/cancel`,{method:'POST'});await refresh();}));block.append(card);container.append(block);}
  $('kb-welcome').hidden=Boolean(current?.turns.length||current?.submissions.length);if(bottom)scroll.scrollTop=scroll.scrollHeight;
}
function render(){
  const picker=$('kb-picker');picker.replaceChildren();for(const b of bases){const o=node('option',b.name+(b.kind==='temporary'?' · 私有临时':''));o.value=b.id;picker.append(o);}if(!bases.length)picker.append(new Option('上传资料或等待知识库授权',''));picker.value=baseId||'';
  const title=current?.title||'知识对话',titleDetails=$('chat-title-details');
  if(titleDetails.dataset.conversation!==(current?.id||'')){titleDetails.open=false;titleDetails.dataset.conversation=current?.id||'';}
  $('chat-title').textContent=title;$('chat-title-full').textContent=title;$('chat-scope').textContent=base()?`${base().name}${base().active_snapshot_id?' · 可用版本 v'+base().revision:' · 资料准备中'}`:'选择知识库或上传资料';
  const history=$('kb-history'),search=$('history-search').value.toLowerCase();history.replaceChildren();for(const c of historyItems.filter(c=>c.title.toLowerCase().includes(search))){const b=node('button',c.title,c.id===current?.id?'active':'');b.title=c.title;b.onclick=()=>run(async()=>{if(client.pending)throw new Error('请先确认上次提交');current=await api('/api/chat/conversations/'+encoded(c.id));baseId=current.base_id;restoreDocuments(current.document_ids);remember();render();});history.append(b);}
  const options=$('document-options'),all=button('使用全部可用资料',async()=>{documentIds=null;render();});all.dataset.selectAll='true';options.replaceChildren(all);for(const d of base()?.documents||[]){const label=node('label'),check=node('input');check.type='checkbox';check.value=d.id;check.checked=selectedDocuments().includes(d.id);check.onchange=()=>{const selected=selectedDocuments();documentIds=check.checked?[...new Set([...selected,d.id])]:selected.filter(i=>i!==d.id);scopeCaption();controls();};label.append(check,node('span',d.filename+(d.ready?'':' · 准备中')));options.append(label);}
  scopeCaption();
  const tools=$('entity-memory-tools');
  tools.replaceChildren();
  if(current){
    tools.append(button('我的反馈',()=>showMyFeedback(api)));
    const state=current.entity_memory;
    if(state&&current.memory_enabled){const details=node('details'),summary=node('summary','实体与比较对象（仅作理解线索）'),input=node('textarea');
      input.value=state.entities.map(e=>e.name).join('\n');input.maxLength=720;input.setAttribute('aria-label','实体名称，每行一个');
      details.append(summary,input,node('p',state.entities.map(e=>`${e.name}：用户轮次 ${e.source_turn}`).join('；'),'muted'));
      if(state.summary)details.append(node('p',`较早问题摘要 · 来源轮次 ${state.summary.source_turns.join('、')}`),node('p',state.summary.text));
      details.append(button('保存实体',async()=>{current=await api('/api/chat/conversations/'+encoded(current.id)+'/entities',json('PATCH',{expected_revision:current.revision,names:input.value.split('\n').map(s=>s.trim()).filter(Boolean)}));await refresh();}));tools.append(details);
      details.append(button('恢复自动提取',async()=>{current=await api('/api/chat/conversations/'+encoded(current.id)+'/entities',json('PATCH',{expected_revision:current.revision,names:[],mode:'auto'}));await refresh();}));
      if(state.conditions?.length)details.append(node('p',state.conditions.map(c=>`${c.text}（用户轮次 ${c.source_turn}）`).join('；')));
    }
  }
  $('chat-name').value=current?.title||'';$('memory-notes').value=current?.memory_notes||'';$('archive-chat').textContent=current?.archived?'恢复会话':'归档会话';
  const progress=$('file-progress');progress.replaceChildren();for(const j of jobs.filter(j=>j.status!=='succeeded')){const row=node('div','','file-status');row.append(node('span',`${j.filename} · ${j.error_message||labels[j.stage]||labels[j.status]}`));if(active(j))row.append(button('停止',async()=>{await api(`/api/ingestions/${encoded(j.id)}/cancel`,{method:'POST'});await refresh();}));else if(['failed','needs_attention','cancelled'].includes(j.status))row.append(button('重试',async()=>{let acknowledge=j.status==='needs_attention';if(acknowledge&&!confirm('该任务有未确认调用，可能已计费。核实后是否重试？'))return;await api(`/api/ingestions/${encoded(j.id)}/retry`,json('POST',{acknowledge_uncertain:acknowledge}));await refresh();}));progress.append(row);}
  renderTurns();controls();
}
async function refresh(){
  const previousId=current?.id;[bases,historyItems]=await Promise.all([api('/api/chat/scopes'),api('/api/chat/conversations?archived='+$('archived').checked)]);
  if(client?.pending){const recovered=await client.recover(historyItems);if(recovered){current=recovered;if(!client.pending){$('kb-input').value='';notice('已找到原请求，没有重复提交。');}}}
  else if(previousId)current=await api('/api/chat/conversations/'+encoded(previousId));
  if(current){baseId=current.base_id;}else if(!bases.some(b=>b.id===baseId)){baseId=bases[0]?.id||null;}
  jobs=baseId?await api('/api/ingestions?base_id='+encoded(baseId)):[];render();poll();
}
async function patch(body){current=await api('/api/chat/conversations/'+encoded(current.id),json('PATCH',{expected_revision:current.revision,...body}));await refresh();}
$('new-chat').onclick=()=>run(async()=>{current=null;documentIds=null;jobs=[];remember();rendered='';await refresh();notice('');});
$('kb-picker').onchange=()=>run(async()=>{baseId=$('kb-picker').value;current=null;documentIds=null;remember();await refresh();});
$('chat-refresh').onclick=()=>run(async()=>{failures=0;await refresh();});$('history-search').oninput=render;$('archived').onchange=()=>run(refresh);
$('kb-form').onsubmit=e=>{e.preventDefault();run(async()=>{const question=$('kb-input').value.trim();if(!question)return;if(!selectedDocuments().length)throw new Error('请至少选择一份资料');client.prepare({conversationId:current?.id,baseId,documentIds:documentIds===null?[]:documentIds,question,memoryEnabled:current?Boolean(current.memory_enabled):draftMemory});const id=await client.confirm();current=await api('/api/chat/conversations/'+encoded(id));remember();$('kb-input').value='';$('input-count').textContent='0 / 4000';notice('');await refresh();});};
$('confirm-pending').onclick=()=>run(async()=>{const id=await client.confirm();if(id){current=await api('/api/chat/conversations/'+encoded(id));remember();$('kb-input').value='';}await refresh();});
$('kb-input').oninput=()=>{$('input-count').textContent=`${$('kb-input').value.length} / 4000`;$('kb-input').style.height='auto';$('kb-input').style.height=Math.min(220,$('kb-input').scrollHeight)+'px';};$('kb-input').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!$('kb-send').disabled)$('kb-form').requestSubmit();}};
$('kb-memory').onclick=()=>run(async()=>{if(current)await patch({memory_enabled:!current.memory_enabled});else{draftMemory=!draftMemory;controls();}});$('save-notes').onclick=()=>run(()=>patch({memory_notes:$('memory-notes').value}));$('clear-memory').onclick=()=>run(()=>patch({clear_memory:true}));$('rename-chat').onclick=()=>run(()=>patch({title:$('chat-name').value.trim()}));$('archive-chat').onclick=()=>run(()=>patch({archived:!current.archived}));$('close-source').onclick=()=>$('kb-source').close();
function uploadPolicy(){const b=bases.find(b=>b.id===$('upload-base').value);$('upload-policy').textContent=b?b.activation_policy==='review'?'处理完成后等待管理员审核生效。':'处理完成后自动加入可用资料。':'建立私有临时库，仅你可见，到期后不可检索。';const r=$('replace-document');r.replaceChildren(new Option('作为新资料加入',''));for(const d of b?.documents.filter(d=>d.ready)||[])r.append(new Option(d.filename,d.id));}
$('upload-open').onclick=()=>{const picker=$('upload-base');picker.replaceChildren();for(const b of bases.filter(b=>b.writable))picker.append(new Option(b.name,b.id));picker.append(new Option('新建私有会话临时库','temporary'));picker.value=uploadRecord?.target|| (base()?.writable?baseId:'temporary');uploadPolicy();$('upload-error').textContent=uploadRecord?'上次上传未确认，请重新选择同一批文件以复用请求键。':'';$('upload-dialog').showModal();};$('upload-base').onchange=uploadPolicy;$('upload-close').onclick=()=>$('upload-dialog').close();
$('upload-form').onsubmit=e=>{e.preventDefault();run(async()=>{const files=[...$('upload-file').files],target=$('upload-base').value,replacement=$('replace-document').value||null;if(!files.length)return;if(files.length>50)throw new Error('每批最多选择50份文档');if(replacement&&files.length!==1)throw new Error('替换操作每次请选择一份文件');
    if(uploadRecord&&(uploadRecord.target!==target||uploadRecord.replacement!==replacement||JSON.stringify(uploadRecord.files.map(f=>[f.name,f.size,f.modified]))!==JSON.stringify(files.map(f=>[f.name,f.size,f.lastModified]))))throw new Error('请先核对未确认的原上传，重新选择相同文件与保存位置');
    uploadRecord ||= {target,replacement,createKey:crypto.randomUUID(),baseId:target==='temporary'?null:target,files:files.map(f=>({name:f.name,size:f.size,modified:f.lastModified,key:crypto.randomUUID(),job:null}))};sessionStorage.setItem(storagePrefix+'.upload',JSON.stringify(uploadRecord));
    try{if(!uploadRecord.baseId){const b=await api('/api/chat/temporary',json('POST',{request_key:uploadRecord.createKey}));uploadRecord.baseId=b.id;sessionStorage.setItem(storagePrefix+'.upload',JSON.stringify(uploadRecord));}
      $('upload-submit').disabled=true;const ids=[];for(let n=0;n<files.length;n++){let record=uploadRecord.files[n];if(!record.job){const form=new FormData();form.append('base_id',uploadRecord.baseId);form.append('file',files[n]);if(replacement)form.append('replace_document_id',replacement);record.job=await api('/api/ingestions',{method:'POST',headers:{'Idempotency-Key':record.key},body:form,timeoutMs:120000});sessionStorage.setItem(storagePrefix+'.upload',JSON.stringify(uploadRecord));}ids.push(record.job.document_id);}
      if(baseId!==uploadRecord.baseId){current=null;remember();}baseId=uploadRecord.baseId;documentIds=ids;uploadRecord=null;sessionStorage.removeItem(storagePrefix+'.upload');$('upload-form').reset();$('upload-dialog').close();await refresh();notice('资料已保存，正在自动准备。可以提交问题，资料就绪后会回答。');
    }catch(error){$('upload-error').textContent='上传未确认，请刷新核对；再次提交会复用原请求键。';throw error;}finally{$('upload-submit').disabled=false;}
  });};
document.addEventListener('visibilitychange',()=>{for(const s of Object.values(streams))s.pause(document.hidden);if(document.hidden)clearTimeout(timer);else poll();});window.addEventListener('pagehide',()=>{clearTimeout(timer);for(const s of Object.values(streams))s.dispose();});
try{const session=await window.docqaAuthReady;storagePrefix='docqa.unified.'+(session.user?.id||'local');client=createUnifiedClient({api,storage:sessionStorage,key:storagePrefix+'.pending'});try{uploadRecord=JSON.parse(sessionStorage.getItem(storagePrefix+'.upload')||'null');}catch{}$('kb-admin').hidden=Boolean(session.user&&session.user.role!=='admin');const params=new URLSearchParams(location.search);baseId=params.get('base');await refresh();const requested=params.get('chat')||(params.get('conversation')?'d:'+params.get('conversation'):null)||(!params.has('base')&&sessionStorage.getItem(storagePrefix+'.selected'));if(requested&&!client.pending){current=await api('/api/chat/conversations/'+encoded(requested));restoreDocuments(current.document_ids);baseId=current.base_id;await refresh();}if(client.pending){$('kb-input').value=client.pending.question;notice('有一次提交尚未确认，请刷新核对或确认原请求。');}if(uploadRecord)notice('有上传尚未确认，请先核对库内资料，或重新选择相同文件以确认原上传。');render();}catch(error){notice(error.message||'无法读取工作空间，请刷新');}
