"""只读已存整页/补识别结果，分别报告字符、顺序和表格，不触发推理。"""
from __future__ import annotations
import argparse,json,hashlib
from pathlib import Path
try:
    from scripts.ocr_layout_metrics import aggregate,load_gold,score_page
except ModuleNotFoundError:
    from ocr_layout_metrics import aggregate,load_gold,score_page

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--profile',choices=['baseline','local-small','routed','local-small-guarded','routed-guarded'],required=True)
    parser.add_argument('--split',choices=['dev','holdout'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();gold=load_gold(args.root)
    base=args.root/'outputs'/args.profile/args.split
    summary=json.loads((base/'summary.json').read_text(encoding='utf-8'))
    pages=[]
    for page in [p for p in gold['pages'] if p['split']==args.split]:
        path=base/f'{page["id"]}.json'
        prediction=json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        pages.append(score_page(page,prediction))
    report={'schema_version':'docqa-ocr-layout-score-v1','profile':args.profile,'split':args.split,
            'overall':aggregate(pages),'by_source_kind':{kind:aggregate([p for p in pages if p['source_kind']==kind]) for kind in sorted({p['source_kind'] for p in pages})},
            'pages':pages,'inference_summary':summary}
    with args.output.open('x',encoding='utf-8') as file:json.dump(report,file,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k in ['profile','split','overall']},ensure_ascii=False))
    return 0
if __name__=='__main__':raise SystemExit(main())
