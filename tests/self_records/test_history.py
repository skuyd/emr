from uuid import uuid4

import pytest

from apps.self_records.history import display_row
from apps.self_records.services import create_record
from tests.patients.test_family_access import family


@pytest.mark.django_db
def test_display_uses_entered_local_time_and_date_only_ecog(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-display')
    weight = create_record(patient, actor, {'kind': 'WEIGHT', 'measured_local': '2026-10-25T02:30',
                                            'value': '60', 'unit': 'kg'}, creation_key=uuid4()).record
    ecog = create_record(patient, actor, {'kind': 'ECOG', 'record_date': '2026-10-25', 'score': 0},
                          creation_key=uuid4()).record
    assert display_row(weight)['display_time'] == '2026-10-25T02:30'
    assert display_row(weight)['label'] == '60 kg'
    assert display_row(ecog)['display_time'] == '2026-10-25'
    assert display_row(ecog)['time'] == '' and display_row(ecog)['label'] == '0 分'
