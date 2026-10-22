"""SSE只读重放；每次批次重新检查账号、会话与资料权限，断线不创建任务。"""
import asyncio
import json
import re
from fastapi import HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from app.answer_stream_store import AnswerStreamStore
from app.product_access import actor_context


def install_answer_stream_routes(app,access,questions):
    store=AnswerStreamStore(access.repository)

    def authorize(source,identifier,request):
        actor=access.session(request.cookies.get(access.cookie_name)) if access.team_mode else None
        if access.team_mode and not actor:raise HTTPException(401,'登录已失效')
        token=actor_context.set(actor)
        try:
            if source=='question':
                access.require_question(identifier)
                row=questions.store.row(identifier)
                if not row:raise HTTPException(404,'问答任务不存在')
            else:
                row=app.state.knowledge_questions.require_task(identifier)
                if row['kind']!='question':raise HTTPException(404,'问答任务不存在')
            return row
        finally:actor_context.reset(token)

    def response(source,identifier,request,after):
        header=request.headers.get('last-event-id')
        if header is not None:
            if not re.fullmatch(r'\d{1,18}',header):raise HTTPException(422,'事件游标无效')
            after=max(after,int(header))
        authorize(source,identifier,request)

        async def events():
            cursor=after;previous=None;idle=0
            while not await request.is_disconnected():
                try:row=authorize(source,identifier,request)
                except HTTPException as error:
                    yield 'event: denied\ndata: '+json.dumps({'status':error.status_code})+'\n\n'
                    return
                batch=store.read(source,identifier,cursor)
                for item in batch:
                    # 回放一批事件时也逐条重验；不能在首条后撤权，仍发送整批缓存正文。
                    try:row=authorize(source,identifier,request)
                    except HTTPException as error:
                        yield 'event: denied\ndata: '+json.dumps({'status':error.status_code})+'\n\n'
                        return
                    cursor=item['id']
                    yield f'id: {cursor}\nevent: update\ndata: '+json.dumps(item,ensure_ascii=False,separators=(',',':'))+'\n\n'
                state={'status':row['status'],'stage':row['stage'],'cancel_requested':bool(row['cancel_requested']),
                       'result_id':row.get('result_id'),'attempt':row['attempt_count']}
                if state!=previous:
                    yield 'event: state\ndata: '+json.dumps(state,separators=(',',':'))+'\n\n';previous=state
                if row['status'] not in ('queued','running') and len(batch)<100:return
                idle+=1
                if idle%40==0:yield ': keepalive\n\n'
                await asyncio.sleep(.25)
        return StreamingResponse(events(),media_type='text/event-stream',
            headers={'Cache-Control':'no-store','X-Accel-Buffering':'no','X-Content-Type-Options':'nosniff'})

    @app.get('/api/question-jobs/{job_id}/events')
    async def question_events(job_id:str,request:Request,after:int=Query(default=0,ge=0,le=10**18)):
        return response('question',job_id,request,after)

    @app.get('/api/kb-tasks/{job_id}/events')
    async def knowledge_events(job_id:str,request:Request,after:int=Query(default=0,ge=0,le=10**18)):
        return response('knowledge',job_id,request,after)
