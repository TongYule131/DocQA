"""Isolated native-PDF/image baseline and independent tiled OCR. Default: plan only."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import re
import time
from pathlib import Path

try:
    from scripts.ocr_pdf_candidates import discover_uncovered, native_reliable, tile_boxes
except ModuleNotFoundError:
    from ocr_pdf_candidates import discover_uncovered, native_reliable, tile_boxes


def checked(root, relative, expected):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("Input changed or escaped input root")
    return path


def selected_sources(plan, split):
    pages = plan.get('pages')
    if not isinstance(pages, list) or not pages:
        raise ValueError('invalid_page_plan: no pages')
    seen = set()
    for page in pages:
        identifier = page.get('id') if isinstance(page, dict) else None
        if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', identifier)
                or identifier in seen or page.get('split') not in {'dev', 'holdout'}):
            raise ValueError('invalid_page_plan: unsafe or duplicate page identity')
        seen.add(identifier)
    sources = [p for p in pages if p['split'] == split]
    if not sources:
        raise ValueError('invalid_page_plan: selected split is empty')
    return sources


def checked_baseline(path, source, profile, input_sha256):
    page = json.loads(path.read_text(encoding='utf-8'))
    # 同名输出目录可能来自旧计划；先核对冻结输入再复用，不把旧证据接到新图。
    if (page.get('id') != source['id'] or page.get('profile') != profile
            or page.get('status') != 'success' or page.get('input_sha256') != input_sha256
            or page.get('image_sha256') != source['image_sha256']
            or page.get('width') != source['width'] or page.get('height') != source['height']):
        raise ValueError('baseline input identity mismatch')
    return page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=Path('/inputs'))
    parser.add_argument('--output', type=Path, default=Path('/out'))
    parser.add_argument('--profile', choices=['native-baseline', 'image-baseline', 'region-supplement', 'page-routed', 'routed-supplement'], required=True)
    parser.add_argument('--split', choices=['dev', 'holdout'], required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    raw_plan = (args.inputs / 'plan.json').read_bytes()
    plan = json.loads(raw_plan)
    try:
        sources = selected_sources(plan, args.split)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.execute:
        print(json.dumps({'mode':'plan_only','pages':[p['id'] for p in sources], 'provider_calls':0}))
        return 0
    from run_ocr_profile import GpuMemory
    from run_ocr_layout import export_page, make_reader
    out = args.output / args.profile / args.split
    out.mkdir(parents=True, exist_ok=False)
    memory = GpuMemory()
    started = time.perf_counter()
    records = []
    config = plan['config']
    try:
        cache = Path('/cache/docling/models/RapidOcr')
        for kind in ['det','rec']:
            actual = hashlib.sha256((cache / f'PP-OCRv6_{kind}_small.onnx').read_bytes()).hexdigest()
            if actual != plan['models'][f'small_{kind}']:
                raise ValueError('Small model hash changed')
        reader = converter = None
        is_supplement = args.profile in {'region-supplement', 'routed-supplement'}
        if not is_supplement:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
            from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
            from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
            ocr = RapidOcrOptions(lang=['ch'], scale=config['scale'], force_full_page_ocr=False,
                det_model_path=str(cache/'PP-OCRv6_det_small.onnx'),rec_model_path=str(cache/'PP-OCRv6_rec_small.onnx'),
                cls_model_path=str(cache/'ch_ppocr_mobile_v2.0_cls_mobile.onnx'),rec_keys_path=str(cache/'ppocrv6_dict.txt'))
            pipeline = PdfPipelineOptions(artifacts_path=Path('/cache/docling/models'),ocr_options=ocr,
                do_ocr=True,generate_parsed_pages=True,accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CUDA,num_threads=4),
                ocr_batch_size=1,layout_batch_size=1,table_batch_size=1)
            converter = DocumentConverter(format_options={InputFormat.PDF:PdfFormatOption(pipeline_options=pipeline),
                                                         InputFormat.IMAGE:ImageFormatOption(pipeline_options=pipeline)})
        else:
            import cv2
        for source in sources:
            begin = time.perf_counter()
            image = checked(args.inputs, source['image'], source['image_sha256'])
            native_path = checked(args.inputs,source['native_candidate'],source['native_candidate_sha256'])
            native = json.loads(native_path.read_text(encoding='utf-8'))
            inference_calls = 0
            if not is_supplement:
                use_pdf = args.profile == 'native-baseline' or (args.profile == 'page-routed' and native_reliable(native,config['native_min_chars']))
                file = checked(args.inputs,source['pdf'],source['pdf_sha256']) if use_pdf else image
                kwargs = {'page_range':(source['source_page'],source['source_page'])} if use_pdf else {}
                result = converter.convert(file, **kwargs)
                result.document.save_as_json(out/f'{source["id"]}.docling.json')
                page = export_page(result,source)
                page['actual_input_format'] = 'pdf' if use_pdf else 'image'
                page['native_route_reason'] = 'native_text_eligible' if use_pdf else 'scan_or_low_quality_text_layer'
                page['picture_boxes'] = []
                from run_ocr_layout import normalize_box
                docpage = next(iter(result.document.pages.values()))
                for picture in result.document.pictures:
                    if picture.prov:
                        page['picture_boxes'].append(normalize_box(picture.prov[0].bbox,docpage.size.width,docpage.size.height))
                inference_calls = 1
            else:
                base_profile = 'page-routed' if args.profile == 'routed-supplement' else 'native-baseline'
                baseline_path=args.output/base_profile/args.split/f'{source["id"]}.json'
                page=copy.deepcopy(checked_baseline(baseline_path, source, base_profile, hashlib.sha256(raw_plan).hexdigest()))
                candidates=[];tiles=[]
                # Reliable text pages are revisited only inside detected figures; scans use independent 2x2 tiles.
                boxes=page.get('picture_boxes',[])[:4] if native_reliable(native,config['native_min_chars']) else tile_boxes(config['tile_rows'],config['tile_cols'],config['tile_overlap'])
                if boxes:
                    if reader is None:
                        reader=make_reader('small',{'text_score':.5},plan['models'])
                    frame=cv2.imread(str(image));height,width=frame.shape[:2]
                    for ti,box in enumerate(boxes):
                        x0,y0=max(0,int(box[0]*width)),max(0,int(box[1]*height))
                        x1,y1=min(width,int(box[2]*width)+1),min(height,int(box[3]*height)+1)
                        cropped=frame[y0:y1,x0:x1]
                        if not cropped.size:continue
                        t=time.perf_counter();ocr_result=reader(cropped);inference_calls+=1
                        tiles.append({'bbox':box,'elapsed_seconds':time.perf_counter()-t})
                        if ocr_result.boxes is None:continue
                        for li,(rect,txt,score) in enumerate(zip(ocr_result.boxes,ocr_result.txts,ocr_result.scores)):
                            xs=[float(p[0]) for p in rect];ys=[float(p[1]) for p in rect]
                            candidates.append({'id':f'tile-{ti}-{li}','text':txt,'confidence':float(score),
                                'bbox':[(x0+min(xs))/width,(y0+min(ys))/height,(x0+max(xs))/width,(y0+max(ys))/height],
                                'tile_index':ti,'touches_tile_edge':min(xs)<2 or min(ys)<2 or max(xs)>x1-x0-2 or max(ys)>y1-y0-2})
                accepted,discovery=discover_uncovered(page['ocr_lines'],candidates,config['supplement_confidence'],config['max_supplement_lines'])
                page['ocr_lines']+=accepted
                page['discovery_records']=discovery;page['tiles']=tiles
                page['supplemental_lines']=accepted
                page['review_required']=page.get('review_required',False) or any(r['review_required'] for r in discovery)
            page.update(profile=args.profile,elapsed_seconds=time.perf_counter()-begin,
                input_sha256=hashlib.sha256(raw_plan).hexdigest(),image_sha256=source['image_sha256'])
            (out/f'{source["id"]}.json').write_text(json.dumps(page,ensure_ascii=False,indent=2),encoding='utf-8')
            records.append({'id':source['id'],'elapsed_seconds':page['elapsed_seconds'],'inference_calls':inference_calls,
                            'supplement_lines':len(page.get('supplemental_lines',[]))})
            print(args.profile,args.split,source['id'],round(page['elapsed_seconds'],3),'calls',inference_calls,flush=True)
            memory.sample()
            if max(memory.samples)>config['gpu_device_cap_gib']*1024**3:
                raise MemoryError('Device memory exceeded frozen cap')
        summary={'completed':True,'pages':records}
    except Exception as exc:
        summary={'completed':False,'pages':records,'error':type(exc).__name__,'message':str(exc)}
        print(type(exc).__name__,str(exc),flush=True)
    finally:
        gpu=memory.close()
    summary.update(profile=args.profile,split=args.split,elapsed_seconds=time.perf_counter()-started,gpu=gpu,
        input_sha256=hashlib.sha256(raw_plan).hexdigest(),provider_calls=0,annotation_access=False,
        runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        candidate_sha256=hashlib.sha256(Path(__file__).with_name('ocr_pdf_candidates.py').read_bytes()).hexdigest(),
        versions={n:importlib.metadata.version(n) for n in ['docling-slim','rapidocr','onnxruntime-gpu']})
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if summary['completed'] else 1


if __name__=='__main__':raise SystemExit(main())
