import {createApiClient} from './api-client.js';
import {node} from './answer-view.js';
const api=createApiClient(),$=id=>document.getElementById(id),states=['open','reproduced','fixed','verified','closed'];
async function run(work){try{$('error').textContent='';await work();}catch(e){$('error').textContent=e.message||'读取失败，请刷新';}}
async function detail(id){const row=await api('/api/admin/feedback/'+encodeURIComponent(id)),panel=$('feedback-detail');panel.replaceChildren(node('h2','处理反馈'),node('p',`${row.category} · ${row.status}`));
  if(row.shared_context)panel.append(node('pre',JSON.stringify(row.shared_context,null,2)),node('p',row.comment||''));
  else panel.append(node('p','客户未分享问题、回答和反馈正文。'));
  const form=node('form'),status=node('select'),cause=node('select'),fields={};
  for(const s of states){const o=node('option',s);o.value=s;status.append(o);}status.value=row.status==='closed'?'closed':states[states.indexOf(row.status)+1];
  for(const s of ['parsing','retrieval','ranking','packing','planning','generation','validation','presentation','other']){const o=node('option',s);o.value=s;cause.append(o);}cause.value=row.root_cause||'other';
  for(const [text,control] of [['处理阶段',status],['根因类别',cause]]){const label=node('label',text);label.append(control);form.append(label);}
  for(const [key,title] of [['regression_case','审核后的回归样本编号'],['original_retest','原失败复验的证据编号'],['negative_retest','独立负例复验的证据编号']]){const label=node('label',title),input=node('input');input.maxLength=300;input.value=row[key]||'';fields[key]=input;label.append(input);form.append(label);}
  const submit=node('button','保存处理进度');submit.type='submit';form.append(submit);form.onsubmit=e=>{e.preventDefault();run(async()=>{await api('/api/admin/feedback/'+encodeURIComponent(id),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:row.revision,status:status.value,root_cause:cause.value,...Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value||null]))})});await refresh();await detail(id);});};panel.append(form);
}
async function refresh(){const rows=await api('/api/admin/feedback');$('feedback-list').replaceChildren();for(const row of rows){const button=node('button',`${row.category} · ${row.status} · ${row.created_at}`,'secondary');button.type='button';button.onclick=()=>run(()=>detail(row.id));$('feedback-list').append(button);}if(!rows.length)$('feedback-list').append(node('p','暂无反馈'));}
$('refresh').onclick=()=>run(refresh);await window.docqaAuthReady;await run(refresh);
