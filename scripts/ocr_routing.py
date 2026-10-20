"""只依据实际解析输出选择局部框和模型；此模块不接收gold或题目答案。"""
from __future__ import annotations
import re


def han_fraction(text: str) -> float:
    han=sum('\u4e00'<=c<='\u9fff' for c in text)
    letters=sum(c.isalpha() for c in text)
    return han/letters if letters else 0.


def center_inside(inner: list[float], outer: list[float]) -> bool:
    x,y=(inner[0]+inner[2])/2,(inner[1]+inner[3])/2
    return outer[0]<=x<=outer[2] and outer[1]<=y<=outer[3]


def choose_regions(page: dict, config: dict) -> list[dict]:
    """框来自Docling；OCR行提供风险信号，空白没有伪造的低置信分。"""
    candidates=[]
    for unit in page['units']:
        box=unit.get('bbox')
        if not box or box[2]<=box[0] or box[3]<=box[1] or unit['kind']=='table':continue
        lines=[r for r in page['ocr_lines'] if center_inside(r['bbox'],box)]
        mean=sum(r['confidence'] for r in lines)/len(lines) if lines else None
        height=min((r['bbox'][3]-r['bbox'][1])*page['height'] for r in lines) if lines else None
        reasons=[]
        if mean is not None and mean<config['confidence_trigger']:reasons.append('low_confidence')
        if height is not None and height<config['small_text_pixels']:reasons.append('small_text')
        if unit['kind']=='cell' and re.search(r'\d',unit['text']):reasons.append('table_number')
        if not reasons:continue
        candidates.append({**unit,'reasons':reasons,'baseline_confidence':mean,'line_height_pixels':height})
    candidates.sort(key=lambda r:('low_confidence' not in r['reasons'],r['baseline_confidence'] if r['baseline_confidence'] is not None else 1,r['order'],r['id']))
    return candidates[:config['max_regions_per_page']]


def route_model(unit: dict, page_text: str, config: dict) -> str:
    letters=sum(c.isalpha() for c in unit['text'])
    ratio=han_fraction(unit['text']) if letters>=3 else han_fraction(page_text)
    return 'medium' if ratio>=config['han_fraction_medium'] else 'small'


def accept_candidate(original: str, candidate: str, confidence: float, config: dict) -> tuple[bool,str]:
    # 固定可靠性筛选不是正确率保证；不同文字仍须展示分歧。
    old,new=re.sub(r'\s+','',original),re.sub(r'\s+','',candidate)
    if confidence<config['candidate_min_confidence']:return False,'low_candidate_confidence'
    if not old:return False,'empty_baseline_requires_review'
    ratio=len(new)/len(old)
    if not config['min_length_ratio']<=ratio<=config['max_length_ratio']:return False,'length_guard'
    return True,'experimental_candidate_only'
