"""纯交互使用独立结果分支；不冒充通过RAG证据校验的知识回答。"""
from typing import Literal
from app.schemas import RagAnswer
from app.intent_routing import ROUTING_VERSION


class InteractionAnswer(RagAnswer):
    response_kind: Literal['system'] = 'system'
    route_code: str
    routing_version: str


def interaction_answer(job, snapshot, elapsed_ms=0):
    value = snapshot['intent_resolution']
    route, decision = value['route'], value['decision']
    if route['kind'] != 'SYSTEM' or decision['version'] != ROUTING_VERSION:
        raise ValueError('invalid_system_route')
    action=route.get('execution_action')
    return InteractionAnswer(answer_id='interaction-' + job['id'],
        status='insufficient_evidence' if action=='source_unavailable' else 'clarification_needed' if action=='split_request' else 'answered',
        answer=route['response_text'], document_id=job['document_id'], index_id=job['index_id'],
        parse_version_id=job['parse_version_id'], prompt_version='system-interaction-v2',
        retrieval=dict(candidate_count=0, selected_count=0, context_chars=0, truncated=False),
        timings_ms=dict(retrieval=0, generation=0, total=elapsed_ms),
        route_code=route['code'], routing_version=ROUTING_VERSION).model_dump()


def saved_answer(value):
    model = InteractionAnswer if value.get('response_kind') == 'system' else RagAnswer
    return model.model_validate(value).model_dump()
