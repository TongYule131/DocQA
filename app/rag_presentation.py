"""正文编排只组合已校验事实，不产生新的事实、标题或因果连接。"""
from __future__ import annotations
import json
import re
from app.rag_validation import ModelOutputError

PRESENTATION_VERSION = 'grounded-paragraphs-v4'
ROLES = {'conclusion','mechanism','comparison','definition','procedure','boundary','conflict','evidence'}


def validate_fact_progress(facts):
    """新生成的整篇答案拒绝完全重复；不改写事实或删除不同主体／条件。"""
    seen=set()
    for fact in facts:
        if not isinstance(fact,dict) or not isinstance(fact.get('text'),str):
            continue  # 字段形状仍由原协议校验器负责。
        content={k:v for k,v in fact.items() if k!='part_ids'}
        for key in ('evidence','context'):
            if isinstance(content.get(key),list):
                content[key]=sorted(content[key],key=lambda v:json.dumps(v,sort_keys=True,ensure_ascii=False))
        identity=json.dumps(content,sort_keys=True,ensure_ascii=False)
        if identity in seen:raise ModelOutputError('answer_redundant_fact')
        seen.add(identity)


def context_covered(context,text):
    # 仅比较展示串中的中文排版空白；不合并英文词或数字，不修改原引文和偏移。
    def layout(value):
        value=re.sub(r'\s+',' ',value)
        return re.sub(r'(?<=[\u3400-\u9fff，。；：（）]) +| +(?=[\u3400-\u9fff，。；：（）])','',value).strip()
    return layout(context) in layout(text)


def validate_presentation(value, facts, *, complete=True,max_blocks=12,max_facts_per_block=8):
    if not isinstance(value,dict) or set(value)!={'blocks'} or not isinstance(value['blocks'],list) or len(value['blocks'])>max_blocks:
        raise ModelOutputError('presentation_invalid')
    seen=[];blocks=[]
    for block in value['blocks']:
        if (not isinstance(block,dict) or set(block)!={'role','kind','fact_indices'} or block.get('role') not in ROLES
                or block.get('kind') not in {'paragraph','list'} or not isinstance(block['fact_indices'],list)
                or not 1<=len(block['fact_indices'])<=max_facts_per_block):
            raise ModelOutputError('presentation_invalid')
        indices=block['fact_indices']
        if any(type(i) is not int or not 0<=i<len(facts) for i in indices):
            raise ModelOutputError('presentation_reference_invalid')
        seen.extend(indices);blocks.append(dict(role=block['role'],kind=block['kind'],fact_indices=list(indices)))
    if len(seen)!=len(set(seen)) or (complete and sorted(seen)!=list(range(len(facts)))):
        raise ModelOutputError('presentation_coverage_mismatch')
    return {'blocks':blocks}


def fact_text(fact, *, text_filter=None):
    from app.rag_answer_contract import CONTEXT_LABELS
    def field(name,default=None):return fact.get(name,default) if isinstance(fact,dict) else getattr(fact,name,default)
    text=' '.join(field('text').splitlines())
    if field('claim_type')=='inference':text='【结构推断】'+text
    details=[]
    for context in field('context',[]):
        c=context if isinstance(context,dict) else context.model_dump()
        if not context_covered(c['text'],text):details.append(CONTEXT_LABELS[c['kind']]+'：'+c['text'])
    body=text+('（'+'；'.join(details)+'）' if details else '')
    if text_filter is not None:body=text_filter(body)
    return body+''.join(f'[{r}]' for r in sorted(field('refs',[])))


def render_answer(output):
    facts=output.conclusion+output.explanation
    presentation=validate_presentation(output.presentation,facts)
    paragraphs=[]
    for block in presentation['blocks']:
        sentences=[fact_text(facts[i]) for i in block['fact_indices']]
        paragraphs.append(' '.join(sentences) if block['kind']=='paragraph' else '\n'.join('- '+s for s in sentences))
    if output.status=='clarification_needed':
        from app.rag_prompts import CLARIFICATION_LEAD
        paragraphs.insert(0,CLARIFICATION_LEAD+'\n'+'\n'.join(output.clarification_questions))
    if output.missing_points:
        paragraphs.append('本次尚缺少的依据：'+'；'.join(output.missing_points))
    return '\n\n'.join(paragraphs)
