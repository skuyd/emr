"""Reject retired derived content in old selections and frozen output."""

from .errors import ExportInputError, SnapshotChanged


SELECTION_KEYS = ('treatment_event_ids', 'regimen_ids', 'cycle_ids', 'personal_change_ids', 'glucose_record_ids')
ARRAY_KEYS = ('treatment_events', 'treatment_regimens', 'treatment_cycles', 'cycle_links',
              'cycle_points', 'cycle_key_nodes', 'personal_changes', 'derived_sources', 'glucose_records', 'glucose_record_sources')
OPTION_KEYS = ('cycle_mode', 'cycle_metric_codes', 'include_pending_cycles')
MESSAGE = '治疗周期、个人变化和独立血糖记录功能已移除，请重新选择资料。'


def check_selection(selection):
    if any(selection.get(key) for key in SELECTION_KEYS):
        raise ExportInputError(MESSAGE)


def check_snapshot(snapshot):
    scopes = (snapshot.get('selection', {}), snapshot.get('scope', {}))
    if (any(scope.get(key) for scope in scopes for key in SELECTION_KEYS)
            or any(snapshot.get(key) for key in ARRAY_KEYS)
            or snapshot.get('treatment_fingerprint') or snapshot.get('glucose_document_ids')
            or any(snapshot.get('treatment_binding_ids', {}).values())):
        raise SnapshotChanged(MESSAGE)
