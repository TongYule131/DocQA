"""使用已保存解析结果与原PDF修复数字归位；默认只读计划，不调用OCR或模型。"""
import argparse
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.document_normalizer import normalize_docling_result
from app.parse_worker import ParseWorker
from app.repository import Repository, now_iso
from app.schemas import ParseTask


def prepare(settings,repository,document_id):
    document=repository.get(document_id)
    if not document or document.format!='pdf' or not document.active_parse_version_id:
        raise ValueError('需要已有成功解析版本的PDF')
    version=repository.get_parse_version(document.active_parse_version_id)
    root=settings.data_dir.resolve();original=root/'uploads'/document.id
    result_path,_=repository.version_result_paths(version.id)
    cached=(root/(result_path or '')).resolve()
    if not cached.is_relative_to(root/'parse-results') or not cached.is_file() or not original.is_file():
        raise ValueError('原件或本地结构化结果不可用，未请求重新解析')
    original_hash=sha256(original.read_bytes()).hexdigest()
    if version.origin_hash!=original_hash:raise ValueError('原件哈希与固定版本不一致')
    payload=json.loads(cached.read_text(encoding='utf-8'))
    normalized=normalize_docling_result(document.id,payload,'pdf',parser_version=version.parser_version,pdf_path=original)
    def view(blocks):
        return [(b.block_type,b.text,b.heading_path,[s.model_dump() for s in b.sources]) for b in blocks]
    changed=view(normalized.blocks)!=view(repository.blocks(version.id))
    report=dict(document_id=document.id,previous_version=version.id,
        inline=normalized.stats['inline'],eligible=changed and normalized.stats['inline'].get('recovered',0)>0,
        provider_http=0,index_changed=False)
    return document,version,payload,normalized,original_hash,report


def apply(settings,repository,prepared):
    document,version,payload,normalized,origin_hash,report=prepared
    if not report['eligible']:return report
    if repository.get(document.id).active_parse_version_id!=version.id:
        raise ValueError('活动版本已变化，请重新规划')
    if repository.find_active_task(document.id):raise ValueError('文档已有活动解析任务')
    stamp=now_iso();identifier='parse-layout-'+uuid4().hex
    repository.create_task(ParseTask(id=identifier,document_id=document.id,status='queued',stage='queued',
        created_at=stamp,updated_at=stamp,request_summary={'source':'cached-pdf-layout','previous_version':version.id}))
    task=repository.claim_next_task('cached-layout-repair',settings.worker_lease_seconds,task_id=identifier)
    if task is None:raise ValueError('修复任务无法领取')
    worker=ParseWorker(settings,repository)
    worker._publish(task,task.lease_token,document,payload,None,origin_hash=origin_hash,
        parser_version=version.parser_version,source='cached-pdf-layout',normalized=normalized)
    finished=repository.get_task(identifier)
    if finished.status!='succeeded':raise ValueError('修复未发布；原版本与索引保留，请查看解析任务')
    return dict(report,task_id=identifier,new_version=repository.get(document.id).active_parse_version_id)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--document-id',required=True)
    p.add_argument('--apply',action='store_true',help='发布新解析版本；旧版本及索引保留，索引需另行显式建立')
    args=p.parse_args()
    settings=replace(Settings.from_env(ROOT/'.env'),data_dir=args.data_dir.resolve())
    if not (settings.data_dir/'docqa.db').is_file():p.error('数据目录中没有已有数据库')
    repo=Repository(settings.data_dir/'docqa.db')
    prepared=prepare(settings,repo,args.document_id)
    value=apply(settings,repo,prepared) if args.apply else prepared[-1]
    print(json.dumps(value,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
