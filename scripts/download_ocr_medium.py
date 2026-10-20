"""只下载两份固定medium ONNX权重；默认仅展示计划，不加载业务配置。"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib,json
from pathlib import Path
import httpx

MODELS={
 'PP-OCRv6_det_medium.onnx':('det','92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2'),
 'PP-OCRv6_rec_medium.onnx':('rec','eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b'),
}
BASE='https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6'

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--download',action='store_true')
    args=parser.parse_args()
    plan=[{'filename':name,'url':f'{BASE}/{kind}/{name}','sha256':digest} for name,(kind,digest) in MODELS.items()]
    if not args.download:
        print(json.dumps({'mode':'plan_only','models':plan},indent=2));return 0
    args.output.mkdir(parents=True,exist_ok=True)
    for row in plan:
        dest=args.output/row['filename']
        if dest.exists():
            if hashlib.sha256(dest.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('已有权重哈希不匹配，不覆盖')
            print(row['filename'],'verified_existing',flush=True);continue
        temporary=dest.with_suffix('.download')
        if temporary.exists():raise ValueError('存在未完成下载，保留失败现场，不自动覆盖')
        # 模型托管服务使用签名CDN；只下载固定官方清单URL，HTTPS重定向后仍校验固定SHA256。
        with httpx.Client(timeout=httpx.Timeout(60,connect=20),follow_redirects=True,trust_env=False) as client:
            with client.stream('GET',row['url']) as response:
                response.raise_for_status()
                if response.url.scheme!='https':raise ValueError('拒绝降级HTTP')
                size=0; digest=hashlib.sha256()
                with temporary.open('xb') as stream:
                    for chunk in response.iter_bytes(1024*1024):
                        size+=len(chunk)
                        if size>256*1024*1024:raise ValueError('模型超出256MiB上限')
                        stream.write(chunk);digest.update(chunk)
        if digest.hexdigest()!=row['sha256']:raise ValueError('下载权重SHA256不匹配，保留原下载')
        temporary.rename(dest)
        receipt={**row,'bytes':size,'verified_at':datetime.now(timezone.utc).isoformat(),'provider_calls':0}
        dest.with_suffix('.receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
        print(row['filename'],'downloaded_verified',size,flush=True)
    return 0
if __name__=='__main__':raise SystemExit(main())
