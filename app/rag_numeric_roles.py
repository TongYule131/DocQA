"""先核验文内地址，再核验业务数值；只生成校验视图，不修改事实或引文。"""
from dataclasses import dataclass
import re

from app.rag_validation import ModelOutputError

NUMERIC_ROLE_VERSION = 'grounded-number-roles-v2'
_TRANSLATE = str.maketrans('０１２３４５６７８９．，：（）－', '0123456789.,:()-')
_CN = '零〇一二三四五六七八九十百千万两'
_ID = rf'(?:[0-9]{{1,4}}(?:\.[0-9]{{1,3}}){{0,4}}|[{_CN}]{{1,8}})'
_LIST = rf'{_ID}(?:\s*(?:[、,，和及与]|[—–\-~～至到])\s*(?:第\s*)?{_ID})*'
_PAGE_LIST = rf'{_ID}(?:\s*(?:页|頁)?\s*(?:[、,，和及与]|[—–\-~～至到])\s*(?:第\s*)?{_ID})*'
_LABEL_ID = rf'{_ID}(?:-[0-9]{{1,3}})?'
_LABEL_LIST = rf'{_LABEL_ID}(?:\s*[、,，和及与]\s*{_LABEL_ID})*'
_END = r'(?![0-9A-Za-z]|\.\d|\s*(?:元|万|亿|件|人|天|小时|年|月|日|公斤|千克|公里|米|美元|欧元|℃|°|[%％]))'
_NUMBER_TOKENS = re.compile(r'(?<![\d%％])[+\-−＋－]?\d+(?:[.,]\d+)*(?:\s*[%％])?|\d+(?:[.,]\d+)*(?:\s*[%％])?')


def number_tokens(text):
    """没有定位删除副作用的数值分词；全角转换仅用于比较，原文完全不变。"""
    text=text.translate(_TRANSLATE)
    text=re.sub(r'(?<![\d.])\d{1,3}(?:[ \u00a0]\d{3})+(?:\.\d+)?(?!\d|[ \u00a0]\d)',
                lambda m:re.sub(r'[ \u00a0]','',m.group()),text)
    return {re.sub(r'\s+(?=[%％])','',n) for n in _NUMBER_TOKENS.findall(text)}


def _canonical(value):
    value=value.translate(_TRANSLATE)
    if re.fullmatch(r'[0-9]+(?:\.[0-9]+)*',value):
        return '.'.join(str(int(p)) for p in value.split('.'))
    digits=dict(zip('零〇一二三四五六七八九两',[0,0,1,2,3,4,5,6,7,8,9,2]))
    units={'十':10,'百':100,'千':1000,'万':10000}
    if not value or any(c not in digits and c not in units for c in value):return None
    if all(c in digits for c in value):return str(int(''.join(str(digits[c]) for c in value)))
    total=part=number=0
    for char in value:
        if char in digits:number=digits[char]
        elif char=='万':total+=(part+number)*10000;part=number=0
        else:part+=(number or 1)*units[char];number=0
    return str(total+part+number)


def _values(expression):
    matches=list(re.finditer(_ID,expression));values=[]
    for index,match in enumerate(matches):
        current=_canonical(match.group())
        if current is None:return ()
        if index and re.search(r'[—–\-~～至到]',expression[matches[index-1].end():match.start()]):
            previous=_canonical(matches[index-1].group())
            if '.' in previous or '.' in current:return ()
            start,end=int(previous),int(current)
            if not 1<=start<=end<=start+200:return ()
            values.extend(str(v) for v in range(start+1,end+1))
        else:values.append(current)
    return tuple(values)


@dataclass(frozen=True)
class Locator:
    start:int
    end:int
    kind:str
    values:tuple[str,...]
    nested:bool=False


