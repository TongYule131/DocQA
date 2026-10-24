"""冻结地址候选组件回放：不加载 Settings、不调用供应商、不读取正式数据库。"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.pdf_page_labels import PAGE_LABEL_VERSION, requested_page_slots
from app.schemas import PdfPageLabel, SourceLocation
from app.rag_numeric_roles import NUMERIC_ROLE_VERSION

FIXTURE = ROOT / 'tests/fixtures/rag_quality/page_addresses.json'


def evaluate(path=FIXTURE, split='all'):
    raw = path.read_bytes()
    fixture = json.loads(raw)
    if fixture.get('schema') != 'page-address-fixture-v1' or fixture.get('provenance',{}).get('license') != 'CC0-1.0':
        raise ValueError('invalid_fixture')
    documents = fixture['documents']
    if len({d['id'] for d in documents}) != len(documents): raise ValueError('document_split_leakage')
    case_ids = [c['id'] for d in documents for c in d['cases']]
    if len(case_ids) != len(set(case_ids)): raise ValueError('duplicate_case')
    rows = []
    for doc in documents:
        if split != 'all' and doc['split'] != split: continue
        pool = []
        for page in doc['pages']:
            mapping = PdfPageLabel(catalog_label=page.get('catalog'),printed_label=page.get('printed'),
                footer_node_ref=f"#/footer/{page['physical']}" if page.get('printed') else None,status=page['status']) if page.get('status') else None
            source = SourceLocation(format='pdf',page=page['physical'],pdf_page_label=mapping)
            # 相同字面数字的正文故意不能参与地址匹配。
            pool.append(dict(chunk_id=f"{doc['id']}-{page['physical']}",text='费用19元，比例20%，期限31天。',sources=[source.model_dump()]))
        for case in doc['cases']:
            slots = requested_page_slots(case['question'],pool)
            actual = sorted({pool[i]['sources'][0]['page'] for slot in slots for i in slot['indices']})
            rows.append(dict(case_id=case['id'],document_id=doc['id'],split=doc['split'],
                             actual_pages=actual,expected_pages=case['expected_pages'],
                             passed=bool(slots) and actual==case['expected_pages']))
    if not rows: raise ValueError('no_cases')
    return dict(schema='page-address-report-v1',fixture_sha256=sha256(raw).hexdigest(),page_label_policy=PAGE_LABEL_VERSION,
                numeric_role_policy=NUMERIC_ROLE_VERSION,provider_calls=0,scope='synthetic source-address candidate matching only',
                generated_answer_evaluated=False,ocr_evaluated=False,split_counts=dict(Counter(r['split'] for r in rows)),
                total=len(rows),passed=sum(r['passed'] for r in rows),failed=sum(not r['passed'] for r in rows),cases=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split',choices=['dev','calibration','engineering_holdout','all'],default='dev')
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    # 旧报告独占创建，失败证据不被后续运行覆盖。
    if args.report.exists(): parser.error('report_already_exists')
    report=evaluate(split=args.split)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    with args.report.open('x',encoding='utf-8') as stream: json.dump(report,stream,ensure_ascii=False,indent=2)
    print(json.dumps({k:report[k] for k in ('total','passed','failed','provider_calls','fixture_sha256')}))
    return int(bool(report['failed']))


if __name__=='__main__':
    raise SystemExit(main())
