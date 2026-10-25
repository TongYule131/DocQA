import {createApiClient} from './api-client.js';
import {node} from './answer-view.js';
const api=createApiClient(),$=id=>document.getElementById(id);
const mb=bytes=>(bytes/1024/1024).toFixed(1)+' MB';
async function refresh(){try{
  const data=await api('/api/admin/operations'),panel=$('operations');panel.replaceChildren();
  panel.append(node('h2','队列与外部请求'),node('p',`执行中 ${data.active} · 排队 ${data.queued} · 本日已发送／不确定请求 ${data.calls_today}`),
    node('p',`全局同时执行 ${data.limits.active_global}，每用户 ${data.limits.active_user}；等待上限 ${data.limits.queue_global}／每用户 ${data.limits.queue_user}，等待超时 ${data.limits.queue_wait_seconds} 秒。`),
    node('h2','已报告用量'),node('p',`已知总token ${data.usage.known_total_tokens} · 用量未知请求 ${data.usage.unknown_usage_calls} · 可核实费用：未知`),
    node('h2','存储'),node('p',`原件 ${mb(data.storage.original_bytes)}／${mb(data.storage.global_limit_bytes)} · 尚未结算预留 ${mb(data.storage.reserved_bytes)} · 磁盘可用 ${mb(data.storage.disk_free_bytes)}`));
  panel.append(node('h2','需要处理'));
  const names={uncertain_calls:'存在不确定的在途请求，请核实费用，勿自动重发',pending_storage_reservations:'存在未完成的文件容量预留，请先核对原件',disk_low:'磁盘可用空间低于保留线',worker_not_ready:'执行者未就绪或进展已停滞',provider_circuit:'供应商处于熔断或半开探测',backup_unverified_or_old:'最近备份未知或超过7天，请核对独立备份'};
  if(!data.alerts.length)panel.append(node('p','未发现当前计数触发的告警。'));
  for(const alert of data.alerts)panel.append(node('p',names[alert.code]||'运行状态需要核对','error'));
  const latency=data.latency,format=(value)=>value===null?'未知':`${value} ms`;
  panel.append(node('h2','耗时与上下文'),node('p',`UTC今日知识库任务，最近最多${latency.sample_limit}条；完整任务 P95 ${format(latency.total.p95_ms)}（${latency.total.count}条），首个已校验块 P95 ${format(latency.first_validated_block.p95_ms)}（${latency.first_validated_block.count}条）。`),
    node('p',`外部调用 P95 ${format(latency.external_call.p95_ms)} · 失败 ${latency.external_failures} · 不确定 ${latency.external_uncertain} · 上下文最大 ${latency.context.max_chars??'未知'} 字符（${latency.context.count}条）。`));
  panel.append(node('h2','最近备份'),node('p',data.last_backup.status==='completed'?`${data.last_backup.completed_at} · schema ${data.last_backup.schema_version} · ${data.last_backup.file_count}个文件；仅表示备份完成，恢复验收另计。`:'未找到本目录的已完成备份记录。'),node('p','告警仅在本后台展示，未向外部账号发送通知。','muted'));
  panel.append(node('h2','服务状态'));
  const roles={web:'网页服务','parse-worker':'解析执行者','analysis-worker':'问答与分析执行者'};
  for(const state of [data.services.web,...(data.services.workers||[])])if(state)panel.append(node('p',`${roles[state.role]||'服务'} · ${state.ready?'就绪':'需要处理'} · ${state.detail||''}`));
}catch(error){$('error').textContent=error.message||'读取失败';}}
$('refresh').onclick=refresh;await window.docqaAuthReady;await refresh();
