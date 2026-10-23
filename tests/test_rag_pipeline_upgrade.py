"""CC0合成回归：几何归位、排版边界及失败诊断，禁止读取真实资料。"""
from copy import deepcopy
import json
import pytest

from app.pdf_inline import restore_text, repair_inline_fragments
from app.document_normalizer import NormalizedDocument, _heading_spec
from app.rag_phrase_match import source_phrase_match
from app.rag_pipeline import RetrievalBudget
from app.schemas import Block, SourceLocation


@pytest.mark.parametrize('source,pdf,fragments,expected',[
    ('调查显示， 的受访者采用工具。','调查显示，63.7%的受访者采用工具。',['63.7%'],'调查显示， 63.7%的受访者采用工具。'),
    ('报告（ 版）显示， 的受访者采用工具。','报告（2022版）显示，63.7%的受访者采用工具。',['2022','63.7%'],'报告（ 2022版）显示， 63.7%的受访者采用工具。'),
    ('该厂“支持”项应用。','该厂“支持”12项应用。',['12'],'该厂“支持”12项应用。'),
    ('调查显示， 的受访者采用工具。','调查显示，73.7%的受访者采用工具。',['63.7%'],None),
    ('调查显示， 的受访者采用工具。','调查显示，63.7%的受访者拒绝工具。',['63.7%'],None),
    ('占比为63.7%。','占比为63.7%。',['63.7%'],None),
    ('甲有 件，乙有 件。','甲有12件，乙有12件。',['12'],None),
    ('甲有 件，乙有 件。','甲有12件，乙有12件。',['12','12'],'甲有 12件，乙有 12件。'),
])
def test_pdf_repairs_only_dual_verified_insertions(source,pdf,fragments,expected):
    assert restore_text(source,pdf,fragments)==expected


def block(n,text,box,*,page=1,kind='paragraph'):
    return Block(id=f'b{n}',document_id='d',parse_version_id='v',order_index=n,block_type=kind,label='text',text=text,
        sources=[SourceLocation(format='pdf',page=page,bbox=dict(zip(('l','b','r','t'),box)),coord_origin='BOTTOMLEFT')])


@pytest.mark.parametrize('mutation',['valid','other_page','other_column','footer','word_change'])
def test_fragment_geometry_does_not_borrow_from_other_scope(mutation):
    paragraph='调查报告显示， 的员工接受培训。'+('本次调查仅覆盖白班，夜班未参加。'*3)
    fragment=block(0,'63.7%',(80,198,105,203))
    target=block(1,paragraph,(40,100,240,200))
    if mutation=='other_page':fragment.sources[0].page=2
    if mutation=='other_column':fragment.sources[0].bbox.update(l=300,r=325)
    if mutation=='footer':fragment.sources[0].bbox.update(b=2,t=7)
    document=NormalizedDocument('d','pdf',2,blocks=[fragment,target])
    before=deepcopy(fragment.sources[0])
    text=paragraph.replace('， 的员工','，63.7%的员工')
    if mutation=='word_change':text=text.replace('白班','早班')
    result=repair_inline_fragments(document,lambda page,box:text)
    assert fragment.text=='63.7%' and fragment.sources[0]==before
    assert result['recovered']==(1 if mutation=='valid' else 0)
    if mutation=='valid':
        assert '63.7%的员工' in target.text and fragment.block_type=='inline_fragment'
        assert len(target.sources)==2 and target.char_count==len(target.text)
    else: assert target.text==paragraph and fragment.block_type=='paragraph'


@pytest.mark.parametrize('value',['收稿日期 ：','作者简介：张某','通讯作者：甲','甲乙丙 1 ，丁戊己 2'])
def test_academic_front_matter_is_not_a_section(value):
    assert _heading_spec(value) is None


