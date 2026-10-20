"""在现有解析镜像的独立容器内执行冻结OCR对照；网络必须由docker禁用。

不接触业务数据库，不加载.env，small缓存只读，medium路径明确指定。
每个profile独立进程退出释放会话；输出文件独占创建，首次失败保留。
"""
from __future__ import annotations
import argparse,ctypes,hashlib,importlib.metadata,json,os,threading,time
from pathlib import Path


class GpuMemory:
    """NVML设备级采样；与torch分配器无关，不把权重大小当成显存。"""
    def __init__(self):
        self.lib=ctypes.CDLL('libnvidia-ml.so.1')
        class Info(ctypes.Structure):
            _fields_=[('total',ctypes.c_ulonglong),('free',ctypes.c_ulonglong),('used',ctypes.c_ulonglong)]
        self.Info=Info
        if self.lib.nvmlInit_v2()!=0:raise RuntimeError('NVML初始化失败')
        self.device=ctypes.c_void_p()
        if self.lib.nvmlDeviceGetHandleByIndex_v2(0,ctypes.byref(self.device))!=0:raise RuntimeError('NVML设备不可用')
        self.samples=[];self.stop_event=threading.Event();self.sample()
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
    def sample(self):
        info=self.Info()
        if self.lib.nvmlDeviceGetMemoryInfo(self.device,ctypes.byref(info))!=0:raise RuntimeError('NVML读显存失败')
        self.samples.append(int(info.used))
    def loop(self):
        while not self.stop_event.wait(.05):self.sample()
    def close(self):
        self.stop_event.set();self.thread.join();self.sample()
        return {'scope':'device_total_used_not_per_process','interval_ms':50,'baseline_bytes':self.samples[0],
                'peak_bytes':max(self.samples),'samples':len(self.samples)}


def deskew(image):
    import cv2,numpy as np
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
    mask=cv2.threshold(gray,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
    points=cv2.findNonZero(mask)
    angle=0.
    if points is not None and len(points)>30:
        angle=float(cv2.minAreaRect(points)[-1])
        if angle>45:angle-=90
    if not .3<=abs(angle)<=5:return image,0.
    h,w=image.shape[:2]
    matrix=cv2.getRotationMatrix2D((w/2,h/2),angle,1)
    # 扩大画布避免旋转切掉边缘数字和标点；不改变识别文字。
    cosine,sine=abs(matrix[0,0]),abs(matrix[0,1])
    nw,nh=int(h*sine+w*cosine+1),int(h*cosine+w*sine+1)
    matrix[0,2]+=nw/2-w/2;matrix[1,2]+=nh/2-h/2
    return cv2.warpAffine(image,matrix,(nw,nh),flags=cv2.INTER_CUBIC,borderValue=(255,255,255)),angle


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,required=True)
    parser.add_argument('--profile',choices=['small','small-quality','medium','medium-quality'],required=True)
    parser.add_argument('--split',choices=['dev','holdout'],required=True)
    parser.add_argument('--probe',action='store_true')
    args=parser.parse_args(); root=args.corpus
    corpus_bytes=(root/'corpus.json').read_bytes();data=json.loads(corpus_bytes)
    plan=json.loads((root/'plan.json').read_text())
    if hashlib.sha256(corpus_bytes).hexdigest()!=plan['corpus_sha256']:raise ValueError('冻结标注改变')
    profile=plan['profiles'][args.profile]
    cache=Path('/cache/docling/models/RapidOcr')
    paths={'det':cache/'PP-OCRv6_det_small.onnx','rec':cache/'PP-OCRv6_rec_small.onnx',
           'cls':cache/'ch_ppocr_mobile_v2.0_cls_mobile.onnx','keys':cache/'ppocrv6_dict.txt'}
    if profile['model_size']=='medium':
        paths.update(det=root/'models/PP-OCRv6_det_medium.onnx',rec=root/'models/PP-OCRv6_rec_medium.onnx')
    hashes={k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in paths.items()}
    expected={'small':{'det':'090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f','rec':'6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884'},
              'medium':{'det':'92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2','rec':'eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b'}}
    if any(hashes[k]!=v for k,v in expected[profile['model_size']].items()):raise ValueError('模型哈希改变')
    out=root/'runs'/(args.profile+('-probe' if args.probe else ''))/args.split
    out.mkdir(parents=True,exist_ok=False)
    if args.probe:
        import sys
        sys.path.insert(0,'/project-scripts')
        from serve_with_ocr_probe import install_probe
        install_probe(out/'gpu-probe')
    import cv2
    from rapidocr import ModelType,RapidOCR
    quality=profile['detail']
    params={'Global.text_score':.5,'Global.max_side_len':4000 if quality else 2000,
            'EngineConfig.onnxruntime.use_cuda':True,'EngineConfig.onnxruntime.intra_op_num_threads':4,
            'EngineConfig.onnxruntime.cuda_ep_cfg.device_id':0,
            'Det.model_path':str(paths['det']),'Rec.model_path':str(paths['rec']),
            'Cls.model_path':str(paths['cls']),'Rec.rec_keys_path':str(paths['keys']),
            'Det.model_type':ModelType(profile['model_size']),'Rec.model_type':ModelType(profile['model_size']),
            'Det.limit_side_len':1280 if quality else 736,'Rec.rec_batch_num':6,
            'Global.log_level':'warning'}
    memory=GpuMemory();start=time.perf_counter()
    reader=RapidOCR(params=params)
    init=time.perf_counter()-start
    warm=next(r for r in data['regions'] if r['split']=='dev')
    start=time.perf_counter();reader(str(root/warm['image']));warm_time=time.perf_counter()-start
    predictions=[]
    rows=[warm] if args.probe else [r for r in data['regions'] if r['split']==args.split]
    for row in rows:
        file=(root/row['image']).resolve()
        if not file.is_relative_to(root.resolve()) or hashlib.sha256(file.read_bytes()).hexdigest()!=row['image_sha256']:raise ValueError('输入图像改变')
        image=cv2.imread(str(file));start=time.perf_counter();angle=0.
        if quality:image,angle=deskew(image)
        try:
            result=reader(image)
            record={'id':row['id'],'text':'\n'.join(result.txts or []),
                    'boxes':result.boxes.tolist() if result.boxes is not None else [],
                    'scores':[float(v) for v in result.scores] if result.scores is not None else [],
                    'elapsed_seconds':time.perf_counter()-start,'deskew_degrees':angle}
        except Exception as exc:
            record={'id':row['id'],'text':'','error':type(exc).__name__,'elapsed_seconds':time.perf_counter()-start}
        (out/(row['id']+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        predictions.append(record)
        print(args.profile,args.split,row['id'],round(record['elapsed_seconds'],3),record.get('error','ok'),flush=True)
        if record.get('error'):break
    gpu=memory.close()
    report={'schema_version':'docqa-ocr-inference-v1','profile':args.profile,'split':args.split,
            'corpus_sha256':plan['corpus_sha256'],'model_sha256':hashes,'parameters':{k:getattr(v,'value',v) for k,v in params.items()},
            'versions':{n:importlib.metadata.version(n) for n in ['rapidocr','onnxruntime-gpu','docling-slim']},
            'initialization_seconds':init,'warmup_seconds':warm_time,'gpu':gpu,'probe':args.probe,
            'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'predictions':predictions,
            'provider_calls':0,'completed':len(predictions)==len(rows) and not any(p.get('error') for p in predictions)}
    (out/'predictions.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['completed'] else 1

if __name__=='__main__':raise SystemExit(main())
