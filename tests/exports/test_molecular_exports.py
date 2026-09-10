"""Actual selected molecular output preserves meaning without its private closure."""
from copy import deepcopy
import json

import pytest

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.formats import json_bytes, read_structured_data
from apps.patients.sharing_content import normalize_scope, project_snapshot
from tests.facts.molecular_factories import graph
from tests.facts.pathology_factories import review

pytestmark = pytest.mark.django_db
POLICY = 'MOLECULAR_SEMANTIC_UNIT_V1'


def ready_graph(model, name):
    result = graph(model, name)
    _, patient, _, _, fields = result
    for key in ('specimen', 'assay', 'identity', 'metric'):
        review(patient, fields[key])
    return result


def selection(document, *fields):
    return {'mode': 'documents', 'document_ids': [str(document.pk)],
            'clinical_field_ids': [str(field.pk) for field in fields], 'sections': ['imaging']}


def test_selected_variant_quantity_carries_complete_identity_but_no_private_anchors(django_user_model):
    _, patient, document, _, fields = ready_graph(django_user_model, 'molecular-output-one')
    snapshot = build_snapshot(patient, selection(document, fields['metric']))
    public = read_structured_data(json_bytes(snapshot))
    row, = public['clinical_fields']
    unit = row['content'].get('molecular_semantic_unit', {})
    assert unit.get('policy') == POLICY
    assert public['scope']['molecular_semantic_unit_policy'] == POLICY
    identity, = unit['variants']
    assert identity['identity']['kind'] == 'SMALL_VARIANT'
    assert identity['identity']['scope'] == 'SOMATIC'
    assert identity['identity']['gene'] == {'state': 'PRINTED', 'raw': 'SYN1'}
    assert identity['identity']['coding']['values'] == ['c.12+1G>A']
    assert identity['identity']['protein']['values'] == ['p.?']
    assert identity['identity']['codons']['values'] == ['codon 4']
    assert identity['identity']['transcripts']['values'] == ['NM_SYN.2']
    assert identity['identity']['locations']['values'] == ['build-X chr2:12']
    assert row['content']['value']['values'] == ['01.20']
    assert row['content']['value']['unit'] == '%'
    assert 'raw' not in row['content']['value']
    assert unit['assay_conditions'] == 'NOT_INCLUDED_NOT_COMPARABLE'
    text = json.dumps(public, ensure_ascii=False)
    for private in ('标本甲', '检测甲', 'entity_context', 'dependency_heads', 'proof_fragment_ordinals',
                    'context_snapshot', 'variant:a', str(fields['identity'].pk), str(fields['specimen'].pk), str(fields['assay'].pk)):
        assert private not in text
    assert_snapshot_current(patient, snapshot)


def test_molecular_fine_share_keeps_selected_field_and_fresh_local_scopes(django_user_model):
    _, patient, document, _, fields = ready_graph(django_user_model, 'molecular-output-share')
    snapshot = build_snapshot(patient, selection(document, fields['identity'], fields['metric']))
    scoped = project_snapshot(snapshot, normalize_scope(selection(document, fields['metric'])))
    assert [row['id'] for row in scoped['clinical_fields']] == [str(fields['metric'].pk)]
    unit = scoped['clinical_fields'][0]['content']['molecular_semantic_unit']
    original = next(row for row in snapshot['clinical_fields'] if row['id'] == str(fields['metric'].pk))['content']['molecular_semantic_unit']
    assert unit['policy'] == POLICY
    assert unit['specimen_scope']['token'] != original['specimen_scope']['token']
    assert unit['variants'][0]['alias']['token'] != original['variants'][0]['alias']['token']
    assert '标本甲' not in json.dumps(scoped, ensure_ascii=False)
    assert 'molecular_validation_context' not in scoped


