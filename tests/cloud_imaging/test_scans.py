from uuid import uuid4

from django.core.exceptions import PermissionDenied
import pytest

from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from .factories import stored_document
from .test_decoding import URL


pytestmark = pytest.mark.django_db


def queued(patient, document, *, actor=None, dispatch=None):
    from apps.cloud_imaging.readmodels import document_snapshot
    from apps.cloud_imaging.scan_services import request_scan

    actor = actor or patient.account
    current = document_snapshot(patient, actor=actor, document_id=document.pk)
    return request_scan(patient, actor=actor, document_id=document.pk, expected_source=current['input_token'],
                        operation_id=uuid4(), dispatch=dispatch or (lambda _: None))


@pytest.mark.parametrize('kind', ['image', 'text_pdf', 'exif_jpeg'])
def test_actual_private_original_scan_publishes_pending_qr_with_separate_proof(django_user_model, kind):
    from apps.cloud_imaging.scan_services import run_scan

    _, patient = _patient(django_user_model, 'cloud-scan-' + kind)
    document, page, store = stored_document(patient, pdf=kind == 'text_pdf', exif=kind == 'exif_jpeg')
    scan = queued(patient, document)
    run_scan(scan.pk, store)
    scan.refresh_from_db()
    assert scan.status == 'SUCCEEDED' and scan.attempts == 1
    assert scan.summary['processed_pages'] == 1 and scan.summary['failed_pages'] == 0
    evidence = scan.evidence.get(kind='QR', payload_type='URL')
    assert evidence.payload == URL and evidence.document_page == page and evidence.document_sha256 == document.sha256
    assert evidence.input_sha256 and evidence.render_profile and evidence.transform
    assert evidence.ocr_block_id is None and evidence.start_offset is None and evidence.end_offset is None
    source = document.cloud_imaging_sources.get()
    assert source.status == 'PENDING' and source.current_url == URL and source.evidence == evidence
    assert source.created_by_id == patient.account_id and source.revisions.get().author_id == patient.account_id
    assert not document.facts.exists()
    run_scan(scan.pk, store)
    assert document.cloud_imaging_sources.count() == scan.evidence.count() == 1


def test_pending_input_and_completed_success_are_reused_without_rebinding_actor(django_user_model):
    from apps.cloud_imaging.scan_services import run_scan

    _, patient = _patient(django_user_model, 'cloud-reuse-owner')
    _, member = _patient(django_user_model, 'cloud-reuse-member')
    PatientMembership.objects.create(patient=patient, account=member.account, role='EDITOR')
    document, _, store = stored_document(patient)
    first = queued(patient, document, actor=member.account)
    second = queued(patient, document)
    assert second.pk == first.pk and second.requested_by_id == member.account_id
    run_scan(first.pk, store)
    third = queued(patient, document)
    assert third.pk == first.pk and document.cloud_imaging_scans.count() == 1


def test_ocr_uses_current_raw_unicode_slice_while_qr_keeps_no_text_coordinates(django_user_model):
    from apps.cloud_imaging.scan_services import run_scan

    _, patient = _patient(django_user_model, 'cloud-ocr-and-qr')
    document, page, store = stored_document(patient)
    _, version = parsed_facts(patient, ['云影像😀：' + URL + '。'], document=document)
    scan = queued(patient, document)
    run_scan(scan.pk, store)
    assert scan.evidence.count() == 2
    ocr = scan.evidence.get(kind='OCR')
    block = version.ocr_blocks.get()
    assert ocr.parsing_version == version and ocr.ocr_block == block and ocr.polygon == block.polygon
    assert block.text[ocr.start_offset:ocr.end_offset] == ocr.payload == URL
    assert ocr.document_page == page


@pytest.mark.parametrize('change', ['revoke', 'trash', 'parse'])
def test_scan_cannot_publish_after_actual_access_or_input_change(django_user_model, monkeypatch, change):
    from apps.cloud_imaging import scan_services
    from apps.documents.lifecycle import move_to_trash

    _, patient = _patient(django_user_model, 'cloud-fence-owner-' + change)
    _, member = _patient(django_user_model, 'cloud-fence-member-' + change)
    membership = PatientMembership.objects.create(patient=patient, account=member.account, role='EDITOR')
    document, _, store = stored_document(patient)
    scan = queued(patient, document, actor=member.account)
    real = scan_services.decode_page
    def build_then_change(*args, **kwargs):
        result = real(*args, **kwargs)
        if change == 'revoke':
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=membership.revision)
        elif change == 'trash':
            move_to_trash(patient, document.pk, actor=patient.account)
        else:
            parsed_facts(patient, ['新解析的合成资料'], document=document)
        return result
    monkeypatch.setattr(scan_services, 'decode_page', build_then_change)
    scan_services.run_scan(scan.pk, store)
    scan.refresh_from_db()
    assert scan.status == 'INVALIDATED'
    assert scan.evidence.count() == document.cloud_imaging_sources.count() == 0
    assert scan.error_code in {'access_changed', 'source_changed', 'document_unavailable'}


def test_unavailable_original_is_failed_and_new_retry_preserves_old_failure(django_user_model):
    from apps.cloud_imaging.scan_services import run_scan

    _, patient = _patient(django_user_model, 'cloud-io-failure')
    document, _, store = stored_document(patient)
    original = store.objects.pop(document.original_object_key)
    first = queued(patient, document)
    run_scan(first.pk, store)
    first.refresh_from_db()
    assert first.status == 'FAILED' and first.summary['unknown_pages'] == 1
    assert first.summary['no_qr_pages'] == 0 and first.evidence.count() == 0
    store.objects[document.original_object_key] = original
    second = queued(patient, document)
    assert second.pk != first.pk
    run_scan(second.pk, store)
    second.refresh_from_db()
    first.refresh_from_db()
    assert second.status == 'SUCCEEDED' and first.status == 'FAILED'


def test_viewer_cannot_queue_and_stale_worker_identity_is_not_restored_by_owner_retry(django_user_model):
    from apps.cloud_imaging.scan_services import run_scan

    _, patient = _patient(django_user_model, 'cloud-worker-owner')
    _, member = _patient(django_user_model, 'cloud-worker-member')
    membership = PatientMembership.objects.create(patient=patient, account=member.account, role='VIEWER')
    document, _, store = stored_document(patient)
    with pytest.raises(PermissionDenied):
        queued(patient, document, actor=member.account)
    membership.role = 'EDITOR'
    membership.save(update_fields=['role'])
    first = queued(patient, document, actor=member.account)
    change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=membership.revision)
    second = queued(patient, document)
    assert second.pk != first.pk and second.requested_by_id == patient.account_id
    run_scan(first.pk, store)
    assert first.evidence.count() == 0
    run_scan(second.pk, store)
    assert second.evidence.count() == 1
