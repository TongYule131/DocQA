"""保留原子问身份，区分事实问题与受控的礼貌／展示义务。"""
import re

OBLIGATIONS_VERSION='question-obligations-v1'
# 只完整匹配有限展示要求；含业务对象、否定条件或引语时仍按证据问题处理。
_PRESENTATION=re.compile(r'(?:请)?(?:(?:用|以)(?:一|两|二|三|\d{1,3})(?:句(?:话)?|段|字)(?:回答|说明|作答)?|'
    r'(?:简短|简洁|精简)(?:地)?(?:回答|说明|作答)?|保留引用|不要重复|不必重复|不要列清单)')


def question_obligations(question):
    from app.rag_answer_contract import question_parts
    from app.intent_routing import interaction_family
    result=[]
    for part in question_parts(question):
        text=re.sub(r'[\s。？！?；;]+$','',part['text'])
        kind=('interaction' if interaction_family(text) else 'presentation' if _PRESENTATION.fullmatch(text) else 'evidence_question')
        result.append({**part,'kind':kind})
    return result


def answer_question_parts(question):
    values=question_obligations(question)
    factual=[{'part_id':p['part_id'],'text':p['text']} for p in values if p['kind']=='evidence_question']
    # 纯设置／交互由入口处理；直接调用底层时仍不允许零子问绕过覆盖守卫。
    return factual or [{'part_id':p['part_id'],'text':p['text']} for p in values]
