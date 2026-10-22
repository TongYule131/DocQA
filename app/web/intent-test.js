import { node } from './answer-view.js';
import { createApiClient } from './api-client.js';

export function installIntentTest() {
  const section=node('div'); section.id='intent-test-view'; section.hidden=true;
  document.getElementById('intent-tree-view').before(section);
  const panel=node('section','','panel'),form=node('form');
  panel.append(node('h2','测试实际分流规则'),node('p','此处与文档／知识库会话使用同一分流器。不需要发布知识库；不会执行检索、模型或工具。','muted'));
  const controls=node('div','','form-grid'),scope=node('select'),base=node('select');
  scope.id='test-scope';base.id='test-base';
  const option=(select,label,value)=>{const o=node('option',label);o.value=value;select.append(o);};
  option(scope,'文档会话','document');option(scope,'知识库会话','knowledge');
  for(const [title,id,input] of [['使用入口','test-scope',scope],['关联知识库（可不选）','test-base',base]]) {
    const label=node('label',title);label.htmlFor=id;label.append(input);controls.append(label);
  }
  const label=node('label','测试问题（每行一条，最多100条）');label.htmlFor='intent-test-questions';
  const questions=node('textarea');questions.id='intent-test-questions';questions.rows=5;questions.maxLength=50000;
  questions.value='你好呀\n谢谢，退款多久到账\n这份文档讲了什么';label.append(questions);
  const submit=node('button','测试分流');submit.type='submit';
  const status=node('p','','muted');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  form.append(controls,label,submit,status);panel.append(form);section.append(panel);
  const output=node('section','','panel');output.hidden=true;section.append(output);
  const previous=new Map(),api=createApiClient();let testing=false;
  const methods={exact:'完整示例',interaction:'纯交互规则',keywords:'业务关键词',fallback:'回退检索'};
  form.onsubmit=async event=>{
    event.preventDefault();if(testing)return;
    const batch=questions.value.split('\n').map(q=>q.trim()).filter(Boolean);
    if(!batch.length||batch.length>100||batch.some(q=>q.length>500)){status.textContent='请输入1～100条问题，每条不超过500字符。';return;}
    testing=true;submit.disabled=true;scope.disabled=true;base.disabled=true;questions.disabled=true;status.textContent='正在测试当前配置…';
    try {
      const result=await api('/api/admin/intents/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({questions:batch,scope:scope.value,base_id:base.value||null})});
      output.replaceChildren(node('h2','分流结果'));
      const table=node('table'),head=node('thead'),header=node('tr');
      for(const title of ['问题','实际路径','命中依据','候选节点','上次结果'])header.append(node('th',title));head.append(header);table.append(head);
      const body=node('tbody');let changed=0;
      for(const row of result.results){
        const d=row.decision,key=JSON.stringify([scope.value,base.value,row.question]),prior=previous.get(key);
        if(prior&&prior!==d.code)changed++;
        const tr=node('tr');tr.append(node('td',row.question));
        const route=node('td');route.append(node('strong',d.name),node('p',`${d.kind} · ${d.code}`,'node-code'));
        if(row.response_preview)route.append(node('p',row.response_preview));
        if(row.top_k)route.append(node('small',`TopK ${row.top_k}`));
        const reason=node('td');reason.append(node('p',methods[d.method]||d.method),node('p',d.reason,'muted-detail'));
        tr.append(route,reason,node('td',d.candidates.map(c=>`${c.name} · ${Math.round(c.score*100)}%`).join('；')||'无命中'),node('td',prior||'首次测试'));
        body.append(tr);previous.set(key,d.code);
      }
      table.append(body);const scroll=node('div','','table-scroll intent-test-table');scroll.append(table);output.append(scroll);
      const d=result.results[0]?.decision;
      output.append(node('p',`配置 ${d?.configuration.slice(0,12)||'—'} · ${d?.version||'—'} · 命中强度不是语义概率`,'footnote'));
      output.hidden=false;status.textContent=`完成 ${result.results.length} 条，${changed} 条相较上次改变。模型／工具请求 0 次；测试不保存为会话。`;
    }catch(error){status.textContent=error.message;}
    finally{testing=false;submit.disabled=false;scope.disabled=false;base.disabled=false;questions.disabled=false;}
  };
  return {section,setBases(bases){const current=base.value;base.replaceChildren();option(base,'全局系统交互／默认检索','');bases.forEach(b=>option(base,b.name,b.id));base.value=bases.some(b=>b.id===current)?current:'';if(!output.hidden)status.textContent='配置已刷新，上次结果保留；重新测试可比较变化。';},
    prefill(text,baseId){questions.value=text||questions.value;base.value=baseId||'';scope.value=baseId?'knowledge':'document';questions.focus();}};
}
