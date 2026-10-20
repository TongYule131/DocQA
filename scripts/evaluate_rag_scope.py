"""零调用回放预标注条件适用范围；不读取模型配置、账本或正式库。"""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.rag_evaluation import digest
from app.rag_scope_evaluation import bind_alignment,evaluate_scoped_answer
from tests.fixtures.rag_quality.scope_bindings import make_scope_cases


def evaluate(cases=None):
    cases=make_scope_cases() if cases is None else cases
    rows=[]
    for case in cases:
        values=[('positive',case['positive']),*case['variants'].items()]
        for name,answer in values:
            review=evaluate_scoped_answer(case['annotation'],answer,bind_alignment(
                case['annotation'],answer,case['alignment']))
            expected=('unverified' if case['expected']=='unverified' else
                      'reviewed_literal_scope' if name=='positive' else 'failed')
            rows.append(dict(case_id=case['case_id'],split=case['split'],variant=name,
                expected=expected,matched=review['status']==expected,review=review))
    return dict(schema='docqa-scope-retention-evaluation-v1',cases_sha256=digest(cases),provider_calls=0,
        documents=len(cases),fixture_guard_passed=bool(rows) and all(r['matched'] for r in rows),
        explicit_positive_passes=sum(r['variant']=='positive' and r['review']['retention_gate_passed'] for r in rows),
        explicit_negative_rejections=sum(r['variant']!='positive' and r['review']['status']=='failed' for r in rows),
        unverified_outputs=sum(r['review']['status']=='unverified' for r in rows),
        model_generation='not_executed',semantic_accuracy=None,independent=False,rows=rows,
        boundary='Synthetic labelled scope replay, not a production semantic judge; ambiguous/unaligned outputs never count as passed')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.report.exists():parser.error('保留首次结果，请使用新报告路径')
    report=evaluate();args.report.parent.mkdir(exist_ok=True,parents=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'rows','boundary'}},ensure_ascii=True))
    return 0 if report['fixture_guard_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
