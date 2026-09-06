from datetime import timedelta
import uuid

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.utils import timezone
import pytest

from apps.documents.models import DocumentStatus, PatientUploadQuota, ProcessingRun, ProcessingStage
from apps.labs.dictionary import current_dictionary, default_dictionary
from apps.operations.models import DictionaryRelease, SupportAccessGrant
from apps.operations.permissions import Role, provision_role_groups
from apps.operations.services import (
    QuotaLimits,
    activate_parsing_version,
    change_patient_quota,
    grant_support_access,
    publish_dictionary,
    requeue_processing,
    support_metadata_summary,
)
from apps.patients.models import Patient
from apps.processing.models import ParsingVersion, ParsingVersionStatus
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def staff(django_user_model, role):
    account = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2,
        phone_encrypted="ciphertext",
        is_staff=True,
    )
    provision_role_groups()
    account.groups.add(Group.objects.get(name=role.value))
    return account


def _withdraw_operator_authority(operator, withdrawal):
    if withdrawal == "role":
        operator.groups.clear()
    else:
        type(operator).objects.filter(pk=operator.pk).update(**{withdrawal: False})


@pytest.mark.parametrize("withdrawal", ["is_staff", "role", "is_active"])
def test_bundled_dictionary_publication_rechecks_authority_after_lock(django_user_model, monkeypatch, withdrawal):
    from apps.labs import dictionary_workflow

    manager = staff(django_user_model, Role.DICTIONARY_MANAGER)
    real_lock = dictionary_workflow._publication_lock

    def revoke_after_lock():
        real_lock()
        _withdraw_operator_authority(manager, withdrawal)

    monkeypatch.setattr(dictionary_workflow, "_publication_lock", revoke_after_lock)
    with pytest.raises(PermissionDenied):
        publish_dictionary(manager, "v1.0.0.json", reason_code="validated_release", totp_verified_at=timezone.now())
    assert not DictionaryRelease.objects.exists()


@pytest.mark.parametrize("withdrawal", ["is_staff", "role", "is_active"])
def test_parsing_activation_rechecks_authority_after_lock(django_user_model, monkeypatch, withdrawal):
    from datetime import date
    from apps.operations import services
    from tests.labs.test_trends import _observation

    operator = staff(django_user_model, Role.PROCESSOR_OPERATOR)
    _client, patient = _patient(django_user_model, "revoked-activation")
    document, observation = _observation(patient, date(2026, 8, 20), "5.2")
    run = ProcessingRun.objects.create(
        document=document, parser_version="parser-new", task_type="REPARSE",
        idempotency_key=f"{document.pk}:new", attempt_number=2,
        stage=ProcessingStage.SUCCEEDED, finished_at=timezone.now(),
    )
    target = ParsingVersion.objects.create(
        document=document, processing_run=run, parser_version="parser-new", ocr_provider="fixture",
        ocr_provider_version="2", status=ParsingVersionStatus.READY,
    )
    real_lock = services.lock_document_aggregate

    def revoke_after_lock(*args, **kwargs):
        locked = real_lock(*args, **kwargs)
        _withdraw_operator_authority(operator, withdrawal)
        return locked

    monkeypatch.setattr(services, "lock_document_aggregate", revoke_after_lock)
    with pytest.raises(PermissionDenied):
        activate_parsing_version(operator, target.pk, reason_code="validated_rollback", totp_verified_at=timezone.now())
    target.refresh_from_db()
    run.refresh_from_db()
    assert target.status == ParsingVersionStatus.READY
    assert target.active is False
    assert run.is_current is False
    assert list(document.parsing_versions.filter(active=True).values_list("pk", flat=True)) == [observation.parsing_version_id]


def test_processor_can_requeue_failed_run_without_receiving_document_content(
    django_user_model, django_capture_on_commit_callbacks
):
    operator = staff(django_user_model, Role.PROCESSOR_OPERATOR)
    _client, patient = _patient(django_user_model, "2")
    document, _version = _document(patient, content_type="image/png", page_count=1)
    ProcessingRun.objects.filter(document=document).delete()
    document.status = DocumentStatus.PROCESSING_FAILED
    document.save(update_fields=["status", "updated_at"])
    failed = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v1",
        task_type="INITIAL",
        idempotency_key=f"{document.pk}:failed",
        attempt_number=1,
        stage=ProcessingStage.FAILED,
        finished_at=timezone.now(),
    )
    dispatched = []

    with django_capture_on_commit_callbacks(execute=True):
        result = requeue_processing(
            operator,
            failed.pk,
            reason_code="operator_retry",
            dispatch=dispatched.append,
        )

    assert result.run_id == dispatched[0]
    assert result.attempt_number == 2
    assert result.document_id is None
    assert ProcessingRun.objects.get(pk=result.run_id).stage == ProcessingStage.QUEUED


