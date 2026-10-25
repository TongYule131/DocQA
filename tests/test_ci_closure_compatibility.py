"""首轮整包CI暴露的兼容边界，不放宽维护互斥或多对象澄清。"""
from copy import deepcopy
import pickle
from types import SimpleNamespace
from app.retrieval_channels import Discard
from app.query_understanding import understand


def test_discard_report_copy_and_roundtrip_preserve_machine_attribution():
    source={'discarded':[Discard('chunk-a','context_budget','预算拒绝')]}
    for report in (deepcopy(source),pickle.loads(pickle.dumps(source))):
        value=report['discarded'][0]
        assert str(value)=='预算拒绝' and value.chunk_id=='chunk-a' and value.code=='context_budget'


def test_server_selected_document_resolves_only_unambiguous_material_overview():
    settings=SimpleNamespace(query_term_mapping='{}');question='它一共有多少个部分？'
    result=understand(question,{},settings,scope='document')
    assert not result['ambiguous_reference'] and result['resolved_question']=='当前文档一共有多少个部分？'
    assert result['original_question']==question
    assert understand(question,{},settings)['ambiguous_reference']
    assert understand('它在什么条件下有效？',{},settings,scope='document')['ambiguous_reference']
    memory={'entities':[{'name':'方法甲'},{'name':'方法乙'}]}
    assert understand(question,memory,settings,scope='document')['ambiguous_reference']
