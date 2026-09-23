"""Read-only display identity, report flags and audited method policies."""

from dataclasses import dataclass

from .extraction import _unit_key
from .validation import REFERENCE_BLOCKING_ISSUES


CATEGORY_ALIASES = {'HEMATOLOGY': 'CBC'}
SPECIMEN_LABELS = {'BLOOD': '血液', 'URINE': '尿液', 'STOOL': '粪便', 'OTHER': '其他标本'}
METHOD_RULE_FIELDS = frozenset({'id', 'version', 'kind', 'code', 'specimen', 'unit', 'allow_missing_method',
                                'methods', 'institutions', 'evidence', 'reviewed_by', 'rationale'})
CELL_RESULT_FIELDS = frozenset({'raw_value', 'raw_unit', 'result_type', 'reference_range_raw', 'report_flag_raw'})
DETAIL_ONLY_ISSUES = frozenset({'date_uncertain', 'date_conflict', 'mapping_unknown', 'specimen_unknown',
                               'specimen_conflict', 'source_policy_unknown', 'reference_unknown'})


def cell_review_required(issues):
    """Keep cell warnings about the displayed result; retain other issues in details.

    An unscoped issue can affect the result, so it still needs attention. This
    presentation filter never grants reference or trend calculation eligibility.
    """
    return any(item['code'] not in DETAIL_ONLY_ISSUES
               and (not item.get('fields') or CELL_RESULT_FIELDS.intersection(item['fields']))
               for item in issues)


def display_category(category):
    return CATEGORY_ALIASES.get(category, category) or '未归类'


def display_identity(observation, definition, issues=None, *, dictionary):
    """A name heading is not an assertion of clinical identity or comparability."""
    from tools.sample_dictionary.normalize import normalize_candidate_name
    from .extraction import _candidate_identity

    name = normalize_candidate_name(observation.raw_name, strip_result=False) or observation.raw_name
    if definition and getattr(observation, 'value_sources', {}).get('standard_code', {}).get('revision_id'):
        return definition.standard_name
    if dictionary:
        named = _candidate_identity(name, dictionary)[0]
        if named:
            return named.standard_name
        if definition:
            named = _candidate_identity(name, dictionary, specimen=definition.specimen, panel=definition.category)[0]
            if named and named.code == definition.code:
                return definition.standard_name
    return name


def missing_method_rule(observation, rules):
    """Only one complete reviewed policy may authorize a shared method basis.

    No production indicator is implicitly opted in. Explicit method names outside
    the policy remain separate even when the institutions or units happen to match.
    """
    matches = []
    policies = [rule for rule in rules if rule.get('kind') == 'method_comparability']
    if not policies:
        return None
    institution = getattr(observation, 'comparison_institution', None)
    if institution is None:
        from .institutions import comparison_institutions
        institution = comparison_institutions((observation,))[str(observation.pk)]
    for rule in policies:
        if set(rule) - METHOD_RULE_FIELDS or rule.get('allow_missing_method') is not True:
            continue
        if not all(isinstance(rule.get(field), str) and rule[field].strip() for field in
                   ('id', 'version', 'reviewed_by', 'rationale', 'evidence', 'code', 'specimen', 'unit')):
            continue
        institutions, methods = rule.get('institutions'), rule.get('methods')
        if (not isinstance(institutions, list) or not institutions or not all(isinstance(item, str) and item.strip() and item != '*' for item in institutions)
                or not isinstance(methods, list) or not all(isinstance(item, str) and item.strip() for item in methods)):
            continue
        if (rule['code'] == observation.standard_code and rule['specimen'] == observation.specimen
                and _unit_key(rule['unit']) == _unit_key(observation.raw_unit)
                and institution not in {'医院未识别', '多机构，待核对'} and institution in institutions
                and (not observation.method_raw.strip() or observation.method_raw in methods)):
            matches.append(rule)
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class AbnormalResult:
    status: str
    label: str = ''
    symbol: str = ''
    source: str = ''


def abnormal_result(observation, issues, reference):
    codes = {item['code'] for item in issues}
    blocking = REFERENCE_BLOCKING_ISSUES - {'reference_unknown'}
    if cell_review_required(issues):
        return AbnormalResult('review', '待核对', source='结果或参考依据需要核对')
    if codes & blocking:
        return AbnormalResult('unavailable', source='暂无可用参考依据，具体原因见核对说明')
    flags = {'H': 'above', 'HIGH': 'above', '↑': 'above', '偏高': 'above', '高': 'above',
             'L': 'below', 'LOW': 'below', '↓': 'below', '偏低': 'below', '低': 'below',
             'A': 'different', '*': 'different', '异常': 'different', 'ABNORMAL': 'different',
             'N': 'within', '正常': 'within', 'NORMAL': 'within'}
    raw_flag = observation.report_flag_raw.strip()
    flag = flags.get(raw_flag.upper())
    if observation.result_type not in {'NUMERIC', 'COMPARATOR'} and flag in {'above', 'below'}:
        flag = 'different'
    status = reference['status']
    if raw_flag and flag is None:
        return AbnormalResult('review', '待核对', source='报告原标记含义待核对')
    if flag and status != 'unavailable' and flag != status and not (flag == 'different' and status in {'above', 'below', 'different'}):
        return AbnormalResult('review', '待核对', source='报告原标记与本报告参考范围对照冲突')
    result = (status if status != 'unavailable' else flag) or 'unavailable'
    source = '报告原标记' if flag and result == flag else '按本报告参考范围对照' if status != 'unavailable' else '暂无可用参考依据'
    return AbnormalResult(result, {'above': '偏高', 'below': '偏低', 'different': '异常'}.get(result, ''),
                          {'above': '↑', 'below': '↓'}.get(result, ''), source)
