from copy import deepcopy
import json
from uuid import uuid4

import pytest

from apps.exports.errors import ExportInputError
from apps.glucose.exporting import selected_material
from apps.glucose.output import card_entries, csv_content, share_material
from apps.glucose.payloads import normalize_payload
from apps.glucose.services import create_record, revise_record
from tests.glucose.factories import lab_source
from tests.glucose.test_payloads import payload
from tests.glucose.test_sources import import_source
from tests.patients.test_family_access import family


def snapshot(patient, record):
    material = selected_material(patient, {'glucose_record_ids': [str(record.pk)]})
    return {'glucose_records': material['records'], 'glucose_record_sources': material['sources'],
            'glucose_document_ids': material['document_ids'], 'glucose_fingerprint': material['fingerprint'],
            'card': {'glucose_record_ids': [str(record.pk)]}}


@pytest.mark.django_db
def test_portable_values_keep_original_revision_but_share_has_only_selected_current_entry(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-output-current')
    record = create_record(patient, actor, payload(value='100', unit='mg/dL', notes='PRIVATE OLD NOTE'),
                           creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0,
                  changes=payload(value='110', unit='mg/dL', notes='selected current note'))
    frozen = snapshot(patient, record)
    before = deepcopy(frozen)
    table = csv_content(frozen)['glucose_records'][0]
    assert table['raw_value'] == '110' and table['raw_unit'] == 'mg/dL'
    assert table['normalized_value'] == '6.1061' and table['original_data']['raw_value'] == '100'
    assert table['created_by'] == table['revision_author'] == str(actor.pk)
    scope = {'sections': ['glucose'], 'glucose_record_ids': [str(record.pk)]}
    shared = share_material(frozen, scope)
    row = shared['glucose_records'][0]
    assert row['data']['raw_value'] == '110' and row['revision_number'] == 1
    assert 'PRIVATE OLD NOTE' not in json.dumps(shared) and str(actor.pk) not in json.dumps(shared)
    assert not {'original_data', 'created_by', 'updated_by', 'revision_author', 'revision_id'} & set(row)
    assert frozen == before
    with pytest.raises(ExportInputError):
        share_material(frozen, {**scope, 'glucose_record_ids': [str(uuid4())]})


@pytest.mark.django_db
def test_report_printable_time_keeps_seconds_unconfirmed_zone_and_distinct_reporting_role(django_user_model):
    _, patient, document, _, observation = lab_source(django_user_model, marker='glucose-output-source')
    record = import_source(patient, observation).record
    frozen = snapshot(patient, record)
    text = card_entries(frozen)[0]['text']
    assert all(value in text for value in ('2026-08-02T06:12:34', '时间精度：秒', '时区未确认',
                                          '原件采样时间', '原件报告时间：2026-08-02T09:24:56', '时段：未注明'))
    assert 'Asia/Shanghai' not in text and 'UTC+08' not in text
    assert str(document.pk) in text and '第 1 页' in text
    shared = share_material(frozen, {'sections': ['glucose'], 'glucose_record_ids': [str(record.pk)]})
    source = shared['glucose_record_sources'][0]
    assert source['document_id'] == str(document.pk) and source['sampling']['precision'] == 'SECOND'
    assert not {'field_sources', 'source_fingerprint', 'observation_id', 'parsing_version_id'} & set(source)


@pytest.mark.parametrize('precision,local,label', [('DAY', '2026-08-02', '日'), ('MONTH', '2026-08', '月'),
                                                  ('YEAR', '2026', '年'), ('UNKNOWN', '', '不详')])
def test_partial_time_and_non_single_values_are_printed_without_inventing_a_point(precision, local, label):
    data = normalize_payload(payload(value='>30', unit='', time_precision=precision, measured_local=local,
                                     timezone='', timezone_origin='UNCONFIRMED'), source_kind='NURSING', allow_imprecise=True)
    frozen = {'card': {'glucose_record_ids': ['R1']}, 'glucose_records': [
        {'id': 'R1', 'source_kind_label': '护理原件核对录入', 'revision_number': 0, 'data': data}],
        'glucose_record_sources': []}
    text = card_entries(frozen)[0]['text']
    assert '>30 单位未注明' in text and '时间精度：' + label in text
    assert '不进入曲线' in text and '时段：未注明' in text
    assert '换算值' not in text and 'T00:00' not in text and 'UTC+08' not in text
    assert data['normalized_value'] is None and data['measured_at'] is None
