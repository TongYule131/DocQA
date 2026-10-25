"""会话实体的当前 epoch 可编辑投影；清除／停用后不回读历史实体。"""
import json
from fastapi import HTTPException
from app.query_understanding import build_memory, ENTITY_VERSION, MEMORY_TURN_LIMIT, explicit_correction
from app.repository import now_iso


def validate_entities(values):
    if (not isinstance(values, list) or len(values) > 12 or any(not isinstance(e, dict)
            or set(e) != {'name', 'source_turn', 'source'} or not isinstance(e['name'], str)
            or not 1 <= len(e['name'].strip()) <= 60 or e['source'] not in ('user_question', 'user_edit')
            or not isinstance(e['source_turn'], (str, int)) or len(str(e['source_turn'])) > 150 for e in values)):
        raise ValueError('invalid_entity_memory')
    return values


def context(db, conv, settings):
    if not conv['memory_enabled']:
        return build_memory([])
    history = [{'turn':r['turn_no'],'question':json.loads(r['input_json'])['question'][:600],
                'missing_points':(json.loads(r['result_json']) if r['result_json'] else {}).get('missing_points',[])}
               for r in db.execute("SELECT t.turn_no,j.input_json,j.result_json FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id "
                   "WHERE t.conversation_id=? AND j.status='succeeded' AND json_extract(j.input_json,'$.memory_epoch')=? "
                   "AND json_extract(j.input_json,'$.route.kind')!='SYSTEM' ORDER BY t.turn_no DESC LIMIT ?",
                   (conv['id'], conv['memory_epoch'],MEMORY_TURN_LIMIT))][::-1]
    row = db.execute('SELECT * FROM conversation_entity_memory WHERE conversation_id=? AND epoch=?',
                     (conv['id'], conv['memory_epoch'])).fetchone()
    saved=json.loads(row['entities_json']) if row else None
    seed=saved if isinstance(saved,dict) and saved.get('kind')=='bridge_seed' else None
    edit_record=saved if isinstance(saved,dict) and saved.get('kind')=='memory_edit' else None
    edited=validate_entities(edit_record['entities']) if edit_record and edit_record['mode']=='replace' else validate_entities(saved) if row and seed is None and edit_record is None else None
    initial=seed or (edit_record.get('bridge_seed') if edit_record else None)
    result = build_memory(history, notes=conv['memory_notes'], edited=edited,
                          summary_enabled=settings.memory_summary_enabled,
                          seed=validate_entities(initial['entities']) if initial and edited is None else (),
                          seed_comparison=initial.get('comparison_objects',[]) if initial and edited is None else ())
    result['mode']=edit_record['mode'] if edit_record else 'replace' if edited is not None else 'auto'
    if edit_record and edit_record['mode']=='merge':
        result['entities']=list({e['name']:e for e in [*result['entities'],*validate_entities(edit_record['entities'])]}.values())[-12:]
    result['revision'] = row['revision'] if row else 0
    result['epoch'] = conv['memory_epoch']
    return result


def edit(knowledge, identifier, values, expected_revision, mode='replace'):
    if mode not in ('auto','replace','merge'):raise HTTPException(422,'记忆模式无效')
    conv = knowledge.require(identifier)
    if not conv['memory_enabled']:
        raise HTTPException(409, '请先开启记忆')
    try:
        values = validate_entities([{'name': name.strip(), 'source_turn': 'user', 'source': 'user_edit'} for name in values])
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, '实体须为至多12个、每个1至60字的名称') from None
    with knowledge.repository.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT * FROM kb_conversations WHERE id=?', (identifier,)).fetchone()
        if current['revision'] != expected_revision or current['memory_epoch'] != conv['memory_epoch']:
            raise HTTPException(409, '会话已变化，请刷新')
        if db.execute("SELECT 1 FROM kb_conversation_turns t JOIN kb_tasks j ON j.id=t.task_id WHERE t.conversation_id=? AND j.status IN ('queued','running')", (identifier,)).fetchone():
            raise HTTPException(409, '请等待本轮完成或停止后修改记忆')
        prior=db.execute('SELECT entities_json FROM conversation_entity_memory WHERE conversation_id=? AND epoch=?',(identifier,conv['memory_epoch'])).fetchone()
        prior=json.loads(prior[0]) if prior else None
        bridge=prior if isinstance(prior,dict) and prior.get('kind')=='bridge_seed' else prior.get('bridge_seed') if isinstance(prior,dict) else None
        db.execute('INSERT INTO conversation_entity_memory VALUES (?,?,?,?,1,?) ON CONFLICT(conversation_id,epoch) DO UPDATE SET entities_json=excluded.entities_json,revision=revision+1,updated_at=excluded.updated_at',
                   (identifier, conv['memory_epoch'], ENTITY_VERSION, json.dumps({'kind':'memory_edit','mode':mode,'entities':values,'bridge_seed':bridge}, ensure_ascii=False), now_iso()))
        db.execute('UPDATE kb_conversations SET revision=revision+1,updated_at=? WHERE id=?', (now_iso(), identifier))
    return knowledge.detail(identifier)
