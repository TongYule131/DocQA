"""M5 恢复演练脚本：注入合成解析版本并用固定模型跑一次分析任务。

**本脚本不发起任何真实模型调用。** 它用固定回复的桩模型替换模型边界，
只验证真实迁移、任务领取、检查点、结果发布与重启后读取；在线语义验收由
`scripts/evaluate_analysis.py --allow-online` 在明确预算内单独完成。

用法（务必先显式设置独立数据目录）：

    $env:DOCQA_DATA_DIR = 'D:\\python\\DocQA\\data\\stable-release-dev\\m5-run'
    .\\.venv\\Scripts\\python.exe scripts\\seed_recovery_sample.py `
        --data-dir data/stable-release-dev/m5-run [--run-job]

`--data-dir` 仅用于**断言**传入的数据目录与当前 `DOCQA_DATA_DIR` 一致：
不一致时立即退出，避免把演练数据写进正式库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analysis_jobs import AnalysisJobService  # noqa: E402
from app.analysis_worker import AnalysisWorker  # noqa: E402
from app.config import Settings  # noqa: E402
from app.repository import Repository, now_iso  # noqa: E402
from app.schemas import (  # noqa: E402
    Block,
    Document,
    ParseTask,
    ParseVersion,
    SourceLocation,
)

SAMPLE_VERSION_ID = "v-recovery-sample"
SAMPLE_DOCUMENT_ID = "doc-recovery-sample"

# 合成样本：明确的合成声明 + 一处数据事实与一处观点，便于核对引用是否回到原文。
SAMPLE_BLOCKS = [
    ("section_header", "第一章 演练数据", "第一章 演练数据"),
    ("paragraph", "本次演练样本的营业收入为 600 万元，同比持平。", "第一章 演练数据"),
    ("paragraph", "演练负责人李某表示，该样本仅用于恢复演练，不代表任何真实业务。", "第二章 说明"),
]


def ensure_sample(repository: Repository) -> tuple[str, str]:
    """写入合成文档与解析版本；已存在时直接复用（幂等，方便重复演练）。"""
    repository.initialize()
    existing = repository.get_parse_version(SAMPLE_VERSION_ID)
    if existing is not None:
        return existing.document_id, existing.id

    repository.create(Document(
        id=SAMPLE_DOCUMENT_ID, filename="合成样本-恢复演练.txt", size=512,
        created_at=now_iso(), status="uploaded", format="txt"))
    repository.create_task(ParseTask(
        id=f"ptask-{SAMPLE_DOCUMENT_ID}", document_id=SAMPLE_DOCUMENT_ID, status="succeeded",
        stage="done", created_at=now_iso(), updated_at=now_iso()))
    blocks = [
        Block(id=f"{SAMPLE_VERSION_ID}-b{index}", document_id=SAMPLE_DOCUMENT_ID,
              parse_version_id=SAMPLE_VERSION_ID, order_index=index, block_type=block_type,
              text=text, heading_path=heading,
              sources=[SourceLocation(format="txt", page=1, line_start=1, line_end=3,
                                      note="合成样本来源：TXT 逻辑页，不是物理 PDF 页码")])
        for index, (block_type, text, heading) in enumerate(SAMPLE_BLOCKS)
    ]
    version = ParseVersion(
        id=SAMPLE_VERSION_ID, document_id=SAMPLE_DOCUMENT_ID, task_id=f"ptask-{SAMPLE_DOCUMENT_ID}",
        origin_hash="recovery-sample", parser_name="synthetic-fixture", parser_version="fixture-1",
        config_summary='{"synthetic":true}', result_schema_version="synthetic-1",
        result_hash="recovery-sample-hash", quality_status="ok",
        quality_summary="合成演练样本，仅用于启停与恢复演练",
        block_count=len(blocks), page_count=1, chunk_count=0, created_at=now_iso())
    with repository.connect() as db:
        db.execute(
            """INSERT INTO parse_versions(id, document_id, task_id, origin_hash, parser_name,
               parser_version, config_summary, result_schema_version, result_hash, quality_status,
               quality_summary, block_count, page_count, chunk_count, result_json_path,
               markdown_path, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (version.id, version.document_id, version.task_id, version.origin_hash,
             version.parser_name, version.parser_version, version.config_summary,
             version.result_schema_version, version.result_hash, version.quality_status,
             version.quality_summary, len(blocks), version.page_count, 0, None, None,
             version.created_at))
        for block in blocks:
            db.execute(
                """INSERT INTO blocks(id, document_id, parse_version_id, order_index, block_type,
                   label, text, heading_path, table_json, node_ref, sheet_name, char_count, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (block.id, block.document_id, block.parse_version_id, block.order_index,
                 block.block_type, None, block.text, block.heading_path, None, None, None,
                 len(block.text), now_iso()))
            for ordinal, source in enumerate(block.sources):
                db.execute(
                    """INSERT INTO sources(id, document_id, parse_version_id, block_id, ordinal,
                       format, page_no, page_end, line_start, line_end, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"{block.id}-src{ordinal}", block.document_id, block.parse_version_id,
                     block.id, ordinal, source.format, source.page, None, source.line_start,
                     source.line_end, source.note))
        db.execute("UPDATE documents SET active_parse_version_id=?, status='parsed',"
                   " page_count=1, chunk_count=0 WHERE id=?",
                   (version.id, SAMPLE_DOCUMENT_ID))
    return SAMPLE_DOCUMENT_ID, SAMPLE_VERSION_ID


