"""可解释的确定性分流。模式均由程序限定，不执行管理员提供的正则或代码。"""
import hashlib
import json
import re
import unicodedata

ROUTING_VERSION = 'intent-rules-v2'
INTENT_SCHEMA = """
ALTER TABLE intent_nodes ADD COLUMN match_mode TEXT NOT NULL DEFAULT 'exact'
 CHECK(match_mode IN ('exact','interaction','keywords'));
ALTER TABLE intent_nodes ADD COLUMN interaction_kind TEXT
 CHECK(interaction_kind IN ('welcome','about','thanks','correction'));
ALTER TABLE intent_nodes ADD COLUMN keywords_json TEXT NOT NULL DEFAULT '[]';
"""

PATTERNS = {
    'welcome': r'(?:你好|您好|嗨|哈[喽啰]|hello|hi|hey|早上好|上午好|中午好|下午好|晚上好|早安|晚安|在吗|有人吗|你在不在|你在吗)(?:呀|啊|哈|哦|呢|鸭|啦|喔|docqa|there)?',
    'about': r'(?:你是谁|你是什么(?:助手|软件|机器人|系统|ai)|你叫(?:什么|啥)(?:名字)?|怎么称呼你|你(?:能|可以)(?:做(?:什么|啥)|帮(?:我)?(?:做)?(?:什么|啥))|介绍(?:一下)?你自己|你是(?:chatgpt|gpt|deepseek|docqa)(?:吗)?|你用的是什么模型|你是什么模型|你有哪些功能|你有什么功能)(?:呀|啊|呢|吗|嘛|么)?',
    'thanks': r'(?:谢谢(?:你|您|啦|了|你啦|你了)?|感谢(?:你|您|帮助|你的帮助)?|多谢(?:你|啦)?|辛苦(?:你|您)?了|干得不错|做得很好|很有帮助|帮大忙了|太棒了|真棒|回答得(?:很)?好|回答不错|不错|好样的|厉害|赞|thanks|thankyou)(?:啊|呀|呢|啦|了|哦|哈)?',
    'correction': r'(?:你(?:刚才)?(?:说|答)的(?:不对|不准确|有问题)|(?:这个)?回答(?:得)?(?:不对|不准确|有误|错了|不太准确|有问题)|不太准确|不对|不正确|答非所问|没回答我的问题|没有回答我的问题|我不满意|你理解错了|不是这个意思)(?:啊|呀|呢|了)?',
}


def normalized(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    return ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))


def public_decision(decision):
    """可进入管理链路的元数据，不含客户问题、匹配原句或系统回复正文。"""
    return {k: decision[k] for k in ('version', 'configuration', 'scope', 'code', 'name', 'kind',
            'method', 'reason', 'candidates') if k in decision}


def decide(question, base_id, rows, scope='knowledge'):
    if scope not in ('knowledge', 'document'):
        raise ValueError('invalid_intent_scope')
    by_id = {r['id']: r for r in rows}
    parents = {r['parent_id'] for r in rows if r['parent_id']}
    query = normalized(question)
    signature = hashlib.sha256(json.dumps([ROUTING_VERSION, rows], ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    eligible = []
    for row in rows:
        current, seen, allowed = row, set(), True
        while current:
            if current['id'] in seen or not current['enabled'] or current['base_id'] not in (None, base_id):
                allowed = False; break
            seen.add(current['id'])
            parent = current['parent_id']
            if parent and parent not in by_id:
                allowed = False; break
            current = by_id.get(parent)
        if (allowed and row['id'] not in parents and (scope == 'knowledge' or row['kind'] == 'SYSTEM')
                and (row['kind'] != 'SYSTEM' or row['response_text'].strip())):
            eligible.append(row)
    family = next((key for key, pattern in PATTERNS.items() if re.fullmatch(pattern, query)), None)
    groups = [[], [], []]
    for row in eligible:
        method, score = None, 0
        if query and any(query == normalized(example) for example in row['examples']):
            method, score = 'exact', 1.0
        elif (row.get('match_mode') == 'interaction' and row['kind'] == 'SYSTEM'
              and family and row.get('interaction_kind') == family):
            method, score = 'interaction', 1.0
        elif row.get('match_mode') == 'keywords' and row['kind'] == 'KB':
            words = [normalized(w) for w in row.get('keywords', [])]
            hits = sum(bool(w) and w in query for w in words)
            if hits:
                method, score = 'keywords', round(hits / len(words), 3)
        if method:
            groups[{'exact': 0, 'interaction': 1, 'keywords': 2}[method]].append((row, method, score))
    matches = next((g for g in groups if g), [])
    candidates = [{'code': r['code'], 'name': r['name'], 'kind': r['kind'], 'method': m,
                   'score': s} for r, m, s in matches]
    if len(matches) == 1:
        route, method, _ = matches[0]
        reason = {'exact': '命中完整示例', 'interaction': '命中限定的纯交互表达',
                  'keywords': '命中当前知识库的业务关键词'}[method]
    else:
        route = {'kind': 'KB', 'code': 'knowledge-default', 'base_id': base_id, 'top_k': None}
        method, reason = 'fallback', '多个节点同时命中，保留知识检索' if matches else '未命中受控规则，保留知识检索'
    decision = dict(version=ROUTING_VERSION, configuration=signature, scope=scope, code=route['code'],
                    name=route.get('name', '知识检索'), kind=route['kind'], method=method,
                    reason=reason, candidates=candidates, score_note='规则命中强度，不是语义概率')
    return route, decision


def migrate_intent_rules(db):
    # 仅为仍保持初始示例的内置节点启用同义交互；保留管理员已改写的节点行为。
    defaults = {
        'sys-welcome': ('welcome', ['你好','您好','hello','hi','在吗','早上好']),
        'sys-about': ('about', ['你是谁','你能做什么','你是ChatGPT吗']),
        'sys-thanks': ('thanks', ['谢谢','谢谢你','干得不错','很有帮助','太棒了']),
        'sys-correction': ('correction', ['你说的不对','不太准确','答非所问']),
    }
    for code, (family, examples) in defaults.items():
        row = db.execute('SELECT examples_json FROM intent_nodes WHERE id=? AND code=?', ('intent-' + code, code)).fetchone()
        if row and json.loads(row[0]) == examples:
            db.execute("UPDATE intent_nodes SET match_mode='interaction',interaction_kind=? WHERE id=?", (family, 'intent-' + code))
