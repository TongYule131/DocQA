"""在上游JSON仍在到达时验证完整论断；只输出局部草稿，不替代最终整体校验。"""
import json

from app.rag_answer_contract import ANSWER_CONTRACT_VERSION, question_parts, validate_task_output
from app.rag_validation import ModelOutputError, _reject_duplicate_keys, validate_user_condition_boundary

STREAM_VERSION = 'validated-fact-stream-v1'
MAX_STREAM_CHARS = 262144


class FactStreamParser:
    """增量读取顶层结论/解释数组；未闭合字符串、对象和思考均不会进入回调。"""
    def __init__(self, completed):
        self.completed=completed;self.buffer='';self.position=0;self.state='start'
        self.key=None;self.keys=set();self.ordinal=0
        self.decoder=json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)

    def feed(self, text):
        if not isinstance(text,str):raise ModelOutputError('invalid_stream_fragment')
        self.buffer+=text
        if len(self.buffer)>MAX_STREAM_CHARS:raise ModelOutputError('stream_too_large')
        while True:
            while self.position<len(self.buffer) and self.buffer[self.position].isspace():self.position+=1
            if self.position==len(self.buffer):return
            char=self.buffer[self.position]
            if self.state=='start':
                if char=='`':
                    newline=self.buffer.find('\n',self.position)
                    if newline<0:return
                    if self.buffer[self.position:newline].strip().lower() not in ('```','```json'):return
                    self.position=newline+1;continue
                if char!='{':return
                self.position+=1;self.state='key'
            elif self.state=='key':
                if char=='}':self.state='done';return
                try:key,end=self.decoder.raw_decode(self.buffer,self.position)
                except json.JSONDecodeError:return
                if not isinstance(key,str) or key in self.keys:raise ModelOutputError('duplicate_key')
                self.key=key;self.keys.add(key);self.position=end;self.state='colon'
            elif self.state=='colon':
                if char!=':':return
                self.position+=1;self.state='value'
            elif self.state=='value':
                if self.key in ('conclusion','explanation'):
                    if char!='[':return
                    self.position+=1;self.ordinal=0;self.state='item'
                else:
                    try:_,end=self.decoder.raw_decode(self.buffer,self.position)
                    except json.JSONDecodeError:return
                    self.position=end;self.state='separator'
            elif self.state=='item':
                if char==']':self.position+=1;self.state='separator';continue
                start=self.position
                try:value,end=self.decoder.raw_decode(self.buffer,start)
                except json.JSONDecodeError:return
                if not isinstance(value,dict):raise ModelOutputError('invalid_stream_fact')
                if self.ordinal>=20:raise ModelOutputError('too_many_facts')
                self.completed(self.key,self.ordinal,value)
                self.ordinal+=1;self.position=end;self.state='item_separator'
            elif self.state=='item_separator':
                if char==',':self.position+=1;self.state='item'
                elif char==']':self.position+=1;self.state='separator'
                else:return
            elif self.state=='separator':
                if char==',':self.position+=1;self.state='key'
                elif char=='}':self.state='done';return
                else:return
            else:return


def checked_block(category,ordinal,fact,pack,question,rag,filenames=None):
    # 占位覆盖只用于复用原有单论断严格校验，不对外宣称问题已完整回答。
    ids=fact.get('part_ids',[]);parts=question_parts(question)
    if not isinstance(ids,list) or any(not isinstance(p,str) for p in ids):raise ModelOutputError('coverage_mismatch')
    complete=isinstance(ids,list) and set(ids)=={p['part_id'] for p in parts}
    coverage=[dict(part_id=p['part_id'],state='answered' if p['part_id'] in ids else 'missing_evidence',
                   missing_detail='' if p['part_id'] in ids else p['part_id']+'仍在生成') for p in parts]
    gaps=[p['missing_detail'] for p in coverage if p['missing_detail']]
    payload=dict(status='answered' if complete else 'partial',conclusion=[],explanation=[],
                 clarification_questions=[],missing_points=[] if complete else gaps,answer_parts=coverage)
    payload[category]=[fact]
    checked=validate_task_output(json.dumps(payload,ensure_ascii=False),pack,question=question,contract_version=ANSWER_CONTRACT_VERSION)
    validate_user_condition_boundary(checked,pack,question=question)
    from app.rag_support_review import review_claims
    review_claims(checked,pack)
    citations=rag._citations(pack,checked.quotes,checked.citation_bindings)
    item=(checked.conclusion+checked.explanation)[0]
    identities={c.reference_id:c.evidence_id for c in citations}
    from app.schemas import RagFact
    result=RagFact(text=item.text,refs=sorted(item.refs),context=item.context,claim_type=item.claim_type,
                   support_review=item.support_review,evidence_ids=[identities[r] for r in sorted(item.refs)]).model_dump()
    return dict(version=STREAM_VERSION,category=category,ordinal=ordinal,fact=result,
                citations=[dict(c.model_dump(),**({'filename':filenames[c.document_id]} if filenames else {})) for c in citations],
                validation_scope='single_fact',draft=True)
