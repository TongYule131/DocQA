"""抽取、未展示范围拒引、跨页逐论断绑定、推断风险和持久化反例。"""
import copy
import json

import pytest

from app.config import Settings
from app.quote_spans import task_segment_payload
from app.rag_context import EvidenceItem,EvidencePack,build_evidence
from app.rag_extract_context import (SELECTION_VERSION,strict_segments,extract_ranges,visible_segments,
    continuation_pairs,plan_admission,budget_profile,admitted_ranges)
from app.rag_support_review import review_claims
from app.rag_validation import ValidatedFact,ValidatedOutput,ModelOutputError
from app.rag_answer_contract import validate_task_output,fact_display_text
from tests.test_rag_claim_evidence import body,fact
from tests.test_retrieval_planning import chunk,vector_result


def item(text,ref=1,chunk_id='c1',order=0,page=1,heading='规则',kind='text',version='v'):
    return EvidenceItem(reference_id=ref,chunk_id=chunk_id,document_id='d',parse_version_id=version,
        page=page,text=text,order_index=order,heading_path=heading,chunk_type=kind)


def pack(items,pairs=None):
    for i in items:
        i.read_ranges=[[0,len(i.text)]];i.segment_policy='sentence-segments-v2'
    from app.rag_extract_context import assign_unit_options
    assign_unit_options(items,pairs or [])
    return EvidencePack(items=items,candidates=items,index_id='idx',parse_version_id='v',truncated=False,
        context_chars=0,review_policy=SELECTION_VERSION,continuation_pairs=pairs or [])


def output(facts,bindings,quotes):
    return ValidatedOutput(status='answered',conclusion=facts,quotes=quotes,citation_bindings=bindings)


@pytest.mark.parametrize('text',['短句。下一句。','😀指标为1.25万元，条件不可省略。\n下一行。','甲'*120+'；乙。',
    '句末有文献[21]。仍保留原位置。','第一行\r\n第二行。'])
def test_strict_segment_roundtrip_never_cleans_source(text):
    assert ''.join(strict_segments(text))==text
    assert all(0<len(s)<=60 for s in strict_segments(text))


def test_extraction_keeps_conditions_and_never_paraphrases_or_mutates():
    source='背景资料。'*90+'设备运行温度为适配关键条件。仅在接口版本一致时有效。'+'无关历史。'*90
    ranges=extract_ranges(source,'设备的运行温度和适配条件是什么？','text',max_chars=150)
    selected=''.join(source[a:b] for a,b in ranges)
    assert '设备运行温度' in selected and '仅在接口版本一致时有效。' in selected
    assert len(selected)<len(source)/2
    assert all(source[a:b] in source for a,b in ranges)


def test_extracting_an_anchor_keeps_dependent_following_detail():
    source='无关背景。'*100+'海外顾客不理解旧接口。很多操作他们也不会使用。'+'其他历史。'*100
    selected=''.join(source[a:b] for a,b in extract_ranges(source,'海外顾客为何不理解旧接口？','text',max_chars=30))
    assert '很多操作他们也不会使用。' in selected


def test_ocr_spacing_is_normalized_only_for_scoring_not_for_quotes_or_offsets():
    source='历史资料。'*80+'接 口 版 本 决 定 设 备 适 配。'+'无关材料。'*80
    ranges=extract_ranges(source,'接口版本决定什么？','text',max_chars=80)
    selected=''.join(source[a:b] for a,b in ranges)
    assert '接 口 版 本 决 定 设 备 适 配。' in selected
    assert len(selected)<len(source)/2


def test_overview_keeps_full_source_instead_of_claiming_full_reading_after_compression():
    source=item('概述所有内容。'*150)
    profile=budget_profile('总结全文',max_items=32,context_chars=24000)
    plan_admission([source],'总结全文',profile)
    assert source.read_ranges==[[0,len(source.text)]]


def test_table_and_short_source_keep_headers_footnotes_zero_and_empty():
    text='单位：万元\n主体|收入|备注\n甲|0|\n注：未经审计，不适用于集团。'*40
    assert extract_ranges(text,'收入是多少？','table_rows',max_chars=80)==[[0,len(text)]]
    assert extract_ranges('完整短句。','问题','text')==[[0,len('完整短句。')]]


