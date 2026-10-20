"""整页Docling及局部/语言分流隔离验证。默认只显示计划，--execute才推理。

容器只读取无答案inputs；gold不挂载。候选改字一律标记review_required，
不接入正式库、索引或问答证据，不把候选置信当成正确率。
"""
from __future__ import annotations
import argparse,copy,hashlib,importlib.metadata,json,time
from pathlib import Path
try:
    from scripts.ocr_routing import accept_candidate,choose_regions,center_inside,route_model
except ModuleNotFoundError:
    from ocr_routing import accept_candidate,choose_regions,center_inside,route_model


def normalize_box(box,width,height):
    if box is None:return None
    value=box.to_top_left_origin(height)
    return [max(0.,min(1.,v)) for v in [value.l/width,value.t/height,value.r/width,value.b/height]]


def export_page(result,source):
    if result.status.value != 'success':
        raise ValueError('conversion did not complete successfully')
    doc=result.document;page=next(iter(doc.pages.values()));w,h=page.size.width,page.size.height
    units=[];tables=[]
    for order,(item,level) in enumerate(doc.iterate_items()):
        if not getattr(item,'prov',None):continue
        box=normalize_box(item.prov[0].bbox,w,h)
        if hasattr(item,'data') and hasattr(item.data,'table_cells'):
            table={'id':item.self_ref,'bbox':box,'order':order,'rows':item.data.num_rows,'cols':item.data.num_cols,'cells':[]}
            units.append({'id':item.self_ref,'kind':'table','bbox':box,'text':'','order':order})
            for i,cell in enumerate(item.data.table_cells):
                record={'id':f'{item.self_ref}/cell-{i}','table_id':item.self_ref,'kind':'cell','order':order,
                        'bbox':normalize_box(cell.bbox,w,h),'text':cell.text,'r0':cell.start_row_offset_idx,
                        'r1':cell.end_row_offset_idx,'c0':cell.start_col_offset_idx,'c1':cell.end_col_offset_idx,
                        'column_header':cell.column_header}
                units.append(record);table['cells'].append(record)
            tables.append(table)
        elif getattr(item,'text',None) is not None:
            units.append({'id':item.self_ref,'kind':'text','bbox':box,'text':item.text,'order':order})
    lines=[]
    for parsed in result.pages:
        if parsed.parsed_page is None:raise ValueError('缺少OCR行，不能伪造置信分')
        for i,cell in enumerate(parsed.parsed_page.textline_cells):
            lines.append({'id':f'ocr-{i}','bbox':normalize_box(cell.rect.to_bounding_box(),w,h),
                          'text':cell.text,'confidence':float(cell.confidence)})
    if not lines:raise ValueError('解析无OCR行')
    return {'id':source['id'],'width':source['width'],'height':source['height'],'units':units,'tables':tables,
            'ocr_lines':lines,'status':result.status.value,'review_required':False}


def make_reader(size,config,models):
    from rapidocr import RapidOCR,ModelType
    cache=Path('/cache/docling/models/RapidOcr');root=cache if size=='small' else Path('/models')
    return RapidOCR(params={'Global.text_score':config['text_score'],'Global.log_level':'warning',
         'EngineConfig.onnxruntime.use_cuda':True,'EngineConfig.onnxruntime.intra_op_num_threads':4,
         'Det.model_path':str(root/f'PP-OCRv6_det_{size}.onnx'),'Rec.model_path':str(root/f'PP-OCRv6_rec_{size}.onnx'),
         'Cls.model_path':str(cache/'ch_ppocr_mobile_v2.0_cls_mobile.onnx'),'Rec.rec_keys_path':str(cache/'ppocrv6_dict.txt'),
         'Det.model_type':ModelType(size),'Rec.model_type':ModelType(size)})


def verify_weights(plan):
    hashes={}
    for size in ['small','medium']:
        root=Path('/cache/docling/models/RapidOcr') if size=='small' else Path('/models')
        for kind in ['det','rec']:
            key=f'{size}_{kind}'
            hashes[key]=hashlib.sha256((root/f'PP-OCRv6_{kind}_{size}.onnx').read_bytes()).hexdigest()
            if hashes[key]!=plan['models'][key]:raise ValueError('模型哈希与冻结清单不符')
    return hashes