@pytest.mark.parametrize('phrase,source,accepted',[
    ('基础级—进阶级—领军级',"采用'基础级-进阶级-领军级'架构。",True),
    ('理解、评估、使用','结构包括理解、评估和使用。',True),
    ('认知理解、伦理评估、技术应用','认知理解维度、伦理评估维度和技术应用维度。',True),
    ('电商数据分析','电商数据 分析实训。',True),
    ('基础级—领军级','基础级和领军级。',True),
    ('基础级—进阶级','基础级没有提到进阶级。',False),
    ('理解、评估、使用','使用、评估和理解。',False),
    ('理解、评估、使用','理解。其他地方讲评估。另一个观点使用。',False),
    ('买了七天','自签收之日起七天可退货。',False),
    ('63.7%采用工具','63.7%的员工采用工业工具。',False),
    ('300万元','300 万元',False),
    ('Open AI','OpenAI',False),
])
def test_phrase_layout_never_becomes_general_semantic_matching(phrase,source,accepted):
    assert source_phrase_match(phrase,source)==accepted


@pytest.mark.parametrize('phrase,kind,text,allowed',[
    ('跨域适配机制','inference','可归纳为“跨域适配机制”。',True),
    ('通用化矛盾','inference','据此分析题干所问的“通用化矛盾”。',True),
    ('跨域适配机制','direct','原文提出“跨域适配机制”。',False),
    ('跨域适配机制','inference','论文定义“跨域适配机制”。',False),
    ('我遇到的问题','inference','可归纳为“我遇到的问题”。',False),
    ('购买七天的条件','inference','可归纳为“购买七天的条件”。',False),
    ('63.7%采用工具','inference','可归纳为“63.7%采用工具”。',False),
    ('跨域适配机制已经完成','inference','可归纳为“跨域适配机制已经完成”。',False),
])
def test_inference_framing_is_reviewed_while_user_premises_stay_strict(phrase,kind,text,allowed):
    from app.rag_validation import ValidatedFact,ValidatedOutput,validate_user_condition_boundary,ModelOutputError
    from app.rag_context import EvidenceItem,EvidencePack
    item=EvidenceItem(1,'c','d','v',1,'培训应与岗位要求结合。')
    pack=EvidencePack([item],[item],None,'v',False,100)
    fact=ValidatedFact(text,[1],claim_type=kind)
    result=ValidatedOutput('answered',conclusion=[fact])
    if allowed:
        validate_user_condition_boundary(result,pack,question='请分析'+phrase)
        from app.rag_support_review import review_claims
        from app.rag_extract_context import SELECTION_VERSION
        pack.review_policy=SELECTION_VERSION
        result.quotes={1:item.text}
        result.citation_bindings={1:dict(source_ref=1,char_start=0,char_end=len(item.text))}
        review_claims(result,pack)
        assert 'analysis_term_not_verbatim_source' in fact.support_review['risk_codes']
    else:
        with pytest.raises(ModelOutputError,match='mixed_user_source'):
            validate_user_condition_boundary(result,pack,question='请分析'+phrase)


def test_index_worker_keeps_safe_failure_reason_and_request_count(tmp_path):
    from tests.test_knowledge_chain import library
    from tests.test_question_jobs import LocalRuntime
    from app.knowledge_pipeline import KnowledgePipeline
    from app.knowledge_worker import KnowledgeWorker
    from app.embedding import EmbeddingError
    env,access,base=library(tmp_path)
    job=KnowledgePipeline(access).submit_index('policy','network',rebuild=True)
    def unavailable(texts):raise EmbeddingError(502,'合成向量连接失败')
    env.embedding.embed=unavailable
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=env.model)
    assert worker.run_once(job['id'])
    row=worker.tasks.row(job['id'])
    assert row['status']=='failed' and row['error_code']=='call_failed' and row['requests_used']==1
    assert not worker.run_once(job['id'])


@pytest.mark.parametrize('values',[(8,32,12,24000),(0,32,12,24000),(8,101,12,24000),(8,32,12,999),(True,32,12,24000)])
def test_three_stage_budget_rejects_invalid_limits(values):
    if values==(8,32,12,24000):assert RetrievalBudget(*values).candidate_limit==32
    else:
        with pytest.raises(ValueError):RetrievalBudget(*values)


