from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
import pytest

from apps.cloud_imaging import scan_services
from apps.cloud_imaging.models import CloudImagingScan
from apps.cloud_imaging.readmodels import source_details
from apps.cloud_imaging.services import revise_source
from tests.documents.test_detail_viewer import _patient
from .factories import stored_document
from .test_scans import queued


pytestmark = pytest.mark.django_db(transaction=True)


def setup_scan(django_user_model, suffix):
    _, patient = _patient(django_user_model, 'cloud-recovery-' + suffix)
    document, _, store = stored_document(patient)
    return patient, document, store, queued(patient, document)


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_existing_local_worker_drains_durable_cloud_work_without_a_broker(django_user_model, monkeypatch):
    from apps.documents import backends
    from apps.processing import tasks

    _, document, store, scan = setup_scan(django_user_model, 'local')
    monkeypatch.setattr(backends, 'get_object_store', lambda: store)
    def no_ocr():
        pytest.fail('Cloud-only work must not initialize the OCR pipeline')
    monkeypatch.setattr(tasks, 'get_processing_pipeline', no_ocr)
    output = StringIO()
    call_command('run_local_processing_worker', once=True, stdout=output, verbosity=0)
    scan.refresh_from_db()
    assert scan.status == 'SUCCEEDED' and document.cloud_imaging_sources.count() == 1
    assert 'SYNTHETIC_QR' not in output.getvalue()


def test_expired_worker_keeps_all_pages_unknown_and_cannot_publish(django_user_model, monkeypatch):
    _, document, store, scan = setup_scan(django_user_model, 'expired')
    original = scan_services.decode_page
    def after_decoding(*args, **kwargs):
        result = original(*args, **kwargs)
        CloudImagingScan.objects.filter(pk=scan.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        return result
    monkeypatch.setattr(scan_services, 'decode_page', after_decoding)
    scan_services.run_scan(scan.pk, store)
    scan.refresh_from_db()
    assert scan.status == 'FAILED' and scan.error_code == 'lease_expired'
    assert scan.summary.get('unknown_pages') == document.page_count
    assert scan.summary['no_qr_pages'] == 0 and not scan.evidence.exists()


def test_recovery_expires_abandoned_lease_and_dispatches_queued_without_erasing_history(django_user_model):
    patient, document, _, scan = setup_scan(django_user_model, 'abandoned')
    scan.status, scan.lease_token = 'RUNNING', uuid4()
    scan.lease_expires_at = timezone.now() - timedelta(seconds=2)
    scan.save()
    deliveries = []
    scan_services.recover_scans(dispatch=deliveries.append)
    scan.refresh_from_db()
    assert deliveries == [] and scan.status == 'FAILED'
    assert scan.summary.get('unknown_pages') == document.page_count
    second = queued(patient, document)
    scan_services.recover_scans(dispatch=deliveries.append)
    assert deliveries == [second.pk] and second.pk != scan.pk


def test_rule_upgrade_invalidates_confirmed_machine_evidence(django_user_model, monkeypatch):
    from apps.cloud_imaging import decoding

    patient, document, store, scan = setup_scan(django_user_model, 'rule')
    scan_services.run_scan(scan.pk, store)
    source = document.cloud_imaging_sources.get()
    initial = source_details(patient, actor=patient.account, source_id=source.pk)
    revise_source(patient, actor=patient.account, source_id=source.pk, action='CONFIRM',
        expected_revision=initial['revision_number'], expected_source=initial['source_token'],
        operation_id=uuid4(), checked_original=True)
    confirmed = source_details(patient, actor=patient.account, source_id=source.pk)
    assert confirmed['usable']
    monkeypatch.setattr(decoding, 'RULES_VERSION', 'cloud-local-scan-upgrade-synthetic')
    current = source_details(patient, actor=patient.account, source_id=source.pk)
    assert not current['usable'] and not current['source_valid'] and current['status'] == 'STALE'
    assert current['source_token'] != confirmed['source_token']