def apply_crop(page,unit,reader,image,config):
    import cv2
    width,height=page['width'],page['height'];box=unit['bbox']
    pad=0 if unit['kind']=='cell' else 3
    x0,y0=max(0,int(box[0]*width)-pad),max(0,int(box[1]*height)-pad)
    x1,y1=min(width,int(box[2]*width)+pad+1),min(height,int(box[3]*height)+pad+1)
    crop=image[y0:y1,x0:x1]
    if not crop.size:return {'accepted':False,'reason':'empty_crop','id':unit['id']}
    start=time.perf_counter();result=reader(crop)
    text='\n'.join(result.txts or [])
    confidence=float(sum(result.scores)/len(result.scores)) if result.scores is not None and len(result.scores) else 0.
    accepted,reason=accept_candidate(unit['text'],text,confidence,config)
    changed=''.join(text.split())!=''.join(unit['text'].split())
    record={'id':unit['id'],'kind':unit['kind'],'bbox':box,'crop_pixels':[x0,y0,x1,y1],
            'original_text':unit['text'],'candidate_text':text,'candidate_confidence':confidence,
            'accepted':accepted,'reason':reason,'changed':changed,'review_required':changed,
            'triggers':unit['reasons'],'elapsed_seconds':time.perf_counter()-start,'new_lines':[]}
    if result.boxes is not None:
        for i,(rect,txt,score) in enumerate(zip(result.boxes,result.txts,result.scores)):
            points=rect.tolist();xs=[p[0] for p in points];ys=[p[1] for p in points]
            record['new_lines'].append({'id':f'crop:{unit["id"]}:{i}',
                'bbox':[(x0+min(xs))/width,(y0+min(ys))/height,(x0+max(xs))/width,(y0+max(ys))/height],
                'text':txt,'confidence':float(score)})
    return record


