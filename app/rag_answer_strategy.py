"""按本地查询任务选择表达策略；规则是固定系统指令，不生成文档事实或新增外调。"""
import hashlib
import json
import re

ANSWER_STRATEGY_VERSION = 'task-aware-composition-v1'
RULES = {
    'fact': '事实直答：只回答所问对象与决定判断的条件，用一两句完成。简洁完整的原句可以直接作为正文，不必改写成近义句后再附同一原句的限定；选择必要context时优先把其完整原文自然写进text。无需背景、定义、其他对象的规则或重复总结；有必要例外仍保留。',
    'document_overview': '文档概述：按研究问题、核心论证、主要结论及限制组织，概括每部分的作用，不按检索片段逐条复述；局部资料不能冒充全文覆盖。',
    'multi_hop': '多跳论证：首段用联合论断回答中心问题。对照案例时，优先将双方实际结果、关键差异与综合判断写在同一条inference中，同时引用双方结果及解释依据；不要拆成案例事实与一条缺少案例引用的“据此”判断。主题背景不能代替成功结果。后续每段的主句直接解释一种机制如何支持这一判断，把必要事实作为前提纳入同一论断，避免两条独立事实之后让读者自行补联系。结构归纳标inference；解释性联系不等于实证因果，保留可以、有助于、可能的强度，不将解释路径宣布为必要或充分条件。原文不足以建立联系时说明缺项。合并论述时核对陈述主体：作者、媒体、学界、受访者和调研结果不得相互替换；与所问关系无关的他人解释直接省略，不能归到作者名下。非量化题用有据的定性结果，不为背景丰富追加销量等数字。',
    'comparison': '共同维度比较：先回答关键异同，再按相同维度把两个对象放在一起比较，解释差异对结论的意义。不把甲、乙各写一份摘要；递进题还要交代前一步如何引出后一步。',
    'conceptual': '概念关系辨析：先给关系判断，再按两组概念各自的划分依据解释层级、交叉或支持关系。定义只在解释需要时出现一次，不能开头完整列举后再列一遍；数量相同不构成对应。',
    'boundary': '边界逐项判断：直接判断题干各命题以及其推导是否成立，再用条件、反例或限制解释。承认挑战不等于采纳题干建议，能缓解不等于消失；不为简短省掉任何关键边界。',
    'formula_check': '公式冲突核对：直接把同事项公式与解释两端放在同一条判断中，分别引用并保留符号方向、条件和原文位置；再用一句说明如何并列引用和不能确定笔误端，不擅自纠错或删除冲突一端。通常一段两句已足够，不在首段判断后重新列出相同公式和解释，再作第三遍总结。',
    'causal_evidence': '因果证据判断：首句直接回答是否已经证明，并在同一论断中纳入决定判断的观察与研究设计依据。明示实际观察结果与题干待证明结果之间缺少哪个验证环节，把证据的对象、采集方式和研究设计限制接到结论，不只依次罗列三件事。不能把资料未报告的测量、样本或干预杜撰为不存在。短材料在一段两条事实内完成，后句仅补尚未交代的限制或验证需要，不重复首句。',
    'boundary_relations': '路径与边界关系：直接否定过度推导后，说明措施缓解的是什么问题、仍须治理的风险是什么，以及为何二者需要同时存在。每段补一个新环节，不再把首句判断和路径重复讲一遍；具体监管措施只保留解释该关系所需的代表项，不罗列全部清单。原文另有风险警示时保持其原有行为主体与触发条件，不把另一种行为的后果直接转移给所问措施。',
}
RULES['boundary_relations']+=' 题干给定措施有益，仍不能代替原文中的实际作用机制：要说明它怎样改变问题根源，不用问题定义或治理清单冒充缓解机制。按否定过度推导、实际机制、剩余风险关系三环节组织，最多三段、每段一条完整有据的关系论断。'
RULES['formula_check']+=' 区分本次能确认的文字方向冲突与不能据此确定的实际高低或错误端，解释为什么并列保留是引用限制；不能把其中一端当成已经核实的单向结论。'
PRESENTATION_POLICIES={
    'causal_evidence':{'max_blocks':1,'max_facts_per_block':2},
    'bounded_formula':{'max_blocks':1,'max_facts_per_block':2},
    'boundary_relations':{'max_blocks':3,'max_facts_per_block':1},
}
PRIORITY = ('formula_check', 'conceptual', 'comparison', 'boundary', 'multi_hop', 'document_overview', 'fact')
# 表达任务独立于检索预算：多一个问号/格式要求不构成多跳，“人工智能模型”也不是数学核对。
PATTERNS = {
    'formula_check': r'公式|不等式|数学模型|推论\s*\d|命题\s*\d',
    'conceptual': r'一一对应|分类(?:依据|轴)|概念(?:结构|层级|层面)|(?:两组|两类).{0,20}(?:对应|配对)|(?:数量|次序).{0,12}(?:配对|对应)|论证关系|划分依据|如何定位|之间的关系',
    'comparison': r'比较|对比|相比|异同|区别|两者|二者|两种机制',
    'boundary': r'是否(?:就|已经)?(?:意味着|说明|符合|支持|证明|失效)|(?:是否|能否).{0,16}(?:推导|认定|证明)|(?:无需|不需要).{0,10}(?:治理|审查|风险)|风险边界|不能支持|有观点认为',
    'multi_hop': r'多跳|综合|串联|连贯论证|结合|为何|为什么|因果|如何.{0,10}(?:支持|引出|破解|影响|缓解)|作用机制',
    'document_overview': r'(?:文档|论文|文章|报告|这篇|这份).{0,12}(?:讲了什么|说了什么|主要内容|讲的什么|概述|摘要)|概括全文|全文总结|整体介绍',
}


