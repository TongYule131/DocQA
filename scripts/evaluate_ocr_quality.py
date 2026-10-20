"""只评分已保存OCR结果；不调用OCR、供应商、.env或正式数据库。"""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.ocr_quality_metrics import load_corpus,score_predictions,promotion_gate

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,required=True)
    parser.add_argument('--predictions',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,help='可选：同一保留集的small结果，用于冻结切换关卡')
    args=parser.parse_args()
    data=load_corpus(args.corpus)
    output=json.loads(args.predictions.read_text(encoding='utf-8'))
    import hashlib
    if output['corpus_sha256']!=hashlib.sha256((args.corpus/'corpus.json').read_bytes()).hexdigest():raise ValueError('结果不属于冻结标注')
    split=output['split'];data={**data,'regions':[r for r in data['regions'] if r['split']==split]}
    report=score_predictions(data,output['predictions'])
    report.update(profile=output['profile'],split=split,gpu=output['gpu'],
                  initialization_seconds=output['initialization_seconds'],warmup_seconds=output['warmup_seconds'])
    if args.baseline:
        baseline=json.loads(args.baseline.read_text(encoding='utf-8'))
        baseline_score=score_predictions(data,baseline['predictions'])
        report['promotion']=promotion_gate(baseline_score,report,baseline,output)
    with args.output.open('x',encoding='utf-8') as file:json.dump(report,file,ensure_ascii=False,indent=2)
    print(json.dumps({'profile':output['profile'],'split':split,**report['overall']},ensure_ascii=False))
    return 0 if not args.baseline or report['promotion']['accepted_for_default'] else 1
if __name__=='__main__':raise SystemExit(main())