def apply_record(page,record):
    if record.get('changed'):page['review_required']=True
    if not record.get('accepted'):return
    for unit in page['units']:
        if unit['id']==record['id']:unit['text']=record['candidate_text']
    for table in page['tables']:
        for cell in table['cells']:
            if cell['id']==record['id']:cell['text']=record['candidate_text']
    page['ocr_lines']=[line for line in page['ocr_lines'] if not center_inside(line['bbox'],record['bbox'])]+record['new_lines']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path,default=Path('/inputs'))
    parser.add_argument('--output',type=Path,default=Path('/out'))
    parser.add_argument('--profile',choices=['baseline','local-small','routed'],default='baseline')
    parser.add_argument('--split',choices=['dev','holdout'],default='dev')
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    plan_bytes=(args.inputs/'plan.json').read_bytes();plan=json.loads(plan_bytes)
    if not args.execute:
        print(json.dumps({'mode':'plan_only','profile':args.profile,'split':args.split,'pages':[p['id'] for p in plan['pages'] if p['split']==args.split],'provider_calls':0}));return 0
    out=args.output/args.profile/args.split;out.mkdir(parents=True,exist_ok=False)
    from run_ocr_profile import GpuMemory
    memory=GpuMemory();start=time.perf_counter();config=plan['config'];records=[];reader=None;converter=None
    try:
        model_hashes=verify_weights(plan)
        if args.profile=='baseline':
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.accelerator_options import AcceleratorOptions,AcceleratorDevice
            from docling.datamodel.pipeline_options import PdfPipelineOptions,RapidOcrOptions
            from docling.document_converter import DocumentConverter,ImageFormatOption
            cache=Path('/cache/docling/models/RapidOcr')
            options=RapidOcrOptions(lang=['ch'],scale=config['scale'],det_model_path=str(cache/'PP-OCRv6_det_small.onnx'),
                rec_model_path=str(cache/'PP-OCRv6_rec_small.onnx'),cls_model_path=str(cache/'ch_ppocr_mobile_v2.0_cls_mobile.onnx'),rec_keys_path=str(cache/'ppocrv6_dict.txt'))
            pipeline=PdfPipelineOptions(artifacts_path=Path('/cache/docling/models'),ocr_options=options,
                do_ocr=True,generate_parsed_pages=True,accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CUDA,num_threads=4),
                ocr_batch_size=1,layout_batch_size=1,table_batch_size=1)
            converter=DocumentConverter(format_options={InputFormat.IMAGE:ImageFormatOption(pipeline_options=pipeline)})
        else:
            import cv2
        for source in [p for p in plan['pages'] if p['split']==args.split]:
            image=(args.inputs/source['image']).resolve()
            if not image.is_relative_to(args.inputs.resolve()) or hashlib.sha256(image.read_bytes()).hexdigest()!=source['image_sha256']:raise ValueError('输入图像改变')
            begin=time.perf_counter()
            if args.profile=='baseline':
                result=converter.convert(image)
                result.document.save_as_json(out/f'{source["id"]}.docling.json')
                page=export_page(result,source);page['crop_records']=[]
            else:
                baseline_path=args.output/'baseline'/args.split/f'{source["id"]}.json'
                baseline=json.loads(baseline_path.read_text(encoding='utf-8'))
                page=copy.deepcopy(baseline);page['crop_records']=[]
                selected=choose_regions(baseline,config)
                frame=cv2.imread(str(image));page_text=' '.join(u['text'] for u in baseline['units'])
                local_path=args.output/'local-small'/args.split/f'{source["id"]}.json'
                local=json.loads(local_path.read_text(encoding='utf-8')) if args.profile=='routed' else None
                cached={r['id']:r for r in local['crop_records']} if local else {}
                for unit in selected:
                    size=route_model(unit,page_text,config) if args.profile=='routed' else 'small'
                    if size=='small' and local:
                        record=copy.deepcopy(cached[unit['id']]);record['reused']=True
                    else:
                        if reader is None:reader=make_reader(size,config,plan['models'])
                        # routed中只有medium需要新会话，small复用已存结果，避免双模型同时占显存。
                        record=apply_crop(page,unit,reader,frame,config);record.update(model=size,reused=False)
                    page['crop_records'].append(record);apply_record(page,record)
            page.update(profile=args.profile,elapsed_seconds=time.perf_counter()-begin,
                        input_sha256=hashlib.sha256(plan_bytes).hexdigest(),image_sha256=source['image_sha256'])
            (out/f'{source["id"]}.json').write_text(json.dumps(page,ensure_ascii=False,indent=2),encoding='utf-8')
            records.append({'id':source['id'],'elapsed_seconds':page['elapsed_seconds'],'crop_calls':sum(not r.get('reused') for r in page['crop_records']),
                            'reused':sum(r.get('reused',False) for r in page['crop_records']),'review_required':page['review_required']})
            print(args.profile,args.split,source['id'],round(page['elapsed_seconds'],3),'crops',records[-1]['crop_calls'],flush=True)
            memory.sample()
            if max(memory.samples)>config['gpu_device_cap_gib']*1024**3:raise MemoryError('设备显存超过冻结上限；保留已完成页，不继续投递')
        summary={'completed':True,'pages':records}
    except Exception as exc:
        summary={'completed':False,'pages':records,'error':type(exc).__name__,'message':str(exc)}
        print(type(exc).__name__,str(exc),flush=True)
    finally:
        gpu=memory.close()
    summary.update(profile=args.profile,split=args.split,elapsed_seconds=time.perf_counter()-start,gpu=gpu,
          input_sha256=hashlib.sha256(plan_bytes).hexdigest(),runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          routing_sha256=hashlib.sha256(Path(__file__).with_name('ocr_routing.py').read_bytes()).hexdigest(),
          provider_calls=0,annotation_access=False,model_sha256=locals().get('model_hashes'),
          versions={n:importlib.metadata.version(n) for n in ['docling-slim','rapidocr','onnxruntime-gpu']})
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if summary['completed'] else 1
if __name__=='__main__':raise SystemExit(main())