# 共用一套语法供答案和原文使用；优先吃完整范围，防止只删除区间尾部数字。
_PATTERNS=[
    # 答案自身的列举次序不是文档条目地址；明确“原文第N项”不会命中此首句语法。
    ('answer_order',rf'^\s*第\s*(?P<ids>{_ID})\s*[项点](?=\s*(?:说明|[,，、:：]))'),
    ('answer_order',r'^\s*[（(]?\s*(?P<ids>[1-9][0-9]?)\s*(?:[)）、]|\.(?!\d))'),
    ('page',rf'第\s*(?P<ids>{_PAGE_LIST})\s*[页頁]'),
    ('page',rf'(?:参见|详见|见于|位于|见)\s*(?P<ids>{_PAGE_LIST})\s*[页頁]'),
    ('page',rf'页码\s*[:：]?\s*(?P<ids>{_PAGE_LIST})(?:\s*[页頁])?'+_END),
    ('page',rf'\b(?:pages?|pp?\.)\s*(?P<ids>{_LIST})'+_END),
    ('section_chained',rf'第\s*(?P<parent>{_ID})\s*[章节]\s*第\s*(?P<child>{_ID})\s*(?:小节|节)'+_END),
    ('section_nested',rf'第\s*(?P<parent>{_ID})\s*[章节](?:的)?\s*(?P<child>[0-9]+(?:\.[0-9]+)+)'+_END),
    ('section',rf'第\s*(?P<ids>{_LIST})\s*(?:章节|章|小节|节|部分)'+_END),
    ('section',rf'(?<![\d.])(?P<ids>[0-9]+(?:\.[0-9]+)+)\s*(?:小节|节)'+_END),
    ('section',rf'(?:章节|小节|\bsections?|\bsec\.|\bchapters?)\s*[:：]?\s*(?P<ids>{_LIST})'+_END),
    ('statement',rf'(?P<label>命题|推论|定理|引理|\bproposition|\bcorollary|\btheorem|\blemma)\s*[（(]?\s*(?P<ids>{_LIST})\s*[)）]?'+_END),
    ('figure',rf'(?:图|\bfigures?|\bfigs?\.)\s*[（(]?\s*(?P<ids>{_LABEL_LIST})\s*[)）]?'+_END),
    ('table',rf'(?:表格?|表编号\s*[:：]|\btables?)\s*[（(]?\s*(?P<ids>{_LABEL_LIST})\s*[)）]?'+_END),
    ('equation',rf'(?:公式|式|\bequations?|\beqs?\.)\s*[（(]?\s*(?P<ids>{_LABEL_LIST})\s*[)）]?'+_END),
    ('ordinal',rf'第\s*[（(]?\s*(?P<ids>{_LIST})\s*[)）]?\s*(?P<unit>项|条|款|点|段|行|列)'+_END),
    # 旧问题的显式章节简称，仍须实际标题支持，不将任意小数列表当章节。
    ('section',r'第\s*(?P<ids>\d+(?:\.\d+)+(?:\s*[、,，]\s*\d+(?:\.\d+)+)*)(?=分别|与|和|及)'),
    ('section',r'(?:推出|对应|配对|小节为|章节为)\s*(?P<ids>\d+(?:\.\d+)+(?:\s*[、,，]\s*\d+(?:\.\d+)+)*)(?=(?:与|和|及)(?:另|其他)[^。；，]{0,16}(?:层面|机制|章节|小节))'),
    ('section',rf'(?:原文|论文)在[“"‘](?P<ids>{_ID})\s+(?=[^”"’]{{2,80}}[”"’](?:开篇|中|部分))'),
]
_COMPILED=[(kind,re.compile(pattern,re.I)) for kind,pattern in _PATTERNS]


def locate(text):
    """返回带原始偏移的类型化地址；不使用题干或未引用文档作依据。"""
    normalized=text.translate(_TRANSLATE);found=[]
    for kind,pattern in _COMPILED:
        for match in pattern.finditer(normalized):
            if any(match.start()<p.end and match.end()>p.start for p in found):continue
            if kind in {'section_nested','section_chained'}:
                values=(_canonical(match['parent']),_canonical(match['child']))
                if kind=='section_chained' and '.' not in values[1]:values=(values[0],values[0]+'.'+values[1])
                actual='section';nested=True
            else:
                values=_values(match['ids']);actual=kind;nested=False
                if kind in {'figure','table','equation'}:
                    # 表2-1是一个稳定编号，不能按数值区间2到1处理。
                    values=tuple('-'.join(_canonical(p) for p in v.strip().split('-'))
                                 for v in re.split(r'[、,，和及与]',match['ids']))
                if kind=='ordinal':actual=match['unit']
                if kind=='statement':
                    label=match['label'].lower()
                    actual={'proposition':'命题','corollary':'推论','theorem':'定理','lemma':'引理'}.get(label,label)
            found.append(Locator(match.start(),match.end(),actual,values,nested))
    return sorted(found,key=lambda p:p.start)


def _heading_numbers(items,headings=()):
    result=set()
    for heading in [*(i.heading_path for i in items),*headings]:
        for part in re.split(r'\s*/\s*',heading or ''):
            match=re.match(rf'\s*(?:第\s*)?({_ID})(?:\s*[章节部分]+|\s+)',part.translate(_TRANSLATE))
            if match:
                number=_canonical(match[1]);result.add(number)
                if '.' in number:
                    for n in range(1,len(number.split('.'))):result.add('.'.join(number.split('.')[:n]))
    return result


