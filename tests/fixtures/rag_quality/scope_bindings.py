"""CC0条件范围开发标签；全部随源码可见，不是独立盲测。"""
import copy
from app.rag_evaluation import digest


def _span(source_id,source,quote):
    start=source.index(quote)
    return dict(source_id=source_id,start=start,end=start+len(quote),quote=quote)


def make_scope_cases():
    specs=[
        ('annual','甲社2025年营业收入1280万元，2025年数据未经审计；2024年营业收入1100万元，已经审计。',
         '2025年营业收入1280万元','2024年营业收入1100万元','2025年数据未经审计','2025年数据已经审计','explicit_text'),
        ('learning','45人报名，其中40名结业者参加完整前后测；32人通过独立任务，比例80%，比例仅以40名结业者为分母。',
         '32人通过独立任务，比例80%','45人报名','仅以40名结业者为分母','以45名报名者为分母','explicit_text'),
        ('grants','在校且本学年有学籍者可申请300元补助。校外人员咨询费50元，与该补助无关。',
         '可申请300元补助','校外人员咨询费50元','在校且本学年有学籍','校外人员也可申请','exception'),
        ('inventory','期末库存120件，不含在途货物；另列在途货物8件，不计入期末库存。',
         '期末库存120件','在途货物8件','不含在途货物','含在途货物','explicit_text'),
        ('factory','白班合格108件，该合格数仅统计白班；夜班检查80件，夜班合格数未提供。',
         '白班合格108件','夜班检查80件','仅统计白班','包含夜班合格数','explicit_text'),
        ('visits','2025年服务量3200人次，重复到访按多个人次记录；另有独立受访者50人。',
         '2025年服务量3200人次','独立受访者50人','重复到访按多个人次记录','重复到访不重复计数','explicit_text'),
        ('budget','2026年预算支出800万元，尚未执行；2025年实际支出800万元，已经审计。',
         '2026年预算支出800万元','2025年实际支出800万元','尚未执行','已经执行','explicit_text'),
        ('refund','2025年退款金额0万元，该年度退款数据未经审计；另表列2026年收入预算1500万元。',
         '2025年退款金额0万元','2026年收入预算1500万元','退款数据未经审计','退款数据已经审计','explicit_text'),
        ('table-row','指标 | 2025年金额万元 | 2024年金额万元 | 行限定\n服务成本 | 860 | 790 | 本行2025年数据未经审计\n管理支出 | 200 | 180 | 已审计',
         '服务成本 | 860 | 790','管理支出 | 200 | 180','本行2025年数据未经审计','本行2025年数据已经审计','row'),
        ('section','第一节 境内项目：收入200万元。本节仅含境内项目。\n第二节 境外项目：收入80万元。',
         '收入200万元','收入80万元','仅含境内项目','包含境外项目','section'),
        ('accessibility','验收要求作品提交与现场说明。无障碍申请者可口述替代书面说明，但作品仍须提交。旁听者仅需签到。',
         '可口述替代书面说明','旁听者仅需签到','但作品仍须提交','作品可以免交','exception'),
        ('uncertain-ocr','左栏：营业收入1200万元。右栏：预计支出600万元。\n页底残缺注释：未经审计。原件无法确定注释对应哪一栏。',
         '营业收入1200万元','预计支出600万元','未经审计','已经审计','uncertain'),
    ]
    cases=[]
    for name,source,primary,control,phrase,opposite,basis in specs:
        sid='scope-'+name
        annotation=dict(document_id=name,parse_version_id='scope-v1',
            sources=[dict(source_id=sid,text=source,text_sha256=digest(source))],
            facts=[dict(fact_id='primary',anchor=_span(sid,source,primary)),
                   dict(fact_id='control',anchor=_span(sid,source,control))],
            scopes=[dict(scope_id='qualification',kind='condition',span=_span(sid,source,phrase),
                applies_to=['primary'],excludes=['control'],basis=basis,contradictions=[opposite])])
        facts=[dict(text=text+'。',refs=[1],context=[dict(kind='subject',text=text,ref=1)]) for text in (primary,control)]
        facts[0]['context'].append(dict(kind='condition',text=phrase,ref=1))
        positive=dict(conclusion=facts,explanation=[],citations=[dict(reference_id=1,document_id=name,
            parse_version_id='scope-v1',chunk_id=sid,quote=source)])
        variants={}
        for mutation in ('omitted','wrong_attachment','contradicted'):
            value=copy.deepcopy(positive)
            value['conclusion'][0]['context'].pop()
            if mutation=='wrong_attachment':
                value['conclusion'][1]['context'].append(dict(kind='condition',text=phrase,ref=1))
            elif mutation=='contradicted':value['conclusion'][0]['text']+='该项'+opposite+'。'
            variants[mutation]=value
        cases.append(dict(case_id=name,licence='CC0-1.0',split='validation' if name in {
            'refund','table-row','section','accessibility','uncertain-ocr'} else 'development',annotation=annotation,
            alignment=[dict(answer_index=0,source_fact_ids=['primary']),dict(answer_index=1,source_fact_ids=['control'])],
            positive=positive,variants=variants,expected='unverified' if basis=='uncertain' else 'literal_scope_checked',
            boundary='人工标签及对齐，仅测明确限定的逐字保留，不代表自动语义正确或独立盲测'))
    return cases