@pytest.mark.parametrize('fact,source,valid',[
    ('成本3万元。','成本3万元。',True),('成本3元。','成本3万元。',False),
    ('员工3人。','员工3名。',True),('重量3千克。','重量3公斤。',True),
    ('耗时3小时。','耗时3天。',False),('销量3件。','图3给出销量。',True),
    ('温度3℃。','温度3摄氏度。',True),('客流3人。','客流3人次。',False),
    ('收入3万元。','收入3万元，支出3元。',True),
])
def test_explicit_units_are_checked_separately_from_locator_numbers(fact,source,valid):
    from app.rag_numeric_roles import validate_numeric_units
    from app.rag_validation import ModelOutputError
    # ‘图3’不提供测量单位；该探针只验证单位检查，数字在引用中仍由生产合约单独强制核对。
    if valid:validate_numeric_units(fact,[source])
    else:
        with pytest.raises(ModelOutputError,match='number_unit_mismatch'):validate_numeric_units(fact,[source])


def test_numeric_context_addresses_only_point_to_original_segments():
    from app.quote_spans import task_segment_payload
    entry=task_segment_payload([dict(ref=1,content='甲社2022年收入120万元，数据未经审计。')])[0]
    spans={w['span']:w['segments'] for w in entry['content']}
    assert entry['numeric_context_choices']
    for choice in entry['numeric_context_choices']:
        original=spans[choice['span']][str(choice['segment'])]
        assert all(value in original for value in choice['values'])
    assert ''.join(s for w in entry['content'] for s in w['segments'].values())=='甲社2022年收入120万元，数据未经审计。'


def test_bad_stream_saves_exact_failure_location_and_private_body_without_retry(tmp_path):
    from tests.test_knowledge_chain import library
    from tests.test_question_jobs import LocalRuntime
    from app.knowledge_questions import KnowledgeQuestions
    from app.knowledge_worker import KnowledgeWorker
    from app.knowledge_traces import KnowledgeTraces
    from app.answer_stream_store import AnswerStreamStore
    env,access,base=library(tmp_path)
    marker='SYNTHETIC-PRIVATE-DIAGNOSTIC'
    class Model:
        calls=0
        last_call_metadata={'usage':{'total_tokens':42},'usage_status':'reported'}
        def generate_stream(self,system,user,feed,active):
            self.calls+=1
            entry=next(i for i in json.loads(user)['references'] if '保留包装' in str(i['content']))
            good=dict(text='退货需要保留包装。',claim_type='direct',context=[],
                evidence=[dict(ref=entry['ref'],unit=1)],part_ids=['p1'])
            bad={**good,'text':'合成比率63.7%。'+marker}
            raw=json.dumps(dict(status='answered',conclusion=[good,bad],explanation=[],
                clarification_questions=[],missing_points=[],
                answer_parts=[dict(part_id='p1',state='answered',missing_detail='')]),ensure_ascii=False)
            for i in range(0,len(raw),13):active();feed(raw[i:i+13])
            self.completed=raw
            return raw
    model=Model();service=KnowledgeQuestions(access,env.rag);conv=service.create(base['id'],'c')
    task=service.submit(conv['id'],'合成问答诊断：退货条件是什么？','q')
    worker=KnowledgeWorker(env.settings,env.repository,LocalRuntime(),embedding=env.embedding,model=model)
    assert worker.run_once(task['id'])
    with env.repository.connect() as db:
        row=dict(db.execute('SELECT * FROM kb_tasks WHERE id=?',(task['id'],)).fetchone())
        diagnostic=json.loads(db.execute("SELECT payload_json FROM kb_task_checkpoints WHERE task_id=? AND name='diagnostic_validation_attempt_1'",(task['id'],)).fetchone()[0])
        usage=db.execute("SELECT usage_json FROM kb_task_calls WHERE task_id=? AND role='generation'",(task['id'],)).fetchone()[0]
    assert row['status']=='failed' and row['result_json'] is None
    assert row['error_code']=='output_numeric_context_missing'
    assert diagnostic['output']==model.completed and diagnostic['location']['ordinal']==1
    assert diagnostic['location']['category']=='conclusion' and '42' in usage
    trace=KnowledgeTraces(env.repository).detail(task['id'])
    assert trace['diagnostics'][-1]['location']['ordinal']==1
    assert marker not in json.dumps(trace,ensure_ascii=False)
    assert '合成问答诊断' not in json.dumps(trace,ensure_ascii=False)
    events=AnswerStreamStore(env.repository).read('knowledge',task['id'])
    assert len([e for e in events if e['kind']=='block'])==1
    assert not worker.run_once(task['id']) and model.calls==1 and row['requests_used']==2