@pytest.mark.parametrize('damage', ['missing_policy', 'missing_identity', 'bare_value'])
def test_reader_rejects_new_molecular_data_with_lost_identity_or_policy(django_user_model, damage):
    _, patient, document, _, fields = ready_graph(django_user_model, 'molecular-reader-' + damage)
    public = json.loads(json_bytes(build_snapshot(patient, selection(document, fields['metric']))))
    if damage == 'missing_policy':
        public['scope'].pop('molecular_semantic_unit_policy', None)
    elif damage == 'missing_identity':
        public['clinical_fields'][0]['content'].get('molecular_semantic_unit', {}).pop('variants', None)
    else:
        public['clinical_fields'][0]['content'].pop('molecular_semantic_unit', None)
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(public))


def test_actual_pdf_json_csv_zip_contents_and_explicit_original_bytes(django_user_model):
    import csv
    import hashlib
    import io
    import zipfile
    from pypdf import PdfReader
    from apps.exports.formats import build_artifact, csv_tables
    from tests.documents.fakes import InMemoryObjectStore
    from apps.documents.models import Document
    _, patient, document, _, fields = ready_graph(django_user_model, 'molecular-every-format')
    payload = b'SYNTHETIC-EXPLICIT-ORIGINAL-ONLY'
    Document.objects.filter(pk=document.pk).update(sha256=hashlib.sha256(payload).hexdigest(), byte_size=len(payload))
    document.refresh_from_db()
    for field in fields.values(): review(patient, field)
    snapshot = build_snapshot(patient, {**selection(document, fields['metric']), 'details': True})
    store = InMemoryObjectStore(); store.objects[document.original_object_key] = payload
    artifact = build_artifact(snapshot, {'format': 'zip', 'parts': ['pdf', 'json', 'csv', 'originals']}, store)
    with zipfile.ZipFile(io.BytesIO(artifact.payload)) as package:
        original = [name for name in package.namelist() if name.startswith('originals/')]
        assert len(original) == 1 and package.read(original[0]) == payload
        data = read_structured_data(package.read('records.json'))
        pdf_text = ''.join(p.extract_text() for p in PdfReader(io.BytesIO(package.read('visit-card.pdf'))).pages)
        csv_name = next(name for name in package.namelist() if name.endswith('/clinical_fields.csv') or name == 'clinical_fields.csv')
        csv_row, = csv.DictReader(io.StringIO(package.read(csv_name).decode('utf-8-sig')))
        assert json.loads(csv_row['content']) == data['clinical_fields'][0]['content']
    assert '01.20' in pdf_text and 'NM_SYN.2' in pdf_text and 'c.12+1G>A' in pdf_text
    assert 'codon 4' in pdf_text and 'build-X chr2:12' in pdf_text
    public = json.dumps(data, ensure_ascii=False) + pdf_text
    assert '标本甲' not in public and '检测甲' not in public and 'variant:a' not in public
    assert all(name in data for name in ('facts', 'labs', 'sources', 'self_records', 'glucose_records', 'treatment_events'))
    without = build_artifact(snapshot, {'format': 'zip', 'parts': ['json']}, store)
    with zipfile.ZipFile(io.BytesIO(without.payload)) as package:
        assert not any(name.startswith('originals/') for name in package.namelist())


