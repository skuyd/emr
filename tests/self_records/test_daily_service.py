from uuid import uuid4

import pytest

from apps.self_records.models import DailyRecord
from apps.self_records.services import RecordConflict, create_record, revise_record
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db


def weight(value='60', minute='2026-10-25T02:30'):
    return {'kind': 'WEIGHT', 'measured_local': minute, 'value': value, 'unit': 'kg'}


def test_same_submission_retries_once_but_new_submission_keeps_same_minute(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-retry-local')
    key = uuid4()
    first = create_record(patient, actor, weight(), creation_key=key)
    retry = create_record(patient, actor, weight(), creation_key=key)
    second = create_record(patient, actor, weight(), creation_key=uuid4())
    assert first.created and not retry.created
    assert retry.record.pk == first.record.pk != second.record.pk
    assert DailyRecord.objects.filter(patient=patient, deleted_at__isnull=True).count() == 2
    assert first.record.record_date.isoformat() == '2026-10-25'
    assert first.record.record_time.strftime('%H:%M') == '02:30'
    assert first.record.measured_at is None


def test_ecog_zero_is_date_only_in_database(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-ecog-date')
    record = create_record(patient, actor, {'kind': 'ECOG', 'record_date': '2026-10-01', 'score': 0},
                           creation_key=uuid4()).record
    assert record.record_date.isoformat() == '2026-10-01'
    assert record.record_time is None and record.measured_at is None
    assert record.current_data['score'] == 0


def test_correction_updates_same_record_without_new_business_history(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-current-only')
    record = create_record(patient, actor, weight(), creation_key=uuid4()).record
    revised = revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0,
                            changes=weight('61', '2026-10-26T08:15'))
    assert revised.pk == record.pk and DailyRecord.objects.filter(patient=patient).count() == 1
    assert revised.record_date.isoformat() == '2026-10-26'
    assert revised.record_time.strftime('%H:%M') == '08:15'
    assert revised.current_data['raw_value'] == '61'
    assert revised.original_data == revised.current_data
    assert revised.revision_number == 1 and not revised.revisions.exists()
    with pytest.raises(RecordConflict):
        revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=weight('62'))


def test_delete_leaves_no_restorable_content_or_undo_action(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-delete-final')
    record = create_record(patient, actor, weight(), creation_key=uuid4()).record
    deleted = revise_record(patient, actor, record.pk, action='DELETE', expected_revision=0)
    assert deleted.deleted_at is not None
    assert deleted.current_data == deleted.original_data == {}
    assert not deleted.revisions.exists()
    assert DailyRecord.objects.filter(patient=patient, deleted_at__isnull=True).count() == 0
    with pytest.raises((RecordConflict, ValueError)):
        revise_record(patient, actor, record.pk, action='UNDO', expected_revision=1)
