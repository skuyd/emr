import pytest

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import SnapshotChanged
from apps.labs.reports import correct_report
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


def test_report_clock_correction_invalidates_export_even_with_unchanged_date(django_user_model):
    _, patient = _patient(django_user_model, 'report-output-revision')
    _, _, unit = report(patient)
    snapshot = build_snapshot(patient, {'mode': 'all'})
    correct_report(patient, patient.account, unit.pk, {'sampled_at': '2026-09-17 10:30'},
        expected_revision=0, source_evidence={'page_number': 1, 'polygon': [[.1, .1], [.9, .1], [.9, .2], [.1, .2]]},
        rationale='按原件更正采样时刻', operation_id='output-clock')
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)


def test_export_carries_current_report_identity_and_sampling_precision(django_user_model):
    _, patient = _patient(django_user_model, 'report-output-identity')
    _, _, unit = report(patient, at='2026-09-17 08:30:12')
    correct_report(patient, patient.account, unit.pk, {'institution': '核对后的合成医院'},
        expected_revision=0, source_evidence={'page_number': 1, 'polygon': [[.1, .01], [.9, .01], [.9, .02], [.1, .02]]},
        rationale='核对原件医院', operation_id='output-hospital')
    snapshot = build_snapshot(patient, {'mode': 'all'})
    lab, = snapshot['labs']
    assert lab['institution'] == '核对后的合成医院'
    assert lab['report']['sampling_time'] == '2026-09-17 08:30:12'
    assert lab['report']['precision'] == 'SECOND'
    assert lab['report']['number'] == 'A100'
    assert lab['report']['revision'] == 1


def test_fine_result_selection_cannot_keep_unselected_main_report_time(django_user_model):
    from tests.labs.test_report_readmodels import continuation_pair

    _, patient = _patient(django_user_model, 'report-output-fine-scope')
    _, rows, _ = continuation_pair(patient)
    snapshot = build_snapshot(patient, {'mode': 'all', 'observation_ids': [str(rows[1].pk)]})
    assert snapshot['labs'] == []


def test_share_projection_excludes_continuation_when_its_donor_is_not_shared(django_user_model):
    from apps.patients.sharing_content import project_snapshot
    from tests.labs.test_report_readmodels import continuation_pair

    _, patient = _patient(django_user_model, 'report-share-fine-scope')
    documents, rows, _ = continuation_pair(patient)
    snapshot = build_snapshot(patient, {'mode': 'all'})
    shared = project_snapshot(snapshot, {'document_ids': [str(document.pk) for document in documents],
        'sections': ['labs'], 'lab_ids': [str(rows[1].pk)]})
    assert shared['labs'] == []


def test_output_groups_equal_results_by_date_hospital_and_keeps_all_sources(django_user_model):
    _, patient = _patient(django_user_model, 'report-output-fold')
    _, first, _ = report(patient, number='A1', value='5.0')
    _, second, _ = report(patient, number='A2', at='2026-09-17 10:30', value='5.00')
    _, third, _ = report(patient, number='A3', at='2026-09-17 11:30', value='6')
    first.reference_range_raw = '4-10'
    first.save(update_fields=['reference_range_raw'])
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    assert len(snapshot['labs']) == 3
    assert len(snapshot['lab_columns']) == 1
    assert len(snapshot['lab_results']) == 2
    folded = next(result for result in snapshot['lab_results'] if str(first.pk) in result['source_ids'])
    assert set(folded['source_ids']) == {str(first.pk), str(second.pk)}
    assert folded['latest_sampling_time'] == '2026-09-17 10:30'
    assert folded['reference_difference']
    assert folded['reference_label'] == '参考信息有差异'
    assert folded['report_count'] == folded['image_count'] == 2
    assert set(snapshot['card']['detail_lab_ids']) == {str(first.pk), str(second.pk), str(third.pk)}


def test_share_refolds_only_selected_sources_and_recomputes_counts_and_references(django_user_model):
    from apps.patients.sharing_content import project_snapshot

    _, patient = _patient(django_user_model, 'report-share-refold')
    left_doc, first, _ = report(patient, number='A1', value='5')
    right_doc, second, _ = report(patient, number='A2', at='2026-09-17 10:30', value='5.0')
    first.reference_range_raw = '4-10'
    first.save(update_fields=['reference_range_raw'])
    snapshot = build_snapshot(patient, {'mode': 'all'})
    shared = project_snapshot(snapshot, {'document_ids': [str(left_doc.pk), str(right_doc.pk)],
        'sections': ['labs'], 'lab_ids': [str(first.pk)]})
    result, = shared['lab_results']
    assert result['source_ids'] == [str(first.pk)]
    assert result['latest_sampling_time'] == '2026-09-17 08:30'
    assert not result['reference_difference']
    assert result['report_count'] == result['image_count'] == 1
    assert str(second.pk) not in str(shared['lab_results'])


