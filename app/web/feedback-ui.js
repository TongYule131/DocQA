import {node} from './answer-view.js';

const categories={number_unit:'数字或单位',citation_page:'引用或页码',missing:'遗漏信息',off_topic:'未回答问题',source_conflict:'来源冲突',expression:'语言表达',helpful:'有帮助'};
const states={open:'待处理',reproduced:'已复现',fixed:'已修复',verified:'复验通过',closed:'已关闭'};
const json=(method,body)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});

export function feedbackButton(task,{api,run,notice}){
  const button=node('button','反馈','secondary');button.type='button';
  let dialog=null,key=null;
  button.onclick=()=>{
    if(dialog){dialog.showModal();return;}
    key=crypto.randomUUID();dialog=node('dialog');
    const title=node('h3','回答反馈'),category=node('select'),comment=node('textarea'),share=node('input'),label=node('label'),error=node('p','','error'),submit=node('button','提交反馈'),close=node('button','关闭','secondary');
    category.setAttribute('aria-label','错误类型');for(const [value,name] of Object.entries(categories)){const option=node('option',name);option.value=value;category.append(option);}
    comment.maxLength=2000;comment.setAttribute('aria-label','反馈说明');comment.placeholder='说明问题发生在哪里（可选）';
    share.type='checkbox';label.append(share,node('span','自愿向管理员分享本题问题、回答和引用快照，可在“我的反馈”撤回。'));
    submit.type=close.type='button';close.onclick=()=>dialog.close();
    submit.onclick=()=>run(async()=>{submit.disabled=true;error.textContent='';try{
      await api('/api/feedback',json('POST',{source:task.source,task_id:task.id,request_key:key,category:category.value,comment:comment.value,share_context:share.checked}));
      dialog.close();dialog.remove();dialog=null;notice('反馈已保存，可在“我的反馈”查看处理进度。');
    }catch(exc){error.textContent=exc.message||'提交尚未确认，请使用同一窗口重新确认。';throw exc;}finally{submit.disabled=false;}});
    dialog.append(title,category,comment,label,error,submit,close);document.body.append(dialog);dialog.showModal();
  };
  return button;
}

export async function showMyFeedback(api){
  const dialog=node('dialog'),close=node('button','关闭','secondary'),body=node('div');close.type='button';close.onclick=()=>{dialog.close();dialog.remove();};
  dialog.append(node('h3','我的反馈'),body,close);document.body.append(dialog);dialog.showModal();
  try{const rows=await api('/api/feedback');if(!rows.length)body.append(node('p','暂无反馈'));
    for(const row of rows){const item=node('section');item.append(node('p',`${categories[row.category]} · ${states[row.status]}`),node('p',row.comment||''));
      if(row.share_context){const revoke=node('button','撤回正文分享','secondary');revoke.onclick=async()=>{try{await api(`/api/feedback/${encodeURIComponent(row.id)}/shared-context`,{method:'DELETE'});revoke.disabled=true;revoke.textContent='已撤回';}catch(e){body.append(node('p',e.message,'error'));}};item.append(revoke);}body.append(item);}
  }catch(e){body.append(node('p',e.message,'error'));}
}
