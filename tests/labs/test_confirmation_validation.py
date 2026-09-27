import pytest

from apps.labs.revisions import effective_observation, revise_observation
from apps.labs.validation import validate_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_batch_confirmation import preview
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('code,fields,resolved', [
    ('recognition_uncertain', ['raw_value'], True),
    ('association_conflict', ['raw_name', 'specimen'], True),
    ('normalization_uncertain', ['raw_value'], True),
    ('magnitude_suspect', ['raw_value'], True),
    ('specimen_conflict', ['specimen', 'raw_name'], True),
    ('internal_conflict', ['raw_value'], True),
    ('reference_conflict', ['reference_range_raw'], True),
    ('source_policy_unknown', ['raw_value'], True),
    ('recognition_uncertain', [], False),
    ('association_conflict', ['observation_date'], False),
    ('association_conflict', ['method_raw'], False),
    ('source_unavailable', ['raw_value'], False),
])
def test_confirmation_resolves_only_result_review_issues(django_user_model, code, fields, resolved):
    _, patient = _patient(django_user_model, 'confirmed-fields')
    _, row, _ = report(patient)
    row.quality_issues = [{'code': code, 'fields': fields}]
    row.save(update_fields=['quality_issues'])
    assert code in {item['code'] for item in validate_observation(effective_observation(row))}

    revise_observation(patient.account, row.pk, action='CONFIRM', changes={}, expected_revision=0)
    row.refresh_from_db()

    codes = {item['code'] for item in validate_observation(effective_observation(row))}
    assert (code not in codes) == resolved
    assert row.quality_issues == [{'code': code, 'fields': fields}]


def test_correction_requires_new_confirmation_of_complete_result(django_user_model):
    client, patient = _patient(django_user_model, 'confirmed-corrected')
    _, row, _ = report(patient)
    revise_observation(patient.account, row.pk, action='CONFIRM', changes={}, expected_revision=0)
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': '6'}, expected_revision=1)

    reports = preview(client)
    assert sum(item['pending_count'] for item in reports) == 1
    assert sum(item['confirmed_count'] for item in reports) == 0