def test_spaced_quote_and_bibliography_after_full_stop_do_not_create_false_continuation():
    left=item('独立完整观点。'*12+"这是最后的完整观点。 ' [20]")
    right=item('下段重新介绍另一位作者的理论。',ref=2,chunk_id='c2',order=1)
    assert continuation_pairs([left,right])==[]


def test_leading_bibliography_and_outer_quotes_are_not_mandatory_sentence_body():
    source=item("前句完整。' [22] 适配条件必须一致。 '")
    start=source.text.index('适配');end=source.text.index('。',start)+1
    value=output([ValidatedFact('适配条件必须一致。',[1],claim_type='direct')],
        {1:dict(source_ref=1,char_start=start,char_end=end)},{1:source.text[start:end]})
    review_claims(value,pack([source]))


def test_predeclared_quote_units_cover_full_sentence_across_windows_without_fabrication():
    from app.rag_extract_context import quote_units,support_sentence_ranges
    text='界'*730+'。独立的另一句。'
    units=quote_units(text,[[0,len(text)]])
    visible={span:positions for span,_,positions in visible_segments(text,[[0,len(text)]],strict=True)}
    assert len(units['1'])>1
    for number,parts in units.items():
        from app.rag_extract_context import merge_ranges
        ranges=merge_ranges([visible[span][segment] for span,first,last in parts for segment in range(first,last+1)])
        a,b=support_sentence_ranges(text)[int(number)-1]
        assert any(x<=a and b<=y for x,y in ranges)
    partial=quote_units(text,[[731,len(text)]])
    assert '1' not in partial


def test_ellipsis_inside_a_list_does_not_drop_the_following_predicate():
    source='海外顾客不认识甲、乙……这些人物是谁呢？很多剧情他们也不理解。'
    selected=''.join(source[a:b] for a,b in extract_ranges(source,'海外顾客为何不认识甲？','text',max_chars=18))
    assert '这些人物是谁呢？' in selected and '很多剧情他们也不理解。' in selected


def test_sentence_body_check_still_rejects_partial_predicate_after_bibliography():
    source=item('[8]设备适配的必要条件是接口一致。')
    start=source.text.index('必要');end=len(source.text)
    value=output([ValidatedFact('必要条件是接口一致。',[1],claim_type='direct')],
        {1:dict(source_ref=1,char_start=start,char_end=end)},{1:source.text[start:end]})
    with pytest.raises(ModelOutputError,match='incomplete_quote_sentence'):review_claims(value,pack([source]))


@pytest.mark.parametrize('mutation',['hidden','unknown','duplicate','old_ranges','invalid_refs','extra_root'])
def test_v5_unit_selection_rejects_forged_or_hidden_units_without_fallback(mutation):
    source=item('第一条完整规则。第二条完整规则。')
    evidence=pack([source]);source.read_ranges=[[8,len(source.text)]]
    value=body([fact()]);value.pop('evidence_quotes')
    value['conclusion'][0]['evidence']=[dict(ref=1,unit=2)]
    if mutation=='hidden':value['conclusion'][0]['evidence'][0]['unit']=1
    if mutation=='unknown':value['conclusion'][0]['evidence'][0]['unit']=999
    if mutation=='duplicate':value['conclusion'][0]['evidence']*=2
    if mutation=='old_ranges':value['conclusion'][0]['evidence']=[dict(ref=1,span=1,start_segment=1,end_segment=2)]
    if mutation=='invalid_refs':value['conclusion'][0]['refs']=None
    if mutation=='extra_root':value['evidence_quotes']=[]
    with pytest.raises(ModelOutputError):
        validate_task_output(json.dumps(value,ensure_ascii=False),evidence,question='规则是什么？',contract_version='fact-context-parts-v5')


def test_v5_exact_unit_uses_original_ranges_and_preserves_body_and_ordinal_locator():
    source=item('第3节规定，设备收入为21万元。')
    evidence=pack([source]);value=body([fact('第3节介绍接口规则。')]);value.pop('evidence_quotes')
    value['conclusion'][0]['evidence']=[dict(ref=1,unit=1)]
    validated=validate_task_output(json.dumps(value,ensure_ascii=False),evidence,question='规则是什么？',contract_version='fact-context-parts-v5')
    assert validated.quotes[1]==source.text
    assert validated.citation_bindings[1]==dict(source_ref=1,char_start=0,char_end=len(source.text))
    # 排除章节定位，不排除真实统计数字，仍需context和数值来源。
    value['conclusion'][0]['text']='第3节收入为21万元。'
    with pytest.raises(ModelOutputError,match='numeric_context_missing'):
        validate_task_output(json.dumps(value,ensure_ascii=False),evidence,question='收入多少？',contract_version='fact-context-parts-v5')


