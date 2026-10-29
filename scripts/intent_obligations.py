"""封存职责的字段核对；只判机械约束，不给对象语义或语言质量打分。"""
from __future__ import annotations

import re


def tool_input_contract(tool):
    """已给出的命令参数须对应登记字段；缺参桩不能抹掉命令中已有的值。"""
    failures=[]
    for name, definition in tool['properties'].items():
        title=definition.get('title',name)
        match=re.search(re.escape(title)+r'=([^，。；\n]+)',tool['command'])
        value=tool['initial_arguments'].get(name)
        if match and value!=match[1]:failures.append(name+':explicit_value_changed_or_missing')
        if value is not None and (not match or match[1]!=value):failures.append(name+':field_not_bound_to_command')
    if set(tool['initial_arguments'])-set(tool['properties']):failures.append('unregistered_parameter')
    return not failures,failures


def joint_claim_uses_sources(fact,citations,expected_document_ids):
    # 联合归纳与显式推断均可携带双方依据，审计不强制把合法归纳改成推断。
    expected=set(expected_document_ids)
    return len(expected)>=2 and fact.get('claim_type') in {'synthesis','inference'} and {citations.get(ref) for ref in fact.get('refs',[])}>=expected


def field_obligations(case, observed, fixture):
    """逐条返回适用、缺测和失败；不把缺字段或空断言计成True。"""
    identifier=case['id']; expected=case['expected']; payload=observed.get('payload') or {}
    result=observed.get('result') or {}; task=observed.get('task') or {}
    understanding=payload.get('understanding') or {}; execution=payload.get('execution') or {}
    entries=[e for e in observed['entries'] if e['phase']=='case']; first=entries[0]['response'] if entries else {}
    rows=[]
    def check(name,passed,paths,source='objects_parameters_clarification'):
        rows.append({'obligation':name,'annotation_field':'expected.'+source,
            'annotation_text':expected[source],'evidence_paths':paths,
            'status':'unmeasured' if passed is None else 'passed' if passed else 'failed'})
    action=expected['action']
    if fixture.get('tool'):
        valid,failures=tool_input_contract(fixture['tool'])
        check('fixture_parameter_identity',valid,['inputs.cases.'+identifier+'.tool'])
        rows[-1]['details']=failures
    if payload:
        question=payload.get('question')
        check('original_question_retained',understanding.get('original_question')==question,
              ['payload.question','payload.understanding.original_question'])
        if not payload.get('tool_continuation'):
            check('submitted_question_retained',question==case['question'],['payload.question','question'])
    if action=='retrieve':
        ids=observed['expected_document_ids']
        check('object_material_scope',execution.get('resources',{}).get('document_ids')==ids and
            [d['document_id'] for d in payload.get('documents',[])]==ids,
            ['payload.execution.resources.document_ids','payload.documents'],'resources')
        snapshots=[d.get('snapshot') for d in payload.get('documents',[])]
        check('fixed_parse_and_index',bool(snapshots) and all(s and s.get('parse_version_id') and s.get('index_id') for s in snapshots),
            ['payload.documents.*.snapshot'],'resources')
    if identifier=='IR-D01':
        check('no_object_or_parameters',understanding.get('bindings')==[] and understanding.get('entities')==[] and
            not result.get('conclusion') and not result.get('explanation') and 'tool_arguments' not in result,
            ['payload.understanding.bindings','payload.understanding.entities','result'])
    if identifier in ('IR-D02','IR-D03','IR-C01','IR-C02','IR-H01','IR-H02'):
        check('no_reference_rewrite',understanding.get('resolved_question')==case['question'] and understanding.get('bindings')==[],
            ['payload.understanding.resolved_question','payload.understanding.bindings'])
    if action=='clarify':
        count=2 if identifier=='IR-D04' else 1
        check('specific_clarification_cardinality',result.get('clarification',{}).get('needed_count')==count and
            result.get('clarification',{}).get('choices')==[] and understanding.get('bindings')==[],
            ['result.clarification','payload.understanding.bindings'])
        if identifier=='IR-H05':check('cleared_entities_not_revived',understanding.get('entities')==[],['payload.understanding.entities'])
    if identifier=='IR-D05':
        check('unique_entity_bound_without_losing_original',understanding.get('bindings')==[{'reference':'该方法','names':['分层检验法']}] and
            understanding.get('resolved_question')=='分层检验法还受什么条件限制？',
            ['payload.understanding.bindings','payload.understanding.resolved_question'])
    if action in ('source_unavailable','split_request'):
        check('no_business_fact_or_guessed_parameters',not result.get('conclusion') and not result.get('explanation') and
            'tool_arguments' not in result and result.get('answer')==payload.get('route',{}).get('response_text'),
            ['result','payload.route.response_text'])
        if action=='split_request':check('scope_confirmation_present',bool(result.get('clarification_questions')),['result.clarification_questions'])
    if action=='query_tool':
        arguments=fixture['tool'].get('continuation_arguments') if payload.get('tool_continuation') else fixture['tool']['initial_arguments']
        if expected['outcome'] in ('answered','clarification_needed','needs_attention') or identifier=='IR-H07':
            check('parameters_preserved',observed.get('tool_parameter_checkpoint')==arguments,['tool_parameter_checkpoint'])
        if expected['outcome']=='answered':
            check('only_registered_arguments_reached_tool',result.get('tool_arguments')==arguments and bool(observed['business_http']) and
                observed['business_http'][-1].get('params',{}).get('arguments')==arguments,['result.tool_arguments','business_http'])
        if identifier=='IR-D09':
            check('ask_only_missing_product',result.get('clarification',{}).get('fields')==[{'name':'product','label':'货号'}] and
                result.get('clarification',{}).get('needed_count')==1,['result.clarification.fields','result.clarification.needed_count'])
        if identifier in ('IR-C06','IR-C07'):
            continuation=payload.get('tool_continuation',{})
            check('confirmation_only_fills_missing_date',continuation.get('known_arguments')=={'point':'乙站','date':None} and
                continuation.get('confirmed_fields')=={'date':'本周一'},['payload.tool_continuation'])
        if identifier=='IR-C07':
            check('changed_known_parameter_not_checkpointed',observed.get('tool_parameter_checkpoint') is None and result=={} and
                observed.get('business_http')==[] and [c['status'] for c in observed['calls']]==['failed'],
                ['tool_parameter_checkpoint','result','business_http','calls'])
        if identifier=='IR-C05':
            check('credentials_fail_before_parameter_model',task.get('error_code')=='tool_credentials_missing' and
                not observed['model_inputs'] and observed.get('tool_parameter_checkpoint') is None,['task.error_code','model_inputs','tool_parameter_checkpoint'])
        if identifier in ('IR-H07','IR-H08'):
            state='failed' if identifier=='IR-H07' else 'uncertain'
            check('first_http_tombstone_and_no_second_send',[c['status'] for c in observed['calls']]==['succeeded',state] and
                [r['method'] for r in observed['business_http']]==['initialize'],['calls','business_http'])
    if identifier=='IR-D12':
        check('same_task_submission_replayed',len(entries)>=3 and entries[0]['response'].get('task_id')==task.get('id')==entries[1]['response'].get('task_id') and
            entries[0]['response'].get('id')==entries[1]['response'].get('id') and observed['new_tasks']==1 and observed['new_call_rows']==2,
            ['entries[phase=case]','task.id','new_tasks','new_call_rows'])
    if identifier in ('IR-C03','IR-C11'):
        facts=result.get('conclusion',[])+result.get('explanation',[])
        citations={c['reference_id']:c['document_id'] for c in result.get('citations',[])}
        check('joint_claim_keeps_both_sources',any(joint_claim_uses_sources(f,citations,observed['expected_document_ids']) for f in facts),
            ['result.conclusion','result.explanation','result.citations'],'source_and_safety')
        if identifier=='IR-C03':check('two_subquestion_records',[p['part_id'] for p in result.get('answer_parts',[])]==['p1','p2'] and
            all(p['state']=='answered' for p in result['answer_parts']),['result.answer_parts'])
    if identifier=='IR-C04':
        check('waiting_bound_to_selected_upload',bool(first.get('document_ids_json')) and first.get('document_ids_json')!='[]' and
            first.get('waiting_jobs_json') not in (None,'[]') and first.get('task_id') is None,['entries[phase=case].0.response'])
        replay=observed.get('waiting_replay')
        check('waiting_refresh_is_idempotent',None if replay is None else replay['same_submission'] and replay['new_tasks']==0 and replay['new_calls']==0,
            ['waiting_replay'])
    if identifier=='IR-C10':
        check('no_evidence_no_fabricated_fact',not result.get('conclusion') and not result.get('explanation') and
            not result.get('citations') and not observed['model_inputs'],['result','model_inputs'])
    if identifier in ('IR-D10','IR-C08','IR-C09','IR-H10','IR-H11'):
        check('rejection_does_not_disclose_object',set(first)=={'detail'} and first.get('detail')==expected['entry_http']['response_detail'] and
            observed['new_tasks']==0 and observed.get('payload') is None and observed.get('result') is None,
            ['entries[phase=case].0.response','new_tasks','payload','result'])
        if identifier=='IR-C08':check('cancelled_confirmation_stays_cancelled',observed.get('original_confirmation_state')=='cancelled',['original_confirmation_state'])
    if identifier=='IR-D11':
        check('revoked_task_has_no_result_or_calls',task.get('error_code')=='access_revoked' and observed.get('result') is None and not observed['calls'],
            ['task.error_code','result','calls'])
    if identifier=='IR-C12':
        preservation=observed.get('preservation')
        check('old_success_originals_parse_and_index_preserved',None if preservation is None else
            preservation['before']==preservation['after'] and preservation['old_task_status']=='succeeded', ['preservation'])
    if identifier=='IR-H06':
        check('other_document_really_present',len(observed['corpus_texts'])>=2,['corpus_texts'],'resources')
        check('node_only_narrows_evidence',execution.get('constraints',{}).get('top_k')==1 and
            result.get('retrieval',{}).get('selected_count')==1,['payload.execution.constraints.top_k','result.retrieval.selected_count'])
    if identifier=='IR-H09':
        check('failed_submission_without_new_task',first.get('status')=='failed' and first.get('error_code')=='scope_unavailable' and
            first.get('task_id') is None and observed['new_tasks']==0 and observed.get('payload') is None,
            ['entries[phase=case].0.response','new_tasks','payload'])
    if identifier=='IR-H12':
        parts=result.get('answer_parts',[])
        check('answer_and_gap_not_collapsed',len(parts)==2 and parts[0]['state']=='answered' and bool(parts[0].get('fact_indices')) and
            parts[1]['state']=='missing_evidence' and parts[1].get('fact_indices')==[] and bool(parts[1].get('missing_detail')) and bool(result.get('missing_points')),
            ['result.answer_parts','result.missing_points'])
    return {'checks':rows,'passed':sum(r['status']=='passed' for r in rows),'failed':sum(r['status']=='failed' for r in rows),
        'unmeasured':sum(r['status']=='unmeasured' for r in rows),'semantic_correctness':'not_evaluated',
        'complete':bool(rows) and all(r['status']=='passed' for r in rows)}
