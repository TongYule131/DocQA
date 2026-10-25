import {createApiClient} from './api-client.js';
import {node} from './answer-view.js';
const api=createApiClient(),$=id=>document.getElementById(id),states=['open','reproduced','fixed','verified','closed'];
const labels={open:'待处理',reproduced:'已复现',fixed:'已修复',verified:'复验通过',closed:'已关闭'};
const causes={parsing:'解析',retrieval:'召回',ranking:'排序',packing:'证据装入',planning:'问题规划',generation:'生成',validation:'校验',presentation:'表达呈现',other:'其他'};
const categories={number_unit:'数字／单位',citation_page:'引用／页码',missing:'遗漏',off_topic:'未回答',source_conflict:'来源冲突',expression:'表达',helpful:'有帮助'};
const json=body=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
let offset=0;const limit=20;
async function run(work){try{$('error').textContent='';await work();}catch(e){$('error').textContent=e.message||'读取失败，请刷新';}}
function button(text,action){const b=node('button',text);b.type='button';b.onclick=()=>run(action);return b;}
function option(select,text,value){const o=node('option',text);o.value=value;select.append(o);}
const filters=node('section','','feedback-filters'),stateFilter=node('select'),categoryFilter=node('select');
stateFilter.setAttribute('aria-label','按处理状态筛选');categoryFilter.setAttribute('aria-label','按反馈类型筛选');
option(stateFilter,'全部处理状态','');option(categoryFilter,'全部反馈类型','');
for(const [key,text] of Object.entries(labels))option(stateFilter,text,key);
for(const [key,text] of Object.entries(categories))option(categoryFilter,text,key);
stateFilter.onchange=categoryFilter.onchange=()=>run(async()=>{offset=0;await refresh();});
const previous=button('上一页',async()=>{offset=Math.max(0,offset-limit);await refresh();});
const next=button('下一页',async()=>{offset+=limit;await refresh();});
filters.append(stateFilter,categoryFilter,previous,next);$('feedback-list').before(filters);
const quality=node('section','','quality-summary');filters.before(quality);
const operations=node('a','运行与用量');operations.href='/admin/operations';filters.append(operations);
async function detail(id){
  const row=await api('/api/admin/feedback/'+encodeURIComponent(id)),panel=$('feedback-detail');
  panel.replaceChildren(node('h2','处理反馈'),node('p',`${categories[row.category]} · ${labels[row.status]} · 第${row.generation}轮处理`));
  if(row.shared_context)panel.append(node('details','','shared-case'));
  if(row.shared_context){const disclosure=panel.lastElementChild;disclosure.append(node('summary','客户分享的本题快照'),node('pre',JSON.stringify(row.shared_context,null,2)),node('p',row.comment||''));}
  else panel.append(node('p','客户未分享本题正文。'));
  if(row.share_context)panel.append(button('建立／导出本轮回归包',async()=>{
    const bundle=await api(`/api/admin/feedback/${encodeURIComponent(id)}/case`,json({}));
    const url=URL.createObjectURL(new Blob([JSON.stringify(bundle,null,2)],{type:'application/json'}));
    const a=node('a');a.href=url;a.download=bundle.id+'.json';a.click();URL.revokeObjectURL(url);await detail(id);
  }));
  if(row.status==='fixed'||row.status==='verified'){
    const label=node('label','导入已通过的定向复验证据（原失败和独立负例分别导入）'),input=node('input');input.type='file';input.accept='.json,application/json';
    input.onchange=()=>run(async()=>{if(!input.files?.[0])return;const proof=JSON.parse(await input.files[0].text());await api(`/api/admin/feedback/${encodeURIComponent(id)}/evidence`,json(proof));await detail(id);});
    label.append(input);panel.append(label,node('p','使用随项目提供的 prepare_feedback_evidence.py 绑定已有JUnit、回归包、输入文件和当前代码；导入不会运行模型。','muted'));
  }
  const form=node('form'),status=node('select'),cause=node('select'),fields={};
  for(const s of states)option(status,labels[s],s);status.value=row.status==='closed'?'open':states[states.indexOf(row.status)+1];
  for(const [s,label] of Object.entries(causes))option(cause,label,s);cause.value=row.root_cause||'other';
  for(const [text,control] of [['处理阶段',status],['根因类别',cause]]){const label=node('label',text);label.append(control);form.append(label);}
  for(const [key,title] of [['regression_case','当前回归案例'],['original_retest','原失败通过证据'],['negative_retest','独立负例通过证据']]){
    const label=node('label',title),input=node('input');input.readOnly=true;input.value=row[key]||'';fields[key]=input;label.append(input);form.append(label);
  }
  const reply=node('textarea');reply.maxLength=1000;reply.value=row.client_reply||'';reply.setAttribute('aria-label','客户可见处理结果');form.append(node('p','向客户说明处理结果'),reply);
  const submit=node('button','保存处理进度');submit.type='submit';form.append(submit);
  form.onsubmit=e=>{e.preventDefault();run(async()=>{await api('/api/admin/feedback/'+encodeURIComponent(id),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:row.revision,status:status.value,root_cause:cause.value,client_reply:reply.value,
    ...Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value||null]))})});await refresh();await detail(id);});};panel.append(form);
  const history=node('details');history.append(node('summary','处理与分享历史'));
  for(const event of row.history||[])history.append(node('p',`${event.created_at} · ${labels[event.status]||event.status} · ${event.action}`));panel.append(history);
}
async function refresh(){
  const params=new URLSearchParams({limit:String(limit),offset:String(offset)});if(stateFilter.value)params.set('status',stateFilter.value);if(categoryFilter.value)params.set('category',categoryFilter.value);
  const rows=await api('/api/admin/feedback?'+params);$('feedback-list').replaceChildren();
  for(const row of rows)$('feedback-list').append(button(`${categories[row.category]} · ${labels[row.status]} · ${row.created_at}`,()=>detail(row.id)));
  previous.disabled=offset===0;next.disabled=rows.length<limit;if(!rows.length)$('feedback-list').append(node('p','暂无符合条件的反馈'));
  const data=await api('/api/admin/quality');quality.replaceChildren(node('h2','质量概况'),node('p',`业务任务 ${data.business_tasks.total} · 已回答 ${data.business_tasks.answered} · 已登记工程复验证据 ${data.registered_engineering_evidence}`),
    node('p',data.human_semantic_review.note,'muted'),node('p',data.citation_engineering.note,'muted'));
}
$('refresh').onclick=()=>run(refresh);await window.docqaAuthReady;await run(refresh);