@pytest.mark.parametrize('source_amount,claim_amount,accepted',[('1 000','1000',True),('1 00','100',False),
    ('10 00','1000',False),('1 000','1001',False),('−1 000','1000',False),('1 000.25','1000.25',True)])
def test_unambiguous_thousands_grouping_only_is_numeric_equivalence(source_amount,claim_amount,accepted):
    source=item('收入为'+source_amount+'万元。');evidence=pack([source])
    value=body([fact('收入为'+claim_amount+'万元。')]);value.pop('evidence_quotes')
    value['conclusion'][0].update(evidence=[dict(ref=1,unit=1)],context=[dict(kind='unit',ref=1,span=1,segment=1)])
    if accepted:
        result=validate_task_output(json.dumps(value,ensure_ascii=False),evidence,question='收入多少？',contract_version='fact-context-parts-v5')
        assert result.quotes[1]==source.text
    else:
        with pytest.raises(ModelOutputError,match='number_not_in_quote'):
            validate_task_output(json.dumps(value,ensure_ascii=False),evidence,question='收入多少？',contract_version='fact-context-parts-v5')


def test_subsection_locator_does_not_erase_statistical_value_or_unit_requirement():
    from app.rag_answer_contract import _tokens
    assert _tokens('第3.1节说明第2章，第4页。')==set()
    assert _tokens('第3.1节收入为21万元。')=={'21'}
    assert _tokens('第3.2、3.3分别与第二节比较。')==set()
    assert _tokens('原文在“2 世界概念”开篇指出观点。')==set()
    assert _tokens('收入为2万元。')=={'2'}


def test_pdf_wrapped_line_is_not_a_standalone_citable_sentence():
    from app.rag_extract_context import quote_units,support_sentence_ranges
    text='这样的评价也同样发生在《星河：旅途》等游\n戏上。'
    assert len(support_sentence_ranges(text))==1
    units=quote_units(text,[[0,len(text)]])
    assert len(units)==1
    source=item(text);evidence=pack([source]);start=text.index('戏上')
    value=output([ValidatedFact('这种评价发生在该游戏上。',[1],claim_type='direct')],
        {1:dict(source_ref=1,char_start=start,char_end=len(text))},{1:text[start:]})
    with pytest.raises(ModelOutputError,match='incomplete_quote_sentence'):review_claims(value,evidence)


def test_trailing_space_only_does_not_bind_next_independent_sentence_to_previous_page():
    left=item('完整背景。'*12+'也可',order=0)
    right=item('解释另一个园区的困难。 独立的新规则。',ref=2,chunk_id='c2',order=1)
    pairs=continuation_pairs([left,right]);evidence=pack([left,right],pairs)
    start=right.text.index('。')+1
    value=output([ValidatedFact('存在独立的新规则。',[2],claim_type='direct')],
        {2:dict(source_ref=2,char_start=start,char_end=len(right.text))},{2:right.text[start:]})
    review_claims(value,evidence)


def test_user_boundary_can_read_only_own_contiguous_original_ranges_across_windows():
    from app.rag_validation import validate_user_condition_boundary
    source=item('甲'*55+'共同比较概念必须准确。')
    evidence=pack([source]);start=source.text.index('共同')
    value=output([ValidatedFact('“共同比较概念”由资料解释。',[1,2],claim_type='direct')],
        {1:dict(source_ref=1,char_start=0,char_end=60),2:dict(source_ref=1,char_start=60,char_end=len(source.text))},
        {1:source.text[:60],2:source.text[60:]})
    validate_user_condition_boundary(value,evidence,question='请解释“共同比较概念”。')
    value.conclusion[0].refs=[2]
    with pytest.raises(ModelOutputError,match='mixed_user_source'):
        validate_user_condition_boundary(value,evidence,question='请解释“共同比较概念”。')


def test_historical_century_remains_number_checked_without_statistical_context_requirement():
    source=item('21世纪初该游戏在区域市场流行。');evidence=pack([source])
    value=body([fact('21世纪初该游戏在区域市场流行。')]);value.pop('evidence_quotes')
    value['conclusion'][0]['evidence']=[dict(ref=1,unit=1)]
    validate_task_output(json.dumps(value,ensure_ascii=False),evidence,question='理论能否解释区域差异？',contract_version='fact-context-parts-v5')
    value['conclusion'][0]['text']='22世纪初该游戏流行。'
    with pytest.raises(ModelOutputError,match='number_not_in_quote'):
        validate_task_output(json.dumps(value,ensure_ascii=False),evidence,question='理论能否解释区域差异？',contract_version='fact-context-parts-v5')


