import { node } from './answer-view.js';
import { createApiClient } from './api-client.js';

export function installIntentTest() {
  const section=node('div'); section.id='intent-test-view'; section.hidden=true;
  document.getElementById('intent-tree-view').before(section);
  const panel=node('section','','panel'),form=node('form');
  panel.append(node('h2','预览受控规则与查询计划'),node('p','这里只预览本地规则及证据需求，不执行检索、模型或工具。普通LLM／文档自动分流在会话worker执行，本页没有运行该模型，候选结果不代表最终动作。','muted'));
  const controls=node('div','','form-grid'),scope=node('select'),base=node('select');
  scope.id='test-scope';base.id='test-base';
  const option=(select,label,value)=>{const o=node('option',label);o.value=value;select.append(o);};
  option(scope,'知识库会话','knowledge');option(scope,'兼容文档入口','document');
  for(const [title,id,input] of [['使用入口','test-scope',scope],['关联知识库（可不选）','test-base',base]]) {
    const label=node('label',title);label.htmlFor=id;label.append(input);controls.append(label);
  }
  const label=node('label','测试问题（每行一条，最多100条）');label.htmlFor='intent-test-questions';
  const questions=node('textarea');questions.id='intent-test-questions';questions.rows=5;questions.maxLength=50000;
  questions.value='这份文档讲了什么\n比较两种机制的适用条件\n核对公式与正文是否一致\n两组概念是否一一对应\n你好';label.append(questions);
  const contextLabel=node('label','用户上下文（可选JSON：实体、比较对象和条件；不是证据）'),context=node('textarea');context.rows=3;context.maxLength=12000;
  context.setAttribute('aria-label','测试用户上下文');contextLabel.append(context);
  const submit=node('button','测试分流');submit.type='submit';
  const status=node('p','','muted');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  form.append(controls,label,contextLabel,submit,status);panel.append(form);section.append(panel);
  const output=node('section','','panel');output.hidden=true;section.append(output);
  const previous=new Map(),api=createApiClient();let testing=false;
  const methods={exact:'完整示例',interaction:'纯交互规则',keywords:'业务关键词',fallback:'回退检索',semantic:'已校准语义候选'};
  form.onsubmit=async event=>{
    event.preventDefault();if(testing)return;
    const batch=questions.value.split('\n').map(q=>q.trim()).filter(Boolean);
    if(!batch.length||batch.length>100||batch.some(q=>q.length>500)){status.textContent='请输入1～100条问题，每条不超过500字符。';return;}
    let userContext=null;try{userContext=context.value.trim()?JSON.parse(context.value):null;}catch{status.textContent='用户上下文须为有效JSON。';return;}
    testing=true;submit.disabled=true;scope.disabled=true;base.disabled=true;questions.disabled=true;status.textContent='正在测试当前配置…';
    try {
      const result=await api('/api/admin/intents/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({questions:batch,scope:scope.value,base_id:base.value||null,context:userContext})});
      output.replaceChildren(node('h2','分流结果'));
      const table=node('table'),head=node('thead'),header=node('tr');
      for(const title of ['问题','业务路径','任务策略与证据需求','命中依据','候选节点','上次结果'])header.append(node('th',title));head.append(header);table.append(head);
      const body=node('tbody');let changed=0;
      for(const row of result.results){
        const d=row.decision,key=JSON.stringify([scope.value,base.value,row.question,userContext]),prior=previous.get(key);
        const signature=`${d.code} / ${row.query_plan?.task_type||'legacy'}`;
        if(prior&&prior!==signature)changed++;
        const tr=node('tr');tr.append(node('td',row.question));
        const route=node('td');route.append(node('strong',d.name),node('p',`${d.kind} · ${d.code}`,'node-code'));
        if(row.response_preview)route.append(node('p',row.response_preview));
        if(row.top_k)route.append(node('small',`TopK ${row.top_k}`));
        if(row.availability&&!row.availability.ready)route.append(node('p',row.availability.reason,'form-error'));
        if(row.understanding){route.append(node('p','理解后问题：'+row.understanding.resolved_question));if(row.understanding.clarification)route.append(node('p',row.understanding.clarification.question,'form-error'));}
        if(row.semantic_pending)route.append(node('p','语义候选待实际执行时复用查询向量；此处未发向量请求。'));
        if(row.execution){
          route.append(node('p','执行动作：'+row.execution.name));
          if(row.execution.resources.tool_id)route.append(node('p','工具：'+row.execution.resources.tool_id));
          if(row.execution.requires_retrieval)route.append(node('small','范围：当前授权发布快照；文档筛选继续生效'));
        }
        const strategy=node('td'),plan=row.query_plan,budget=row.retrieval_budget;
        if(plan){
          strategy.append(node('strong',plan.name));
          plan.effects.forEach(effect=>strategy.append(node('p',effect,'muted-detail')));
          if(plan.required_evidence.length)strategy.append(node('p',`证据需求：${plan.required_evidence.join('、')}`));
          if(budget?.candidate_limit)strategy.append(node('small',`召回 ${budget.recall_limit} / 候选 ${budget.candidate_limit} / 最终最多 ${budget.context_limit} 块 · ${budget.context_chars} 字符硬上限`));
        }
        const reason=node('td');reason.append(node('p',methods[d.method]||d.method),node('p',d.reason,'muted-detail'));
        const candidates=[...d.candidates,...(d.semantic_candidates||[])];
        tr.append(route,strategy,reason,node('td',candidates.map(c=>`${c.name||c.code||c.node_id} · ${methods[c.method]||c.method||'语义候选'}${Number.isFinite(c.score)?' · '+c.score.toFixed(3):''}`).join('；')||'自动任务策略'),node('td',prior||'首次测试'));
        body.append(tr);previous.set(key,signature);
      }
      table.append(body);const scroll=node('div','','table-scroll intent-test-table');scroll.append(table);output.append(scroll);
      const d=result.results[0]?.decision;
      output.append(node('p',`配置 ${d?.configuration.slice(0,12)||'—'} · ${d?.version||'—'} · 命中强度不是语义概率`,'footnote'));
      output.hidden=false;status.textContent=`完成 ${result.results.length} 条，${changed} 条相较上次改变。模型／工具请求 0 次；测试不保存为会话。`;
    }catch(error){status.textContent=error.message;}
    finally{testing=false;submit.disabled=false;scope.disabled=false;base.disabled=false;questions.disabled=false;}
  };
  installSemanticControls(section,api);
  return {section,setBases(bases){const current=base.value;base.replaceChildren();option(base,'全局系统交互／默认检索','');bases.forEach(b=>option(base,b.name,b.id));base.value=bases.some(b=>b.id===current)?current:'';if(!output.hidden)status.textContent='配置已刷新，上次结果保留；重新测试可比较变化。';},
    prefill(text,baseId){questions.value=text||questions.value;base.value=baseId||'';scope.value=baseId?'knowledge':'document';questions.focus();}};
}

function installSemanticControls(section,api){
  const panel=node('section','','panel semantic-controls'),status=node('p','','muted'),refresh=node('button','查看语义策略状态','secondary');refresh.type='button';
  const prepare=node('button','按上限准备语义示例（会产生向量请求）'),limit=node('input'),ack=node('input'),ackLabel=node('label',''),calibration=node('input');
  prepare.type='button';limit.type='number';limit.min='1';limit.max='20';limit.value='4';limit.setAttribute('aria-label','示例准备请求上限');
  ack.type='checkbox';ackLabel.className='row';ackLabel.append(ack,node('span','我已核实上次不确定请求，允许明确重试；可能重复计费'));
  calibration.type='file';calibration.accept='.json,application/json';calibration.setAttribute('aria-label','导入已审核校准JSON（cases、threshold、margin）');
  const prepareLabel=node('label','本次示例准备最多请求数','prepare-label');prepareLabel.append(limit);
  const calibrationLabel=node('label','导入已审核校准JSON（cases、threshold、margin；仅读取保存向量）','calibration-label');calibrationLabel.append(calibration);
  panel.append(node('h2','语义候选准备与校准'),node('p','默认关闭。准备示例由后台任务按上限执行，复用相同版本缓存；测试台本身不发模型请求。校准只读取已有向量和标签，必须包含正例与回退负例。相似度不是正确概率。'),prepareLabel,prepare,ackLabel,refresh,calibrationLabel,status);section.append(panel);
  let requestKey=null;
  const inspect=async()=>{const r=await api('/api/admin/intents/semantic/status');status.textContent=`功能${r.enabled?'已开启':'已关闭'} · ${r.calibrated?'已有有效校准':'尚无有效校准'} · 门槛 ${r.threshold??'未设置'} · 分差 ${r.margin??'未设置'}`;};
  refresh.onclick=async()=>{try{await inspect();}catch(e){status.textContent=e.message;}};
  prepare.onclick=async()=>{const maximum=Number(limit.value);if(!Number.isInteger(maximum)||maximum<1||maximum>20){status.textContent='请求上限须为1～20。';return;}
    requestKey=requestKey||crypto.randomUUID();prepare.disabled=true;try{const r=await api('/api/admin/intents/semantic/prepare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_requests:maximum,request_key:requestKey,acknowledge_uncertain:ack.checked})});
      status.textContent=`准备任务 ${r.task_id} · ${r.status} · 最多新增 ${r.expected_http_requests} 次HTTP。完成后刷新状态；示例或校准变化需重新校准。`;requestKey=null;ack.checked=false;
    }catch(e){status.textContent=e.message;}finally{prepare.disabled=false;}};
  calibration.onchange=async()=>{if(!calibration.files?.[0])return;try{const body=JSON.parse(await calibration.files[0].text());const r=await api('/api/admin/intents/semantic/calibrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});status.textContent=`校准 ${r.correct}/${r.total}，未产生模型请求；此记录不代替独立人工语义验收。`;}catch(e){status.textContent=e.message;}};
}