class FixedModel:
    """固定回复的桩模型：记录调用次数，绝不访问网络。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if len(self.calls) > len(self.replies):
            raise AssertionError("桩模型调用次数超出预设")
        return self.replies[len(self.calls) - 1]


def main() -> int:
    parser = argparse.ArgumentParser(description="M5 恢复演练：注入合成样本并可跑通一次分析任务（零真实调用）")
    parser.add_argument("--data-dir", required=True,
                        help="本次演练使用的数据目录；必须与 DOCQA_DATA_DIR 一致")
    parser.add_argument("--kind", choices=["extraction", "summary"], default="extraction")
    parser.add_argument("--run-job", action="store_true", help="用桩模型执行一次任务并发布结果")
    parser.add_argument("--evidence-dir", default=None, help="证据输出目录（默认写在数据目录下）")
    args = parser.parse_args()

    expected = Path(args.data_dir).resolve()
    actual = Path(os.getenv("DOCQA_DATA_DIR", "")).resolve() if os.getenv("DOCQA_DATA_DIR") else None
    if actual is None:
        print("拒绝执行：未设置 DOCQA_DATA_DIR。请先显式指向独立数据目录。", file=sys.stderr)
        return 2
    if actual != expected:
        print(f"拒绝执行：DOCQA_DATA_DIR={actual} 与 --data-dir={expected} 不一致。", file=sys.stderr)
        return 2
    if expected.name == "data":
        print("拒绝执行：不允许在默认 data 目录做演练。", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    repository = Repository(expected / "docqa.db")
    summary = repository.initialize()
    print(f"数据目录 : {event_data_dir(settings)}")
    print(f"迁移     : from={summary['from']} to={summary['to']} applied={summary['applied']} "
          f"backup={summary['backup']}")
    document_id, version_id = ensure_sample(repository)
    print(f"合成样本 : document={document_id} version={version_id}")

    service = AnalysisJobService(settings, repository)
    plan = service.build_plan(document_id, args.kind)
    print(f"输入规划 : 可分析单元 {len(plan.plan.units)}"
          f" · 批次 {len(plan.plan.batches)}"
          f" · 请求上界 {plan.plan.request_upper_bound}"
          f" · 可执行 {plan.plan.executable}")

    evidence = Path(args.evidence_dir) if args.evidence_dir else expected / "m5-evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    plan_path = evidence / f"plan-{args.kind}.json"
    plan_path.write_text(
        json.dumps(service.plan_response(plan, args.kind).model_dump(), ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"规划证据 : {plan_path}")

    if not args.run_job:
        print("未指定 --run-job：仅完成规划与样本注入（零模型调用）。")
        return 0

    batch = plan.plan.batches[0]
    target_ref = next(index for index, unit in enumerate(batch, start=1) if "600 万元" in unit.text)
    target_text = batch[target_ref - 1].text
    if args.kind == "extraction":
        reply = json.dumps({
            "items": [{"kind": "data", "content": "营业收入为 600 万元",
                       "name": "营业收入", "value_text": "600 万元", "period": None,
                       "refs": [target_ref]}],
            "quotes": {str(target_ref): target_text},
            "sections": {"data": "present", "conclusion": "none", "viewpoint": "none"},
            "limitations": [],
        }, ensure_ascii=False)
    else:
        reply = json.dumps({
            "topic_overview": "演练样本概述",
            "main_points": [{"text": "营业收入为 600 万元", "refs": [target_ref]}],
            "exceptions": [],
            "quotes": {str(target_ref): target_text},
            "limitations": [],
        }, ensure_ascii=False)

    response, status = service.submit(document_id, args.kind, None)
    job_id = response.job.id
    print(f"任务创建 : http={status} job={job_id} status={response.job.status} "
          f"max_requests={response.job.max_requests}")

    model = FixedModel([reply])
    worker = AnalysisWorker(settings, repository, model=model, worker_id="m5-recovery-worker")
    executed = worker.run_job(job_id)
    job = repository.get_analysis_job(job_id)
    print(f"任务执行 : claimed={executed} status={job.status} result={job.result_id} "
          f"requests_used={job.requests_used} 模型调用={len(model.calls)}")
    calls = [call.model_dump() for call in repository.analysis_calls(job_id)]
    ledger = evidence / "call-ledger.json"
    ledger.write_text(json.dumps({"job_id": job_id, "calls": calls}, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    print(f"调用账本 : {ledger}（{len(calls)} 条，全部来自桩模型，未发生真实调用）")

    if job.result_id:
        for fmt in ("markdown", "json"):
            body, _media, filename = service.export(job.result_id, fmt)
            (evidence / f"export-{fmt}").write_text(body, encoding="utf-8")
        print(f"导出证据 : {evidence}\\export-markdown、export-json")
    return 0


def event_data_dir(settings: Settings) -> str:
    return str(settings.data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