def test_real_worker_restores_one_quoted_term_across_windows_without_borrowing_other_facts(tmp_path):
    from app.schemas import Chunk
    from tests.test_question_jobs import question_fixture,submit,LocalRuntime
    from app.question_worker import QuestionWorker
    env=question_fixture(tmp_path);phrase='共同概念'+'文'*610
    source=phrase+'意指接口之间的连接。'
    env.repository.finish_parse('qdoc',1,[Chunk(id='joint-term',document_id='qdoc',parse_version_id='joint-v',
        text=source,order_index=0,chunk_type='text')])
    env.rag.index.build('qdoc',rebuild=True);env.embedding.calls.clear()
    value=body([fact('“'+phrase+'”意指接口之间的连接。')]);value.pop('evidence_quotes')
    value['conclusion'][0]['evidence']=[dict(ref=1,unit=1)]
    from tests.fixtures.rag_quality.claim_protocol import declare_parts
    env.model.handler=lambda *_:json.dumps(declare_parts(value,derive_refs=True),ensure_ascii=False)
    job,_=submit(env,question='请解释“'+phrase+'”。')
    QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    done=env.service.get_job(job['id']);assert done['status']=='succeeded',done
    result=env.service.get_result(done['result_id'])['answer']
    assert len(result['citations'])>1 and len(env.model.calls)==len(env.embedding.calls)==1
    assert all(c['verification']=='exact_match' for c in result['citations'])


def test_v6_derives_all_fact_indices_from_each_facts_own_part_ids():
    from tests.fixtures.rag_quality.claim_protocol import declare_parts
    source=item('规则甲。规则乙。');evidence=pack([source])
    value=body([fact('规则甲。'),fact('规则乙。')]);value.pop('evidence_quotes')
    for f,unit in zip(value['conclusion'],[1,2]):f['evidence']=[dict(ref=1,unit=unit)]
    result=validate_task_output(json.dumps(declare_parts(value),ensure_ascii=False),evidence,question='规则？',contract_version='fact-context-parts-v6')
    assert result.answer_parts[0]['fact_indices']==[0,1]


@pytest.mark.parametrize('part_ids',[[],['unknown'],['p1','p1'],[True],None])
def test_v6_rejects_missing_duplicate_or_unknown_part_ownership(part_ids):
    from tests.fixtures.rag_quality.claim_protocol import declare_parts
    source=item('规则甲。');evidence=pack([source]);value=body([fact()]);value.pop('evidence_quotes')
    value['conclusion'][0]['evidence']=[dict(ref=1,unit=1)];value=declare_parts(value)
    value['conclusion'][0]['part_ids']=part_ids
    with pytest.raises(ModelOutputError,match='coverage_mismatch'):
        validate_task_output(json.dumps(value,ensure_ascii=False),evidence,question='规则？',contract_version='fact-context-parts-v6')


def test_eight_units_may_expand_to_nine_ranges_without_changing_the_selected_text():
    from tests.fixtures.rag_quality.claim_protocol import declare_parts
    source=item(('甲'*89+'。')*8);evidence=pack([source]);value=body([fact()]);value.pop('evidence_quotes')
    value['conclusion'][0]['evidence']=[dict(ref=1,unit=n) for n in range(1,9)]
    result=validate_task_output(json.dumps(declare_parts(value),ensure_ascii=False),evidence,question='规则？',contract_version='fact-context-parts-v6')
    assert len(result.quotes)>8
    assert all(q in source.text for q in result.quotes.values())


