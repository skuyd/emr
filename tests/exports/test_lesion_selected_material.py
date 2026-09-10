"""Selected stable identities must never expand the actual selected fields."""
from copy import deepcopy
import json

import pytest

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.facts.readmodels import effective_fact
from apps.lesions.models import Lesion
from apps.lesions.readmodels import review_observations
from apps.lesions.services import match_observations, rename_lesion
from tests.facts.test_laterality_review_guards import fixture
from tests.facts.test_scoped_laterality import confirm
from tests.lesions.factories import imaging_observation
from tests.lesions.test_relationships import expectations


pytestmark = pytest.mark.django_db


def pair(user_model, name='selected-lesions'):
    patient, first_doc, first = imaging_observation(user_model, name=name, size='1.25', unit='cm', suv='3.2')
    _, second_doc, second = imaging_observation(user_model, patient=patient, day='2026-09-01', size='15', suv='4.1')
    rows = review_observations(patient, actor=patient.account)
    match_observations(patient, actor=patient.account, first_id=rows[0]['id'], second_id=rows[1]['id'],
                       expectations=expectations(rows), checked_original=True, name='=选定观察 <A>')
    return patient, (first_doc, second_doc), (first, second), Lesion.objects.get(patient=patient)


def ids(report, *keys):
    return [str(field.pk) for field in report.fields.filter(field_key__in=keys)]


def selection(documents, lesion, fields):
    return {'mode': 'documents', 'document_ids': [str(doc.pk) for doc in documents],
            'lesion_ids': [str(lesion.pk)], 'clinical_field_ids': fields, 'sections': ['imaging'], 'details': True}


def test_no_lesion_opt_in_does_not_add_graph_rows_or_change_old_arrays(django_user_model):
    patient, documents, reports, lesion = pair(django_user_model)
    scope = selection(documents, lesion, ids(reports[0], 'lesion.site', 'lesion.dimensions'))
    plain = build_snapshot(patient, {k: v for k, v in scope.items() if k != 'lesion_ids'})
    opted = build_snapshot(patient, scope)
    assert all(plain[key] == [] for key in ('lesions', 'lesion_observations', 'lesion_measurements'))
    assert opted['lesions']
    for key in ('clinical_reports', 'clinical_fields', 'clinical_field_sources', 'facts', 'labs', 'documents'):
        assert plain[key] == opted[key]


def test_selected_measurement_copies_only_selected_context_and_preserves_exact_number(django_user_model):
    patient, documents, reports, lesion = pair(django_user_model, 'selected-field-intersection')
    chosen = ids(reports[0], 'lesion.site', 'lesion.dimensions')
    snapshot = build_snapshot(patient, selection(documents, lesion, chosen))
    assert len(snapshot['lesions']) == len(snapshot['lesion_observations']) == len(snapshot['lesion_measurements']) == 1
    observation = snapshot['lesion_observations'][0]
    point = snapshot['lesion_measurements'][0]
    assert set(observation['field_ids']) == set(chosen)
    assert observation['report_id'] == str(reports[0].pk)
    assert point['date_value'] is None and point['date_reliable'] is False and point['method'] == ['', '']
    assert point['raw_value'] == '1.25' and point['raw_unit'] == 'cm' and point['value'] == '12.5'
    assert point['conversion'] == {'factor': '10', 'from': 'cm', 'to': 'mm', 'rule': 'metric_length_cm_to_mm_v1'}
    assert point['report_maximum'] is False and point['context_field_ids'] == []
    public = json.dumps({key: snapshot[key] for key in ('lesions', 'lesion_observations', 'lesion_measurements')}, ensure_ascii=False)
    assert str(reports[1].pk) not in public and '2026-08-01' not in public and '3.2' not in public
    assert 'source_binding' not in public and 'operation_id' not in public and 'hidden_count' not in public


def test_context_is_selected_by_field_uuid_not_same_entity_or_report(django_user_model):
    patient, documents, reports, lesion = pair(django_user_model, 'selected-actual-context')
    chosen = ids(reports[0], 'lesion.site', 'lesion.dimensions') + ids(reports[1], 'report.exam_date', 'imaging.modality')
    snapshot = build_snapshot(patient, selection(documents, lesion, chosen))
    assert snapshot['lesion_measurements'][0]['date_value'] is None
    assert snapshot['lesion_measurements'][0]['method'] == ['', '']
    chosen += ids(reports[0], 'report.exam_date', 'imaging.modality')
    complete = build_snapshot(patient, selection(documents, lesion, chosen))
    point = complete['lesion_measurements'][0]
    assert point['date_value'] == '2026-08-01' and point['date_reliable']
    assert point['method'][0] == 'CT'
    assert set(point['context_field_ids']) == set(ids(reports[0], 'report.exam_date', 'imaging.modality'))


@pytest.mark.parametrize('keys', [(), ('lesion.dimensions',), ('report.exam_date',)])
def test_identity_never_implicitly_selects_its_site_or_other_report(django_user_model, keys):
    patient, documents, reports, lesion = pair(django_user_model, 'selected-no-site')
    with pytest.raises(ExportInputError):
        build_snapshot(patient, selection(documents, lesion, ids(reports[0], *keys)))


