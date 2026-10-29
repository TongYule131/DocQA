"""只读核对封存职责的字段证据，原报告及原观察不改写。"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.evaluate_intent_responsibilities import digest,new_output,read,write,validate_inputs
from scripts.intent_obligations import field_obligations


def audit_fields(bundle_path, runs, output):
    bundle=read(bundle_path);validate_inputs(bundle['annotations'],bundle['inputs'])
    if digest({k:v for k,v in bundle.items() if k not in ('application_source_identity','input_hash','source_files')})!=bundle['input_hash']:
        raise ValueError('frozen_bundle_changed')
    for name,h in bundle['source_files'].items():
        if sha256((Path(bundle_path).parent/'source-documents'/name).read_bytes()).hexdigest()!=h:
            raise ValueError('frozen_source_changed')
    cases={c['id']:c for c in bundle['annotations']['cases']}; rows=[];artifacts={};seen=set()
    def bind(path):artifacts[str(Path(path).resolve())]=sha256(Path(path).read_bytes()).hexdigest()
    bind(bundle_path)
    for directory in runs:
        report_path=Path(directory)/'report.json'; report=read(report_path);bind(report_path)
        if any(report.get(key)!=bundle[key] for key in ('input_hash','application_source_identity')) or report['runner_sha256']!=bundle['runner_sha256']:
            raise ValueError('capture_identity_mismatch')
        for entry in report['cases']:
            identifier=entry['id'];case=cases[identifier]
            if identifier in seen:raise ValueError('duplicate_case')
            observed_path=Path(directory)/identifier/'observed.json';o=read(observed_path);bind(observed_path)
            if o['id']!=identifier or o['question']!=case['question'] or o['split']!=case['split']:
                raise ValueError('capture_case_identity_mismatch')
            seen.add(identifier)
            rows.append({'id':identifier,**field_obligations(case,o,bundle['inputs']['cases'][identifier])})
    out=new_output(output)
    result={'version':'intent-field-obligations-audit-v1','mode':'read_only_saved_observations',
        'annotation_sha256':bundle['annotation_sha256'],'input_hash':bundle['input_hash'],
        'capture_source_identity':bundle['application_source_identity'],'capture_runner_sha256':bundle['runner_sha256'],
        'audit_sources_sha256':{p:sha256((ROOT/p).read_bytes()).hexdigest() for p in
            ('scripts/audit_intent_fields.py','scripts/intent_obligations.py','scripts/evaluate_intent_responsibilities.py')},
        'cases':rows,'artifact_sha256':artifacts,'total':len(rows),'complete_cases':sum(r['complete'] for r in rows),
        'failed_checks':sum(r['failed'] for r in rows),'unmeasured_checks':sum(r['unmeasured'] for r in rows),
        'passed_checks':sum(r['passed'] for r in rows),'new_annotation_labels':False,'capture_rerun':False,
        'real_semantics_or_language_certified':False,'full_responsibility_acceptance_passed':False,'provider_http':0,'local_full_executed':False}
    write(out/'report.json',result)
    for relative in result['audit_sources_sha256']:
        (out/Path(relative).name).write_bytes((ROOT/relative).read_bytes())
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--runs',type=Path,nargs='+',required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=audit_fields(args.bundle,args.runs,args.output)
    print(json.dumps({k:v for k,v in result.items() if k not in ('cases','artifact_sha256')},ensure_ascii=False))
    return 0 if result['total'] and result['complete_cases']==result['total'] else 1


if __name__=='__main__':raise SystemExit(main())