def _source_addresses(sources):
    addresses={}
    for source in sources:
        normalized=source.translate(_TRANSLATE)
        for locator in locate(source):
            values=locator.values
            # 原扫描文本“推论61)”指推论6的第1项，不能反过来声称存在推论61。
            if locator.kind in {'命题','推论','定理','引理'}:
                raw=normalized[locator.start:locator.end]
                joined=re.search(r'(?:命题|推论|定理|引理)\s*(\d{2,})([)）])',raw)
                if joined and joined[1][-1] in '123456789':
                    values=(str(int(joined[1][:-1])),)
                    for kind in ['项','条','款','点']:addresses.setdefault(kind,set()).add(joined[1][-1])
            addresses.setdefault(locator.kind,set()).update(values)
        for match in re.finditer(rf'(?:^|[；;。\n])\s*[（(]?\s*([0-9]{{1,3}}|[{_CN}]{{1,6}})\s*(?:[)）、]|\.(?!\d))',normalized):
            number=_canonical(match[1])
            for kind in ['项','条','款','点']:addresses.setdefault(kind,set()).add(number)
    return addresses


def _allowed(items,sources):
    addresses=_source_addresses(sources)
    addresses['section']=_heading_numbers(items)|addresses.get('section',set())
    pages=set();lines=set();rows=set();columns=set();tables=set()
    def add_span(target,start,end):
        if start is not None and end is not None and 1<=start<=end<=start+200:target.update(str(v) for v in range(start,end+1))
    for item in items:
        if item.page is not None:pages.add(str(item.page))
        for source in item.sources:
            add_span(pages,source.page,source.page_end or source.page)
            add_span(lines,source.line_start,source.line_end or source.line_start)
            if source.table_no is not None:tables.add(str(source.table_no))
            if source.row_index is not None:add_span(rows,source.row_index,source.row_index+(source.row_span or 1)-1)
            if source.col_index is not None:add_span(columns,source.col_index,source.col_index+(source.col_span or 1)-1)
    addresses['page']=pages  # 不能用正文里提到的其他页码代替本条实际来源页。
    for kind,values in [('行',lines|rows),('列',columns),('table',tables)]:addresses.setdefault(kind,set()).update(values)
    return addresses


def _remove(text,locators):
    view=list(text)
    for locator in locators:view[locator.start:locator.end]=[' ']*(locator.end-locator.start)
    return ''.join(view)


def numeric_view(text,items,sources):
    """完整地址校验与业务数值检查分层；不支持的定位给专用错误，而非缺数值限定。"""
    allowed=_allowed(items,sources);locators=locate(text)
    for locator in locators:
        supported=bool(locator.values) and (locator.kind=='answer_order' or set(locator.values)<=allowed.get(locator.kind,set()))
        if locator.nested:supported=supported and locator.values[1].startswith(locator.values[0]+'.')
        if not supported:
            code=('page_locator_not_supported' if locator.kind=='page' else
                  'statement_locator_not_supported' if locator.kind in {'命题','推论','定理','引理','项','条','款','点'} else 'locator_not_supported')
            raise ModelOutputError(code)
    return _remove(text,locators)


def source_number_tokens(text):
    # 来源中的地址不能为答案里的金额/人数提供数字背书；其余真实数字保持原规则。
    return number_tokens(_remove(text,locate(text)))


def numeric_units(text):
    """抽取字面值/量纲，地址不参与；不隐式进行单位换算或推断百分比分母。"""
    view=_remove(text.translate(_TRANSLATE),locate(text));result={}
    units=re.compile(r'\s*(亿元|万元|美元|欧元|公斤|千克|小时|分钟|人次|摄氏度|元|人|名|件|吨|天|日|年|月|米|℃)')
    aliases={'名':'人','公斤':'千克','日':'天','摄氏度':'℃'}
    for match in _NUMBER_TOKENS.finditer(view):
        value=re.sub(r'\s+(?=%)','',match.group())
        unit='%' if value.endswith('%') else None
        if unit is None:
            tail=units.match(view,match.end())
            if tail:unit=aliases.get(tail[1],tail[1])
        if unit:result.setdefault(value,set()).add(unit)
    return result


def validate_numeric_units(text,sources):
    source_units={}
    for source in sources:
        for value,units in numeric_units(source).items():source_units.setdefault(value,set()).update(units)
    for value,units in numeric_units(text).items():
        # 只有原文和论断都明确给出同一字面值的单位，才作可判定的冲突检查。
        if value in source_units and not units <= source_units[value]:
            raise ModelOutputError('number_unit_mismatch',location={'numeric_count':1})
