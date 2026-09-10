"""Unknown statements keep private originals and need an explicit output label."""
from copy import deepcopy
import csv
import io
import json

from pypdf import PdfReader
import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.formats import csv_tables, json_bytes, read_structured_data
from apps.exports.pdf import render_pdf
from tests.cancer_ordering.test_export_selection import selected_body
from tests.cancer_ordering.test_services import _collect, _revise
from tests.documents.test_detail_viewer import _patient

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('suffix', ['未经选择的详细上下文甲乙丙', '合成诊断描述' * 30])
@pytest.mark.parametrize('confirmed', [False, True])
def test_automatic_unknown_clause_needs_label_even_after_source_confirmation(django_user_model, suffix, confirmed):
    _, patient = _patient(django_user_model, 'unknown-clause')
    _collect(patient, ('出院诊断：胃癌，' + suffix + '。',))
    row, = candidate_rows(patient)
    original = deepcopy(CancerCandidate.objects.get(pk=row['id']).original_data)
    if confirmed:
        _revise(patient, row, 'CONFIRM', checked_original=True)
        row, = candidate_rows(patient)
    with pytest.raises(ExportInputError, match='标签'):
        build_snapshot(patient, selected_body(patient, cancer_candidate_ids=[row['id']]))
    assert CancerCandidate.objects.get(pk=row['id']).original_data == original
    assert row['content']['profile'] is None


def test_actual_selection_error_has_review_path_and_unknown_label_can_be_corrected(django_user_model):
    client, patient = _patient(django_user_model, 'unknown-label-ui')
    _collect(patient, ('出院诊断：胃癌，未经选择的详细上下文甲乙丙。',))
    row, = candidate_rows(patient)
    original = deepcopy(CancerCandidate.objects.get(pk=row['id']).original_data)
    post = {'mode': 'documents', 'nickname': patient.display_name, 'sections': ['cancer_ordering'],
            'cancer_candidate_ids': [row['id']], 'action': 'preview',
            'cancer_expected_fingerprint': resolve_ordering(patient)['fingerprint']}
    for path in ('/visit/', f'/patients/{patient.pk}/shares/'):
        response = client.post(path, post)
        assert response.status_code == 400
        assert '标签' in response.content.decode()
        assert f'/cancer-ordering/?patient={patient.pk}' in response.content.decode()
    assert client.get('/cancer-ordering/', {'patient': str(patient.pk)}).status_code == 200
    detail = f"/cancer-ordering/candidates/{row['id']}/?patient={patient.pk}"
    assert client.get(detail).status_code == 200
    response = client.post(detail, {'patient': str(patient.pk), 'action': 'CORRECT',
        'expected_revision': row['revision_number'], 'expected_source': row['current_source_token'],
        'checked_original': 'on', 'label': '胃癌', 'profile': '',
        'assertion': row['content']['assertion'], 'subject': row['content']['subject'],
        'reason': '对照合成原件明确本次携带标签，完整原稿保留'})
    assert response.status_code == 303
    updated, = candidate_rows(patient)
    assert updated['id'] == row['id'] and updated['content']['profile'] is None
    assert CancerCandidate.objects.get(pk=row['id']).original_data == original
    snapshot = build_snapshot(patient, selected_body(patient, cancer_candidate_ids=[row['id']]))
    item, = read_structured_data(json_bytes(snapshot))['cancer_candidates']
    assert item['label'] == '胃癌' and item['value_origin'] == 'MANUAL_CORRECTION'
    assert item['source']['state'] == 'OMITTED'
    records = list(csv.DictReader(io.StringIO(csv_tables(snapshot)['cancer_candidates.csv'].decode('utf-8-sig'))))
    assert records[0]['label'] == '胃癌'
    pdf_text = ''.join(page.extract_text() for page in PdfReader(io.BytesIO(render_pdf(snapshot))).pages)
    assert '胃癌' in pdf_text and '未经选择的详细上下文甲乙丙' not in pdf_text
    assert '未经选择的详细上下文甲乙丙' not in json_bytes(snapshot).decode()
    from apps.cancer_ordering.output import share_material
    shared = share_material(snapshot, {'sections': ['cancer_ordering'],
        'cancer_candidate_ids': [row['id']], 'include_indicator_ordering': False},
        {'documents': [], 'facts': [], 'clinical_fields': []})
    assert shared['cancer_candidates'][0]['label'] == '胃癌'
    assert '未经选择的详细上下文甲乙丙' not in json.dumps(shared, ensure_ascii=False)


def test_pre_fix_snapshot_version_is_invalidated(django_user_model):
    from apps.cancer_ordering.exporting import assert_current
    _, patient = _patient(django_user_model, 'old-label-policy')
    _collect(patient)
    row, = candidate_rows(patient)
    snapshot = build_snapshot(patient, selected_body(patient, cancer_candidate_ids=[row['id']]))
    snapshot['cancer_ordering_version'] = 'cancer-selected-output-1'
    with pytest.raises(SnapshotChanged):
        assert_current(patient, snapshot)