def test_dictionary_publish_switches_only_valid_deployed_artifact(django_user_model):
    manager = staff(django_user_model, Role.DICTIONARY_MANAGER)

    release = publish_dictionary(
        manager,
        "v1.0.0.json",
        reason_code="validated_release",
        totp_verified_at=timezone.now(),
    )

    assert release.active is True
    assert release.version == default_dictionary().version
    assert release.indicator_count == 125
    assert current_dictionary().content_hash == default_dictionary().content_hash
    assert DictionaryRelease.objects.filter(active=True).count() == 1
    assert release.regression_report['passed'] is True
    assert release.regression_report['dataset_kind'] == 'SYNTHETIC'


def test_bundled_dictionary_publication_cannot_bypass_fixed_regression(django_user_model, monkeypatch):
    from apps.labs import dictionary_workflow as regression
    from apps.operations.services import InvalidOperation
    manager = staff(django_user_model, Role.DICTIONARY_MANAGER)
    monkeypatch.setattr(regression, 'evaluate_publication', lambda *args, **kwargs: {'passed': False})
    with pytest.raises(InvalidOperation, match='regression'):
        publish_dictionary(manager, 'phase-two.json', reason_code='validated_release', totp_verified_at=timezone.now())
    assert not DictionaryRelease.objects.exists()


def test_processor_switches_only_terminal_traceable_parse_version_with_totp(django_user_model):
    operator = staff(django_user_model, Role.PROCESSOR_OPERATOR)
    _client, patient = _patient(django_user_model, "6")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    finished_at = timezone.now()
    first_run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v1",
        task_type="INITIAL",
        idempotency_key=f"{document.pk}:parser-v1:initial",
        attempt_number=1,
        stage=ProcessingStage.SUCCEEDED,
        finished_at=finished_at,
        is_current=True,
    )
    first = ParsingVersion.objects.create(
        document=document,
        processing_run=first_run,
        parser_version="parser-v1",
        ocr_provider="fixture",
        ocr_provider_version="1.0",
        status=ParsingVersionStatus.READY,
    )
    ParsingVersion.objects.activate(first)
    second_run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v2",
        task_type="REPARSE",
        idempotency_key=f"{document.pk}:parser-v2:reparse",
        attempt_number=2,
        stage=ProcessingStage.SUCCEEDED,
        finished_at=finished_at,
    )
    second = ParsingVersion.objects.create(
        document=document,
        processing_run=second_run,
        parser_version="parser-v2",
        ocr_provider="fixture",
        ocr_provider_version="2.0",
        status=ParsingVersionStatus.READY,
    )

    activated = activate_parsing_version(
        operator,
        second.pk,
        reason_code="validated_rollback",
        totp_verified_at=timezone.now(),
    )

    first.refresh_from_db()
    second_run.refresh_from_db()
    assert activated.pk == second.pk and activated.active is True
    assert first.active is False
    assert second_run.is_current is True


def test_privacy_admin_changes_bounded_quota_and_every_other_role_is_denied(django_user_model):
    admin = staff(django_user_model, Role.PRIVACY_ADMIN)
    support = staff(django_user_model, Role.SUPPORT)
    _client, patient = _patient(django_user_model, "3")
    limits = QuotaLimits(20, 60, 400, 1200, 3 * 1024**3)

    quota = change_patient_quota(
        admin,
        patient.pk,
        limits,
        reason_code="trial_extension",
        totp_verified_at=timezone.now(),
    )

    assert quota.document_limit == 400
    assert PatientUploadQuota.objects.get(patient=patient).page_limit == 1200
    with pytest.raises(PermissionDenied):
        change_patient_quota(
            support,
            patient.pk,
            limits,
            reason_code="trial_extension",
            totp_verified_at=timezone.now(),
        )


def test_support_metadata_requires_short_lived_reasoned_grant_and_never_returns_identity(
    django_user_model,
):
    privacy_admin = staff(django_user_model, Role.PRIVACY_ADMIN)
    support = staff(django_user_model, Role.SUPPORT)
    _client, patient = _patient(django_user_model, "4")
    _document(patient, content_type="image/png", page_count=1)
    now = timezone.now()

    with pytest.raises(PermissionDenied):
        support_metadata_summary(support, patient.pk, now=now)
    grant = grant_support_access(
        privacy_admin,
        support.pk,
        patient.pk,
        duration=timedelta(minutes=30),
        reason_code="user_support_request",
        totp_verified_at=now,
        now=now,
    )
    summary = support_metadata_summary(support, patient.pk, now=now)

    assert SupportAccessGrant.objects.get(pk=grant.pk).expires_at == now + timedelta(minutes=30)
    assert set(summary) == {"account_state", "document_count_bucket", "deletion_state"}
    serialized = repr(summary)
    assert patient.display_name not in serialized
    assert patient.account.phone_hash not in serialized
    with pytest.raises(PermissionDenied):
        support_metadata_summary(support, patient.pk, now=now + timedelta(minutes=31))
