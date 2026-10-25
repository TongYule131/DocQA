import pytest
from types import SimpleNamespace
from app.query_understanding import understand,validate_plan

SETTINGS=SimpleNamespace(query_term_mapping='{}')


@pytest.mark.parametrize('question',['它们有什么区别？','这两种算法分别有什么局限？','比较这两套方案。','请先问清具体是哪两套。'])
def test_unnamed_plural_objects_require_two_confirmed_names(question):
    result=understand(question,{},SETTINGS)
    assert result['ambiguous_reference'] and result['clarification']['needed_count']==2


def test_known_pair_resolves_plural_once_without_reprocessing_it_as_singular():
    memory={'entities':[{'name':'方法甲'},{'name':'方法乙'}],'comparison_objects':['方法甲','方法乙']}
    result=understand('它们有什么区别？前者的条件是什么？',memory,SETTINGS)
    assert not result['ambiguous_reference']
    assert result['resolved_question']=='方法甲与方法乙有什么区别？方法甲的条件是什么？'
    assert result['bindings'][0]['names']==['方法甲','方法乙']
    validated=validate_plan({'search_terms':[],'subquestions':[],'task_type':'comparison','bindings':[{'reference':'它们','name':'方法甲'}]},'它们有什么区别？',memory)
    assert validated['bindings'][0]['reference']=='它们'


def test_nonreferential_two_types_and_mixed_business_are_not_intercepted():
    assert not understand('论文提到的两种训练方法是什么？',{},SETTINGS)['ambiguous_reference']
    assert not understand('谢谢，先确认退货条件。',{},SETTINGS)['ambiguous_reference']
