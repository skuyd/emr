"""Omit external access strings only at the boundary of delivered patient data.

The input is a detached output projection, never a model, OCR or revision.
Rule URLs require an exact, server-owned rule and a fixed output location.
"""

from copy import deepcopy
import re

from apps.exports.errors import SnapshotChanged


PROJECTION_RULE = 'cloud-access-omission-v1'
OMITTED = '［已省略外部访问内容］'
ACCESS_STRING = re.compile(r'https?://[^\s<>"\'\u3002\uff0c\uff1b\uff01\uff1f\u3001]+', re.IGNORECASE)
CONTEXT_FIELDS = frozenset({'raw_text', 'source_text', 'raw_context', 'source_context', 'excerpt'})
OFFSET_FIELDS = frozenset({'start_offset', 'end_offset'})


def _allowed_rule_url(value, path, parents):
    # No currently shipped conversion rule contains a URL. New rule exceptions
    # must validate its server definition AND its exact top-level output path.
    # A caller-controlled key named url/rule_id/conversion is never an exception.
    return False


def _contains_access(value, path=(), parents=()):
    if isinstance(value, str):
        return bool(ACCESS_STRING.search(value)) and not _allowed_rule_url(value, path, parents)
    if isinstance(value, dict):
        return any(_contains_access(item, (*path, key), (*parents, value)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_access(item, (*path, index), (*parents, value)) for index, item in enumerate(value))
    return False


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

    return project(snapshot)


def assert_safe_snapshot(snapshot):
    """Old stored outputs cannot bypass the newly enforced projection policy."""
    if _contains_access(snapshot):
        raise SnapshotChanged('旧输出包含尚未明确选择的外部访问内容，请重新选择并生成。')
