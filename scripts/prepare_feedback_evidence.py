"""给已有定向JUnit绑定工单／输入／当前代码；不运行测试、不发模型请求。"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from xml.etree import ElementTree

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.runtime import source_identity


def prepare(bundle_path,fixture_path,report_path,*,kind,test_name,run_id):
    bundle=json.loads(bundle_path.read_text(encoding='utf-8'))
    content=report_path.read_text(encoding='utf-8')
    if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():raise ValueError('DTD is not supported')
    root=ElementTree.fromstring(content)
    matches=[t for t in root.iter('testcase') if t.get('name')==test_name or t.get('classname','')+'::'+t.get('name','')==test_name]
    if len(matches)!=1 or any(matches[0].find(k) is not None for k in ('failure','error','skipped')):raise ValueError('targeted testcase must pass')
    identity=source_identity()
    recorded={p.get('name'):p.get('value') for p in root.iter('property')}
    if recorded.get('docqa.source_fingerprint')!=identity or recorded.get('docqa.source_fingerprint_after')!=identity:
        raise ValueError('JUnit source identity is missing, stale, or changed during testing')
    fixture_hash=sha256(fixture_path.read_bytes()).hexdigest()
    test_properties={p.get('name'):p.get('value') for p in matches[0].iter('property')}
    required={'docqa.case_sha256':bundle['case_sha256'],'docqa.feedback_generation':str(bundle['generation']),
        'docqa.fixture_sha256':fixture_hash,'docqa.fixture_sha256_after':fixture_hash}
    if any(test_properties.get(k)!=v for k,v in required.items()):raise ValueError('targeted test must consume feedback_repro_input and record the actual case and fixture')
    properties=ElementTree.SubElement(root,'properties')
    values={'docqa.case_sha256':bundle['case_sha256'],'docqa.feedback_generation':str(bundle['generation']),
        'docqa.source_fingerprint':identity,'docqa.fixture_sha256':fixture_hash,
        'docqa.original_report_sha256':sha256(report_path.read_bytes()).hexdigest()}
    for name,value in values.items():ElementTree.SubElement(properties,'property',name=name,value=value)
    return {'case_id':bundle['id'],'kind':kind,'code_identity':identity,'fixture_sha256':fixture_hash,
        'report_xml':ElementTree.tostring(root,encoding='unicode'),'test_name':test_name,'run_id':run_id}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('bundle','fixture','report','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--kind',choices=('original','negative'),required=True)
    parser.add_argument('--test-name',required=True);parser.add_argument('--run-id',required=True)
    args=parser.parse_args();result=prepare(args.bundle,args.fixture,args.report,kind=args.kind,test_name=args.test_name,run_id=args.run_id)
    with args.output.open('x',encoding='utf-8') as target:json.dump(result,target,ensure_ascii=False,indent=2)
    print(json.dumps({'prepared':True,'provider_calls':0,'tests_executed':0}))


if __name__=='__main__':main()
