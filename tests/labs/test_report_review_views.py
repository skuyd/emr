from datetime import date
import uuid

import pytest

from apps.labs.models import LabReportRevision, LabReportUnit, ReportAssociation, ReportAssociationEvent
from apps.labs.reports import effective_report, report_relations
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def test_identity_conflict_does_not_replace_missing_time_rejection_label(django_user_model):
    client, patient = _patient(django_user_model, 'report-rejected-conflict-label')
    report(patient)
    _, _, rejected = report(patient, at='2026-09-17', person='合成人乙')
    assert effective_report(rejected).status == 'REJECTED'
    response = client.get(f'/labs/reports/{rejected.pk}/')
    assert response.status_code == 200
    assert response.context['report_conflict']
    assert response.context['status'] == '不满足接纳条件'
    response = client.get('/labs/reports/')
    shown = next(item for item in response.context['reports'] if item['unit'].pk == rejected.pk)
    assert shown['status'] == '不满足接纳条件'


def test_report_detail_uses_effective_results_and_preserves_automatic_evidence(django_user_model):
    from apps.labs.revisions import revise_observation

    client, patient = _patient(django_user_model, 'report-detail-effective-result')
    _, row, unit = report(patient)
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': '7'}, expected_revision=0)
    response = client.get(f'/labs/reports/{unit.pk}/')
    assert response.status_code == 200
    shown, = response.context['observations']
    assert shown.raw_value == '7' and shown.value_origin == 'USER'
    assert '白细胞 · 白细胞计数：7' in response.content.decode()
    row.refresh_from_db()
    assert row.raw_value == '5'
    assert row.revisions.count() == 1


def test_report_review_does_not_show_deleted_donors_clock(django_user_model):
    from django.utils import timezone
    from tests.labs.test_report_readmodels import continuation_pair

    client, patient = _patient(django_user_model, 'report-ui-donor')
    documents, _, units = continuation_pair(patient)
    assert '2026-09-17 08:30' in client.get(f'/labs/reports/{units[1].pk}/').content.decode()
    documents[0].deleted_at = timezone.now()
    documents[0].save(update_fields=['deleted_at'])
    for path in ('/labs/reports/', f'/labs/reports/{units[1].pk}/'):
        response = client.get(path)
        assert response.status_code == 200
        assert '采样时间来源不可用或关联已撤销' in response.content.decode()
        identities = ([response.context['identity']] if 'identity' in response.context
                      else [report['identity'] for report in response.context['reports']])
        assert all(identity.sampled_at is None for identity in identities)


def test_report_review_lists_original_identity_and_all_sources(django_user_model):
    client, patient = _patient(django_user_model, 'report-ui-list')
    left_doc, _, left = report(patient)
    right_doc, _, right = report(patient)
    response = client.get('/labs/reports/')
    assert response.status_code == 200
    text = response.content.decode()
    for value in ('A100', '2026-09-17 08:30', '自动归并', str(left.pk), str(right.pk)):
        assert value in text
    detail = client.get(f'/labs/reports/{left.pk}/')
    assert detail.status_code == 200
    assert '框选' not in detail.content.decode()  # The form uses source-backed location choices.
    assert '原件位置' in detail.content.decode()
    assert str(left_doc.pk) in detail.content.decode()
    assert str(right_doc.pk) != str(left_doc.pk)


def test_missing_number_can_be_manually_related_then_undone_with_audit(django_user_model):
    client, patient = _patient(django_user_model, 'report-ui-manual')
    _, _, left = report(patient, number='')
    _, _, right = report(patient, number='')
    assert report_relations(patient) == ()
    start = client.post('/labs/reports/relate/', {'left': str(left.pk), 'right': str(right.pk)})
    assert start.status_code == 302
    association = ReportAssociation.objects.get()
    assert association.state == 'REVIEW'
    operation = str(uuid.uuid4())
    payload = {'action': 'SAME', 'expected_revision': association.revision_number,
               'rationale': '对照报告患者信息和版面，确认是同一份报告', 'operation_id': operation}
    path = f'/labs/report-relations/{association.pk}/'
    assert client.post(path, payload).status_code == 302
    assert client.post(path, payload).status_code == 302
    association.refresh_from_db()
    assert association.state == 'SAME'
    assert ReportAssociationEvent.objects.filter(action='SAME').count() == 1
    stale = {**payload, 'action': 'DIFFERENT', 'operation_id': str(uuid.uuid4())}
    assert client.post(path, stale).status_code == 409
    assert client.post(path, {'action': 'UNDO', 'expected_revision': association.revision_number,
        'rationale': '核对后撤销此关联', 'operation_id': str(uuid.uuid4())}).status_code == 302
    assert report_relations(patient)[0].state == 'UNDONE'


