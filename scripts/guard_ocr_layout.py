"""只对已保存候选施加v2保护，不执行任何OCR或供应商调用。"""
from __future__ import annotations
import argparse,json,hashlib,time
from pathlib import Path
try:from scripts.ocr_candidate_guards import guarded_page
except ModuleNotFoundError:from ocr_candidate_guards import guarded_page

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--profile',choices=['local-small','routed'],required=True)
    p.add_argument('--split',choices=['dev','holdout'],required=True);args=p.parse_args()
    begin=time.perf_counter();root=args.root/'outputs'
    raw_dir=root/args.profile/args.split;out=root/(args.profile+'-guarded')/args.split;out.mkdir(parents=True,exist_ok=False)
    raw_summary=json.loads((raw_dir/'summary.json').read_text(encoding='utf-8'))
    for row in raw_summary['pages']:
        name=row['id']+'.json'
        baseline=json.loads((root/'baseline'/args.split/name).read_text(encoding='utf-8'))
        raw=json.loads((raw_dir/name).read_text(encoding='utf-8'))
        result=guarded_page(baseline,raw)
        (out/name).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    summary={**raw_summary,'profile':args.profile+'-guarded','raw_profile':args.profile,
             'guard_sha256':hashlib.sha256(Path(__file__).with_name('ocr_candidate_guards.py').read_bytes()).hexdigest(),
             'offline_guard_seconds':time.perf_counter()-begin,'additional_ocr_calls':0}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(args.profile,args.split,'guarded',len(raw_summary['pages']))
    return 0
if __name__=='__main__':raise SystemExit(main())
