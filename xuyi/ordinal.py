"""序号冲突：编号事实清单里，同一主语-关系出现新值时旧值让位（最新者胜）。

这是续忆更正链在英文"编号事实"语料上的最小等价物（t_valid 方向的最小实现）：
- 只作用于形如 ``N. 句子.`` 的行；中文聊天与无编号文本一律不触发。
- 冲突键由通用英文句法抽取，不含任何基准专用模板：
  定指句 "The R of S is O" 取到第一个 ``is``；其余句取到谓词后的第一个介词
  （主语里的 of/the 等小写虚词不算谓词）；都没有时取谓词后的第一个 is/was。
- 先后顺序 = (记录时间, 序号)；同序不同值即歧义，整组弃权。
- 零模型调用、纯派生：原话不改写，结果可随时由档案重算。
"""
import re

FACT_LINE = re.compile(r'^(\d{1,7})\. (.+)$', re.M)
PREPOSITIONS = {'of', 'by', 'to', 'for', 'in', 'with', 'at', 'as', 'on', 'from', 'into'}
# Lowercase tokens that can sit inside a proper-noun subject ("Albert of Saxony").
SUBJECT_PARTICLES = {'of', 'the', 'de', 'and', 'von', 'van', 'da', 'del', 'la', 'le', 'du', 'di',
                     'y', 'a', 'an', '&'}
COPULAS = {'is', 'was'}


def fact_key(sentence):
    """(冲突键, 值) or None. The key is lowercased; the value keeps its original form."""
    words = sentence.strip().rstrip('.').split()
    if len(words) < 3:
        return None
    split = None
    if words[0] == 'The':
        split = next((i for i in range(1, len(words) - 1) if words[i] == 'is'), None)
    if split is None:
        predicate = next((i for i in range(1, len(words))
                          if words[i][:1].islower() and words[i] not in SUBJECT_PARTICLES), None)
        if predicate is None:
            return None
        split = next((j for j in range(predicate, len(words) - 1) if words[j] in PREPOSITIONS), None)
        if split is None:
            split = next((j for j in range(predicate, len(words) - 1) if words[j] in COPULAS), None)
    if split is None:
        return None
    return ' '.join(words[:split + 1]).lower(), ' '.join(words[split + 1:])


def _same_value(first, second):
    norm = lambda text: ' '.join(re.sub(r'[^\w\s]', ' ', text.lower()).split())
    return norm(first) == norm(second)


def facts(events):
    """Every parseable numbered fact line, with its exact span in the event text."""
    found = []
    for event in events:
        for match in FACT_LINE.finditer(event['text']):
            parsed = fact_key(match.group(2))
            if parsed is None:
                continue
            found.append({'event_id': event['event_id'], 'recorded_at': event['recorded_at'],
                          'ordinal': int(match.group(1)), 'start': match.start(), 'end': match.end(),
                          'text': match.group(0), 'key': parsed[0], 'value': parsed[1]})
    return found


def supersessions(events):
    """{(event_id, start): newest fact} for every fact line a later, different value replaced."""
    groups = {}
    for fact in facts(events):
        groups.setdefault(fact['key'], []).append(fact)
    replaced = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda fact: (fact['recorded_at'], fact['ordinal']))
        orders = [(fact['recorded_at'], fact['ordinal']) for fact in group]
        if any(orders[i] == orders[i + 1] and not _same_value(group[i]['value'], group[i + 1]['value'])
               for i in range(len(group) - 1)):
            continue  # 同序不同值：无法裁决，弃权
        newest = group[-1]
        for fact in group[:-1]:
            if not _same_value(fact['value'], newest['value']):
                replaced[(fact['event_id'], fact['start'])] = newest
    return replaced
