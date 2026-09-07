from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied

from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.self_records.exporting import assert_records_current, record_fingerprint, selected_material
from apps.self_records.services import create_record, revise_record
from tests.patients.test_family_access import family
from tests.self_records.test_payloads import payload


pytestmark = pytest.mark.django_db


def test_record_projection_is_explicit_and_keeps_current_raw_value_author_time_and_revision(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-selected')
    record = create_record(patient, actor, payload(kind='TEMPERATURE', value='98.6', unit='°F'), creation_key=uuid4()).record
    create_record(patient, actor, payload(notes='unselected secret'), creation_key=uuid4())
    assert selected_material(patient, {}) == []
    rows = selected_material(patient, {'self_record_ids': [str(record.pk)]})
    assert len(rows) == 1 and rows[0]['id'] == str(record.pk)
    assert rows[0]['data']['raw_value'] == '98.6' and rows[0]['data']['normalized_value'] == '37'
    assert rows[0]['created_by'] == str(actor.pk) and rows[0]['revision_number'] == 0
    assert rows[0]['data']['timezone'] == 'Asia/Shanghai' and rows[0]['data']['time_precision'] == 'MINUTE'
    assert str(patient.pk) in rows[0]['source']['url'] and str(record.pk) in rows[0]['source']['url']
    assert 'unselected secret' not in str(rows)


def test_record_fingerprint_rejects_changed_or_deleted_selected_source_but_not_unselected_changes(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-fingerprint')
    selected = create_record(patient, actor, payload(), creation_key=uuid4()).record
    other = create_record(patient, actor, payload(), creation_key=uuid4()).record
    scope = {'self_record_ids': [str(selected.pk)]}
    snapshot = {'selection': scope, 'self_record_fingerprint': record_fingerprint(selected_material(patient, scope))}
    revise_record(patient, actor, other.pk, action='CORRECT', expected_revision=0, changes=payload(value='62'))
    assert_records_current(patient, snapshot)
    revise_record(patient, actor, selected.pk, action='CORRECT', expected_revision=0, changes=payload(value='63'))
    with pytest.raises(SnapshotChanged):
        assert_records_current(patient, snapshot)
    snapshot['self_record_fingerprint'] = record_fingerprint(selected_material(patient, scope))
    revise_record(patient, actor, selected.pk, action='DELETE', expected_revision=1)
    with pytest.raises(SnapshotChanged):
        assert_records_current(patient, snapshot)


def test_deleted_foreign_and_malformed_record_selections_never_become_exports(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-export-denied')
    _, other, _, _, _ = family(django_user_model, 'daily-export-other')
    foreign = create_record(other, other.account, payload(), creation_key=uuid4()).record
    with pytest.raises(PermissionDenied):
        selected_material(patient, {'self_record_ids': [str(foreign.pk)]})
    with pytest.raises(ExportInputError):
        selected_material(patient, {'self_record_ids': 'all'})
    with pytest.raises(ExportInputError):
        selected_material(patient, {'self_record_ids': ['wrong']})
    own = create_record(patient, actor, payload(), creation_key=uuid4()).record
    revise_record(patient, actor, own.pk, action='DELETE', expected_revision=0)
    with pytest.raises(ExportInputError):
        selected_material(patient, {'self_record_ids': [str(own.pk)]})
