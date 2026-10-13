"""稳定版独立收尾的固定阶段入口；默认只核对计划，不读取密钥、不发请求。

真实执行还需用户直接授权具体案例和总上限。approval.json 只是该授权的本地记录，
命令行开关和本脚本均不构成授权。保留旧阶段账本，不借用 DeepSeek 的历史余额。
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parent.parent
PHASE_DIR = ROOT / 'data/stable-closure-20261009'
PHASE_ID = 'stable-closure-20261009'


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def verify_plan() -> tuple[dict, str]:
    """先检查固定证据和代码，发生变化时要求重新规划，不静默扩大获批范围。"""
    path = PHASE_DIR / 'frozen-plan.json'
    plan = json.loads(path.read_text(encoding='utf-8'))
    if plan['phase_id'] != PHASE_ID or {c['case_id'] for c in plan['cases']} != {
            'E1', 'E2', 'E3', 'E4', 'S1', 'S2', 'S3', 'S4'}:
        raise ValueError('阶段或固定八项案例不匹配')
    if len(plan['cases']) != 8 or not all(c['executable'] for c in plan['cases']):
        raise ValueError('存在缺失、重复或不可执行案例')
    for entry in plan['files']:
        if digest(Path(entry['path'])) != entry['sha256']:
            raise ValueError('输入、预期或代码发生变化，须重新核对计划')
    if sum(c['requests'] for c in plan['cases']) != plan['planned_requests']:
        raise ValueError('总请求上界与逐题计划不一致')
    return plan, digest(path)


def require_approval(plan: dict, plan_hash: str) -> int:
    path = PHASE_DIR / 'approval.json'
    if not path.exists():
        raise ValueError('尚无本阶段用户预算授权记录，拒绝收费请求')
    approval = json.loads(path.read_text(encoding='utf-8'))
    cap = approval.get('max_requests')
    if (approval.get('approved') is not True or approval.get('phase_id') != PHASE_ID
            or approval.get('plan_sha256') != plan_hash
            or type(cap) is not int or not plan['planned_requests'] <= cap <= 16
            or not approval.get('authorization_source')):
        raise ValueError('授权记录与具体计划或有限预算不匹配')
    return cap


def generate_with_evidence(settings, system_prompt: str, user_prompt: str, prefix: Path) -> str:
    """在 SDK 拒绝截断结果前留存最终正文；不保存思考、请求头或完整 HTTP 响应。"""
    import json as json_module
    from openai import DefaultHttpxClient
    from unittest.mock import patch
    from app import deepseek

    started = monotonic()

    def record_response(response):
        # HTTPX 响应 hook 先读取正文，SDK 随后仍按原生产规则校验，绝不接受截断内容。
        response.read()
        evidence = {'http_status': response.status_code, 'elapsed_seconds': round(monotonic() - started, 3),
                    'finish_reason': None, 'has_final_body': False}
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            payload = None
        if isinstance(payload, dict):
            choices = payload.get('choices')
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
                reason = choice.get('finish_reason')
                evidence['finish_reason'] = reason if isinstance(reason, str) else None
                message = choice.get('message')
                content = message.get('content') if isinstance(message, dict) else None
                if isinstance(content, str):
                    prefix.with_suffix('.raw.txt').write_text(content, encoding='utf-8')
                    evidence['has_final_body'] = True
            usage = payload.get('usage')
            if isinstance(usage, dict):
                evidence['usage'] = {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                                     if type(usage.get(key)) is int}
        prefix.with_suffix('.response.json').write_text(
            json_module.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')

    sdk_client = deepseek.OpenAI
    # 仅本评估进程这一调用替换 HTTP 客户端；模型参数、零重试和业务校验全部保留。
    with DefaultHttpxClient(event_hooks={'response': [record_response]}) as http_client:
        def recorded_client(**kwargs):
            return sdk_client(**kwargs, http_client=http_client)
        with patch.object(deepseek, 'OpenAI', recorded_client):
            return deepseek.DeepSeekModel(settings).generate(system_prompt, user_prompt)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-online', action='store_true')
    args = parser.parse_args(argv)
    try:
        plan, plan_hash = verify_plan()
        if not args.allow_online:
            ledger_path = PHASE_DIR / 'online-ledger.json'
            used = json.loads(ledger_path.read_text(encoding='utf-8'))['used'] if ledger_path.exists() else 0
            print(json.dumps({'phase_id': PHASE_ID, 'planned_requests': plan['planned_requests'],
                              'cases': [{k: c[k] for k in ('case_id', 'filename', 'batches', 'requests')}
                                        for c in plan['cases']], 'paid_requests': used,
                              'authorization_required': True}, ensure_ascii=False, indent=2))
            return 0
        cap = require_approval(plan, plan_hash)
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return run_online(plan, cap)


def run_online(plan: dict, cap: int) -> int:
    # 必须先验证授权，再导入并读取真实配置。所有数据库操作固定在独立目录。
    sys.path.insert(0, str(ROOT))
    from app.analysis_jobs import AnalysisJobService, analysis_model_signature
    from app.analysis_worker import AnalysisWorker
    from app.config import Settings
    from app.deepseek import ModelError
    from app.repository import Repository
    from app.schemas import AnalysisJobRequest
    from scripts.evaluate_analysis import Ledger, phase_ledger_lock

    data = PHASE_DIR / 'case-data'
    os.environ['DOCQA_DATA_DIR'] = str(data)
    settings = replace(Settings.from_env(), data_dir=data, **plan['analysis_settings'])
    if not settings.deepseek_api_key or analysis_model_signature(settings) != plan['model_signature']:
        raise ValueError('模型缺配置或配置与冻结计划不一致，零请求停止')
    repository = Repository(data / 'docqa.db')
    repository.initialize()
    service = AnalysisJobService(settings, repository)
    output = PHASE_DIR / 'online'
    output.mkdir(exist_ok=True)
    ledger_path = PHASE_DIR / 'online-ledger.json'
    with phase_ledger_lock(ledger_path):
        ledger = Ledger.load(ledger_path, cap)
        for case in plan['cases']:
            outcome = service.build_plan(case['document_id'], case['kind'], case['parse_version_id'])
            if outcome.plan.fingerprint != case['plan_fingerprint']:
                raise ValueError('实际任务计划发生变化，零新增请求停止')
            key = f'{PHASE_ID}-{case["case_id"]}'
            existing = repository.find_analysis_job_by_idempotency(case['document_id'], key)
            if existing is not None:
                if existing.status == 'succeeded':
                    continue
                # 不确定/失败的旧尝试绝不因再次启动脚本而重发；留给原因分析与明确修复。
                raise ValueError('存在未完成或失败的既有尝试；禁止脚本自动重试')
            if ledger.remaining() < case['requests']:
                raise ValueError('剩余预算不足以覆盖本题全部批次，拒绝开始')
            response, _ = service.submit(case['document_id'], case['kind'], AnalysisJobRequest(
                parse_version_id=case['parse_version_id'], plan_fingerprint=case['plan_fingerprint'],
                idempotency_key=key, regenerate=True))

            class RecordedModel:
                def generate(self, system_prompt, user_prompt):
                    request_hash = sha256((system_prompt + '\0' + user_prompt).encode()).hexdigest()
                    payload = json.loads(user_prompt)
                    role = 'batch' if 'units' in payload else 'reduce'
                    if role == 'batch' and request_hash not in case['batch_request_hashes']:
                        raise ModelError(503, '实际输入与冻结批次不一致，未发送请求')
                    if role == 'reduce' and not case['reduce_required']:
                        raise ModelError(503, '冻结计划没有汇总调用，未发送请求')
                    if sum(entry['case_id'] == case['case_id'] for entry in ledger.entries) >= case['requests']:
                        raise ModelError(503, '本题达到冻结请求上界，禁止增加或自动重试')
                    sequence = ledger.used + 1
                    prefix = output / f'{sequence:02}-{case["case_id"]}-{role}'
                    prefix.with_suffix('.request.json').write_text(json.dumps(
                        {'system': system_prompt, 'user': user_prompt, 'sha256': request_hash},
                        ensure_ascii=False, indent=2), encoding='utf-8')
                    ledger.online_started = True
                    entry = ledger.precharge(case['case_id'], role, '独立收尾固定输入')
                    entry.update(request_sha256=request_hash, job_id=response.job.id)
                    ledger.save()
                    try:
                        raw = generate_with_evidence(settings, system_prompt, user_prompt, prefix)
                        if not prefix.with_suffix('.raw.txt').exists():
                            # 离线模型桩没有 HTTP 响应 hook；真实正文由上面的 hook 原样保存。
                            prefix.with_suffix('.raw.txt').write_text(raw, encoding='utf-8')
                    except Exception:
                        status = 'response_rejected' if prefix.with_suffix('.response.json').exists() else 'uncertain'
                        ledger.settle(entry, status=status)
                        raise
                    # 仅记录收到正文；生产校验及人工语义判定分别保存，绝不写成通过。
                    ledger.settle(entry, status='response_received')
                    return raw

            AnalysisWorker(settings, repository, model=RecordedModel()).run_job(response.job.id)
            job = repository.get_analysis_job(response.job.id)
            evidence = {'case_id': case['case_id'], 'job': job.model_dump(mode='json'),
                        'semantic_verdict': 'pending_manual_review', 'phase_requests_used': ledger.used}
            if job.result_id:
                evidence['result'] = service.get_result(job.result_id).model_dump(mode='json')
            (output / f'{case["case_id"]}-result.json').write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'{case["case_id"]}: 工程状态 {job.status}，阶段已用 {ledger.used}/{ledger.total_budget}，语义待逐条复核')
            if job.status != 'succeeded':
                return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