@pytest.mark.parametrize('kind', ['SMALL_VARIANT', 'COPY_NUMBER', 'FUSION'])
def test_all_identity_kinds_retain_original_components_and_order(django_user_model, kind):
    from tests.facts.molecular_factories import add
    from tests.facts.test_molecular_contracts import variant
    _, patient, document, report, fields = graph(django_user_model, 'molecular-kind-' + kind)
    identity = add(patient, report, 'variant.identity', 'variant:chosen', variant(kind),
                   {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']})
    for field in (fields['specimen'], fields['assay'], identity): review(patient, field)
    public = read_structured_data(json_bytes(build_snapshot(patient, selection(document, identity))))
    value = public['clinical_fields'][0]['content']['value']
    expected = deepcopy(variant(kind)); expected.pop('raw')
    assert value == expected
    if kind == 'FUSION':
        assert [p['gene']['raw'] for p in value['partners']] == ['SYNB', 'SYNA']
        damaged = deepcopy(public)
        damaged['clinical_fields'][0]['content']['value']['partners'].reverse()
        with pytest.raises(ExportInputError): read_structured_data(json.dumps(damaged))


@pytest.mark.parametrize('damage', ['schema', 'legacy_disguise', 'result_type', 'row_private', 'content_private', 'missing_omission', 'conditions', 'scope', 'raw_clause', 'identity_component', 'role', 'scope_alias'])
def test_strict_reader_cannot_restore_private_data_or_remove_required_meaning(django_user_model, damage):
    _, patient, document, _, fields = ready_graph(django_user_model, 'molecular-strict-' + damage)
    data = json.loads(json_bytes(build_snapshot(patient, selection(document, fields['metric']))))
    row = data['clinical_fields'][0]; content = row['content']; unit = content['molecular_semantic_unit']
    if damage == 'schema': row.pop('schema_version')
    elif damage == 'legacy_disguise': row.update(schema_version='1.0', field_key='report.exam_date')
    elif damage == 'result_type': content['result_type'] = 'DERIVED_POSITIVE'
    elif damage == 'row_private': row['entity_key'] = 'private:entity'
    elif damage == 'content_private': content['manual_source'] = {'own_fragment_count': 1}
    elif damage == 'missing_omission': content.pop('source_context_omitted')
    elif damage == 'conditions': unit['assay_conditions'] = 'COMPARABLE'
    elif damage == 'scope': unit['variants'][0]['identity']['scope'] = 'UNKNOWN'
    elif damage == 'raw_clause': content['value']['raw'] = 'private adjacent clause'
    elif damage == 'identity_component': unit['variants'][0]['identity'].pop('transcripts')
    elif damage == 'role': content['source_role'] = unit['source_role'] = 'CONTROL'
    else: unit['assay_scope']['token'] = 'not-a-scope'
    with pytest.raises(ExportInputError): read_structured_data(json.dumps(data))


def test_shared_date_keeps_ihc_mode_and_uses_same_assay_alias_as_molecular_value(django_user_model):
    from tests.facts.molecular_factories import add
    _, patient, document, report, fields = graph(django_user_model, 'molecular-shared-date')
    date = add(patient, report, 'assay.report_date', 'assay:a', {'value': '2026-09', 'precision': 'MONTH'},
               {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}, raw='标本甲；检测甲；2026年9月')
    for field in (fields['specimen'], fields['assay'], date, fields['identity'], fields['metric']): review(patient, field)
    data = read_structured_data(json_bytes(build_snapshot(patient, selection(document, date, fields['metric']))))
    rows = {r['field_key']: r for r in data['clinical_fields']}
    assert rows['assay.report_date']['schema_version'] == 'PATHOLOGY_IHC_V1'
    assert rows['assay.report_date']['content']['value'] == {'value': '2026-09', 'precision': 'MONTH'}
    assert rows['assay.report_date']['content']['semantic_qualifiers']['assay_scope'] == rows['variant.allele_fraction']['content']['molecular_semantic_unit']['assay_scope']
    assert data['clinical_reports'][0]['routing_kind'] == 'MOLECULAR'


def test_drug_selected_level_has_only_complete_same_group_meaning(django_user_model):
    from tests.facts.test_molecular_context import drug_graph
    from tests.facts.molecular_factories import add, context_for
    from tests.facts.test_molecular_contracts import component
    patient, document, report, fields, targets, context, raw = drug_graph(django_user_model, 'molecular-drug-output')
    targets = {**targets, 'DRUG_EVIDENCE': fields['drugs']}
    context = context_for(report, targets, deepcopy(context['association']))
    values = {'statement': {'text': '仅原报告依据'}, 'direction': {'code': 'REPORT_RESISTANCE', 'raw': '报告耐药'},
              'context': {'text': '限定报告背景'}, 'level': {'grade': component('II'), 'system': component(state='UNKNOWN'), 'raw': 'II；UNSELECTED_ADJACENT'}}
    for name, value in values.items():
        fields[name] = add(patient, report, 'drug_evidence.' + name, 'drug_evidence:a', value, targets,
                           raw=raw + ';仅原报告依据；报告耐药；限定报告背景；II；UNSELECTED_ADJACENT', context=context, role='REPORT_DRUG_EVIDENCE')
    for field in fields.values(): review(patient, field)
    data = read_structured_data(json_bytes(build_snapshot(patient, selection(document, fields['level']))))
    row, = data['clinical_fields']; unit = row['content']['molecular_semantic_unit']; drug = unit['drug_evidence']
    assert drug['drugs']['names'] == ['SYN-A', 'SYN-B'] and drug['drugs']['relation'] == 'AND'
    assert drug['direction']['value']['code'] == 'REPORT_RESISTANCE'
    assert drug['level']['value']['system'] == {'state': 'UNKNOWN', 'raw': None}
    assert drug['report_date'] == {'state': 'NOT_STATED', 'value': None}
    assert [v['identity']['kind'] for v in unit['variants']] == ['SMALL_VARIANT', 'FUSION']
    assert drug['association']['variants'] == [v['alias'] for v in unit['variants']]
    public = json.dumps(data, ensure_ascii=False)
    for private in ('UNSELECTED_ADJACENT', '标本甲', '检测甲', '01.20', 'drug_evidence:a', str(fields['drugs'].pk)):
        assert private not in public
    assert '报告未说明' in row['content']['text'] and '不是治疗建议' in row['content']['text']
    damaged = deepcopy(data); damaged['clinical_fields'][0]['content']['molecular_semantic_unit']['variants'].reverse()
    with pytest.raises(ExportInputError): read_structured_data(json.dumps(damaged))


@pytest.mark.parametrize('key,value,raw', [
    ('assay.msi_category', {'code': 'MSI_L', 'raw': 'MSI-L'}, 'MSI-L'),
    ('assay.tmb_qualitative', {'code': 'UNKNOWN', 'raw': '原文未分类'}, '原文未分类'),
    ('assay.name', {'text': '=合成检测 https://portal.invalid/secret?code=CANARY'}, '=合成检测 https://portal.invalid/secret?code=CANARY'),
])
def test_selected_categorical_and_text_fields_keep_values_and_omit_access_strings(django_user_model, key, value, raw):
    from tests.facts.molecular_factories import add
    from apps.exports.formats import csv_tables
    _, patient, document, report, fields = graph(django_user_model, 'molecular-category-' + key)
    field = add(patient, report, key, 'assay:a', value, {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}, raw='标本甲；检测甲；' + raw)
    for item in (fields['specimen'], fields['assay'], field): review(patient, item)
    snapshot = build_snapshot(patient, selection(document, field))
    data = read_structured_data(json_bytes(snapshot))
    assert 'CANARY' not in json.dumps(data)
    assert 'portal.invalid' not in json.dumps(data)
    assert data['clinical_fields'][0]['content']['molecular_semantic_unit']['variants'] == []
    assert csv_tables(snapshot)['clinical_fields.csv']


@pytest.mark.parametrize('kind,key', [('PANEL_SIZE', 'assay.panel_size'), ('MSI', 'assay.msi_value'), ('TMB', 'assay.tmb_value'), ('COPY_NUMBER', 'variant.copy_number')])
@pytest.mark.parametrize('unit_state', ['PRINTED', 'NOT_PRINTED', 'UNKNOWN'])
def test_numeric_units_ranges_approximation_and_measurement_kind_are_independent(django_user_model, kind, key, unit_state):
    from tests.facts.molecular_factories import add, variant_source
    from tests.facts.test_molecular_contracts import quantity, panel
    _, patient, document, report, fields = graph(django_user_model, 'molecular-number-' + kind + unit_state)
    value = panel(values=['2', '5'], comparator='RANGE') if kind == 'PANEL_SIZE' else quantity(kind, values=['01.20', '03.40'], comparator='RANGE', approximate=True)
    value.update(unit_state=unit_state, unit=value['unit'] if unit_state == 'PRINTED' else None)
    targets = {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}
    if key.startswith('variant.'):
        targets['VARIANT'] = fields['identity']
    field = add(patient, report, key, 'variant:a' if key.startswith('variant.') else 'assay:a', value, targets,
                raw='标本甲；检测甲；' + variant_source() + '; ' + value['raw'])
    for item in (fields['specimen'], fields['assay'], fields['identity'], field): review(patient, item)
    row, = read_structured_data(json_bytes(build_snapshot(patient, selection(document, field))))['clinical_fields']
    expected = deepcopy(value); expected.pop('raw')
    assert row['content']['value'] == expected
    assert row['content']['molecular_semantic_unit']['reported_assertion'] == {'code': 'AS_REPORTED_NO_POSITIVITY_INFERRED', 'raw': None}


def test_limited_negative_keeps_printed_kind_targets_and_limits_without_spreading(django_user_model):
    from tests.facts.molecular_factories import add
    _, patient, document, report, fields = graph(django_user_model, 'molecular-negative-output')
    value = {'text': 'negative', 'assertion': 'NEGATIVE', 'scope': {'state': 'EXPLICIT', 'raw': 'small variants',
        'detection_kinds': [{'code': 'SMALL_VARIANT', 'raw': 'small variants'}], 'targets': ['SYN1'], 'limitations': ['region A only']}}
    field = add(patient, report, 'assay.negative_statement', 'assay:a', value,
                {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}, raw='标本甲；检测甲；negative; small variants; SYN1; region A only')
    for item in (fields['specimen'], fields['assay'], field): review(patient, item)
    data = read_structured_data(json_bytes(build_snapshot(patient, selection(document, field))))
    row, = data['clinical_fields']
    assert row['content']['value'] == value and row['content']['molecular_semantic_unit']['variants'] == []
    assert 'COPY_NUMBER' not in json.dumps(data) and 'FUSION' not in json.dumps(data)
    damaged = deepcopy(data); damaged['clinical_fields'][0]['content']['value']['scope']['detection_kinds'][0]['code'] = 'COPY_NUMBER'
    with pytest.raises(ExportInputError): read_structured_data(json.dumps(damaged))
    from apps.exports import molecular
    damaged = deepcopy(data); row = damaged['clinical_fields'][0]
    row['content']['value']['scope'] = {'state': 'UNKNOWN', 'raw': None, 'detection_kinds': [], 'targets': [], 'limitations': []}
    row['content']['text'] = molecular._text(row['field_key'], row['content']['value'], row['content']['molecular_semantic_unit'])
    with pytest.raises(ExportInputError): read_structured_data(json.dumps(damaged))


def test_long_expression_unicode_and_ordered_repeated_components_are_not_truncated(django_user_model):
    from tests.facts.molecular_factories import add
    from tests.facts.test_molecular_contracts import variant
    _, patient, document, report, fields = graph(django_user_model, 'molecular-long-output')
    value = variant(); tail = 'c.' + '1234567890' * 100 + 'G>A'
    value['expression']['raw'] = value['raw'] = tail
    value['transcripts']['values'] = ['NM_SYN.2', 'NM_SYN.3', 'NM_SYN.2']
    identity = add(patient, report, 'variant.identity', 'variant:long', value,
                   {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']},
                   raw='标本甲；检测甲；SYN1;' + tail + ';c.12+1G>A;p.?;codon 4;NM_SYN.2;NM_SYN.3;build-X chr2:12')
    for item in (fields['specimen'], fields['assay'], identity): review(patient, item)
    data = read_structured_data(json_bytes(build_snapshot(patient, selection(document, identity))))
    assert data['clinical_fields'][0]['content']['value']['expression']['raw'] == tail
    assert data['clinical_fields'][0]['content']['value']['transcripts']['values'] == ['NM_SYN.2', 'NM_SYN.3', 'NM_SYN.2']


@pytest.mark.parametrize('change', ['identity', 'new_assay_member', 'report', 'same_value_undo'])
def test_unselected_actual_context_changes_invalidate_old_minimum_output(django_user_model, change):
    from tests.facts.molecular_factories import add
    from apps.facts.clinical_services import revise_report
    from apps.facts.clinical_readmodels import report_source_token
    _, patient, document, report, fields = ready_graph(django_user_model, 'molecular-output-lifecycle-' + change)
    snapshot = build_snapshot(patient, selection(document, fields['metric']))
    if change == 'identity': review(patient, fields['identity'], 'EXCLUDE')
    elif change == 'report':
        revise_report(patient, actor=patient.account, report_id=report.pk, action='EXCLUDE', expected_revision=report.revision_number, expected_source=report_source_token(report))
    elif change == 'new_assay_member':
        add(patient, report, 'assay.name', 'assay:a', {'text': '新增已印检测名'}, {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']})
    else:
        review(patient, fields['metric'], 'REVOKE'); review(patient, fields['metric'], 'UNDO')
    with pytest.raises(SnapshotChanged): assert_snapshot_current(patient, snapshot)


def test_molecular_report_pdl1_keeps_its_actual_ihc_assay_and_score_policy(django_user_model):
    from tests.facts.molecular_factories import add
    from tests.facts.pathology_factories import score_value
    _, patient, document, report, fields = graph(django_user_model, 'molecular-report-ihc')
    specimen = fields['specimen']
    assay = add(patient, report, 'assay.identity', 'assay:ihc', {'label': '免疫组化检测', 'raw': '免疫组化检测'}, {'SPECIMEN': specimen}, raw='标本甲；免疫组化检测')
    marker = add(patient, report, 'ihc.marker', 'ihc:one', {'code': 'PD_L1', 'label': 'PD-L1', 'raw': 'PD-L1'}, {'SPECIMEN': specimen, 'ASSAY': assay}, raw='标本甲；免疫组化检测；PD-L1')
    cps = add(patient, report, 'ihc.score', 'ihc:one', score_value('CPS', '21', None), {'SPECIMEN': specimen, 'ASSAY': assay, 'MARKER': marker}, raw='标本甲；免疫组化检测；PD-L1；CPS 21')
    for field in (*fields.values(), assay, marker, cps): review(patient, field)
    data = read_structured_data(json_bytes(build_snapshot(patient, selection(document, fields['metric'], cps))))
    rows = {row['field_key']: row for row in data['clinical_fields']}
    assert rows['ihc.score']['schema_version'] == 'PATHOLOGY_IHC_V1'
    ihc = rows['ihc.score']['content']['semantic_qualifiers']; mol = rows['variant.allele_fraction']['content']['molecular_semantic_unit']
    assert ihc['policy'] == 'IHC_SCORE_SEMANTIC_UNIT_V1' and ihc['marker']['code'] == 'PD_L1'
    assert ihc['specimen_scope'] == mol['specimen_scope'] and ihc['assay_scope'] != mol['assay_scope']
    assert 'molecular_semantic_unit' not in rows['ihc.score']['content']


def test_similar_private_choices_identify_the_actual_assay_without_stable_public_ids(django_user_model):
    from tests.facts.molecular_factories import add, variant_source
    from tests.facts.test_molecular_contracts import variant, quantity
    from apps.exports.forms import SelectionForm
    _, patient, document, report, fields = graph(django_user_model, 'molecular-choice-identity')
    assay = add(patient, report, 'assay.identity', 'assay:second', {'label': '第二检测', 'raw': '第二检测'}, {'SPECIMEN': fields['specimen']}, raw='标本甲；第二检测')
    targets = {'SPECIMEN': fields['specimen'], 'ASSAY': assay}
    identity = add(patient, report, 'variant.identity', 'variant:second', variant(), targets, raw='标本甲；第二检测；' + variant_source())
    metric = add(patient, report, 'variant.allele_fraction', 'variant:second', quantity(), {**targets, 'VARIANT': identity}, raw='标本甲；第二检测；' + variant_source() + ';01.20 %')
    for field in (*fields.values(), assay, identity, metric): review(patient, field)
    labels = dict(SelectionForm(patient, actor=patient.account).fields['clinical_field_ids'].choices)
    assert labels[str(fields['metric'].pk)] != labels[str(metric.pk)]
    assert '检测甲' in labels[str(fields['metric'].pk)] and '第二检测' in labels[str(metric.pk)]
    row, = read_structured_data(json_bytes(build_snapshot(patient, selection(document, metric))))['clinical_fields']
    assert row['id'] == str(metric.pk)
    assert '第二检测' not in json.dumps(row, ensure_ascii=False)


@pytest.mark.parametrize('component_key,raw', [('gene', 'SYN1'), ('expression', 'c.12+1G>A (p.?)'),
    ('coding', 'c.12+1G>A'), ('protein', 'p.?'), ('codon', 'codon 4'), ('transcript', 'NM_SYN.2'),
    ('location', 'build-X chr2:12'), ('change', '拷贝数增加'), ('tier', '报告分级甲')])
def test_each_selected_variant_component_keeps_full_matching_identity(django_user_model, component_key, raw):
    from tests.facts.molecular_factories import add, variant_source
    from tests.facts.test_molecular_contracts import variant, component
    from apps.exports import molecular
    _, patient, document, report, fields = graph(django_user_model, 'molecular-component-' + component_key)
    targets = {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}
    identity = fields['identity']
    if component_key == 'change':
        identity = add(patient, report, 'variant.identity', 'variant:copy', variant('COPY_NUMBER'), targets)
    field = add(patient, report, 'variant.' + component_key, identity.entity_key, component(raw),
                {**targets, 'VARIANT': identity}, raw='标本甲；检测甲；' + variant_source('COPY_NUMBER' if component_key == 'change' else 'SMALL_VARIANT') + ';' + raw)
    for item in (fields['specimen'], fields['assay'], identity, field): review(patient, item)
    data = read_structured_data(json_bytes(build_snapshot(patient, selection(document, field))))
    row, = data['clinical_fields']; unit = row['content']['molecular_semantic_unit']
    assert row['content']['value'] == component(raw)
    expected = variant('COPY_NUMBER' if component_key == 'change' else 'SMALL_VARIANT'); expected.pop('raw')
    assert unit['variants'][0]['identity'] == expected
    assert str(identity.pk) not in json.dumps(data) and '01.20' not in json.dumps(data)
    if component_key != 'tier':
        row['content']['value']['raw'] = 'NOT_THE_PRINTED_COMPONENT'
        row['content']['text'] = molecular._text(row['field_key'], row['content']['value'], unit)
        with pytest.raises(ExportInputError): read_structured_data(json.dumps(data))


@pytest.mark.parametrize('key,kind', [('variant.allele_fraction', 'ALLELE_FRACTION'), ('variant.copy_number', 'COPY_NUMBER'),
    ('assay.msi_value', 'MSI'), ('assay.tmb_value', 'TMB'), ('assay.panel_size', 'PANEL_SIZE')])
def test_unresolved_selected_quantity_is_explicit_not_zero_or_crash(django_user_model, key, kind):
    from tests.facts.molecular_factories import add
    from tests.facts.test_molecular_contracts import quantity, component
    _, patient, document, report, fields = graph(django_user_model, 'molecular-unresolved-' + kind)
    value = quantity(kind, status='UNRESOLVED', values=[], comparator=None, raw='量值无法解释', unit=None, unit_state='UNKNOWN')
    if kind == 'PANEL_SIZE': value['count_object'] = component(state='UNKNOWN')
    targets = {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}
    if key.startswith('variant.'): targets['VARIANT'] = fields['identity']
    field = add(patient, report, key, 'variant:a' if key.startswith('variant.') else 'assay:a', value, targets,
                raw='标本甲；检测甲；SYN1 c.12+1G>A (p.?)；codon 4；NM_SYN.2；build-X chr2:12；量值无法解释')
    for item in (fields['specimen'], fields['assay'], fields['identity'], field): review(patient, item)
    row, = read_structured_data(json_bytes(build_snapshot(patient, selection(document, field))))['clinical_fields']
    assert row['content']['value']['values'] == [] and row['content']['value']['comparator'] is None
    assert '原数值未能解释' in row['content']['text'] and '单位未知' in row['content']['text']


@pytest.mark.parametrize('date_value,date_raw', [({'value': '2026-09', 'precision': 'MONTH'}, '2026年9月'),
    ({'value': None, 'precision': 'UNKNOWN'}, '报告日期未知')])
def test_drug_report_date_keeps_actual_precision_without_whole_private_clause(django_user_model, date_value, date_raw):
    from tests.facts.test_molecular_context import drug_graph
    from tests.facts.molecular_factories import add
    patient, document, report, fields, _, _, _ = drug_graph(django_user_model, 'molecular-drug-date-' + date_value['precision'])
    date = add(patient, report, 'assay.report_date', 'assay:a', date_value,
               {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']},
               raw='标本甲；检测甲；PRIVATE_ADJACENT_DATE_NOTE；' + date_raw)
    for field in (fields['specimen'], fields['assay'], date): review(patient, field)
    for name, field in fields.items():
        if name not in {'specimen', 'assay'}: review(patient, field)
    data = read_structured_data(json_bytes(build_snapshot(patient, selection(document, fields['drugs']))))
    row, = data['clinical_fields']
    assert row['content']['molecular_semantic_unit']['drug_evidence']['report_date'] == {'state': 'REPORTED', 'value': date_value}
    for private in ('标本甲', '检测甲', 'PRIVATE_ADJACENT_DATE_NOTE', str(date.pk)):
        assert private not in json.dumps(data, ensure_ascii=False)


def test_original_numeric_uncertainty_is_visible_in_actual_pdf_and_share_projection(django_user_model):
    import io
    from pypdf import PdfReader
    from apps.exports.formats import build_artifact
    from tests.documents.fakes import InMemoryObjectStore
    from tests.facts.molecular_factories import add
    from tests.facts.test_molecular_contracts import quantity
    _, patient, document, report, fields = graph(django_user_model, 'molecular-value-uncertain')
    field = add(patient, report, 'assay.tmb_value', 'assay:a', quantity('TMB', assertion='UNCERTAIN'),
                {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}, raw='标本甲；检测甲；01.20 %；不确定')
    for item in (fields['specimen'], fields['assay'], field): review(patient, item)
    chosen = selection(document, field)
    snapshot = build_snapshot(patient, {**chosen, 'details': True})
    data = read_structured_data(json_bytes(snapshot)); row, = data['clinical_fields']
    assert row['content']['value']['assertion'] == 'UNCERTAIN' and '原文不确定' in row['content']['text']
    artifact = build_artifact(snapshot, {'format': 'pdf'}, InMemoryObjectStore())
    text = ''.join(page.extract_text() for page in PdfReader(io.BytesIO(artifact.payload)).pages)
    assert '原文不确定' in text and '01.20' in text
    shared = project_snapshot(snapshot, normalize_scope(chosen))
    assert '原文不确定' in shared['clinical_fields'][0]['content']['text']
