"""工具补参只恢复已确认的原意图、工具和参数；不重新猜路由或在接口中外调。"""
import json
from fastapi import HTTPException
from app.knowledge_tasks import dump, fingerprint


def question_with_values(original, fields, values):
    return original + '\n用户补充参数：\n' + '\n'.join(field['name'] + '：' + value for field, value in zip(fields, values))


def resolve_continuation(manager, question, base_id, context, confirmation_id, conversation):
    with manager.repository.connect() as db:
        row=db.execute('SELECT * FROM conversation_clarifications WHERE id=? AND conversation_id=? AND owner_user_id IS ?',
                       (confirmation_id,conversation['id'],conversation['owner_user_id'])).fetchone()
        if not row or row['state'] not in ('resolving','resolved') or row['epoch']!=conversation['memory_epoch']:
            raise HTTPException(409,'补充信息已取消或会话状态已变化，请重新提问')
        source=db.execute('SELECT * FROM kb_tasks WHERE id=?',(row['task_id'],)).fetchone()
        if not source or source['status']!='succeeded' or source['base_id']!=base_id:
            raise HTTPException(409,'原工具查询已失效')
        result=json.loads(source['result_json']);clar=result.get('clarification',{})
        if clar.get('kind')!='tool_parameters':raise HTTPException(409,'此确认不属于工具补参')
        original=json.loads(source['input_json'])
        resolved=db.execute("SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name='resolved_intent'",(source['id'],)).fetchone()
        route=json.loads(resolved[0])['route'] if resolved else original['route']
        saved=db.execute("SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name='tool_parameters'",(source['id'],)).fetchone()
        arguments=json.loads(saved[0]) if saved else None
        values=json.loads(row['choices_json'])
    if (not isinstance(arguments,dict) or len(values)!=len(clar['fields']) or
        question!=question_with_values(original['question'],clar['fields'],values)):
        raise HTTPException(409,'补参内容与原请求不一致')
    from app.intent_routing import eligible_rows,ROUTING_VERSION
    current=next((r for r in eligible_rows(manager.list(),base_id) if r['id']==route.get('id')),None)
    if current!=route or route['kind']!='MCP':raise HTTPException(409,'原意图配置已变化，请重新提问')
    from app.knowledge_tools import ToolRegistry
    tool=ToolRegistry(manager.repository).get(route['tool_id'])
    if not tool or not tool['enabled'] or not tool['read_only'] or tool['revision']!=clar['tool_revision']:
        raise HTTPException(409,'原工具已停用或配置已变化，请重新提问')
    from app.query_understanding import understand
    from app.resource_governance import policy
    from app.query_planning import plan_query
    from app.intent_execution import execution_contract
    understanding=understand(question,context,policy(manager.repository));plan=plan_query(question,route)
    decision={'version':ROUTING_VERSION,'configuration':fingerprint([route,tool['revision']]),'scope':'knowledge',
              'code':route['code'],'name':route['name'],'kind':'MCP','method':'tool_continuation',
              'reason':'用户补充原查询所缺参数；保留原工具和已确认参数','candidates':[],'semantic_candidates':[]}
    return {'route':route,'business_route':route,'decision':decision,'query_plan':plan,'understanding':understanding,
            'execution':execution_contract(route,understanding,plan),'semantic_pending':False,
            'tool_continuation':{'confirmation_id':confirmation_id,'known_arguments':arguments,
                                 'confirmed_fields':dict(zip([f['name'] for f in clar['fields']],values))}}