def test_report_conflict_reference_label_matches_comparison_and_recomputes_in_share(django_user_model):
    from apps.labs.comparison import comparison_view
    from apps.patients.sharing_content import project_snapshot

    _, patient = _patient(django_user_model, 'report-reference-conflict-scope')
    left_doc, first, _ = report(patient)
    _, second, _ = report(patient, person='合成人乙')
    for row in (first, second):
        row.reference_range_raw = '4-10'
        row.save(update_fields=['reference_range_raw'])
    table = comparison_view(patient)
    assert {cell.reference_label for row in table.rows for column in row.cells for cell in column} == {'无法对照'}
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    assert all(result['disputed'] for result in snapshot['lab_results'])
    assert {result['reference_label'] for result in snapshot['lab_results']} == {'无法对照'}
    shared = project_snapshot(snapshot, {'document_ids': [str(left_doc.pk)], 'sections': ['labs']})
    result, = shared['lab_results']
    assert not result['disputed'] and result['reference_label'] == '范围内'
    assert result['source_ids'] == [str(first.pk)]


def test_share_defaults_include_early_and_different_values_in_selected_documents(django_user_model):
    from apps.patients.sharing_content import project_snapshot

    _, patient = _patient(django_user_model, 'report-share-all-values')
    first_doc, first, _ = report(patient, at='2026-09-16 08:30', value='4')
    second_doc, second, _ = report(patient, at='2026-09-17 08:30', value='5')
    snapshot = build_snapshot(patient, {'mode': 'all'})
    shared = project_snapshot(snapshot, {'document_ids': [str(first_doc.pk), str(second_doc.pk)], 'sections': ['labs']})
    assert {row['id'] for row in shared['labs']} == {str(first.pk), str(second.pk)}


def test_static_card_folds_main_rows_and_prints_every_sampling_source(django_user_model):
    from apps.exports.pdf import card_sections

    _, patient = _patient(django_user_model, 'report-static-card')
    report(patient, number='A1', at='2026-09-16 08:30', value='5.0')
    report(patient, number='A2', at='2026-09-16 10:30', value='5.00')
    report(patient, number='A3', at='2026-09-17 11:30', value='6')
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    sections = card_sections(snapshot)
    section = next(item for item in sections if item['key'] == 'labs')
    assert len([entry for entry in section['entries'] if 'cells' in entry]) == 2
    text = str(section)
    for value in ('2026-09-16 08:30', '2026-09-16 10:30', '2026-09-17 11:30', 'A1', 'A2', 'A3'):
        assert value in text


def test_grouped_static_and_shared_details_retain_other_field_source_pages(django_user_model):
    from datetime import date
    from apps.exports.lab_output import lab_sections
    from apps.exports.pdf import card_sections
    from tests.labs.test_trends import _observation

    _, patient = _patient(django_user_model, 'report-field-pages')
    _, row = _observation(patient, date(2026, 9, 17), '5', page_count=2)
    row.field_evidence['method_raw'] = {'page_number': 2, 'polygon': None, 'precision': 'page'}
    row.save(update_fields=['field_evidence'])
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True, 'sections': ['labs']})
    sources = lab_sections(snapshot)[0]['results'][0]['sources']
    assert sources[0]['other_field_pages'] == [2]
    text = str(next(item for item in card_sections(snapshot) if item['key'] == 'labs'))
    assert '字段来源另见' in text and '第 2 页' in text


