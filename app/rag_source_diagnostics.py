"""固定解析版本的只读质量/数值角色诊断；不修正文、不推测 OCR 正确率。"""
from collections import Counter
import re

from app.rag_numeric_roles import NUMERIC_ROLE_VERSION, locate, source_number_tokens
from app.rag_math_evidence import formula_info

SOURCE_DIAGNOSTIC_VERSION = 'source-diagnostics-v3'


def source_diagnostics(chunks, version, *, blocks=()):
    roles = Counter()
    regions = []
    for chunk in chunks:
        locators = locate(chunk.text)
        roles.update(locator.kind for locator in locators)
        replacement = chunk.text.count('\ufffd')
        private_use = sum('\ue000' <= ch <= '\uf8ff' for ch in chunk.text)
        # 偏移始终指向完整原文，不把检索提示/用户给出的数字填入源文。
        regions.append(dict(chunk_id=chunk.id, block_id=chunk.block_id,
            parse_version_id=chunk.parse_version_id, has_location=bool(chunk.sources),
            replacement_chars=replacement, private_use_chars=private_use,
            source_value_count=len(source_number_tokens(chunk.text)),
            formula=formula_info(chunk.text,chunk.chunk_type),
            locators=[dict(start=l.start,end=l.end,kind=l.kind) for l in locators]))
    indexed_blocks={c.block_id for c in chunks}
    unindexed=[dict(block_id=b.id,parse_version_id=b.parse_version_id,
        formula=formula_info(b.text,'formula')) for b in blocks if b.block_type=='formula' and b.id not in indexed_blocks]
    warnings = Counter(w.code for w in (version.warnings if version else []) if re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', w.code))
    report=dict(version=SOURCE_DIAGNOSTIC_VERSION, numeric_role_version=NUMERIC_ROLE_VERSION,
        quality_status=version.quality_status if version else 'unknown', warning_codes=dict(warnings),
        region_count=len(regions), missing_location_count=sum(not r['has_location'] for r in regions),
        replacement_chars=sum(r['replacement_chars'] for r in regions),
        private_use_chars=sum(r['private_use_chars'] for r in regions),
        locator_counts=dict(roles), source_value_count=sum(r['source_value_count'] for r in regions),
        regions=regions, ocr_accuracy_verified=False, formula_semantics_verified=False)
    report.update(formula_states=dict(Counter(r['formula']['state'] for r in regions if r['formula']['state']!='not_formula')),
        unindexed_formula_regions=unindexed,unindexed_formula_count=len(unindexed))
    # 按物理页去重，不暴露客户页标签、页脚或原文。
    page_labels={(s.page,s.pdf_page_label.status) for c in chunks for s in c.sources if s.pdf_page_label}
    report['page_label_states']=dict(Counter(status for _,status in page_labels))
    return report


def public_source_diagnostics(report):
    return {key: report[key] for key in ('version','numeric_role_version','quality_status','region_count',
        'missing_location_count','replacement_chars','private_use_chars','locator_counts',
        'source_value_count','ocr_accuracy_verified','formula_semantics_verified','formula_states','unindexed_formula_count','page_label_states') if key in report}
