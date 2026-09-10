"""Actual selected narrative output, separate from any selected original/Fact."""
from copy import deepcopy
import csv
import hashlib
import io
import json
import zipfile

from pypdf import PdfReader
import pytest

from apps.cancer_ordering.exporting import selected_material
from apps.cancer_ordering.readmodels import candidate_rows
from apps.cancer_ordering.services import collect_current
from apps.exports.content import build_snapshot
from apps.exports.formats import build_artifact, csv_tables, json_bytes, read_structured_data
from apps.exports.pdf import render_pdf
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.sharing import create_share
from tests.cancer_ordering.test_export_selection import selected_body
from tests.cancer_ordering.test_narrative_views import CONTEXT, uploaded_narrative
from tests.cancer_ordering.test_views import _decision, _url
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.patients.test_family_shares import exchange


pytestmark = pytest.mark.django_db(transaction=True)
OMITTED = {'state': 'OMITTED', 'reason': 'SOURCE_CONTENT_NOT_SELECTED'}
PUBLIC_KEYS = {'id', 'label', 'assertion', 'subject', 'status', 'value_origin', 'source'}


def _assert_minimal(item, candidate):
    assert set(item) == PUBLIC_KEYS
    assert item['id'] == str(candidate.pk) and item['source'] == OMITTED
    assert str(candidate.source_narrative_id) not in json.dumps(item)
    assert 'fact_id' not in item['source']


def _assert_private_absent(text, tokens):
    for token in tokens:
        assert token not in text, f'Unselected private token in output: {token}'


@pytest.mark.parametrize('kind', ['no_fact', 'overlapping_parent', 'manual'])
def test_uploaded_narrative_formats_and_actual_share_only_carry_current_selected_values(
        uploaded_narrative, django_user_model, kind):
    manual = kind == 'manual'
    lines = ('现病史：患者诊断为肺癌，已行化疗。',) if kind == 'overlapping_parent' else ()
    owner, patient, document, candidate = uploaded_narrative(*lines)
    parents = list(Fact.objects.filter(document=document))
    assert len(parents) == (1 if kind == 'overlapping_parent' else 0)
    row, = candidate_rows(patient)
    if manual:
        response = owner.post(_url(row), _decision(patient, row, action='CORRECT', label='胰腺癌',
            profile='PANCREAS', assertion='UNCERTAIN', subject='HISTORICAL', reason='C2_PRIVATE_REVIEW_REASON'))
        assert response.status_code == 303
    selection = selected_body(patient, cancer_candidate_ids=[str(candidate.pk)])
    snapshot = build_snapshot(patient, selection)
    before = deepcopy(snapshot)
    expected = {'id': str(candidate.pk), 'label': '胰腺癌' if manual else '肺癌',
                'assertion': 'UNCERTAIN' if manual else 'AFFIRMED',
                'subject': 'HISTORICAL' if manual else 'CURRENT_PRIMARY',
                'status': 'CONFIRMED' if manual else 'PENDING',
                'value_origin': 'MANUAL_CORRECTION' if manual else 'REPORT', 'source': OMITTED}
    assert snapshot['cancer_candidates'] == [expected] and snapshot['indicator_ordering'] == []
    _assert_minimal(snapshot['cancer_candidates'][0], candidate)
    private = (CONTEXT, '治疗后不适', '无发热', '现病史', '已行化疗', 'C2_PRIVATE_REVIEW_REASON',
               str(candidate.source_narrative_id), str(patient.account_id),
               candidate.source_narrative.source_key, snapshot['cancer_ordering_fingerprint'],
               'cancer_ordering_fingerprint', 'source_narrative_id', 'original_source',
               'label_raw', 'character_map', 'parent_source_token', row['source']['url'],
               *(str(parent.pk) for parent in parents))
    raw = json_bytes(snapshot)
    portable = read_structured_data(raw)
    assert portable['schema_version'] == '1.6' and portable['cancer_candidates'] == [expected]
    assert portable['documents'] == portable['facts'] == portable['sources'] == []
    _assert_private_absent(raw.decode(), private)

    tables = csv_tables(snapshot)
    # Other domains retain their existing empty CSV headers (including glucose
    # original_data). Exact candidate keys above reject private narrative data.
    rows = list(csv.DictReader(io.StringIO(tables['cancer_candidates.csv'].decode('utf-8-sig'))))
    assert len(rows) == 1
    assert {**rows[0], 'source': json.loads(rows[0]['source'])} == expected
    for payload in tables.values():
        _assert_private_absent(payload.decode('utf-8-sig'), private)

    def check_pdf(payload):
        text = ''.join(page.extract_text() for page in PdfReader(io.BytesIO(payload)).pages)
        assert expected['label'] in text and ('已核对原件' if manual else '待核对') in text
        assert '来源内容未纳入本次选择' in text
        if manual:
            assert '疑似或待排' in text and '既往病史' in text
        _assert_private_absent(text, private)
    check_pdf(render_pdf(snapshot))

    with build_artifact(snapshot, {'format': 'zip', 'parts': ['pdf', 'json', 'csv']}, InMemoryObjectStore()) as artifact:
        with zipfile.ZipFile(artifact.stream) as archive:
            manifest_bytes = archive.read('manifest.json')
            _assert_private_absent(manifest_bytes.decode(), private)
            manifest = json.loads(manifest_bytes)
            assert manifest['cancer_candidate_ids'] == [str(candidate.pk)]
            assert not any(name.startswith('originals/') for name in archive.namelist())
            for member in manifest['files']:
                payload = archive.read(member['path'])
                assert len(payload) == member['byte_size'] and hashlib.sha256(payload).hexdigest() == member['sha256']
                if member['path'].endswith('.pdf'):
                    check_pdf(payload)
                else:
                    _assert_private_absent(payload.decode('utf-8-sig'), private)
    assert snapshot == before

    viewer, _ = _patient(django_user_model, 'c2-narrative-share-recipient')
    created = create_share(patient, patient.account, selection)
    share_id = exchange(viewer, created.token)
    response = viewer.get(f'/shared/{share_id}/')
    assert response.status_code == 200
    html = response.content.decode()
    assert expected['label'] in html and ('已核对原件' if manual else '待核对') in html
    assert '来源内容未纳入本次选择' in html
    _assert_private_absent(html, private)
    assert created.share.snapshot['cancer_candidates'] == [expected]
    assert created.share.snapshot['documents'] == [] and not created.share.source_bindings.exists()
    assert viewer.get(f'/shared/{share_id}/documents/{document.pk}/').status_code == 404
    assert candidate.revisions.count() == (1 if manual else 0)


