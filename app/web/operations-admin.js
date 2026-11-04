import {createApiClient} from './api-client.js';
import {node} from './answer-view.js';
const api=createApiClient(),$=id=>document.getElementById(id);
const mb=bytes=>(Number(bytes||0)/1024/1024).toFixed(1)+' MB';
const num=value=>Number(value||0).toLocaleString('zh-CN');
const pct=(value,total)=>total>0?Math.min(100,Math.max(0,Math.round(value/total*100))):0;
const dateText=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'未知';
function card(title,text,extra){const section=node('section','','ops-card'),head=node('div','','ops-card-head');head.append(node('h2',title));if(text)head.append(node('span',text,'ops-card-note'));section.append(head);if(extra)section.classList.add(extra);return section;}
function meter(value,total,tone='blue'){const bar=node('div','','ops-meter'),fill=node('span','','ops-meter-fill '+tone);fill.style.width=pct(value,total)+'%';bar.append(fill);return bar;}
function kv(label,value){const row=node('div','','ops-kv');row.append(node('span',label),node('strong',value));return row;}
async function refresh(){
  try{
    $('error').textContent='';const data=await api('/api/admin/operations'),root=$('operations');root.replaceChildren();
    const limits=data.limits||{},usage=data.usage||{},storage=data.storage||{},latency=data.latency||{},budget=data.context_budget;
    const kpis=[
      ['执行中',num(data.active),`上限 ${num(limits.active_global)} · 每用户 ${num(limits.active_user)}`,'blue'],
      ['排队',num(data.queued),`上限 ${num(limits.queue_global)} · 等待 ${num(limits.queue_wait_seconds)}s`,'amber'],
      ['本日请求',num(data.calls_today),`未知用量 ${num(usage.unknown_usage_calls)} 次`,'purple'],
      ['已报告 token',num(usage.known_total_tokens),'费用：未知，不记为 0','green'],
    ];
    const kpiGrid=node('div','','ops-kpis');
    for(const [label,value,note,tone] of kpis){const item=node('article','','ops-kpi');item.append(node('span',label,'ops-kpi-label'),node('strong',value,'ops-kpi-value'),node('span',note,'ops-kpi-note'));item.classList.add('tone-'+tone);kpiGrid.append(item);}
    root.append(kpiGrid);

    const grid=node('div','','ops-grid');
    const storageCard=card('存储与容量','原件与磁盘',"span-2");
    const stored=storage.original_bytes||0,limit=storage.global_limit_bytes||0,ratio=pct(stored,limit);
    storageCard.append(meter(stored,limit));
    storageCard.append(kv('已用原件',`${mb(stored)} / ${mb(limit)}（${ratio}%）`),kv('未结算预留',mb(storage.reserved_bytes)),kv('磁盘可用',mb(storage.disk_free_bytes)),node('p','容量告警只在后台展示；未知用量不折算为容量或费用。','ops-footnote'));

    const budgetCard=card('上下文额度','UTC 今日');
    if(budget){budgetCard.append(meter(budget.reported_tokens,budget.daily_limit||0,'purple'),kv('已实报 token',`${num(budget.reported_tokens)} / ${num(budget.daily_limit)}`),kv('待核预留',num(budget.held_units)+' 计量单位'),kv('未知用量',num(budget.unknown_calls)+' 次'),kv('超出预留',num(budget.overrun_calls)+' 次'),node('p',`单任务默认 ${num(budget.task_limit)}。预留为估算，未知费用不记为零。`,'ops-footnote'));}
    else budgetCard.append(node('p','当前未启用统一上下文额度。','ops-footnote'));

    const latencyCard=card('耗时与上下文','UTC 今日 · 最近样本',"span-2");
    const metrics=node('div','','ops-metrics');
    const ms=value=>value===null||value===undefined?'未知':`${Math.round(value)} ms`;
    for(const [label,value,note] of [
      ['任务 P95',ms(latency.total?.p95_ms),`${num(latency.total?.count)} 条任务`],
      ['首块 P95',ms(latency.first_validated_block?.p95_ms),`${num(latency.first_validated_block?.count)} 条`],
      ['外部调用 P95',ms(latency.external_call?.p95_ms),`失败 ${num(latency.external_failures)} · 不确定 ${num(latency.external_uncertain)}`],
      ['上下文最大',latency.context?.max_chars==null?'未知':`${num(latency.context.max_chars)} 字符`,`${num(latency.context?.count)} 条样本`],
    ]){const box=node('div','','ops-metric');box.append(node('span',label),node('strong',value),node('span',note,'ops-metric-note'));metrics.append(box);}
    latencyCard.append(metrics);

    const servicesCard=card('服务状态','本地进程');
    const roles={web:'网页服务','parse-worker':'解析执行者','analysis-worker':'问答与分析执行者'};
    for(const state of [data.services?.web,...(data.services?.workers||[])].filter(Boolean)){
      const row=node('div','','ops-service'),dot=node('span','','ops-dot '+(state.ready?'ok':'warn'));dot.setAttribute('aria-hidden','true');
      row.append(dot,node('span',roles[state.role]||state.role||'服务','ops-service-name'),node('span',state.ready?'就绪':'需要处理','ops-service-state'));servicesCard.append(row);
      if(state.detail)servicesCard.append(node('p',state.detail,'ops-footnote'));
    }

    const alertCard=card('需要处理','告警仅后台展示',"span-2");
    const names={uncertain_calls:'存在不确定的在途请求，请核实费用，勿自动重发',pending_storage_reservations:'存在未完成的文件容量预留，请先核对原件',disk_low:'磁盘可用空间低于保留线',worker_not_ready:'执行者未就绪或进展已停滞',provider_circuit:'供应商处于熔断或半开探测',backup_unverified_or_old:'最近备份未知或超过 7 天，请核对独立备份',resident_audit_invalid:'登录驻留审计记录存在异常'};
    if(!(data.alerts||[]).length)alertCard.append(node('p','当前没有需要处理的计数告警。','ops-empty'));
    for(const alert of data.alerts||[]){const row=node('p','','ops-alert');row.append(node('span','•'),node('span',names[alert.code]||'运行状态需要核对'));alertCard.append(row);}

    const backupCard=card('最近备份','不代替恢复验收');
    backupCard.append(kv('状态',data.last_backup?.status==='completed'?'已完成':'未知'),kv('完成时间',dateText(data.last_backup?.completed_at)),kv('schema',String(data.last_backup?.schema_version??'—')),kv('文件数',num(data.last_backup?.file_count)));
    const session=data.resident_session_evidence||{};
    backupCard.append(node('p',session.different_logon_context_observed?'已观测到不同登录上下文；仍需核对实际注销与重新登录。':session.startup_count?'已记录本版驻留启动，尚无跨登录证据。':'尚无本版驻留启动记录。','ops-footnote'));

    grid.append(storageCard,budgetCard,servicesCard,latencyCard,alertCard,backupCard);root.append(grid);
    $('updated').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  }catch(error){$('error').textContent=error.message||'读取失败，请刷新重试';}
}
$('refresh').onclick=refresh;await window.docqaAuthReady;await refresh();
