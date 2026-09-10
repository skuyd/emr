"""Reported literals inside verified narrative slots; no clinical inference."""
from datetime import date
import re

from . import matching
from .narrative_layout import NarrativeInput, _DATE


MATCHING_VERSION = 'reported-cancer-narratives-1'
_LEFT = re.compile(matching.LEFT_BOUNDARY.pattern + r'|(?:因|诊断|确诊|转移性)[“"\']?$')
_RIGHT = re.compile(matching.RIGHT_BOUNDARY.pattern + r'|^(?:[”"\']|收入院|入院)')
_EXPLICIT_PATIENT = re.compile(r'^(?:患者|本患者)(?:目前|现)?(?:诊断为|诊断|确诊为|确诊|患有|患)')
_CURRENT_MARKER = re.compile(r'^(?:目前|现)(?:患者)?(?:诊断为|诊断|确诊为|确诊|患有|患)')
_EXAM = re.compile(r'^(?:(?:胸部)?CT|MRI|影像)?(?:检查)?(?:诊断[:：]?|检查所见[:：]?|结果[:：]?|提示|考虑)+')


def _predicate_context(context):
    # Calendar separators and the symptom noun 不适 are not a disjunction or a
    # denial of the reported disease. The original context stays in row.raw.
    event = _DATE.match(context)
    if event:
        context = context[event.end():]
    return re.sub(r'不适(?!合|用)', '症状', context)


def _date_relation(source, context):
    event = _DATE.match(context)
    if event is None:
        return context, None
    values = {row['value'] for row in source.record_dates}
    try:
        day = date(*(int(item) for item in event.groups()))
        record = date.fromisoformat(next(iter(values))) if len(values) == 1 and None not in values else None
    except (ValueError, TypeError):
        record = None
    relation = 'UNKNOWN' if record is None or day > record else 'HISTORICAL' if day < record else 'CURRENT_PRIMARY'
    return context[event.end():], relation


def _subject(source, context):
    if matching.OTHER_PERSON.search(context):
        return 'OTHER_PERSON'
    if re.search(r'既往|曾患|曾诊断|历史|此前', context):
        return 'HISTORICAL'
    if matching.METASTATIC.search(context):
        return 'METASTATIC_SITE'
    local, time = _date_relation(source, context)
    if time == 'UNKNOWN':
        return time
    subject = 'UNKNOWN'
    prefix = _EXPLICIT_PATIENT.match(local) or _CURRENT_MARKER.match(local)
    if prefix:
        subject = matching._subject(local[prefix.end():])
    elif source.role == 'CHIEF_COMPLAINT':
        subject = matching._subject(local)
    elif source.role == 'ADMISSION_NARRATIVE':
        body = re.sub(r'^(?:患者|本患者)?因[“"\']?', '', local)
        if body != local and re.search(r'收入院|入院', local):
            subject = matching._subject(body)
    elif source.role == 'AUXILIARY_FINDINGS' and time in {'CURRENT_PRIMARY', 'HISTORICAL'}:
        exam = _EXAM.match(local)
        if exam:
            subject = matching._subject(local[exam.end():])
    return 'HISTORICAL' if subject == 'CURRENT_PRIMARY' and time == 'HISTORICAL' else subject


def narrative_candidates(source):
    if not isinstance(source, NarrativeInput):
        raise ValueError('需要带原位置的叙述来源。')
    text, positions = matching._view(source.text)
    start = next((index for index, position in enumerate(positions) if position >= source.body_start), len(text))
    result = []
    for left, right in matching._clauses(text, start):
        body = text[left:right]
        if not matching.MALIGNANCY_WORD.search(body):
            continue
        found = []
        for match in matching.LITERAL.finditer(body):
            prefix = matching.ORDINAL.sub('', body[:match.start()])
            if (prefix and not _LEFT.search(prefix)) or not _RIGHT.match(body[match.end():]):
                continue
            found.append(match)
            context, ambiguous, shared = matching._context(body, *match.span())
            begin, end = positions[left], positions[right - 1] + 1
            match_start, match_end = positions[left + match.start()], positions[left + match.end() - 1] + 1
            subject = _subject(source, context)
            result.append({'label': match.group(), 'profile': matching.ALIASES[match.group()],
                'assertion': shared or matching._assertion(_predicate_context(context), ambiguous_scope=ambiguous,
                    disjunctive=bool(re.search(r'或|/', _predicate_context(body)))),
                'subject': subject, 'raw': source.text[begin:end], 'start': begin, 'end': end,
                'label_raw': source.text[match_start:match_end], 'match_start': match_start, 'match_end': match_end,
                'rule_version': MATCHING_VERSION, 'limitations': ['narrative_subject_unproved'] if subject == 'UNKNOWN' else []})
        if any(not any(match.start() <= word.start() < match.end() for match in found)
               for word in matching.MALIGNANCY_WORD.finditer(body)):
            begin, end = positions[left], positions[right - 1] + 1
            raw = source.text[begin:end]
            result.append({'label': raw, 'profile': None, 'assertion': matching._assertion(_predicate_context(body)),
                'subject': _subject(source, body), 'raw': raw, 'start': begin, 'end': end,
                'label_raw': raw, 'match_start': begin, 'match_end': end, 'rule_version': MATCHING_VERSION,
                'limitations': ['unsupported_reported_diagnosis']})
    return tuple(sorted(result, key=lambda row: (row['match_start'], row['match_end'])))
