"""统一的本地查询计划：任务策略与业务路由分离，不产生文档事实或额外外调。"""
from __future__ import annotations

import re

QUERY_PLAN_VERSION = 'query-plan-v3'

# 这些是执行策略，不是为某篇论文预设的答案或后台演示节点。
TASKS = {
    'fact': ('事实查询', 5, 900, '相关原句及其条件、单位和例外'),
    'document_overview': ('文档概述', 32, 900, '摘要、正文各节与结论；实际装入全部索引块才声明完整索引阅读'),
    'multi_hop': ('多跳综合', 12, 700, '分别保留子问、点名对象和相关章节证据'),
    'comparison': ('对比分析', 12, 700, '分别保留比较对象及同一比较口径'),
    'boundary': ('观点与边界判断', 10, 700, '优先原文立场、条件及反例，不把超越局限等同理论失效'),
    'conceptual': ('概念结构辨析', 8, 650, '定义与机制正文优先，不按数量或顺序强行对应'),
    'formula_check': ('公式与正文核对', 12, 800, '同时保留公式、符号条件及解释正文，冲突分别引用'),
}

REQUIREMENTS = {
    'overview_abstract': ('概述与研究问题', r'摘要|abstract|引言|绪论|introduction', 'heading'),
    'overview_body': ('正文论证', r'.', 'body'),
    'author_position': ('作者结论与立场', r'结论|结语|总结|conclusions?', 'heading'),
    'direct_definition': ('直接定义', r'是指|意指|指的是|定义为|定义是|defined as|means\b', 'text'),
    'mechanism_body': ('机制及关系解释', r'机制|基础设施|物质基础|层面|层次|层级|结构|职能|关系|作用|分类|分为|原则|流程|要素|由于|从而|因此|导致|根源|路径|影响', 'text'),
    'boundary_conditions': ('条件、例外与否定边界', r'但是|然而|并非|不意味|不代表|不能|局限|仅|例外|条件|前提|除外', 'text'),
    'formula_source': ('原始公式及不等式', r'[=<>≤≥≠∂∑]|\\(?:frac|leq|geq|partial|sum)\b', 'text'),
    'formula_explanation': ('命题、推论的解释正文', r'表明|说明|意味着|解释|高于|低于|增加|减少|上升|下降', 'text'),
}


def plan_query(question: str, route: dict | None = None) -> dict:
    """保守识别可叠加的任务需求；问题内容只作检索线索，未知表达回退事实查询。"""
    route = route or {'kind': 'KB'}
    kind = route['kind']
    if kind != 'KB':
        return dict(version=QUERY_PLAN_VERSION, route_kind=kind,
            task_type='interaction' if kind == 'SYSTEM' else 'tool_query',
            facets=[], requirements=[], subquestions=[], scope_policy='authorized_selected_snapshot',
            planner='local_rules', supplier_calls=0)
    text = re.sub(r'\s+', '', question)
    parts = list(dict.fromkeys(p.strip() for p in re.split(r'[？?；;\n]+', question) if p.strip()))[:8]
    facets = []
    if re.search(r'公式|不等式|推导|符号|数学模型', text) and re.search(r'核对|矛盾|一致|冲突|比较|解释|说明', text):
        facets.append('formula_check')
    if re.search(r'一一对应|辨析|两组|概念结构|如何定位|分类轴|概念层级|配对|什么关系|何种关系|有何联系|定义|界定|什么是', text):
        facets.append('conceptual')
    if re.search(r'对比|比较|相比|异同|区别|两者|二者|两种机制|有何不同|怎样对照|各自', text):
        facets.append('comparison')
    if re.search(r'是否符合|是否意味着|是否说明|是否支持|是否失效|是否(?:就|已经|足以|都|只能|能)|能否|可否|是不是|算不算|不需要|不再需要|有观点认为|边界|否定|该说法', text):
        facets.append('boundary')
    if len(parts) > 1 or len(set(re.findall(r'《([^》]+)》', text))) > 1 or re.search(r'综合|多跳|分别|连贯论证|为什么|为何|怎样导致|如何导致|什么原因', text):
        facets.append('multi_hop')
    if not facets and re.search(r'讲了什么|讲的什么|说了什么|主要讲|主要内容|概述|总结|概括|摘要|全文|整体介绍', text):
        facets.append('document_overview')
    if not facets:
        facets.append('fact')
    requirements = []
    def need(*names):
        for name in names:
            if name not in requirements: requirements.append(name)
    if 'document_overview' in facets: need('overview_abstract', 'overview_body', 'author_position')
    if 'conceptual' in facets: need('direct_definition', 'mechanism_body')
    if re.search(r'为什么|为何|怎样导致|如何导致|什么原因|(?:任务|认知).*(?:变化|递进)',text): need('mechanism_body')
    if 'boundary' in facets: need('author_position', 'boundary_conditions')
    if 'formula_check' in facets: need('formula_source', 'formula_explanation')
    return dict(version=QUERY_PLAN_VERSION, route_kind=kind, task_type=facets[0], facets=facets,
        requirements=requirements, subquestions=parts or [question],
        scope_policy='authorized_selected_snapshot', planner='local_rules', supplier_calls=0)


def execution_budget(plan: dict, settings, route: dict | None = None) -> dict:
    """单一预算来源；管理员节点只能收紧最终证据上限，不能扩大用户授权范围。"""
    route = route or {}
    if plan['route_kind'] != 'KB': return {}
    target = max(TASKS[f][1] for f in plan['facets'])
    return dict(recall_limit=min(20, settings.rag_retrieval_k),
        candidate_limit=settings.question_candidate_k,
        context_limit=min(settings.question_context_k, route.get('top_k') or settings.question_context_k, target),
        context_chars=settings.question_context_max_chars)


def public_plan(plan: dict) -> dict:
    """链路仅公开固定策略标签，不泄露子问、检索词、实体或客户正文。"""
    return {key: plan[key] for key in ('version', 'route_kind', 'task_type', 'facets',
        'requirements', 'scope_policy', 'planner', 'supplier_calls') if key in plan}


def describe_plan(plan: dict) -> dict:
    value = public_plan(plan)
    value['name'] = TASKS[plan['task_type']][0] if plan['task_type'] in TASKS else {'interaction':'系统交互','tool_query':'只读工具查询','general':'通用LLM回答'}[plan['task_type']]
    value['effects'] = [TASKS[f][3] for f in plan['facets']]
    value['required_evidence'] = [REQUIREMENTS[r][0] for r in plan['requirements']]
    return value


def task_instructions(plan: dict) -> str:
    if plan['route_kind'] != 'KB': return ''
    return '\n# 本轮证据策略\n' + '；'.join(TASKS[f][3] for f in plan['facets']) + '。策略不是文档事实或已覆盖证明，缺少原文时明确报告缺项；不按题干补造依据。'