@pytest.mark.parametrize('additional', ['original', 'overlapping_fact', 'unrelated_fact'])
def test_selected_original_or_fact_never_becomes_a_narrative_source_reference(uploaded_narrative, additional):
    lines = ('现病史：患者诊断为肺癌，已行化疗。',) if additional == 'overlapping_fact' else ()
    owner, patient, document, candidate = uploaded_narrative(*lines)
    documents, chosen_fact, sections = [str(document.pk)], None, ['cancer_ordering', 'sources']
    if additional == 'overlapping_fact':
        chosen_fact = Fact.objects.get(document=document)
        sections.append('treatment')
    elif additional == 'unrelated_fact':
        other, version = parsed_facts(patient, ['出院诊断：胰腺癌。'])
        chosen_fact = Fact.objects.get(parsing_version=version)
        documents.append(str(other.pk))
        sections.append('diagnosis')
    if chosen_fact:
        revise_fact(patient, chosen_fact.pk, actor=patient.account, action='CONFIRM',
                    expected_revision=0, checked_original=True)
        collect_current(patient, actor=patient.account)
        row = next(row for row in candidate_rows(patient) if row['id'] == str(candidate.pk))
        assert owner.post(_url(row), _decision(patient, row)).status_code == 303
    selection = selected_body(patient, document_ids=documents,
        fact_ids=[str(chosen_fact.pk)] if chosen_fact else [], sections=sections,
        cancer_candidate_ids=[str(candidate.pk)])
    snapshot = build_snapshot(patient, selection)
    _assert_minimal(snapshot['cancer_candidates'][0], candidate)
    assert {row['id'] for row in snapshot['documents']} == set(documents)
    if chosen_fact:
        assert [row['id'] for row in snapshot['facts']] == [str(chosen_fact.pk)]
        # The separately selected Fact retains its own source grant.
        assert snapshot['facts'][0]['source']['document_id'] == str(chosen_fact.document_id)
        assert str(chosen_fact.pk) not in json.dumps(snapshot['cancer_candidates'])
    original_only = build_snapshot(patient, {**selection, 'cancer_candidate_ids': []})
    original_data, selected_data = json.loads(json_bytes(original_only)), json.loads(json_bytes(snapshot))
    for key, value in original_data.items():
        if isinstance(value, list) and key not in {'cancer_candidates', 'indicator_ordering'}:
            assert selected_data[key] == value
    assert read_structured_data(json_bytes(snapshot))['cancer_candidates'] == snapshot['cancer_candidates']


def test_invalid_legacy_projection_placeholder_cannot_create_a_narrative_fact_reference(uploaded_narrative):
    _, patient, document, candidate = uploaded_narrative()
    # Defensive internal projection contract, not a claimed HTTP exploit:
    # build_snapshot produces UUID Fact IDs. A bad legacy placeholder supplied
    # by a caller must still never turn a no-Fact narrative into a Fact source.
    result = selected_material(patient, selected_body(patient, cancer_candidate_ids=[str(candidate.pk)]),
        documents=[{'id': str(document.pk)}], facts=[{'id': 'None'}])
    _assert_minimal(result['cancer_candidates'][0], candidate)