def test_v7_cross_page_unit_is_advertised_before_generation_and_binds_both_real_sources():
    from tests.fixtures.rag_quality.claim_protocol import declare_parts
    left=item('完整背景。'*10+'它可解释甲园区，也可',order=0)
    right=item('解释乙园区的推广困难。另一条完整规则。',ref=2,chunk_id='c2',order=1,page=2)
    pairs=continuation_pairs([left,right]);evidence=pack([left,right],pairs)
    unit=next(u for u,p in left.unit_choices.items() if {r[0] for r in p}=={1,2})
    shown=task_segment_payload(evidence.payload())[0]['quote_units'][unit]
    assert any(len(part)==4 and part[0]==2 for part in shown)
    value=body([fact('该方法能解释两个园区的情况。',kind='synthesis')]);value.pop('evidence_quotes')
    value['conclusion'][0]['evidence']=[dict(ref=1,unit=int(unit))]
    result=validate_task_output(json.dumps(declare_parts(value,derive_refs=True),ensure_ascii=False),evidence,
        question='规则？',contract_version='fact-context-parts-v7')
    review_claims(result,evidence)
    assert {b['source_ref'] for b in result.citation_bindings.values()}=={1,2}
    assert all(q in left.text or q in right.text for q in result.quotes.values())


def test_v7_does_not_offer_a_dangling_unit_when_the_other_page_is_not_visible():
    from app.rag_extract_context import assign_unit_options
    left=item('完整背景。'*10+'也可',order=0)
    right=item('解释另一园区。独立的新规则。',ref=2,chunk_id='c2',order=1,page=2)
    evidence=pack([left,right],continuation_pairs([left,right]))
    right.read_ranges=[[right.text.index('独立'),len(right.text)]]
    assign_unit_options(evidence.items,evidence.continuation_pairs)
    assert all(all(part[0]!=2 for part in parts) for parts in left.unit_choices.values())
    assert str(len(__import__('app.rag_extract_context',fromlist=['support_sentence_ranges']).support_sentence_ranges(left.text))) not in left.unit_choices


def test_sparse_payload_contains_only_visible_original_segments_and_no_private_fields():
    text='甲'*600+'目标规则。'+'乙'*600
    entry={'ref':1,'content':text,'_read_ranges':[[600,605]],'_strict_segments':True}
    before=copy.deepcopy(entry)
    payload=task_segment_payload([entry])[0]
    assert not any(key.startswith('_') for key in payload)
    assert entry==before
    shown=''.join(v for w in payload['content'] for v in w['segments'].values())
    assert '目标规则。' in shown and len(shown)<len(text)/3
    for window in payload['content']:
        number,segments,positions=next(w for w in visible_segments(text,[[600,605]],strict=True) if w[0]==window['span'])
        assert all(text[a:b]==segments[str(k)] for k,(a,b) in positions.items())


def test_passage_option_keeps_definition_and_its_example_in_one_predeclared_choice():
    from app.rag_extract_context import assign_unit_options
    source=item('设备兼容性理论解释接口的接受偏好。这个理论被反复援引。比如说它可以解释园区甲的采用情况。')
    evidence=pack([source]);assert set(source.unit_choices)=={'1'}
    value=body([fact('设备兼容性理论解释园区甲的采用情况。')]);value.pop('evidence_quotes')
    value['conclusion'][0].update(evidence=[dict(ref=1,unit=1)],part_ids=['p1'])
    value['conclusion'][0].pop('refs');value['answer_parts'][0].pop('fact_indices')
    result=validate_task_output(json.dumps(value),evidence,question='请说明规则。',contract_version='fact-context-parts-v7')
    assert ''.join(result.quotes.values())==source.text
    from app.rag_validation import validate_user_condition_boundary
    validate_user_condition_boundary(result,evidence,question='“设备兼容性理论”是否能解释采用情况？')


def test_passage_options_never_fill_an_unread_gap():
    from app.rag_extract_context import assign_unit_options
    source=item('接口甲允许接入。隐藏的例外不得外引。接口乙允许接入。')
    source.read_ranges=[[0,8],[source.text.index('接口乙'),len(source.text)]];source.segment_policy='sentence-segments-v2'
    assign_unit_options([source],[])
    assert len(source.unit_choices)==2
    windows=task_segment_payload([dict(ref=1,content=source.text,_read_ranges=source.read_ranges,
        _strict_segments=True,_unit_choices=source.unit_choices)])[0]
    assert '隐藏' not in ''.join(v for w in windows['content'] for v in w['segments'].values())


