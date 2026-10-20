"""现有Docling镜像中的隔离解析入口，显式选择small/medium。

模型缓存只读；不替换线上权重。不使用VLM、纠错或生成模型。
单次至多4页。输出保持Docling原文和坐标，初次失败目录不覆盖。
"""
from __future__ import annotations
import argparse,hashlib,importlib.metadata,json,time
from pathlib import Path


def install_model_probe(output: Path, models: set[Path]):
    import onnxruntime as ort
    from serve_with_ocr_probe import summarize_profile
    import threading,uuid
    output.mkdir(parents=True,exist_ok=False)
    original_init,original_run=ort.InferenceSession.__init__,ort.InferenceSession.run
    def init(session,path_or_bytes,sess_options=None,*args,**kwargs):
        path=Path(path_or_bytes) if isinstance(path_or_bytes,(str,Path)) else None
        watched=path in models
        if watched:
            prefix=output/uuid.uuid4().hex
            sess_options=sess_options or ort.SessionOptions()
            sess_options.enable_profiling=True;sess_options.profile_file_prefix=str(prefix)
        original_init(session,path_or_bytes,sess_options,*args,**kwargs)
        if watched:
            record={'model':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                    'session_providers':session.get_providers(),'first_run_completed':False,'node_provider_counts':{}}
            target=prefix.with_suffix('.session.json')
            target.write_text(json.dumps(record,indent=2),encoding='utf-8')
            session._docqa_model_probe=(record,target,threading.Lock())
    def run(session,*args,**kwargs):
        probe=getattr(session,'_docqa_model_probe',None)
        if probe is None:return original_run(session,*args,**kwargs)
        record,target,lock=probe
        with lock:
            result=original_run(session,*args,**kwargs)
            if not record['first_run_completed']:
                profile=Path(session.end_profiling())
                record.update(first_run_completed=True,node_provider_counts=summarize_profile(json.loads(profile.read_text())),profile=str(profile))
                target.write_text(json.dumps(record,indent=2),encoding='utf-8')
            return result
    ort.InferenceSession.__init__,ort.InferenceSession.run=init,run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--models',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--profile',choices=['small','medium'],default='small')
    parser.add_argument('--start-page',type=int,required=True)
    parser.add_argument('--end-page',type=int,required=True)
    parser.add_argument('--force-ocr',action='store_true')
    parser.add_argument('--probe',action='store_true')
    args=parser.parse_args()
    if not 1<=args.start_page<=args.end_page or args.end_page-args.start_page>=4:raise ValueError('单次只接受1至4页')
    if args.input.suffix.lower()!='.pdf':raise ValueError('只接受PDF')
    cache=Path('/cache/docling/models');ocr_cache=cache/'RapidOcr'
    paths={'det':ocr_cache/'PP-OCRv6_det_small.onnx','rec':ocr_cache/'PP-OCRv6_rec_small.onnx',
           'cls':ocr_cache/'ch_ppocr_mobile_v2.0_cls_mobile.onnx','keys':ocr_cache/'ppocrv6_dict.txt'}
    if args.profile=='medium':paths.update(det=args.models/'PP-OCRv6_det_medium.onnx',rec=args.models/'PP-OCRv6_rec_medium.onnx')
    from download_ocr_medium import MODELS
    for key in ('det','rec'):
        if args.profile=='medium' and hashlib.sha256(paths[key].read_bytes()).hexdigest()!=MODELS[paths[key].name][1]:raise ValueError('medium权重哈希不匹配')
    args.output.mkdir(parents=True,exist_ok=False)
    start=time.perf_counter()
    try:
        if args.probe:install_model_probe(args.output/'gpu-probe',set(paths.values()))
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.accelerator_options import AcceleratorOptions,AcceleratorDevice
        from docling.datamodel.pipeline_options import PdfPipelineOptions,RapidOcrOptions
        from docling.document_converter import DocumentConverter,PdfFormatOption
        from rapidocr import ModelType
        options=RapidOcrOptions(lang=['ch'],force_full_page_ocr=args.force_ocr,
            det_model_path=str(paths['det']),rec_model_path=str(paths['rec']),cls_model_path=str(paths['cls']),
            rec_keys_path=str(paths['keys']),scale=3,
            rapidocr_params={'Det.model_type':ModelType(args.profile),'Rec.model_type':ModelType(args.profile)})
        pipeline=PdfPipelineOptions(artifacts_path=cache,do_ocr=True,ocr_options=options,
            accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CUDA,num_threads=4),
            ocr_batch_size=1,layout_batch_size=1,table_batch_size=1)
        converter=DocumentConverter(format_options={InputFormat.PDF:PdfFormatOption(pipeline_options=pipeline)})
        result=converter.convert(args.input,page_range=(args.start_page,args.end_page))
        result.document.save_as_json(args.output/'document.json')
        (args.output/'document.md').write_text(result.document.export_to_markdown(),encoding='utf-8')
        status=str(result.status.value)
        record={'status':status,'profile':args.profile,'source_sha256':hashlib.sha256(args.input.read_bytes()).hexdigest(),
                'page_range':[args.start_page,args.end_page],'force_ocr':args.force_ocr,
                'model_sha256':{k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in paths.items()},
                'elapsed_seconds':time.perf_counter()-start,'probe_enabled':args.probe,
                'versions':{n:importlib.metadata.version(n) for n in ['docling-slim','rapidocr','onnxruntime-gpu']},
                'provider_calls':0,'production_changed':False}
    except Exception as exc:
        record={'status':'failed','exception_type':type(exc).__name__,'message':str(exc),'elapsed_seconds':time.perf_counter()-start}
        (args.output/'summary.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        raise
    (args.output/'summary.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False),flush=True)
    return 0 if status=='success' else 1
if __name__=='__main__':raise SystemExit(main())
