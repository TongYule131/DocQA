"""只消除可证明的排版和枚举差异，不把语义相近当作原文逐字命中。"""
import re

PHRASE_POLICY_VERSION = 'source-phrase-layout-v4'


def is_question_framing_term(phrase, fact, sources=()):
    """结构性归纳可使用题干的分析名词，但不能将用户事实或命题当作原文。"""
    if fact.claim_type!='inference' or any(c.isnumeric() for c in phrase):return False
    if not re.fullmatch(r'[\u3400-\u9fffA-Za-z· /+＋-]{2,40}',phrase):return False
    if re.search(r'我|本人|用户|客户|订单|已经|今天|昨天|本周|购买|买了|签收|收到|支付|发生|不能|可以|必须|应当|不应|禁止|不|未|无',phrase):return False
    if re.search(r'(?:原文|文档|论文|作者)[^。；]{0,8}(?:称为|称作|定义|提出|写道|命名|原话|明确指出)',fact.text):return False
    if '+' in phrase or '＋' in phrase:
        parts=re.split(r'\s*[+＋]\s*',phrase)
        def supported(part,source):
            # ASCII名词必须作为完整词出现，SQL不能借NoSQL获得来源支持。
            if re.fullmatch(r'[A-Za-z -]+',part):
                return bool(re.search(r'(?<![A-Za-z0-9_])'+re.escape(part)+r'(?![A-Za-z0-9_])',source))
            return source_phrase_match(part,source)
        return 2<=len(parts)<=4 and all(2<=len(part)<=16 and any(supported(part,s) for s in sources) for part in parts)
    return bool(re.search(r'(?:机制|结构|框架|体系|矛盾|问题|路径|模式|维度|层面|能力|素养|重构|关系|边界|概念|原则|视角|逻辑|特色|效应|障碍)$',phrase))


def source_phrase_match(phrase, source):
    if phrase in source: return True
    # 用户经历、期限、百分比等不能靠排版归一化或词面拼合获得来源。
    if not re.search(r'[\u3400-\u9fff]', phrase) or any(c.isnumeric() for c in phrase):
        return False
    def layout(text):
        # 只移除成对行内排版标记；不改内容、不拼接来源，也不用于quote逐字校验。
        text=re.sub(r'(`+)([^`\r\n]+)\1',lambda m:m[2],text)
        text=re.sub(r'(\*\*|__)(?=\S)([^\r\n]+?\S|\S)\1',lambda m:m[2],text)
        text = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=\S)|(?<=\S)\s+(?=[\u3400-\u9fff])', '', text)
        return text.translate(str.maketrans('—–－', '---'))
    needle, haystack = layout(phrase), layout(source)
    if needle in haystack: return True
    parts = re.split(r'[、，,]|(?<=[\u3400-\u9fff])-(?=[\u3400-\u9fff])', needle)
    if not 2 <= len(parts) <= 6 or any(not 2 <= len(p) <= 24 for p in parts): return False
    # 只在同一条实际引用内，按原顺序匹配‘A维度、B维度和C维度’这样的显式枚举。
    separator = r'(?:维度|层面|能力|阶段)?\s*(?:[、，,]|以及|和|与|及)\s*'
    return bool(re.search(separator.join(re.escape(p) for p in parts), haystack))
