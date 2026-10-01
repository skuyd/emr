import csv
import io
from uuid import uuid4

import pytest

from apps.exports.formats import csv_tables
from apps.exports.pdf import card_sections
from apps.exports.services import create_preview
from apps.patients.sharing import create_share
from apps.self_records.services import create_record, revise_record
from tests.patients.test_family_access import family
from tests.self_records.test_export_integration import selection


pytestmark = pytest.mark.django_db


def test_selected_ecog_exports_and_share_keep_only_selected_date_and_grade(django_user_model):
    _, patient, client, actor, _ = family(django_user_model, 'daily-output-ecog')
    chosen = create_record(patient, actor, {'kind': 'ECOG', 'record_date': '2026-10-01', 'score': 0},
                           creation_key=uuid4()).record
    create_record(patient, actor, {'kind': 'ECOG', 'record_date': '2026-10-01', 'score': 5},
                  creation_key=uuid4())
    job = create_preview(patient, client.session.session_key, selection(chosen), actor=actor)
    row = job.snapshot['self_records'][0]
    assert len(job.snapshot['self_records']) == 1
    assert row['data']['record_date'] == '2026-10-01'
    assert row['data']['record_time'] is None and row['data']['score'] == 0
    table = list(csv.DictReader(io.StringIO(csv_tables(job.snapshot)['self_records.csv'].decode('utf-8-sig'))))
    assert table[0]['record_date'] == '2026-10-01' and table[0]['record_time'] == ''
    assert table[0]['score'] == '0'
    entries = [entry['text'] for section in card_sections(job.snapshot) for entry in section['entries']]
    daily = next(text for text in entries if 'ECOG评分' in text)
    assert '2026-10-01' in daily and '0 分' in daily
    assert '00:00' not in daily and 'UTC' not in daily
    share = create_share(patient, patient.account, selection(chosen)).share
    assert share.snapshot['self_records'][0]['data']['score'] == 0
    revise_record(patient, actor, chosen.pk, action='CORRECT', expected_revision=0,
                  changes={'kind': 'ECOG', 'record_date': '2026-10-02', 'score': 5})
    job.refresh_from_db()
    share.refresh_from_db()
    assert job.snapshot == {} and share.snapshot == {}
