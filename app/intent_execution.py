"""将意图候选转成有范围和动作的执行决策；不创建任务或执行外部请求。"""
import re

EXECUTION_VERSION = 'intent-execution-v1'
ACTION_NAMES = {'reply': '简短回应', 'clarify': '确认问题对象', 'retrieve': '授权资料检索',
                'query_tool': '只读工具查询', 'source_unavailable': '缺少可用实时数据源',
                'split_request': '确认本轮处理范围'}


def request_requirements(question):
    """只识别显式外部查询要求；文档里的实时概念和被否定的动作不触发工具需求。"""
    text = re.sub(r'[“「《\"]([^”」》\"\n]*)[”」》\"]', '', question)
    clauses = [s.strip() for s in re.split(r'[，,；;。\n]|(?:并且|同时|然后|再|并)(?=查询|查看|获取|调用|核实)', text) if s.strip()]
    live, document = [], []
    for clause in clauses:
        if re.search(r'^(?:不是|并非|不要|不需要|无需|不必|别).{0,8}(?:查|调用|执行|获取|连接)', clause):
            continue
        explains = bool(re.search(r'解释|定义|含义|原理|如何实现|怎么实现|为什么|是什么意思|什么叫|文档.{0,8}(?:中|里)|资料.{0,8}(?:中|里)|论文|报告.{0,8}(?:中|里)', clause))
        selected_material=bool(re.search(r'当前(?:的)?(?:文档|资料|知识库|会话|索引|模型|问题|文件)',clause))
        explicit = bool(re.search(r'(?:调用|使用|执行).{0,20}(?:工具|接口).{0,16}(?:查询|查|获取)|(?:查询|查一下|查查|获取|查看|核实|告诉我).{0,24}(?:实时|当前|此刻|最新|今天|现在|即时).{1,40}|(?:实时|当前|今天|现在).{0,12}(?:库存|余额|物流|订单状态|销售额|天气|温度|汇率).{0,10}(?:多少|怎样|如何|怎么样|还有|有多少)', clause)) and not selected_material
        if explicit and not explains:
            live.append(clause)
        if explains or re.search(r'(?:概括|总结|比较|对比|解读).{0,20}(?:文档|资料|论文|报告|方案)|(?:文档|资料|论文|报告).{0,16}(?:讲了什么|规定|要求|结论|比较)', clause):
            document.append(clause)
    return {'requires_live_source': bool(live), 'mixed_document_and_live': bool(live and document)}


def constrain_route(question, route, decision, *, semantic_deferred=False):
    needs = request_requirements(question)
    action = None
    if needs['mixed_document_and_live']:
        action, code, response = ('split_request', 'request-scope-needed',
            '这条问题同时包含文档解读和实时查询。请先确认本轮处理哪一部分：文档内容，还是实时数据？实时部分还需要匹配到已接入并获授权的工具。')
    elif needs['requires_live_source'] and route['kind'] == 'KB' and not semantic_deferred:
        action, code, response = ('source_unavailable', 'live-source-unavailable',
            '这项查询需要实时业务数据，目前没有匹配到可执行的已接入工具。文档检索不能确认当前状态。请提供可用的数据源，或改为询问资料中的规则与说明。')
    if action:
        route = {'kind': 'SYSTEM', 'code': code, 'base_id': route.get('base_id'),
                 'top_k': None, 'response_text': response, 'execution_action': action}
        decision = {**decision, 'code': code, 'name': ACTION_NAMES[action], 'kind': 'SYSTEM',
                    'method': action, 'reason': response}
    return route, decision


def execution_contract(route, understanding, plan):
    action = route.get('execution_action') or ('clarify' if route['code'] == 'clarification-needed'
              else {'KB': 'retrieve', 'MCP': 'query_tool', 'SYSTEM': 'reply'}[route['kind']])
    return {'version': EXECUTION_VERSION, 'action': action, 'name': ACTION_NAMES[action],
            'task_type': plan['task_type'], 'resources': {
                'base_id': route.get('base_id'), 'tool_id': route.get('tool_id'),
                'scope_policy': 'authorized_selected_snapshot',
                'document_ids': [d['document_id'] for d in understanding.get('material_scope', [])
                                 if isinstance(d, dict) and 'document_id' in d] if action == 'retrieve' else []},
            'constraints': {'top_k': route.get('top_k'), 'read_only': True},
            'requires_retrieval': action == 'retrieve', 'requires_tool': action == 'query_tool',
            'requires_answer_evidence': action in ('retrieve', 'query_tool')}


def local_result(resolution, task_id):
    route=resolution['route'];action=resolution['execution']['action']
    return {'response_kind':'system','answer':route['response_text'],
            'status':'insufficient_evidence' if action=='source_unavailable' else 'clarification_needed' if action=='split_request' else 'answered',
            'citations':[],'conclusion':[],'explanation':[],'limitations':[],
            'clarification_questions':['请确认本轮处理文档内容还是实时数据。'] if action=='split_request' else [],
            'route':route['code'],'prompt_version':'system-intent-v2','execution':resolution['execution'],
            **({'clarification':{'id':'clarify-'+task_id,**resolution['understanding']['clarification']}}
               if route['code']=='clarification-needed' else {})}