@pytest.mark.parametrize('text',['共分为3个层面。','全部内容为合成CC0-1.0数据。'])
def test_structural_count_or_identifier_is_source_checked_but_needs_no_measurement_context(text):
    from app.rag_answer_contract import _context_numbers,_tokens
    assert not _context_numbers(text) and _tokens(text)
    source=item(text);evidence=pack([source]);value=body([fact(text)])
    value.pop('evidence_quotes');f=value['conclusion'][0];f.update(evidence=[dict(ref=1,unit=1)],part_ids=['p1']);f.pop('refs')
    value['answer_parts'][0].pop('fact_indices')
    validate_task_output(json.dumps(value),evidence,question='请说明规则。',contract_version='fact-context-parts-v7')
    f['text']=text.replace('3','4').replace('1.0','2.0')
    with pytest.raises(ModelOutputError,match='number_not_in_quote'):
        validate_task_output(json.dumps(value),evidence,question='请说明规则。',contract_version='fact-context-parts-v7')


@pytest.mark.parametrize('text',['共有3个人。','设备单价3元。','期限3天。','结构分3个层面且共有5名员工。'])
def test_structural_count_exception_does_not_drop_real_measurements(text):
    from app.rag_answer_contract import _context_numbers
    assert _context_numbers(text)


@pytest.mark.parametrize('text',['单价USD10-20.0','金额RMB20-30','售价为3.1元。','第3节报告金额3.1万元。'])
def test_identifier_exemption_cannot_hide_currency_or_decimal_measurements(text):
    from app.rag_answer_contract import _context_numbers
    assert _context_numbers(text)


def test_bare_section_locators_require_real_headings_and_explicit_relation_grammar():
    from app.rag_answer_contract import _without_known_section_locators,_tokens
    sources=[item('定义。',heading='3 机制 / 3.1 审美方式'),
        item('实践。',ref=2,chunk_id='c2',heading='3 机制 / 3.2 研发实践'),
        item('平台。',ref=3,chunk_id='c3',heading='3 机制 / 3.3 平台基础设施')]
    evidence=pack(sources)
    text='3.1的“审美方式”与第2节不同，不能由此推出3.2、3.3与另两个层面逐项配对。'
    assert not _tokens(_without_known_section_locators(text,evidence))
    assert not _tokens(_without_known_section_locators('第3节3.1虽然命名相近，仍是机制。',evidence))
    assert _tokens(_without_known_section_locators('第4节3.1是机制。',evidence))
    assert _tokens(_without_known_section_locators('第3节3.1万元。',evidence))
    for wrong in [text.replace('3.1','3.4'),text.replace('3.3','3.4')]:
        assert _tokens(_without_known_section_locators(wrong,evidence))
    for measured in ['第3节金额为3.1万元。','第3节数值为3.1、3.2和3.3。','金额为3.1。']:
        assert _tokens(_without_known_section_locators(measured,evidence))
    assert _tokens(_without_known_section_locators(text,pack([item('定义。')])))


def test_selector_cannot_use_a_hidden_segment_even_though_full_chunk_exists_on_server():
    source=item('甲'*60+'乙'*60+'丙'*60)
    source.read_ranges=[[120,180]];source.segment_policy='sentence-segments-v2'
    evidence=EvidencePack(items=[source],candidates=[source],index_id='idx',parse_version_id='v',truncated=False,context_chars=0)
    with pytest.raises(ModelOutputError,match='quote_not_in_admitted_input'):
        validate_task_output(json.dumps(body([fact(first=1,last=1)])),evidence,question='请说明规则。',contract_version='fact-context-parts-v4')
    accepted=validate_task_output(json.dumps(body([fact(first=3,last=3)])),evidence,question='请说明规则。',contract_version='fact-context-parts-v4')
    assert accepted.citation_bindings[1]['char_start']==120


def test_a_gap_between_visible_segments_cannot_be_quoted_as_one_contiguous_range():
    source=item('甲'*60+'乙'*60+'丙'*60)
    source.read_ranges=[[0,60],[120,180]];source.segment_policy='sentence-segments-v2'
    evidence=EvidencePack(items=[source],candidates=[],index_id='idx',parse_version_id='v',truncated=False,context_chars=0)
    with pytest.raises(ModelOutputError,match='quote_not_in_admitted_input'):
        validate_task_output(json.dumps(body([fact(first=1,last=3)])),evidence,question='请说明规则。',contract_version='fact-context-parts-v4')


def test_quote_cannot_end_in_middle_of_the_supporting_sentence():
    source=item('背景说明。该能力是重要奖项大满贯的核心竞争力之一。')
    cut=source.text.index('大满')+2
    value=output([ValidatedFact('该能力是核心竞争力之一。',[1],claim_type='direct')],
        {1:dict(source_ref=1,char_start=5,char_end=cut)},{1:source.text[5:cut]})
    with pytest.raises(ModelOutputError,match='incomplete_quote_sentence'):review_claims(value,pack([source]))


