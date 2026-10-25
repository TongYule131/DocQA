"""未见的短问句应保留关系/因果需求；泛指流程不触发编号教学优先。"""
import pytest
from app.query_planning import plan_query
from app.retrieval_planning import plan_retrieval
from app.schemas import Chunk,SourceLocation


@pytest.mark.parametrize('question,facet',[
    ('内层规范与外层目标是什么关系','conceptual'),('三类组件能按数量配对吗','conceptual'),
    ('依据定义说明该概念的用途','conceptual'),('为什么采用新机制仍有公平问题','multi_hop'),
    ('两种结构的维度有何不同','comparison'),('是否已经证明模型有效','boundary'),
])
def test_short_expressions_preserve_evidence_facets(question,facet):
    plan=plan_query(question);assert facet in plan['facets']
    if facet!='comparison':assert plan['requirements']


def test_general_process_relationship_does_not_prioritize_numbered_teaching_steps():
    texts=['模型内环流程与外环规范形成协同关系。','1. 教学第一阶段。模型流程。','2. 教学第二阶段。模型流程。']
    chunks=[Chunk(id=str(i),document_id='d',parse_version_id='v',text=t,chunk_type='text',order_index=i,
        sources=[SourceLocation(format='txt',line_start=i+1,line_end=i+1)]) for i,t in enumerate(texts)]
    result={'index_id':'idx','parse_version_id':'v','results':[dict(chunk_id=c.id,document_id='d',parse_version_id='v',text=c.text,score=.9-i*.1,
        chunk_type='text',order_index=i,sources=[s.model_dump() for s in c.sources]) for i,c in enumerate(chunks)]}
    question='模型内环流程与外环规范是什么关系'
    planned=plan_retrieval(question,result,chunks,query_plan=plan_query(question))
    assert planned['planning']['numbered_stage_groups']==0
    assert 'numbered_stage_context' not in planned['planning']['selection_reasons'].values()