@pytest.mark.parametrize('change', ['rename', 'hidden_field', 'assignment'])
def test_full_private_dependencies_invalidate_filtered_material(django_user_model, change):
    from apps.lesions.services import unlink_observations

    patient, documents, reports, lesion = pair(django_user_model, 'selected-hidden-dependencies')
    scope = selection(documents[:1], lesion, ids(reports[0], 'lesion.site', 'lesion.dimensions'))
    snapshot = build_snapshot(patient, scope)
    if change == 'rename':
        rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk,
                      expected_revision=lesion.revision_number, name='人工更名')
    elif change == 'hidden_field':
        confirm(patient, reports[1].fields.get(field_key='lesion.suvmax'), action='REVOKE')
    else:
        rows = [row for row in review_observations(patient, actor=patient.account) if row['report_id'] == str(reports[1].pk)]
        unlink_observations(patient, actor=patient.account, lesion_id=lesion.pk,
                            expected_revision=lesion.revision_number, observation_ids=[rows[0]['id']], expectations=expectations(rows))
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)


def test_only_scoped_qualifier_exports_own_members_and_invalidates_on_hidden_parent(django_user_model):
    patient, report, parent, child = fixture(django_user_model)
    document = report.document
    confirm(patient, parent)
    confirm(patient, child)
    snapshot = build_snapshot(patient, {'mode': 'documents', 'document_ids': [str(document.pk)],
                                        'clinical_field_ids': [str(child.pk)], 'sections': ['imaging']})
    field = snapshot['clinical_fields'][0]
    assert field['content']['value']['scope'] == 'NAMED_MEMBERS_ONLY'
    assert field['laterality_scope'] == {'scope_state': 'NAMED_MEMBERS_ONLY', 'parent_selected': False, 'parent_field_id': None}
    public = json.dumps({key: snapshot[key] for key in ('clinical_fields', 'clinical_field_sources', 'clinical_reports')}, ensure_ascii=False)
    assert '纵隔' not in public and '双肺门' in public
    assert 'parent_snapshot' not in public and 'current_source_token' not in public
    confirm(patient, parent, action='REVOKE')
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)


def test_legacy_laterality_stays_unknown_in_output_without_rewriting_original_value(django_user_model):
    patient, document, report = imaging_observation(django_user_model, name='selected-legacy-scope', confirmed=False)
    side = report.fields.get(field_key='lesion.laterality')
    # Synthetic legacy record predates scope bindings; preserve its complete original content.
    original = deepcopy(side.automatic_content)
    side.laterality_scope_binding.delete()
    confirm(patient, side)
    current = effective_fact(side)
    assert current['usable'] and current['laterality_scope']['scope_state'] == 'UNKNOWN_SCOPE'
    snapshot = build_snapshot(patient, {'mode': 'documents', 'document_ids': [str(document.pk)],
                                        'clinical_field_ids': [str(side.pk)]})
    assert snapshot['clinical_fields'][0]['laterality_scope']['scope_state'] == 'UNKNOWN_SCOPE'
    side.refresh_from_db()
    assert side.automatic_content == original


def test_omitted_middle_measurement_never_becomes_a_new_selected_baseline(django_user_model):
    patient, documents, reports, lesion = pair(django_user_model, 'selected-middle-gap')
    _, third_doc, third = imaging_observation(django_user_model, patient=patient, day='2026-10-01', size='18', suv='5.0')
    rows = review_observations(patient, actor=patient.account)
    first = next(row for row in rows if row['report_id'] == str(reports[0].pk))
    last = next(row for row in rows if row['report_id'] == str(third.pk))
    match_observations(patient, actor=patient.account, first_id=first['id'], second_id=last['id'],
                       expectations=expectations([first, last]), checked_original=True, target_lesion_id=lesion.pk,
                       expected_lesion_revisions={str(lesion.pk): lesion.revision_number})
    chosen = [identity for report in (reports[0], third) for identity in ids(report, 'lesion.site', 'lesion.dimensions',
                                                                           'report.exam_date', 'imaging.modality')]
    snapshot = build_snapshot(patient, selection([documents[0], third_doc], lesion, chosen))
    point = next(row for row in snapshot['lesion_measurements'] if row['report_id'] == str(third.pk))
    assert point['date_reliable'] and point['value'] == '18'
    assert point['comparison']['previous_id'] is None and point['comparison']['delta'] is None
    assert point['comparison']['comparable'] is False
    assert str(reports[1].pk) not in json.dumps(snapshot['lesion_measurements'])


def test_wrong_actual_report_foreign_key_cannot_create_a_new_output(django_user_model):
    from apps.lesions.models import LesionObservation

    patient, documents, reports, lesion = pair(django_user_model, 'selected-original-report-fk')
    LesionObservation.objects.filter(report=reports[0]).update(report=reports[1])
    with pytest.raises(ExportInputError):
        build_snapshot(patient, selection(documents, lesion, ids(reports[0], 'lesion.site', 'lesion.dimensions')))
