"""实际默认路径的分阶段验收；默认只读计划，沿用已经批准的累计账本。"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.deepseek import DeepSeekModel
from app.embedding import APIEmbedding
from app.evaluation_budget import BudgetTransport, EvaluationBudget, EvaluationStopped
from app.question_jobs import QuestionJobService
from app.question_worker import QuestionWorker
from app.rag import RagService
from app.rag_evaluation import digest, evidence_metrics, validate_case
from app.rag_prompts import build_task_system_prompt, TASK_PROMPT_VERSION
from app.quote_spans import task_segment_payload as task_evidence_payload
from app.retrieval_planning import plan_retrieval
from app.repository import Repository
from app.runtime import code_fingerprint
from app.vector_index import DocumentIndex
from scripts.evaluate_rag_targeted_live import CONFIG_FIELDS, QUERY_FIELDS, file_hash, read, save


class ExtendedAcceptanceBudget(EvaluationBudget):
    """只读接续已被明确追加批准的原账本；不创建、改写或自动扩额。"""
    def __init__(self, path, *, fingerprint, extension, directory):
        extra, total = extension.get('additional_requests'), extension.get('total_request_limit')
        self.path, self.fingerprint = Path(path), fingerprint
        backup = directory / 'ledger-before-extension.db'
        if (extension.get('authorized') is not True or not extension.get('user_reply')
                or extension.get('previous_limit') != 40 or type(extra) is not int or not 1 <= extra <= 40
                or type(total) is not int or total != 40 + extra
                or type(extension.get('previous_used')) is not int or not 1 <= extension['previous_used'] <= 40
                or extension.get('scope_sha256') != file_hash(directory / 'scope-freeze.json')
                or extension.get('previous_approval_sha256') != file_hash(self.path.parent / 'approval.json')
                or not backup.is_file() or file_hash(backup) != extension.get('ledger_backup_sha256')):
            raise EvaluationStopped('追加批准或原账本快照不完整')
        self.limit = total
        with closing(sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True)) as db:
            if (db.execute('SELECT max_requests,fingerprint FROM approval WHERE id=1').fetchone() != (40, fingerprint)
                    or db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] != extension.get('previous_used')):
                raise EvaluationStopped('追加前累计快照不一致')
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('SELECT max_requests,fingerprint FROM approval WHERE id=1').fetchone() != (self.limit, fingerprint):
                raise EvaluationStopped('原账本没有对应追加批准')
        report = self.report()
        if digest(report['calls'][:extension['previous_used']]) != extension.get('previous_calls_sha256'):
            raise EvaluationStopped('追加前调用记录被改变')


def load_scope(directory):
    directory = Path(directory).resolve()
    allowed = (ROOT / 'data').resolve()
    if not directory.is_relative_to(allowed) or directory == allowed:
        raise EvaluationStopped('需要独立验收目录')
    plan, cases, auth, scope = [read(directory / n) for n in (
        'plan.json', 'cases.json', 'authorization-extension.json', 'scope-freeze.json')]
    if (plan.get('schema') != 'docqa-rag-acceptance-v1' or plan.get('limit') != 40
            or auth.get('total_request_limit') != 40 or auth.get('no_new_budget') is not True
            or not auth.get('user_reply') or plan.get('budget_parent') != auth.get('parent_directory')
            or scope.get('plan_sha256') != digest(plan) or scope.get('cases_sha256') != digest(cases)
            or plan.get('cases_sha256') != digest(cases)
            or scope.get('authorization_sha256') != file_hash(directory / 'authorization-extension.json')
            or plan.get('targets') != [c.get('case_id') for c in cases]
            or not cases or len({c['case_id'] for c in cases}) != len(cases)
            or scope.get('paper_rubric_sha256') != file_hash(directory / 'paper-semantic-rubric.json')
            or scope.get('source_rubric_sha256') != file_hash(directory / 'additional-source-rubric.json')):
        raise EvaluationStopped('批准、标签或计划不一致')
    parent = allowed / str(plan['budget_parent'])
    if parent.resolve().parent != allowed or parent == directory:
        raise EvaluationStopped('累计账本目录无效')
    if (not (parent / 'request-ledger.db').is_file()
            or file_hash(parent / 'approval.json') != auth.get('parent_approval_sha256')
            or file_hash(parent / 'runtime-freeze.json') != auth.get('parent_freeze_sha256')):
        raise EvaluationStopped('原批准或累计账本丢失，禁止新建替代账本')
    fingerprint = digest(read(parent / 'runtime-freeze.json'))
    extension_path = directory / 'budget-extension.json'
    if extension_path.exists():
        budget = ExtendedAcceptanceBudget(parent / 'request-ledger.db', fingerprint=fingerprint,
            extension=read(extension_path), directory=directory)
    else:
        with sqlite3.connect((parent / 'request-ledger.db').as_uri() + '?mode=ro', uri=True) as db:
            if db.execute('SELECT max_requests,fingerprint FROM approval WHERE id=1').fetchone() != (40, fingerprint):
                raise EvaluationStopped('原累计批准行不一致')
        budget = EvaluationBudget(parent / 'request-ledger.db', limit=40, fingerprint=fingerprint)
    report = budget.report()
    prefix = auth.get('parent_ledger_rows', 0)
    if prefix < 1 or digest(report['calls'][:prefix]) != auth.get('parent_ledger_rows_sha256'):
        raise EvaluationStopped('历史调用被改写')
    for case in cases:
        validate_case(case)
    return directory, plan, cases, parent, budget


class PreviewReached(Exception):
    pass


def regression_files(directory, case, source_revision, current_request):
    """跨格式回归也须绑定旧通过记录，且实际输入确有变化；不能为凑分重发。"""
    if not source_revision or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in source_revision):
        raise EvaluationStopped('回归来源修订无效')
    source=directory/'runs'/source_revision
    name=case['case_id'].replace(':','-')
    paths=[source/'outputs'/(name+'.result.json'),source/'outputs'/(name+'.preview.json'),
           source/'reviews'/(name+'.json')]
    previous,before,review=map(read,paths)
    answer=previous.get('result',{}).get('answer')
    if (previous['job']['status']!='succeeded' or answer is None
            or review.get('strict_pass') is not True or review.get('case_id')!=case['case_id']
            or review.get('case_sha256')!=digest(case) or review.get('answer_sha256')!=digest(answer)
            or digest(before['request'])==digest(current_request)):
        raise EvaluationStopped('没有绑定的语义通过记录或实际输入未变化，不重复收费验收')
    return {p.relative_to(directory).as_posix():file_hash(p) for p in paths}


def prepare(repository, settings, case, query):
    """让生产RAG构建实际消息，在模型边界截停；不发送生成请求。"""
    index = DocumentIndex(repository, APIEmbedding(settings))
    rag = RagService(settings, repository, index, None)
    snapshot = rag.capture_snapshot(case['document_id'], case['question'])
    if (any(snapshot.get(k) != query['snapshot'].get(k) for k in QUERY_FIELDS)
            or digest(query['result']) != query.get('result_sha256')):
        raise EvaluationStopped('查询缓存的模型或固定来源已变化')
    chunks = repository.chunks(case['document_id'], case['parse_version_id'])
    if digest([c.model_dump() for c in chunks]) != digest(case['chunks']):
        raise EvaluationStopped('冻结原文已变化')
    raw = index.validate_checkpoint(case['document_id'], snapshot, query['result'])
    base = build_task_system_prompt(include_table_structure=False)
    cap = rag._context_cap(base, case['question'], snapshot['filename'])
    planned = plan_retrieval(case['question'], raw, chunks, fulltext_budget=cap,
        max_items=settings.question_context_k, payload_transform=task_evidence_payload)
    captured = {}

    class PreviewModel:
        def generate(self, system, user):
            captured['request'] = dict(system=system, user=json.loads(user))
            raise PreviewReached()

    def observer(stage, details):
        if stage == 'evidence_fixed':
            captured['diagnostics'] = details['diagnostics']

    rag.model = PreviewModel()
    try:
        rag.answer(case['document_id'], case['question'], snapshot=snapshot,
                   retrieval_checkpoint=planned, observer=observer)
    except PreviewReached:
        pass
    if 'request' not in captured:
        raise EvaluationStopped('没有可生成的合格输入')
    selected = [i['chunk_id'] for i in captured['diagnostics']['selected']]
    return dict(snapshot=snapshot, retrieved=planned, **captured,
        retrieval=evidence_metrics(case, [i['chunk_id'] for i in planned['results']], selected))


@contextmanager
def providers(budget, context, guard):
    import httpx
    from openai import OpenAI
    import app.embedding
    import app.deepseek

    def factory(**kwargs):
        guard()
        current = context.get()
        if current is None or kwargs.get('max_retries') != 0:
            raise EvaluationStopped('请求没有归属或启用了自动重试')
        role, name = current
        client = httpx.Client(transport=BudgetTransport(budget, role=role, context=name),
            trust_env=False, follow_redirects=False, timeout=kwargs['timeout'])
        return OpenAI(**kwargs, http_client=client)

    previous = app.embedding.OpenAI, app.deepseek.OpenAI
    app.embedding.OpenAI = app.deepseek.OpenAI = factory
    try:
        yield
    finally:
        app.embedding.OpenAI, app.deepseek.OpenAI = previous


def run(directory, *, phase='plan', allow_online=False, targets=None, revision='v11-default'):
    directory, plan, cases, parent, budget = load_scope(directory)
    if phase == 'plan' or not allow_online:
        return dict(mode='plan_only', provider_calls_this_run=0, used=budget.report()['used'],
                    remaining=budget.report()['remaining'], planned=plan['planned_requests'])
    if phase not in {'index', 'retrieve', 'preview', 'answer'}:
        raise EvaluationStopped('未知阶段')
    targets = targets or plan['targets']
    if len(set(targets)) != len(targets) or not set(targets) <= set(plan['targets']):
        raise EvaluationStopped('题目超出固定范围')
    if not revision or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in revision):
        raise EvaluationStopped('修订名无效')
    if revision != 'v11-default':
        repair=read(directory/('repair-'+revision+'.json'))
        parent_revision=repair.get('parent_revision','')
        if (not parent_revision or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in parent_revision)
                or repair.get('parent_phase','answer') not in {'answer','preview'}):
            raise EvaluationStopped('父修订无效')
        parent_run=directory/'runs'/parent_revision
        review=read(parent_run/'semantic-review.json')
        parent_freeze=read(parent_run/(repair.get('parent_phase','answer')+'-freeze.json'))
        regressions=repair.get('regression_targets',[])
        regression_sources=repair.get('regression_sources',{})
        pending=review.get('remaining_case_ids',[])
        expected=[key for key in plan['targets'] if key in pending or key in regressions]
        if (repair.get('scope_sha256')!=file_hash(directory/'scope-freeze.json')
                or repair.get('parent_review_sha256')!=digest(review)
                or repair.get('targets')!=targets or targets!=expected
                or review.get('overall')!='not_fully_accepted' or not repair.get('reason')
                or repair.get('no_ledger_reset') is not True
                or not isinstance(regressions,list) or len(set(regressions))!=len(regressions)
                or any(key in pending or key not in plan['targets'] for key in regressions)
                or not isinstance(regression_sources,dict) or not set(regression_sources)<=set(regressions)
                or (regressions and not repair.get('regression_reason'))
                or parent_freeze.get('code_fingerprint')==code_fingerprint()):
            raise EvaluationStopped('缺少实质修复或未完成依据，不能重跑通过题')
    settings = replace(Settings.from_env(), data_dir=directory / 'run-data', allow_database_upgrade=False)
    if ({n:getattr(settings,n) for n in CONFIG_FIELDS} != plan['config']
            or settings.question_retrieval_strategy != plan['question_retrieval_strategy']):
        raise EvaluationStopped('实际默认配置已变化')
    if settings.data_dir.resolve() == (ROOT / 'data').resolve():
        raise EvaluationStopped('禁止使用正式库执行')
    for doc in plan['documents']:
        if file_hash(doc['original_path']) != doc['sha256']:
            raise EvaluationStopped('原件已变化')
    repo = Repository(settings.data_dir / 'docqa.db')
    for case in cases:
        if digest([c.model_dump() for c in repo.chunks(case['document_id'],case['parse_version_id'])]) != digest(case['chunks']):
            raise EvaluationStopped('解析原文已变化')
    bound_regression_files={}
    if revision!='v11-default':
        for key in regressions:
            case=next(c for c in cases if c['case_id']==key)
            name=key.replace(':','-')
            now=prepare(repo,settings,case,read(directory/'queries'/(name+'.json')))
            bound_regression_files.update(regression_files(directory,case,
                regression_sources.get(key,parent_revision),now['request']))
    run_dir = directory / 'runs' / revision
    run_dir.mkdir(parents=True, exist_ok=True)
    freeze = dict(code_fingerprint=code_fingerprint(), runner_sha256=file_hash(__file__),
                  scope_sha256=file_hash(directory/'scope-freeze.json'), phase=phase, targets=targets)
    extension_path = directory / 'budget-extension.json'
    if extension_path.exists():
        freeze['budget_extension_sha256'] = file_hash(extension_path)
    if revision!='v11-default':
        freeze['repair_sha256']=file_hash(directory/('repair-'+revision+'.json'))
    if bound_regression_files:
        freeze['regression_files']=bound_regression_files
    path = run_dir / (phase+'-freeze.json')
    if path.exists() and read(path) != freeze:
        raise EvaluationStopped('已冻结执行发生变化，需记录实质修复')
    if not path.exists():
        save(path,freeze)
    state_path = run_dir / (phase+'-state.json')
    state = read(state_path) if state_path.exists() else dict(completed={})
    if state.get('stopped'):
        raise EvaluationStopped('本次执行已停止；保留首次失败，不能自动重发')

    def guard():
        if code_fingerprint() != freeze['code_fingerprint'] or file_hash(__file__) != freeze['runner_sha256']:
            raise EvaluationStopped('执行中代码变化')
        if file_hash(directory/'scope-freeze.json') != freeze['scope_sha256']:
            raise EvaluationStopped('执行中范围变化')
        if (extension_path.exists() != ('budget_extension_sha256' in freeze)
                or extension_path.exists() and file_hash(extension_path) != freeze['budget_extension_sha256']):
            raise EvaluationStopped('执行中追加批准变化')
        if any(file_hash(directory/name)!=expected for name,expected in freeze.get('regression_files',{}).items()):
            raise EvaluationStopped('执行中回归依据变化')
        # 每次真正HTTP仍由共享账本预扣；阶段计划另限制每题/每文档的初次请求数。
        current=context.get()
        if current is not None:
            role,name=current
            calls=[c for c in budget.report()['calls'] if c['role']==role and c['context']==name]
            maximum=1
            if role=='index':
                doc=next((d for d in plan['documents'] if name=='acceptance:'+d['source_id']),None)
                if doc is None or doc.get('existing_index'):
                    raise EvaluationStopped('禁止重建既有正式索引副本')
                maximum=(len(repo.chunks(doc['document_id'],doc['parse_version_id']))+settings.embedding_batch_size-1)//settings.embedding_batch_size
            if len(calls)>=maximum:
                raise EvaluationStopped('固定步骤请求上界已用尽，不能隐式重发')

    def persist():
        save(state_path,state)
        report=budget.report()
        save(parent/'request-ledger.json',report)
        save(directory/'cumulative-usage.json',report)

    context = ContextVar('acceptance_http_context',default=None)
    index = DocumentIndex(repo,APIEmbedding(settings))
    queries = directory/'queries';queries.mkdir(exist_ok=True)
    outputs=run_dir/'outputs';outputs.mkdir(exist_ok=True)
    try:
        with providers(budget,context,guard):
            if phase == 'index':
                for doc in plan['documents']:
                    key=doc['source_id']
                    if key in state['completed']: continue
                    if doc.get('existing_index') and index.index_info(doc['document_id'])['status']!='indexed':
                        raise EvaluationStopped('既有索引不兼容，须重新核算计划')
                    context.set(('index','acceptance:'+key))
                    value=index.build(doc['document_id'])
                    if value['status']!='indexed': raise EvaluationStopped('索引未完成')
                    state['completed'][key]=value;persist()
            else:
                for case in cases:
                    key=case['case_id']
                    if key not in targets or key in state['completed']: continue
                    context.set(None)
                    guard()
                    name=key.replace(':','-')
                    query_path=queries/(name+'.json')
                    if phase=='retrieve':
                        if query_path.exists(): raise EvaluationStopped('查询已保存，禁止重复外调')
                        context.set(('query','acceptance:'+key))
                        snapshot=RagService(settings,repo,index,None).capture_snapshot(case['document_id'],case['question'])
                        raw=index.search_snapshot(case['document_id'],case['question'],settings.rag_retrieval_k,
                            snapshot=snapshot,include_scores=True)
                        save(query_path,dict(snapshot=snapshot,result=raw,result_sha256=digest(raw)))
                        state['completed'][key]=dict(query_sha256=file_hash(query_path));persist()
                        print(json.dumps(dict(case=key,phase=phase,used=budget.report()['used'])),flush=True)
                        continue
                    ready=prepare(repo,settings,case,read(query_path))
                    save(outputs/(name+'.preview.json'),ready)
                    if phase=='preview':
                        state['completed'][key]=dict(retrieval=ready['retrieval']);persist();continue

                    class RecordedModel:
                        def __init__(self): self.real=DeepSeekModel(settings)
                        @property
                        def last_call_metadata(self): return self.real.last_call_metadata
                        def generate(self,system,user):
                            request=dict(system=system,user=json.loads(user))
                            if request!=ready['request']: raise EvaluationStopped('实际消息不符冻结预览')
                            guard()
                            with (outputs/(name+'.request.json')).open('x',encoding='utf-8') as stream:
                                json.dump(request,stream,ensure_ascii=False,indent=2)
                            context.set(('generation','acceptance:'+revision+':'+key))
                            raw=self.real.generate(system,user)
                            (outputs/(name+'.final.txt')).write_text(raw,encoding='utf-8')
                            return raw

                    rag=RagService(settings,repo,index,RecordedModel())
                    service=QuestionJobService(settings,repo,rag)
                    reply,_=service.submit(case['document_id'],dict(question=case['question'],
                        idempotency_key=directory.name+'-'+revision+'-'+key))
                    job=reply['job']
                    if job['status']!='queued': raise EvaluationStopped('已有终态或在途任务，不重发')
                    worker=QuestionWorker(settings,repo,rag=rag)
                    claimed=worker.store.claim(worker.worker_id,settings.worker_lease_seconds,job_id=job['id'])
                    if not claimed or not worker.store.save_checkpoint(job['id'],claimed['lease_token'],
                            role='retrieval',payload=ready['retrieved'],sequence_no=None):
                        raise EvaluationStopped('检索检查点未保存')
                    worker.process_job(claimed)
                    job=service.get_job(job['id'])
                    result=dict(job=job,case_sha256=digest(case),retrieval=ready['retrieval'],semantic_acceptance='unreviewed')
                    if job['result_id']:
                        result['result']=service.get_result(job['result_id'])
                        (outputs/(name+'.md')).write_text(service.export(job['result_id'],'markdown')[0],encoding='utf-8')
                    save(outputs/(name+'.result.json'),result)
                    state['completed'][key]=dict(job_id=job['id'],status=job['status']);persist()
                    print(json.dumps(dict(case=key,phase=phase,status=job['status'],used=budget.report()['used'])),flush=True)
                    if job['status']!='succeeded': raise EvaluationStopped('首次应用或上游失败，停止后续请求')
        persist()
        return dict(phase=phase,used=budget.report()['used'],remaining=budget.report()['remaining'],semantic='unreviewed')
    except BaseException as exc:
        state['stopped']=dict(type=type(exc).__name__,reason=str(exc) if isinstance(exc,EvaluationStopped) else '执行失败')
        persist();raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--phase',choices=['plan','index','retrieve','preview','answer'],default='plan')
    parser.add_argument('--allow-online',action='store_true')
    parser.add_argument('--targets',nargs='+')
    parser.add_argument('--revision',default='v11-default')
    args=parser.parse_args()
    try:
        print(json.dumps(run(args.run_dir,phase=args.phase,allow_online=args.allow_online,
            targets=args.targets,revision=args.revision)),flush=True)
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='stopped',error_type=type(exc).__name__)),flush=True)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
