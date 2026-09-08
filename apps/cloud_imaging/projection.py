"""Omit external access strings only at the boundary of delivered patient data.

The input is a detached output projection, never a model, OCR or revision.
Rule URLs require an exact, server-owned rule and a fixed output location.
"""

from copy import deepcopy
import re

from apps.exports.errors import SnapshotChanged


PROJECTION_RULE = 'cloud-access-omission-v2'
OMITTED = '［已省略外部访问内容］'
ACCESS_STRING = re.compile(r'https?://[^\s<>"\'\u3002\uff0c\uff1b\uff01\uff1f\u3001]+', re.IGNORECASE)
CONTEXT_FIELDS = frozenset({'raw_text', 'source_text', 'raw_context', 'source_context', 'excerpt'})
OFFSET_FIELDS = frozenset({'start_offset', 'end_offset'})


def _allowed_rule_url(value, path, parents):
    # The actual main glucose contract defines this citation. Patient text and
    # arbitrary keys cannot establish a rule exception at another location.
    if (len(path) != 5 or path[0] != 'glucose_records' or type(path[1]) is not int
            or path[2] not in {'data', 'original_data'} or path[3:] != ('conversion', 'source_url')):
        return False
    from apps.glucose.payloads import CONVERSION_SOURCE

    rule = {'rule_id': 'glucose-mg-dl-mmol-l-cdc-v1', 'factor': '0.05551',
            'formula': 'value * 0.05551', 'source_url': CONVERSION_SOURCE}
    return (value == CONVERSION_SOURCE and parents[-1] == rule
            and parents[-2].get('normalized_unit') == 'mmol/L'
            and parents[-2].get('result_type') == 'NUMERIC')


def _contains_access(value, path=(), parents=()):
    if isinstance(value, str):
        return bool(ACCESS_STRING.search(value)) and not _allowed_rule_url(value, path, parents)
    if isinstance(value, dict):
        return any(_contains_access(item, (*path, key), (*parents, value)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_access(item, (*path, index), (*parents, value)) for index, item in enumerate(value))
    return False


def _clinical_display_after_omission(original, projected):
    """Format an existing selected typed field, never create source authority.

    A URL at the end of a value can consume a suffix in its preformatted text.
    Rebuild only that known derived text after the value has been projected.
    Unrecognized shapes/locations and already-omitted values are left alone.
    """
    if not isinstance(original, dict) or not isinstance(original.get('clinical_fields'), list):
        return projected
    from uuid import UUID
    from django.core.exceptions import ValidationError
    from apps.facts.clinical_schema import FIELDS, display_value, validate_value

    for index, field in enumerate(original['clinical_fields']):
        try:
            key, content = field['field_key'], field['content']
            spec = FIELDS.get(key)
            if (spec is None or field['status'] != 'CONFIRMED' or field['category'] != 'IMAGING'
                    or field['schema_version'] != spec.version or field['field_label'] != spec.label
                    or content['field_key'] != key or content['category'] != 'IMAGING'
                    or content['schema_version'] != spec.version or content['value_type'] != spec.value_type
                    or content['result_type'] != 'SOURCE_REPORTED' or not _contains_access(content['value'])):
                continue
            UUID(field['id'])
            UUID(field['report_id'])
            validate_value(key, content['value'])
            if content['text'] != f'{spec.label}：{display_value(key, content["value"])}':
                continue
            target = projected['clinical_fields'][index]['content']
            target['text'] = f'{spec.label}：{display_value(key, target["value"])}'
        except (ValidationError, KeyError, ValueError, TypeError, AttributeError):
            # General omission still applies. A malformed field is not repaired
            # into a trusted field or granted any new report/source relation.
            continue
    return projected


def project_default_snapshot(snapshot):
    """Return a new value; retain private input values and full dependency hashes."""
    def project(value, path=(), parents=()):
        if isinstance(value, str):
            if _allowed_rule_url(value, path, parents):
                return value
            return ACCESS_STRING.sub(OMITTED, value)
        if isinstance(value, list):
            return [project(item, (*path, index), (*parents, value)) for index, item in enumerate(value)]
        if isinstance(value, tuple):
            return tuple(project(item, (*path, index), (*parents, value)) for index, item in enumerate(value))
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {key: project(item, (*path, key), (*parents, value)) for key, item in value.items()}
        changed = any(result[key] != value[key] for key in value)
        omitted_context = [key for key in CONTEXT_FIELDS if key in value and isinstance(value[key], str)
                           and _contains_access(value[key], (*path, key), (*parents, value))]
        for key in omitted_context:
            result[key] = OMITTED
        if omitted_context:
            for key in OFFSET_FIELDS & result.keys():
                result[key] = None
        if changed:
            result['external_access_omitted'] = True
        return result

    return _clinical_display_after_omission(snapshot, project(snapshot))


def assert_safe_snapshot(snapshot):
    """Old stored outputs cannot bypass the newly enforced projection policy."""
    if _contains_access(snapshot):
        raise SnapshotChanged('旧输出包含尚未明确选择的外部访问内容，请重新选择并生成。')
