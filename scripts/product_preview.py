"""离线产品开发预览：真实上传/解析/任务，向量和回答采用合成桩，禁止连收费供应商。"""
import argparse
import os
import sys
import time
from pathlib import Path
from threading import Thread

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8997)
    parser.add_argument('--allow-database-upgrade', action='store_true')
    args = parser.parse_args()
    data = args.data_dir.resolve()
    if not data.is_relative_to(root / 'data') or data == root / 'data':
        parser.error('预览必须使用当前工作树 data 下的独立子目录，不能使用正式 data')
    if os.environ.get('DOCQA_DATA_DIR') != str(data):
        parser.error('请先显式设置 DOCQA_DATA_DIR 为同一独立目录')
    # 在导入应用前固定目录；不读或复制正式 .env。
    from app.config import Settings
    from app.parse_worker import ParseWorker
    from app.question_worker import QuestionWorker
    from app.rag import RagService
    from app.repository import Repository
    from app.runtime import RuntimeMismatch
    from app.vector_index import DocumentIndex
    from tests.test_question_jobs import Embedding, Model
    import app.main as application
    import uvicorn

    settings = Settings(data_dir=data, allow_database_upgrade=args.allow_database_upgrade,
                        embedding_api_key='synthetic-preview-only', deepseek_api_key='synthetic-preview-only', question_max_requests=2)
    repository = Repository(data / 'docqa.db')
    parse_worker = ParseWorker(settings, repository)
    parse_worker.runtime.prepare_database(args.allow_database_upgrade)
    repository.initialize()
    parse_worker.runtime.mark_ready()
    rag = RagService(settings, repository, DocumentIndex(repository, Embedding(settings)), Model())
    question_worker = QuestionWorker(settings, repository, rag=rag)
    question_worker.runtime.mark_ready()
    from app.knowledge_worker import KnowledgeWorker
    knowledge_worker=KnowledgeWorker(settings,repository,question_worker.runtime,embedding=rag.index.embedding,model=rag.model)
    application.APIEmbedding = Embedding
    application.DeepSeekModel = lambda config: Model()
    app = application.create_app(settings)
    app.state.demo_providers = True

    def parse_loop():
        try:
            parse_worker.run_forever()
        except RuntimeMismatch:
            print('代码已更新，解析预览停止，请完整重启开发预览。', flush=True)

    def question_loop():
        try:
            while True:
                question_worker.run_once()
                knowledge_worker.run_once()
                time.sleep(.3)
        except RuntimeMismatch:
            print('代码已更新，问答预览停止，请完整重启开发预览。', flush=True)

    Thread(target=parse_loop, daemon=True).start()
    Thread(target=question_loop, daemon=True).start()
    print(f'开发预览 {data} · http://127.0.0.1:{args.port} · 真实供应商请求0', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
