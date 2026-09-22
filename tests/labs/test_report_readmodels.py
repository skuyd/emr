from datetime import date, datetime

import pytest

from apps.labs.readmodels import effective_rows
from apps.labs.report_reads import attach_report_context
from apps.labs.report_identity import extract_report_units, resolve_continuation_times
from apps.labs.reports import persist_report_units, report_relations, decide_relation, correct_report, effective_report
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report
from tests.labs.test_report_identity import page
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def test_report_time_drives_effective_date_without_mutating_observation(django_user_model):
    _, patient = _patient(django_user_model, 'report-effective')
    _, row, _ = report(patient, at='2026-09-16 10:30')
    rows = attach_report_context(effective_rows(patient, include_uncertain=True))
    assert rows[0].report_identity.sampled_at == datetime(2026, 9, 16, 10, 30)
    assert rows[0].observation_date == date(2026, 9, 16)
    row.refresh_from_db()
    assert row.observation_date == date(2026, 9, 17)


def test_repeated_projection_cannot_clear_an_observation_date_conflict(django_user_model):
    from apps.exports.content import build_snapshot
    from apps.labs.revisions import revise_observation

    _, patient = _patient(django_user_model, 'report-repeated-date-conflict')
    _, row, _ = report(patient)
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'observation_date': '2026-09-18'},
                       expected_revision=0)
    effective, = effective_rows(patient)
    assert effective.report_identity.status == 'REVIEW'
    repeated, = attach_report_context((effective,))
    assert repeated.report_identity.status == 'REVIEW'
    snapshot = build_snapshot(patient, {'mode': 'all'})
    assert snapshot['labs'][0]['report']['status'] == 'REVIEW'
    assert snapshot['lab_results'][0]['disputed']


def test_report_hospital_correction_updates_archive_label_and_search(django_user_model):
    from apps.documents.archive import records_context

    client, patient = _patient(django_user_model, 'report-archive-hospital')
    document, _, unit = report(patient)
    correct_report(patient, patient.account, unit.pk, {'institution': '核对后的合成医院'},
        expected_revision=0, source_evidence={'page_number': 1, 'polygon': [[.1, .01], [.9, .01], [.9, .02], [.1, .02]]},
        rationale='核对原件医院', operation_id='archive-hospital')
    context = records_context(patient, {'q': '核对后的合成医院'})
    assert context['page_obj'].paginator.count == 1
    assert context['page_obj'].object_list[0].pk == document.pk
    response = client.get('/records/')
    assert '核对后的合成医院' in response.content.decode()


def test_historical_date_only_is_retained_but_ineligible(django_user_model):
    _, patient = _patient(django_user_model, 'report-old-date')
    _, row = _observation(patient, date(2026, 9, 17), '5', sampling_time=None)
    rows = attach_report_context(effective_rows(patient, include_uncertain=True, include_invalid=True))
    assert len(rows) == 1
    assert rows[0].report_identity.status == 'REJECTED'
    assert rows[0].report_identity.reason == 'sampling_time_missing'
    assert row.parsing_version.document.deleted_at is None


@pytest.mark.parametrize('sampling_text', ['采样时间：2026-09-17 10:30', '采样时间：2026-09-1710:30'])
def test_historical_complete_source_time_can_be_reused_without_rerunning_ocr(django_user_model, sampling_text):
    _, patient = _patient(django_user_model, 'report-old-time')
    _, row = _observation(patient, date(2026, 9, 17), '5')
    candidate = row.parsing_version.metadata_candidates.get(kind='DOCUMENT_DATE')
    candidate.evidence.source_text = sampling_text
    candidate.evidence.save(update_fields=['source_text'])
    candidate.raw_text = candidate.evidence.source_text
    candidate.save(update_fields=['raw_text'])
    rows = attach_report_context(effective_rows(patient, include_uncertain=True))
    assert rows[0].report_identity.sampled_at == datetime(2026, 9, 17, 10, 30)
    assert rows[0].report_identity.status == 'ACCEPTED'
    assert not rows[0].report_identity.identity_reliable
    from apps.labs.comparison import comparison_view
    view = comparison_view(patient)
    assert view.result_count == 1
    assert not view.pending_sources


def test_report_date_metadata_cannot_be_reinterpreted_as_sampling_time(django_user_model):
    _, patient = _patient(django_user_model, 'report-old-report-date')
    _, row = _observation(patient, date(2026, 9, 17), '5')
    candidate = row.parsing_version.metadata_candidates.get(kind='DOCUMENT_DATE')
    candidate.evidence.source_text = '报告时间：2026-09-17 10:30'
    candidate.evidence.save(update_fields=['source_text'])
    candidate.raw_text = candidate.evidence.source_text
    candidate.save(update_fields=['raw_text'])
    rows = attach_report_context(effective_rows(patient, include_uncertain=True, include_invalid=True))
    assert rows[0].report_identity.status == 'REJECTED'
    assert rows[0].report_identity.sampled_at is None


