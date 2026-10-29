"""知识库新版本生效后，只为仍获授权且真正引用旧版本的本人回答开放历史来源。"""
import json

import pytest

from app.knowledge_worker import KnowledgeWorker
from app.repository import now_iso
from app.schemas import Chunk,SourceLocation
from tests.test_product_access import team,publish
from tests.test_question_jobs import LocalRuntime,QUESTION


@pytest.mark.parametrize('state',['valid','foreign_owner','failed','deleted_conversation','uncited','malformed_json','malformed_citation','revoked'])
def test_old_knowledge_citation_access_keeps_owner_result_and_current_scope_required(team,state):
    env,users,clients=team;admin,alice,bob=clients['admin'],clients['alice'],clients['bob'];base=publish(clients,users)
    conv=alice.post('/api/kb-conversations',json={'base_id':base['id'],'request_key':'historical'}).json()['id']
    task=alice.post('/api/kb-conversations/'+conv+'/turns',json={'question':QUESTION,'request_key':'old-answer'}).json()['id']
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    worker.run_once(task);old=worker.tasks.row(task);assert old['status']=='succeeded',old
    assert json.loads(old['result_json'])['citations'][0]['parse_version_id']=='qv1'
    env.repository.finish_parse('qdoc',1,[Chunk(id='q-new',document_id='qdoc',parse_version_id='qv2',page=1,order_index=0,
        text='全书内容分为22个部分。',chunk_type='text',sources=[SourceLocation(format='txt',line_start=1,line_end=1)])])
    env.rag.index.build('qdoc')
    response=admin.post('/api/knowledge-bases/'+base['id']+'/publish',json={'expected_revision':1})
    assert response.status_code==200,response.text
    with env.repository.connect() as db:
        if state=='foreign_owner':db.execute('UPDATE kb_tasks SET owner_user_id=? WHERE id=?',(users['bob']['id'],task))
        if state=='failed':db.execute("UPDATE kb_tasks SET status='failed' WHERE id=?",(task,))
        if state=='deleted_conversation':db.execute('UPDATE kb_conversations SET deleted_at=? WHERE id=?',(now_iso(),conv))
        if state=='uncited':
            result=json.loads(old['result_json']);result['citations']=[]
            db.execute('UPDATE kb_tasks SET result_json=? WHERE id=?',(json.dumps(result),task))
        if state=='malformed_json':db.execute("UPDATE kb_tasks SET result_json='not-json' WHERE id=?",(task,))
        if state=='malformed_citation':db.execute('UPDATE kb_tasks SET result_json=? WHERE id=?',(json.dumps({'citations':['not-an-object']}),task))
        if state=='revoked':db.execute('DELETE FROM knowledge_base_members WHERE knowledge_base_id=? AND user_id=?',(base['id'],users['alice']['id']))
    counts=len(env.embedding.calls),len(env.model.calls)
    old_source='/api/documents/qdoc/chunks?version_id=qv1'
    response=alice.get(old_source)
    assert response.status_code==(200 if state=='valid' else 404),response.text
    if state=='valid':
        assert response.json()[0]['text'].startswith('全书内容分为21')
        assert bob.get(old_source).status_code==404
        assert alice.get('/api/documents/qdoc/content?version_id=qv1').status_code==200
        assert alice.get('/api/documents/qdoc/chunks?version_id=not-this-version').status_code==404
    assert admin.get(old_source).status_code==200
    assert counts==(len(env.embedding.calls),len(env.model.calls))
