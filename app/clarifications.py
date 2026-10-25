"""澄清仅保存用户确认；唯一请求键衔接一次新任务，不直接外调。"""
import json
from fastapi import HTTPException
from app.knowledge_tasks import dump,fingerprint
from app.repository import now_iso


def require(chat,identifier,clarification):
    conv=chat.detail(identifier)
    with chat.repository.connect() as db:
        row=db.execute('SELECT * FROM conversation_clarifications WHERE id=? AND conversation_id=? AND owner_user_id IS ?',
            (clarification,conv['canonical_id'],chat.owner)).fetchone()
    if not row:raise HTTPException(404,'澄清不存在或未获授权')
    current=chat.knowledge.require(conv['canonical_id'])
    if row['epoch']!=current['memory_epoch']:raise HTTPException(409,'记忆已变化，请重新提问')
    return conv,dict(row)


def resolve(chat,identifier,clarification,names,key):
    conv,row=require(chat,identifier,clarification)
    if not isinstance(names,list) or any(not isinstance(n,str) for n in names):raise HTTPException(422,'请填写对象名称')
    names=[n.strip() for n in names]
    if (len(names)!=row['needed_count'] or any(not isinstance(n,str) or not 1<=len(n.strip())<=60 for n in names)
        or len(set(names))!=len(names)):raise HTTPException(422,'请按要求确认一至两个不同对象，每个不超过60字')
    identity=fingerprint(names)
    if row['state'] in ('resolving','resolved') and (row['resolve_key']!=key or row['resolve_hash']!=identity):raise HTTPException(409,'该澄清已有其他确认')
    if row['state'] not in ('awaiting','resolving','resolved'):raise HTTPException(409,'澄清已取消')
    with chat.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        changed=db.execute("UPDATE conversation_clarifications SET state='resolving',resolve_key=?,resolve_hash=?,choices_json=?,updated_at=? WHERE id=? AND state='awaiting'",
            (key,identity,dump(names),now_iso(),clarification)).rowcount
        current=db.execute('SELECT * FROM conversation_clarifications WHERE id=?',(clarification,)).fetchone()
        if current['state'] not in ('resolving','resolved'):raise HTTPException(409,'澄清已取消')
        if not changed and (current['resolve_key']!=key or current['resolve_hash']!=identity):raise HTTPException(409,'澄清已变化')
        task=db.execute('SELECT input_json FROM kb_tasks WHERE id=?',(row['task_id'],)).fetchone()
    original=json.loads(task['input_json'])
    question=original['question']+'\n用户确认对象：'+'、'.join('“'+n.strip()+'”' for n in names)
    result=chat.submit(identifier,question,'clarify/'+key,original.get('document_filter'),clarification_id=clarification)
    with chat.repository.connect() as db:
        db.execute("UPDATE conversation_clarifications SET state='resolved',resolved_submission_id=?,updated_at=? WHERE id=? AND resolve_key=?",
            (result['id'],now_iso(),clarification,key))
    return result


def cancel(chat,identifier,clarification):
    require(chat,identifier,clarification)
    with chat.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT state FROM conversation_clarifications WHERE id=?',(clarification,)).fetchone()
        if row['state']=='resolved':raise HTTPException(409,'本轮已提交，请在任务中停止后续执行')
        db.execute("UPDATE conversation_clarifications SET state='cancelled',updated_at=? WHERE id=? AND state IN ('awaiting','resolving')",(now_iso(),clarification))
    return {'cancelled':True,'provider_calls':0}
