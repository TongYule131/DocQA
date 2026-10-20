"""整页评分修订：显式遵循Docling逻辑网格的稀疏空白表示，保留首次评分。"""
from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
try:
    from scripts.ocr_layout_metrics import load_gold,score_page,aggregate
    from scripts.ocr_table_grid import canonical_prediction
except ModuleNotFoundError:
    from ocr_layout_metrics import load_gold,score_page,aggregate
    from ocr_table_grid import canonical_prediction

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--profile',required=True);p.add_argument('--split',choices=['dev','holdout'],required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    gold=load_gold(args.root);base=args.root/'outputs'/args.profile/args.split
    summary=json.loads((base/'summary.json').read_text(encoding='utf-8'));pages=[]
    for row in [r for r in gold['pages'] if r['split']==args.split]:
        path=base/f'{row["id"]}.json'
        raw=json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        prediction=canonical_prediction(raw) if raw else None
        pages.append(score_page(row,prediction))
    report={'schema_version':'docqa-ocr-layout-score-grid-v2','profile':args.profile,'split':args.split,
            'canonicalization':'docling-grid-empty-v1-no-gold','overall':aggregate(pages),
            'by_source_kind':{kind:aggregate([p for p in pages if p['source_kind']==kind]) for kind in sorted({p['source_kind'] for p in pages})},
            'pages':pages,'inference_summary':summary,
            'scorer_sha256':hashlib.sha256(Path(__file__).with_name('ocr_layout_metrics.py').read_bytes()).hexdigest(),
            'grid_adapter_sha256':hashlib.sha256(Path(__file__).with_name('ocr_table_grid.py').read_bytes()).hexdigest()}
    with args.output.open('x',encoding='utf-8') as file:json.dump(report,file,ensure_ascii=False,indent=2)
    print(json.dumps({'profile':args.profile,'split':args.split,**report['overall']},ensure_ascii=False))
    return 0
if __name__=='__main__':raise SystemExit(main())
