import {node} from './answer-view.js';

const categories={number_unit:'数字或单位',citation_page:'引用或页码',missing:'遗漏信息',off_topic:'未回答问题',source_conflict:'来源冲突',expression:'语言表达',helpful:'有帮助'};
const states={open:'待处理',reproduced:'已复现',fixed:'已修复',verified:'复验通过',closed:'已关闭'};
const json=(method,body)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});

export function feedbackButton(task,{api,run,notice}){
  const button=node('button','反馈','secondary');button.type='button';
  let dialog=null,key=null;
  // 关闭、取消（Esc）与提交成功共用同一出口：关闭后必须移除节点，避免轮询反复打开时旧弹窗留在 body。
  const dismiss=()=>{if(!dialog)return;dialog.close();dialog.remove();dialog=null;};
  button.onclick=()=>{
    if(dialog){dialog.showModal();return;}
    key=crypto.randomUUID();dialog=node('dialog');dialog.className='feedback-dialog';
    const head=node('div','','feedback-head'),heading=node('div'),title=node('h3','回答反馈','feedback-title'),sub=node('p','反馈仅关联本题，可自愿附带问题、回答与引用版本。','feedback-sub');
    const close=node('button','×','feedback-close');close.type='button';close.setAttribute('aria-label','关闭反馈');close.onclick=dismiss;
    heading.append(title,sub);head.append(heading,close);

    const body=node('div','','feedback-body');
    const categoryField=node('div','','feedback-field'),categoryLabel=node('span','问题类型','feedback-label'),chips=node('div','','feedback-categories');
    let selected='number_unit';chips.setAttribute('role','group');chips.setAttribute('aria-label','问题类型');
    for(const [value,name] of Object.entries(categories)){
      const chip=node('button',name,'feedback-chip');chip.type='button';chip.setAttribute('aria-pressed',String(value===selected));
      chip.onclick=()=>{selected=value;for(const other of chips.children)other.setAttribute('aria-pressed',String(other===chip));};
      chips.append(chip);
    }
    categoryField.append(categoryLabel,chips);

    const commentField=node('div','','feedback-field'),commentLabel=node('label','补充说明（可选）','feedback-label');commentLabel.htmlFor='feedback-comment';
    const comment=node('textarea');comment.id='feedback-comment';comment.maxLength=2000;comment.rows=3;comment.placeholder='例如：回答中的金额与原文不一致，或引用页码不正确。';comment.setAttribute('aria-label','反馈说明');
    commentField.append(commentLabel,comment);

    const share=node('input');share.type='checkbox';const shareLabel=node('label','','feedback-share');
    shareLabel.append(share,node('span','自愿分享本题问题、回答、引用及必要版本／复现信息，不含其他会话。可在“我的反馈”撤回后续读取。'));

    const error=node('p','','feedback-error');error.setAttribute('role','alert');
    const actions=node('div','','feedback-actions'),submit=node('button','提交反馈'),cancel=node('button','取消','secondary');
    submit.type=cancel.type='button';cancel.onclick=dismiss;
    actions.append(cancel,submit);
    body.append(categoryField,commentField,shareLabel,error);
    dialog.append(head,body,actions);document.body.append(dialog);dialog.showModal();
    dialog.oncancel=event=>{if(submit.disabled){event.preventDefault();return;}dismiss();};
    submit.onclick=()=>run(async()=>{submit.disabled=true;error.textContent='';try{
      await api('/api/feedback',json('POST',{source:task.source,task_id:task.id,request_key:key,category:selected,comment:comment.value,share_context:share.checked}));
      dismiss();notice('反馈已保存，可在“我的反馈”查看处理进度。');
    }catch(exc){error.textContent=exc.message||'提交尚未确认，请使用同一窗口重新确认。';throw exc;}finally{submit.disabled=false;}});
  };
  return button;
}

export async function showMyFeedback(api){
  const dialog=node('dialog');dialog.className='feedback-dialog feedback-history';
  const head=node('div','','feedback-head'),heading=node('div'),title=node('h3','我的反馈','feedback-title'),sub=node('p','仅显示本人提交的反馈与处理进度。','feedback-sub');
  const close=node('button','×','feedback-close');close.type='button';close.setAttribute('aria-label','关闭我的反馈');close.onclick=()=>{dialog.close();dialog.remove();};
  heading.append(title,sub);head.append(heading,close);
  const body=node('div','','feedback-list');
  const footer=node('div','','feedback-actions'),previous=node('button','上一页','secondary'),next=node('button','下一页','secondary');
  previous.type=next.type='button';footer.append(previous,next);
  dialog.append(head,body,footer);document.body.append(dialog);dialog.showModal();
  let offset=0;const limit=20;
  async function load(){previous.disabled=next.disabled=true;body.replaceChildren();try{const rows=await api(offset?`/api/feedback?offset=${offset}&limit=${limit}`:'/api/feedback');if(!rows.length)body.append(node('p','暂无反馈','feedback-empty'));
    for(const row of rows){const item=node('section','','feedback-item');
      item.append(node('p',`${categories[row.category]||row.category||'其他'} · ${states[row.status]||row.status||'待处理'}`,'feedback-item-meta'),node('p',row.comment||'（未填写说明）','feedback-item-comment'));
      if(row.client_reply)item.append(node('p','处理结果：'+row.client_reply,'feedback-item-reply'));
      if(row.share_context){const revoke=node('button','撤回正文分享','secondary');revoke.type='button';revoke.onclick=async()=>{try{await api(`/api/feedback/${encodeURIComponent(row.id)}/shared-context`,{method:'DELETE'});revoke.disabled=true;revoke.textContent='已撤回';}catch(e){body.append(node('p',e.message,'feedback-error'));}};item.append(revoke);}body.append(item);}
    previous.disabled=offset===0;next.disabled=rows.length<limit;
  }catch(e){body.append(node('p',e.message,'feedback-error'));previous.disabled=offset===0;}}
  previous.onclick=()=>{offset=Math.max(0,offset-limit);return load();};next.onclick=()=>{offset+=limit;return load();};await load();
}