def test_output_marks_latest_conflicts_and_rechecks_them_after_source_selection(django_user_model):
    from apps.exports.lab_output import project_lab_output

    _, patient = _patient(django_user_model, 'report-output-conflicts')
    report(patient, number='A1', at='2026-09-16 08:30', value='2')
    _, early, _ = report(patient, number='A2', at='2026-09-17 08:30', value='3')
    _, latest, _ = report(patient, number='A3', at='2026-09-17 10:30', value='4')
    _, conflicting, _ = report(patient, number='A4', at='2026-09-17 10:30', value='5')
    snapshot = build_snapshot(patient, {'mode': 'all'})
    assert {source for item in snapshot['lab_results'] if item['disputed'] for source in item['source_ids']} == {
        str(early.pk), str(latest.pk), str(conflicting.pk)}
    selected = [row for row in snapshot['labs'] if row['id'] != str(conflicting.pk)]
    local = project_lab_output(selected, snapshot['lab_results'], snapshot['lab_report_relations'])
    assert not any(item['disputed'] for item in local['lab_results'])


def test_card_trend_cannot_skip_latest_comparator_and_use_earlier_numeric_value(django_user_model):
    _, patient = _patient(django_user_model, 'report-output-comparator')
    report(patient, number='A1', at='2026-09-16 08:30', value='2')
    report(patient, number='A2', at='2026-09-17 08:30', value='3')
    _, last, _ = report(patient, number='A3', at='2026-09-17 10:30', value='<4')
    last.result_type = 'COMPARATOR'
    last.save(update_fields=['result_type'])
    report(patient, number='A4', at='2026-09-18 08:30', value='5')
    snapshot = build_snapshot(patient, {'mode': 'all'})
    points = [point for series in snapshot['card']['trends'] for point in series['points']]
    assert [point['value'] for point in points] == ['2', '5']


def test_undo_invalidates_frozen_report_count_without_unfolding_equal_results(django_user_model):
    from apps.labs.reports import report_relations, decide_relation

    _, patient = _patient(django_user_model, 'report-output-undo')
    report(patient)
    report(patient)
    snapshot = build_snapshot(patient, {'mode': 'all'})
    assert snapshot['lab_results'][0]['report_count'] == 1
    relation, = report_relations(patient)
    decide_relation(patient, patient.account, relation.pk, 'UNDO', expected_revision=relation.revision_number,
                    rationale='对照原件后撤销', operation_id='output-undo')
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)
    current = build_snapshot(patient, {'mode': 'all'})
    assert len(current['lab_results']) == 1
    assert current['lab_results'][0]['report_count'] == 2


def test_json_csv_and_rendered_pdf_preserve_fold_links_and_source_times(django_user_model):
    import csv
    import io
    import json
    from apps.exports.formats import json_bytes, csv_tables, read_structured_data
    from apps.exports.pdf import render_pdf
    from tests.exports.test_formats import _pdf

    _, patient = _patient(django_user_model, 'report-output-formats')
    report(patient, number='A1', at='2026-09-17 08:30', value='5.0')
    report(patient, number='A2', at='2026-09-17 10:30', value='5.00')
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    portable = read_structured_data(json_bytes(snapshot))
    result, = portable['lab_results']
    assert set(result['source_ids']) == {row['id'] for row in portable['labs']}
    tables = csv_tables(snapshot)
    records = list(csv.DictReader(io.StringIO(tables['lab_results.csv'].decode('utf-8-sig'))))
    assert len(records) == 1
    assert set(json.loads(records[0]['source_ids'])) == set(result['source_ids'])
    pdf = _pdf(render_pdf(snapshot), 'lab-report-consolidation.pdf')
    text = '\n'.join(page.extract_text() for page in pdf.pages)
    for value in ('2026-09-17 08:30', '2026-09-17 10:30', 'A1', 'A2', '2 条来源'):
        assert value in text


def test_shared_page_renders_source_details_after_token_exchange(django_user_model):
    from apps.patients.models import PatientShare
    from tests.patients.test_family_shares import create_link, exchange

    owner, patient = _patient(django_user_model, 'report-share-render-owner')
    viewer, _ = _patient(django_user_model, 'report-share-render-viewer')
    document, _, _ = report(patient, number='A1', at='2026-09-17 08:30', value='5.0')
    token = create_link(owner, patient, document, sections=['labs'])
    share_id = exchange(viewer, token)
    response = viewer.get(f'/shared/{share_id}/')
    assert response.status_code == 200
    text = response.content.decode()
    for value in ('检验结果与全部来源', '2026-09-17 08:30', '报告号 A1', '1 份报告', '1 张原图', '1 条展示结果'):
        assert value in text
    share = PatientShare.objects.get(pk=share_id)
    assert share.snapshot['lab_results'][0]['source_count'] == 1
