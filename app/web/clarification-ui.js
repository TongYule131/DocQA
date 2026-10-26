import {node} from './answer-view.js';

export function clarificationForm(task,{conversationId,api,run,refresh}) {
  const value=task.result?.clarification;
  if(!value||!['awaiting','resolving'].includes(value.state))return null;
  if(value.kind==='tool_parameters')return toolParametersForm(value,{conversationId,api,run,refresh});
  const form=node('form','','clarification-form'),label=node('label','确认对象（每行一个）'),input=node('textarea');
  input.rows=value.needed_count;input.maxLength=150;input.required=true;
  input.setAttribute('aria-label','确认问题指向的对象');
  input.placeholder=(value.choices||[]).slice(0,4).join('\n')||'请填写具体名称';label.append(input);
  if(value.state==='resolving'){input.value=(value.confirmed_names||[]).join('\n');input.readOnly=true;form.append(node('p','上次确认尚未完成，可用同一对象继续确认，或取消本轮。','muted'));}
  const send=node('button','确认后继续'),cancel=node('button','取消澄清','secondary');send.type='submit';cancel.type='button';
  const key=value.request_key||crypto.randomUUID();
  form.onsubmit=e=>{e.preventDefault();run(async()=>{const names=input.value.split(/[\n，,、]/).map(s=>s.trim()).filter(Boolean);
    if(names.length!==value.needed_count)throw new Error(`请填写${value.needed_count}个对象`);
    send.disabled=true;try{await api(`/api/chat/conversations/${encodeURIComponent(conversationId)}/clarifications/${encodeURIComponent(value.id)}/resolve`,
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({names,request_key:key})});await refresh();}finally{send.disabled=false;}});};
  cancel.onclick=()=>run(async()=>{await api(`/api/chat/conversations/${encodeURIComponent(conversationId)}/clarifications/${encodeURIComponent(value.id)}/cancel`,{method:'POST'});await refresh();});
  form.append(label,send,cancel);return form;
}

function toolParametersForm(value,{conversationId,api,run,refresh}) {
  const form=node('form','','clarification-form'),inputs=[];
  value.fields.forEach((field,index)=>{
    const label=node('label',field.label||field.name),input=node('input');input.type='text';input.required=true;input.maxLength=600;
    input.setAttribute('aria-label',field.label||field.name);
    if(value.state==='resolving'){input.value=value.confirmed_names?.[index]||'';input.readOnly=true;}
    label.append(input);form.append(label);inputs.push(input);
  });
  const send=node('button','补充后继续查询'),cancel=node('button','取消查询','secondary');send.type='submit';cancel.type='button';
  const key=value.request_key||crypto.randomUUID();
  form.onsubmit=e=>{e.preventDefault();return run(async()=>{
    const names=inputs.map(input=>input.value.trim());
    if(names.some(s=>!s)||names.reduce((n,s)=>n+s.length,0)>2000)throw new Error('请填写全部查询信息，合计不超过2000字。');
    send.disabled=true;try{await api(`/api/chat/conversations/${encodeURIComponent(conversationId)}/clarifications/${encodeURIComponent(value.id)}/resolve`,
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({names,request_key:key})});await refresh();}finally{send.disabled=false;}
  });};
  cancel.onclick=()=>run(async()=>{await api(`/api/chat/conversations/${encodeURIComponent(conversationId)}/clarifications/${encodeURIComponent(value.id)}/cancel`,{method:'POST'});await refresh();});
  form.append(send,cancel);return form;
}
