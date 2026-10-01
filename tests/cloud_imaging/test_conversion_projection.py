from copy import deepcopy
import json

import pytest

from apps.cloud_imaging.projection import assert_safe_snapshot, project_default_snapshot
from apps.exports.errors import SnapshotChanged


PUBLIC_RULE = 'https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2021/DataFiles/GLU_L.htm'


@pytest.mark.parametrize('field', ['data', 'original_data'])
def test_retired_conversion_location_has_no_reference_exception(field):
    value = {'glucose_records': [{field: {
        'normalized_unit': 'mmol/L', 'result_type': 'NUMERIC',
        'conversion': {'rule_id': 'glucose-mg-dl-mmol-l-cdc-v1', 'factor': '0.05551',
                       'formula': 'value * 0.05551', 'source_url': PUBLIC_RULE},
    }}]}
    with pytest.raises(SnapshotChanged):
        assert_safe_snapshot(value)
    assert PUBLIC_RULE not in json.dumps(project_default_snapshot(value))


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