def test_cross_page_continuation_must_be_bound_to_same_claim_not_another_fact():
    left=item('这是关于设备适配理论的完整说明。'*4+'它可解释园区甲的普及，也可',order=0)
    right=item('解释园区乙的推广困难。另一个独立结论是应保留接口。',ref=2,chunk_id='c2',order=1,page=2)
    pairs=continuation_pairs([left,right]);assert len(pairs)==1
    evidence=pack([left,right],pairs)
    values=output([ValidatedFact('理论可解释两个园区的差异。',[1],claim_type='synthesis'),
        ValidatedFact('应保留接口。',[2],claim_type='direct')],
        {1:dict(source_ref=1,char_start=0,char_end=len(left.text)),2:dict(source_ref=2,char_start=0,char_end=len(right.text))},
        {1:left.text,2:right.text})
    with pytest.raises(ModelOutputError,match='continuation_not_bound_to_claim'):review_claims(values,evidence)
    values.conclusion=[ValidatedFact('理论可解释两个园区的差异。',[1,2],claim_type='synthesis')]
    review_claims(values,evidence)
    assert values.conclusion[0].support_review['semantic_status']=='not_reviewed'


@pytest.mark.parametrize('change',['heading','version','order','table'])
def test_adjacent_page_is_not_enough_to_create_a_continuation(change):
    left=item('完整背景。'*20+'例如可');right=item('解释另一地区的差异。',ref=2,chunk_id='c2',order=1,page=2)
    if change=='heading':right.heading_path='其他章节'
    if change=='version':right.parse_version_id='other'
    if change=='order':right.order_index=3
    if change=='table':left.chunk_type='table_rows'
    assert continuation_pairs([left,right])==[]


def test_source_citation_after_sentence_is_not_a_fake_continuation():
    left=item('完整背景。'*20+'这是一句完整的话。[21]');right=item('另一个段落。',ref=2,chunk_id='c2',order=1)
    assert continuation_pairs([left,right])==[]


def test_structural_inference_is_not_allowed_to_call_itself_synthesis():
    source=item('第一组按概念领域划分；第二组按实际功能划分。')
    claim=ValidatedFact('不能由数量相同推出一一对应。',[1],claim_type='synthesis')
    value=output([claim],{1:dict(source_ref=1,char_start=0,char_end=len(source.text))},{1:source.text})
    with pytest.raises(ModelOutputError,match='inference_label_required'):review_claims(value,pack([source]))
    claim.claim_type='inference';review_claims(value,pack([source]))
    assert claim.support_review['status']=='needs_review'
    assert '语义待复核' in fact_display_text(claim)


def test_explicit_source_noncorrespondence_can_still_be_direct():
    source=item('这两组不能直接等同。')
    claim=ValidatedFact('这两组不能直接等同。',[1],claim_type='direct')
    value=output([claim],{1:dict(source_ref=1,char_start=0,char_end=len(source.text))},{1:source.text})
    review_claims(value,pack([source]));assert claim.support_review['status']=='checks_passed'


def test_polarity_risk_is_not_misrepresented_as_an_automatic_semantic_pass():
    source=item('不应取消接口标准。')
    claim=ValidatedFact('应取消接口标准。',[1],claim_type='direct')
    value=output([claim],{1:dict(source_ref=1,char_start=0,char_end=len(source.text))},{1:source.text})
    review_claims(value,pack([source]))
    assert claim.support_review['status']=='needs_review'
    assert claim.support_review['semantic_status']=='not_reviewed'


def test_unrecognized_nonnumeric_falsehood_still_is_not_certified_semantically():
    source=item('材料只说明接口连接方式。')
    claim=ValidatedFact('设备可以在任何环境永久运行。',[1],claim_type='direct')
    value=output([claim],{1:dict(source_ref=1,char_start=0,char_end=len(source.text))},{1:source.text})
    review_claims(value,pack([source]))
    assert claim.support_review['semantic_status']=='not_reviewed'
    assert claim.support_review['status']!='semantically_verified'


