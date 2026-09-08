from copy import deepcopy
import json
from uuid import uuid4

import pytest

from apps.cloud_imaging.projection import assert_safe_snapshot, project_default_snapshot
from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import SnapshotChanged
from apps.exports.formats import csv_tables, json_bytes
from apps.glucose.services import create_record
from apps.patients.sharing_content import project_snapshot
from tests.documents.test_detail_viewer import _patient
from tests.glucose.test_payloads import payload


PUBLIC_RULE = 'https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2021/DataFiles/GLU_L.htm'


@pytest.mark.django_db
def test_real_confirmed_conversion_keeps_its_fixed_public_reference(django_user_model):
    _, patient = _patient(django_user_model, 'cloud-conversion')
    record = create_record(patient, patient.account, payload(value='100', unit='mg/dL'),
                           creation_key=uuid4(), source_kind='METER').record
    selection = {'mode': 'documents', 'document_ids': [], 'glucose_record_ids': [str(record.pk)], 'sections': ['glucose']}
    snapshot = build_snapshot(patient, selection)
    row = snapshot['glucose_records'][0]
    assert row['data']['conversion']['source_url'] == PUBLIC_RULE
    assert row['original_data']['conversion']['source_url'] == PUBLIC_RULE
    assert row['data']['normalized_value'] == '5.551'
    assert PUBLIC_RULE in json_bytes(snapshot).decode()
    assert PUBLIC_RULE in csv_tables(snapshot)['glucose_records.csv'].decode('utf-8-sig')
    assert PUBLIC_RULE in json.dumps(project_snapshot(snapshot, selection))
    assert_snapshot_current(patient, snapshot)
    record.refresh_from_db()
    assert record.current_data['conversion']['source_url'] == PUBLIC_RULE


@pytest.mark.parametrize('location', ['notes', 'facts', 'glucose_records'])
def test_user_controlled_keys_cannot_whitelist_an_access_secret(location):
    malicious = {'rule_id': 'glucose-mg-dl-mmol-l-cdc-v1', 'factor': '0.05551', 'formula': 'value * 0.05551',
                 'source_url': PUBLIC_RULE + '?secret=SYNTHETIC_PARAMETER'}
    data = {'conversion': malicious, 'raw_unit': 'mg/dL', 'normalized_unit': 'mmol/L', 'result_type': 'NUMERIC'}
    value = {location: [{'data': data}]}
    with pytest.raises(SnapshotChanged):
        assert_safe_snapshot(value)
    original = deepcopy(value)
    projected = project_default_snapshot(value)
    assert 'SYNTHETIC_PARAMETER' not in json.dumps(projected)
    assert value == original


def test_even_the_exact_reference_in_patient_text_is_treated_as_patient_content():
    value = {'facts': [{'content': {'conversion': {
        'rule_id': 'glucose-mg-dl-mmol-l-cdc-v1', 'factor': '0.05551', 'formula': 'value * 0.05551',
        'source_url': PUBLIC_RULE,
    }}}]}
    with pytest.raises(SnapshotChanged):
        assert_safe_snapshot(value)
    assert PUBLIC_RULE not in json.dumps(project_default_snapshot(value))