def strategy_for_plan(plan):
    facets = plan.get('facets', []) if isinstance(plan, dict) else []
    facets = [f for f in PRIORITY if f in facets]
    primary = next(iter(facets), 'fact')
    return {'version': ANSWER_STRATEGY_VERSION, 'primary': primary, 'facets': facets or ['fact']}


def strategy_for_question(question):
    text = re.sub(r'\s+', '', question)
    facets = [kind for kind, pattern in PATTERNS.items() if re.search(pattern, text)]
    return strategy_for_plan({'facets': facets})


def strategy_instructions(question):
    style = strategy_for_question(question)
    text = '\n# 本轮回答组织策略（不是文档事实）\n' + RULES[style['primary']]
    # 主组织方式仅选一种，叠加题只补义务，不把所有写法模板堆给模型。
    if 'boundary' in style['facets'] and style['primary'] != 'boundary':
        text += ' 同时回扣各子命题及推导边界，不因综合解释扩大原文结论。'
    if 'formula_check' not in style['facets'] and 'multi_hop' in style['facets'] and style['primary'] != 'multi_hop':
        text += ' 各依据需要共同支持中心判断，资料缺少联系时说明限制，不补造连接。'
    if style['primary']=='boundary' and re.search(r'是否.*证明|因果',question):
        text += ' '+RULES['causal_evidence']
    if style['primary']=='boundary' and re.search(r'推导|无需|风险边界',question):
        text += ' '+RULES['boundary_relations']
    return text


def presentation_policy(question):
    kind=strategy_for_question(question)['primary']
    if kind=='formula_check' and re.search(r'(?:只|仅)核对',question):
        return dict(PRESENTATION_POLICIES['bounded_formula'])
    if kind=='boundary' and re.search(r'是否.*证明|因果',question):
        return dict(PRESENTATION_POLICIES['causal_evidence'])
    if kind=='boundary' and re.search(r'推导|无需|风险边界',question):
        return dict(PRESENTATION_POLICIES['boundary_relations'])
    return {}


def policy_signature():
    return hashlib.sha256(json.dumps([ANSWER_STRATEGY_VERSION, PRIORITY, RULES, PATTERNS,PRESENTATION_POLICIES], ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()