def test_report_time_correction_requires_original_location_and_rebuilds_effective_time(django_user_model):
    client, patient = _patient(django_user_model, 'report-ui-correct')
    _, row, unit = report(patient)
    path = f'/labs/reports/{unit.pk}/'
    payload = {'field': 'sampled_at', 'value': '2026-09-17 10:35:12', 'expected_revision': 0,
               'rationale': '核对本报告采样时间', 'operation_id': str(uuid.uuid4())}
    assert client.post(path, payload).status_code == 400
    assert not LabReportRevision.objects.exists()
    payload['source_index'] = '0'
    payload['expected_source'] = client.get(path).context['source_token']
    assert client.post(path, payload).status_code == 302
    assert client.post(path, payload).status_code == 302
    unit.refresh_from_db()
    assert effective_report(unit).sampling_label == '2026-09-17 10:35:12'
    assert unit.automatic['sampled_at'] == '2026-09-17 08:30:00'
    assert LabReportRevision.objects.count() == 1
    from apps.labs.readmodels import effective_rows
    assert effective_rows(patient)[0].report_identity.sampling_label == '2026-09-17 10:35:12'
    assert client.post(path, {**payload, 'value': '2026-09-18 10:35', 'operation_id': str(uuid.uuid4())}).status_code == 409


def test_historical_review_materializes_existing_evidence_without_ocr(django_user_model):
    client, patient = _patient(django_user_model, 'report-ui-history')
    _, row = _observation(patient, date(2026, 9, 17), '5', sampling_time=None)
    assert not LabReportUnit.objects.exists()
    response = client.get('/labs/reports/')
    assert response.status_code == 200
    unit = LabReportUnit.objects.get()
    assert effective_report(unit).status == 'REJECTED'
    assert '缺少时分' in response.content.decode()
    row.refresh_from_db()
    assert row.report_unit_id == unit.pk


def test_report_review_and_relations_do_not_cross_patient_boundaries(django_user_model):
    client, patient = _patient(django_user_model, 'report-ui-owner')
    outsider, other = _patient(django_user_model, 'report-ui-other')
    _, _, own = report(patient)
    _, _, foreign = report(other)
    assert outsider.get(f'/labs/reports/{own.pk}/').status_code == 404
    assert client.post('/labs/reports/relate/', {'left': own.pk, 'right': foreign.pk}).status_code == 404
    assert not ReportAssociation.objects.exists()
    assert 'A100' not in outsider.get('/labs/reports/', {'patient': str(patient.pk)}).content.decode()


def test_readonly_member_can_review_sources_but_cannot_change_reports(django_user_model):
    from apps.patients.models import PatientMembership
    from django.utils import timezone

    _, patient = _patient(django_user_model, 'report-ui-admin')
    viewer, other = _patient(django_user_model, 'report-ui-viewer')
    member = PatientMembership.objects.create(patient=patient, account=other.account, role='VIEWER')
    _, _, unit = report(patient)
    response = viewer.get(f'/labs/reports/{unit.pk}/')
    assert response.status_code == 200
    assert '保存原件核对与更正' not in response.content.decode()
    assert viewer.post(f'/labs/reports/{unit.pk}/', {'patient_id': patient.pk}).status_code == 403
    member.revoked_at = timezone.now()
    member.save(update_fields=['revoked_at'])
    assert viewer.get(f'/labs/reports/{unit.pk}/').status_code == 404


def test_report_mutations_require_csrf_and_original_current_version(django_user_model):
    from django.test import Client

    _, patient = _patient(django_user_model, 'report-ui-csrf')
    _, _, unit = report(patient)
    client = Client(enforce_csrf_checks=True)
    client.force_login(patient.account)
    assert client.post(f'/labs/reports/{unit.pk}/', {'field': 'institution', 'value': '医院'}).status_code == 403
    unit.parsing_version.active = False
    unit.parsing_version.save(update_fields=['active'])
    assert client.get(f'/labs/reports/{unit.pk}/').status_code == 404
    assert not LabReportRevision.objects.exists()
