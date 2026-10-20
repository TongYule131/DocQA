"""局部OCR候选的保护规则；只接收原始/候选文本，不接收gold。"""
from __future__ import annotations
import copy,re


def numeric_signature(text):
    compact=re.sub(r'\s+','',text)
    return re.findall(r'[+\-−]?\d+(?:[.,/:\-]\d+)*(?:[%％])?',compact)


def restrictions(text):
    cn=re.findall(r'不|未|无|以上|以下|至少|最多|超过|少于',text)
    en=re.findall(r'\b(?:no|not|only|unless|except|without)\b',text,re.I)
    return sorted(cn+[w.lower() for w in en])


def guard_reason(record):
    old,new=record['original_text'],record.get('candidate_text','')
    if not record.get('changed'):return None
    if record['kind']=='cell':return 'table_cell_disagreement_requires_review'
    if numeric_signature(old)!=numeric_signature(new):return 'numeric_disagreement_requires_review'
    if restrictions(old)!=restrictions(new):return 'restriction_disagreement_requires_review'
    return None


def guarded_page(baseline,raw_candidate):
    # 导入运行器只复用纯数据更新函数；不会加载Docling/模型或读取标注。
    try:from scripts.run_ocr_layout import apply_record
    except ModuleNotFoundError:from run_ocr_layout import apply_record
    result=copy.deepcopy(baseline);result['crop_records']=[]
    for raw in raw_candidate['crop_records']:
        record=copy.deepcopy(raw);reason=guard_reason(record)
        if reason:
            record.update(accepted=False,guard=reason,raw_candidate_accepted=raw.get('accepted',False))
        else:record['guard']=None
        apply_record(result,record);result['crop_records'].append(record)
    result.update(profile=raw_candidate['profile']+'-guarded',raw_profile=raw_candidate['profile'],
                  input_sha256=raw_candidate['input_sha256'],elapsed_seconds=raw_candidate['elapsed_seconds'])
    return result
