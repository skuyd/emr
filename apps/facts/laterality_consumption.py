"""Consume reviewed scope without rewriting original fields or their history."""
from copy import deepcopy

from .laterality_schema import SCOPED_KEY, SIDE_KEYS, SIDES


def effective_laterality(fields, parent_sites):
    parents = {row['id']: row for row in parent_sites if row.get('field_key') == 'lesion.site'}
    whole, named, unknown, evidence = [], [], [], []
    for field in fields:
        if field.get('field_key') not in SIDE_KEYS:
            continue
        evidence.append(field['id'])
        scope = field.get('laterality_scope') or {}
        state = scope.get('scope_state', 'UNKNOWN_SCOPE')
        parent = parents.get(scope.get('parent_id'))
        parent_current = bool(parent and parent.get('usable') and parent.get('source_valid', True)
                              and not parent.get('conflict'))
        usable = bool(field.get('usable') and field.get('source_valid', True) and not field.get('conflict')
                      and scope.get('valid') and scope.get('parent_usable') and scope.get('binding_id') and parent_current)
        common = {'field_id': field['id'], 'parent_id': scope.get('parent_id'),
                  'binding_id': scope.get('binding_id'), 'usable': usable, 'conflict': bool(field.get('conflict'))}
        if state == 'WHOLE_ENTITY' and field['field_key'] == 'lesion.laterality':
            value = field['content']['value']
            whole.append({**common, 'value': deepcopy(value), 'usable': usable and value.get('code') in SIDES})
        elif state == 'NAMED_MEMBERS_ONLY' and field['field_key'] == SCOPED_KEY:
            named.append({**common, 'members': deepcopy(field['content']['value']['members'])})
        else:
            unknown.append({'field_id': field['id'], 'scope_state': state})
    candidates = [row for row in whole if row['usable']]
    codes = {row['value']['code'] for row in candidates}
    conflicted = len(codes) > 1 or any(row['conflict'] for row in whole)
    scalar = deepcopy(candidates[0]['value']) if len(codes) == 1 and not conflicted else None
    state = ('WHOLE_ENTITY' if scalar else 'CONFLICT' if conflicted else 'NAMED_MEMBERS_ONLY' if named
             else 'UNKNOWN_SCOPE' if unknown else 'UNAVAILABLE')
    return {'whole_entity': whole, 'named_members': named, 'unknown_fields': unknown,
            'scope_state': state, 'scalar': scalar, 'evidence_ids': evidence}