def continuation_pair(patient):
    documents, rows, identities = [], [], []
    for index in (1, 2):
        document, row = _observation(patient, date(2026, 9, 17), '5')
        identity = extract_report_units((page('合成医院', '检验报告', '报告号：A100', '姓名：合成人甲',
            '采样时间：2026-09-17 08:30' if index == 1 else '续页', f'第{index}页 共2页'),))[0]
        documents.append(document)
        rows.append(row)
        identities.append((f'{document.pk}:1:1', identity))
    resolved = resolve_continuation_times(identities)
    assert all(identity.status == 'ACCEPTED' for identity in resolved.values())
    assert resolved[identities[1][0]].time_source == identities[0][0]
    units = [persist_report_units(row.parsing_version, (resolved[key],))[0]
             for row, (key, _) in zip(rows, identities)]
    return documents, rows, units


def test_continuation_loses_deleted_time_source_and_rechecks_on_restore(django_user_model):
    from django.utils import timezone
    _, patient = _patient(django_user_model, 'continuation-deleted')
    documents, _, units = continuation_pair(patient)
    assert len(effective_rows(patient)) == 2
    original = units[1].automatic.copy()
    documents[0].deleted_at = timezone.now()
    documents[0].save(update_fields=['deleted_at'])
    assert effective_rows(patient) == ()
    pending, = effective_rows(patient, include_invalid=True)
    assert pending.report_identity.sampled_at is None
    assert pending.report_identity.reason == 'sampling_source_unavailable'
    units[1].refresh_from_db()
    assert units[1].automatic == original
    documents[0].deleted_at = None
    documents[0].save(update_fields=['deleted_at'])
    assert len(effective_rows(patient)) == 2


@pytest.mark.parametrize('action', ['UNDO', 'DIFFERENT'])
def test_continuation_cannot_borrow_time_after_relation_is_removed(django_user_model, action):
    _, patient = _patient(django_user_model, 'continuation-removed')
    _, _, units = continuation_pair(patient)
    relation, = report_relations(patient)
    decide_relation(patient, patient.account, relation.pk, action, expected_revision=relation.revision_number,
                    rationale='核对原件后取消关联', operation_id='cancel-time-source')
    rows = effective_rows(patient)
    assert [row.report_unit_id for row in rows] == [units[0].pk]
    assert report_relations(patient)[0].state == ('UNDONE' if action == 'UNDO' else 'DIFFERENT')


def test_corrected_donor_requires_relation_review_before_reusing_its_time(django_user_model):
    _, patient = _patient(django_user_model, 'continuation-changed')
    _, _, units = continuation_pair(patient)
    report_relations(patient)
    correct_report(patient, patient.account, units[0].pk, {'sampled_at': '2026-09-17 10:30'},
        expected_revision=0, source_evidence={'page_number': 1, 'polygon': [[.1, .1], [.9, .1], [.9, .2], [.1, .2]]},
        rationale='核对主报告原件时间', operation_id='donor-time')
    pending = next(row for row in effective_rows(patient) if row.report_unit_id == units[1].pk)
    assert pending.report_identity.status == 'REVIEW'
    assert pending.report_identity.sampled_at is None
    relation, = report_relations(patient)
    assert relation.state == 'REVIEW'
    decide_relation(patient, patient.account, relation.pk, 'SAME', expected_revision=relation.revision_number,
                    rationale='核对续页仍属此主报告', operation_id='confirm-new-time')
    continued = next(row for row in effective_rows(patient) if row.report_unit_id == units[1].pk)
    assert continued.report_identity.status == 'ACCEPTED'
    assert continued.report_identity.sampled_at == datetime(2026, 9, 17, 10, 30)
    assert not continued.report_conflict
    assert effective_report(units[1]).sampled_at == datetime(2026, 9, 17, 8, 30)


def test_selected_continuation_cannot_expose_unselected_donor_time(django_user_model):
    _, patient = _patient(django_user_model, 'continuation-selection')
    _, _, units = continuation_pair(patient)
    rows = [row for row in effective_rows(patient) if row.report_unit_id == units[1].pk]
    scoped, = attach_report_context(rows, allowed_source_keys={units[1].source_key})
    assert scoped.report_identity.status == 'REJECTED'
    assert scoped.report_identity.sampled_at is None
    assert scoped.report_identity.time_source == ''


def test_export_material_excludes_borrowed_time_outside_selected_documents(django_user_model):
    from apps.exports.content import _material
    _, patient = _patient(django_user_model, 'continuation-export')
    documents, _, _ = continuation_pair(patient)
    _, _, observations, labs, sources = _material(patient, [str(documents[1].pk)])
    assert observations == [] and labs == [] and sources == []
    _, _, observations, labs, _ = _material(patient, [str(document.pk) for document in documents])
    assert len(observations) == len(labs) == 2


@pytest.mark.parametrize('confidence,eligible', [(0.99, True), (0.92, False)])
def test_continuation_trend_uses_current_donors_sampling_confidence(django_user_model, confidence, eligible):
    from apps.labs.comparison import comparable_cell

    _, patient = _patient(django_user_model, 'continuation-confidence')
    _, _, units = continuation_pair(patient)
    units[0].automatic['fields']['sampled_at'][0]['confidence'] = confidence
    units[0].save(update_fields=['automatic'])
    continued = next(row for row in effective_rows(patient) if row.report_unit_id == units[1].pk)
    assert continued.report_identity.status == 'ACCEPTED'
    assert comparable_cell(continued).trend_eligible is eligible