def test_new_parse_reuses_exact_old_embedding_inputs_without_sharing_sources(tmp_path):
    from tests.test_question_jobs import question_fixture
    from app.schemas import Chunk
    env=question_fixture(tmp_path)
    old=env.repository.chunks('qdoc')[0]
    env.embedding.calls.clear()
    updated=old.model_copy(update=dict(id='new-chunk',parse_version_id='new-version',page=9))
    env.repository.finish_parse('qdoc',9,[updated])
    env.rag.index.build('qdoc')
    assert env.embedding.calls==[]
    active=env.repository.active_index('qdoc')
    assert active.parse_version_id=='new-version'
    assert [key for key,_ in env.repository.index_vectors(active.id)]==['new-chunk']
    env.rag.index.build('qdoc',rebuild=True)
    assert len(env.embedding.calls)==1


def test_cached_pdf_repair_is_read_only_by_default_and_preserves_old_version(tmp_path):
    from hashlib import sha256
    from tests.test_question_jobs import question_fixture
    from app.repository import now_iso
    from app.schemas import Document,Chunk
    from scripts.repair_cached_pdf_layout import prepare,apply
    env=question_fixture(tmp_path);env.embedding.calls.clear();env.model.calls.clear()
    full='Report (2022 edition) shows 63.7%workers use tools. Only verified participants are counted.'
    stream=f'BT /F1 10 Tf 50 700 Td ({full.replace("(",chr(92)+"(").replace(")",chr(92)+")")}) Tj ET'.encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data=b'%PDF-1.4\n';offsets=[0]
    for n,obj in enumerate(objects,1):
        offsets.append(len(data));data+=f'{n} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref=len(data);data+=b'xref\n0 6\n0000000000 65535 f \n'+b''.join(f'{n:010} 00000 n \n'.encode() for n in offsets[1:])
    data+=f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()
    original=tmp_path/'uploads/pdf';original.parent.mkdir(exist_ok=True);original.write_bytes(data)
    env.repository.create(Document(id='pdf',filename='synthetic.pdf',format='pdf',size=len(data),created_at=now_iso(),status='uploaded'))
    env.repository.finish_parse('pdf',1,[Chunk(id='old-c',document_id='pdf',text='2022 63.7% detached',page=1,parse_version_id='old-pdf')])
    nodes=[]
    for n,text in enumerate(['2022','63.7%',full.replace('2022','').replace('63.7%','')]):
        nodes.append(dict(self_ref=f'#/texts/{n}',parent={'$ref':'#/body'},label='text',text=text,
            prov=[dict(page_no=1,bbox=dict(l=50,b=680,r=580,t=705,coord_origin='BOTTOMLEFT'))]))
    payload=dict(body=dict(self_ref='#/body',children=[{'$ref':n['self_ref']} for n in nodes]),texts=nodes,tables=[],groups=[],pictures=[],pages={'1':{'page_no':1}})
    raw=tmp_path/'parse-results/raw/document.json';raw.parent.mkdir(parents=True);raw.write_text(json.dumps(payload),encoding='utf-8')
    with env.repository.connect() as db:
        db.execute('UPDATE parse_versions SET origin_hash=?,result_json_path=? WHERE id=?',(sha256(data).hexdigest(),'parse-results/raw/document.json','old-pdf'))
        before=[tuple(r) for r in db.execute('SELECT * FROM parse_tasks')]
    planned=prepare(env.settings,env.repository,'pdf')
    assert planned[-1]['eligible'] and planned[-1]['inline']['recovered']==2
    with env.repository.connect() as db:assert before==[tuple(r) for r in db.execute('SELECT * FROM parse_tasks')]
    result=apply(env.settings,env.repository,planned)
    assert result['new_version']!='old-pdf' and original.read_bytes()==data
    assert env.repository.chunks('pdf','old-pdf')[0].text=='2022 63.7% detached'
    assert any('2022' in c.text and '63.7%workers' in c.text for c in env.repository.chunks('pdf'))
    assert not prepare(env.settings,env.repository,'pdf')[-1]['eligible']
    assert env.embedding.calls==env.model.calls==[]