def test_atomic_continuation_packing_does_not_keep_only_half_of_a_pair():
    chunks=[chunk(0,'完整背景。'*20+'例如也可','同一章'),chunk(1,'解释另一个园区的困难。','同一章'),chunk(2,'独立的完整规则。','另一章')]
    result=vector_result(chunks)
    built=build_evidence(result,Settings(rag_context_k=1),expected_document_id='doc',extractive_question='困难是什么？',
        payload_transform=task_segment_payload,max_items=1)
    assert [i.chunk_id for i in built.pack.items]==['c2']


def test_dynamic_profiles_respect_configured_ceiling():
    assert budget_profile('数值是多少？',max_items=32,context_chars=24000)['target_items']==5
    assert budget_profile('甲是多少？乙是多少？',max_items=32,context_chars=24000)['target_items']==12
    assert budget_profile('两组是否一一对应？',max_items=32,context_chars=24000)['target_items']==8
    assert budget_profile('两组是否一一对应？',max_items=32,context_chars=24000)['max_items']==32
    assert budget_profile('两组是否一一对应？',max_items=3,context_chars=24000)['max_items']==3


@pytest.mark.parametrize('tamper',['read_ranges','segment_policy','review'])
def test_answer_restore_rechecks_admission_and_review_without_new_calls(tmp_path,tamper):
    from tests.test_question_jobs import question_fixture,submit,LocalRuntime
    from app.question_worker import QuestionWorker
    env=question_fixture(tmp_path)
    worker=QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime())
    first,_=submit(env);worker.run_job(first['id'])
    finished=env.service.get_job(first['id']);assert finished['status']=='succeeded'
    answer=env.service.get_result(finished['result_id'])['answer']
    assert answer['conclusion'][0]['support_review']['semantic_status']=='not_reviewed'
    later,_=submit(env,key='restore-extraction')
    claimed=worker.store.claim(worker.worker_id,60,job_id=later['id'])
    retrieval=copy.deepcopy(worker.store.checkpoint(first['id'],'retrieval'))
    evidence=copy.deepcopy(worker.store.checkpoint(first['id'],'evidence'))
    if tamper=='read_ranges':evidence['selected'][0]['read_ranges']=[[1,len(evidence['selected'][0]['text'])]]
    if tamper=='segment_policy':evidence['selected'][0]['segment_policy']=None
    if tamper=='review':answer['conclusion'][0]['support_review']['semantic_status']='verified'
    for role,payload in (('retrieval',retrieval),('evidence',evidence),('answer',answer)):
        assert worker.store.save_checkpoint(later['id'],claimed['lease_token'],role=role,payload=payload,sequence_no=None)
    before=(len(env.embedding.calls),len(env.model.calls))
    with pytest.raises((ValueError,ModelOutputError)):
        worker.store.publish(later['id'],claimed['lease_token'],answer)
    assert (len(env.embedding.calls),len(env.model.calls))==before
    assert env.service.get_result(finished['result_id'])['answer']['conclusion'][0]['support_review']['semantic_status']=='not_reviewed'


def test_offline_review_collects_errors_without_rewriting_original_claim_or_quotes():
    source=item('第一组是概念；第二组是功能。')
    claim=ValidatedFact('不能由数量相同推出对应。',[1],claim_type='synthesis')
    value=output([claim],{1:dict(source_ref=1,char_start=0,char_end=len(source.text))},{1:source.text})
    before=(claim.text,claim.claim_type,copy.deepcopy(value.quotes))
    report=review_claims(value,pack([source]),enforce=False)
    assert 'inference_label_required' in report[0]['errors']
    assert (claim.text,claim.claim_type,value.quotes)==before


@pytest.mark.parametrize('field,value',[('evidence_selection_version','unknown'),('claim_review_version',None),('evidence_profile',{})])
def test_unknown_or_missing_new_policy_cannot_downgrade_before_query(tmp_path,field,value):
    from tests.test_question_jobs import question_fixture,submit,LocalRuntime
    from app.question_worker import QuestionWorker
    env=question_fixture(tmp_path);job,_=submit(env)
    with env.repository.connect() as db:
        snapshot=json.loads(db.execute('SELECT snapshot_json FROM question_jobs WHERE id=?',(job['id'],)).fetchone()[0])
        snapshot[field]=value
        db.execute('UPDATE question_jobs SET snapshot_json=? WHERE id=?',(json.dumps(snapshot),job['id']))
    QuestionWorker(env.settings,env.repository,rag=env.rag,runtime=LocalRuntime()).run_job(job['id'])
    assert env.service.get_job(job['id'])['status']=='failed'
    assert not env.embedding.calls and not env.model.calls
