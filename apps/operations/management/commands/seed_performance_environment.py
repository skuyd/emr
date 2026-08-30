from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string
from pypdf import PdfWriter

from apps.accounts.models import AccountSession, ConsentRecord
from apps.documents.backends import get_object_store
from apps.documents.models import (
    BatchStatus,
    Document,
    DocumentPage,
    DocumentStatus,
    PatientUploadQuota,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.labs.models import CapabilityLevel, LabObservation, ResultType
from apps.patients.models import Patient, PatientPreference
from apps.patients.policies import REQUIRED_CONSENT_TYPES, consent_policies
from apps.processing.models import (
    DatePrecision,
    DocumentSummary,
    DocumentType,
    OcrBlock,
    ParsingVersion,
    ParsingVersionStatus,
    SourceEvidence,
)


CONFIRMATION = "SYNTHETIC-PERFORMANCE-DATA"
TREND_CODE = "SYNTHETIC_METRIC"
_UUID_NAMESPACE = uuid.UUID("52e262fd-9d44-4c3b-b1b6-9c2d05b57cb8")
_NAMESPACE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_POLYGON = ((0.1, 0.1), (0.9, 0.1), (0.9, 0.2), (0.1, 0.2))


def _stable_id(namespace: str, *parts: object) -> uuid.UUID:
    return uuid.uuid5(_UUID_NAMESPACE, "/".join((namespace, *(str(part) for part in parts))))


def _synthetic_pdf(page_count: int) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def _ocr_text(account_index: int, document_index: int, page_number: int, length: int) -> str:
    marker = f"synthetic-token-{account_index:03d} document-{document_index:03d} page-{page_number} "
    filler = "synthetic performance archive text "
    return (marker + filler * math.ceil(max(0, length - len(marker)) / len(filler)))[:length]


def _validated_origin(value: str):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CommandError("--base-url must be an HTTPS origin without credentials, path, query, or fragment")
    return parsed, value.rstrip("/")


def _outside_repository(value: str, option_name: str) -> Path:
    path = Path(value).expanduser().resolve()
    root = Path(settings.BASE_DIR).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        pass
    else:
        raise CommandError(f"{option_name} must be outside the repository because it contains session credentials")
    if not path.parent.is_dir():
        raise CommandError(f"parent directory for {option_name} does not exist")
    if path.exists():
        raise CommandError(f"{option_name} already exists; existing credential files are never overwritten")
    return path


def _write_private_json(path: Path, payload) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        os.chmod(path, 0o600)


def _storage_state(origin, session_key: str):
    return {
        "cookies": [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": session_key,
                "domain": origin.hostname,
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def _create_session(account) -> str:
    store_class = import_string(f"{settings.SESSION_ENGINE}.SessionStore")
    store = store_class()
    store[SESSION_KEY] = str(account.pk)
    store[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
    store[HASH_SESSION_KEY] = account.get_session_auth_hash()
    store.set_expiry(settings.SESSION_COOKIE_AGE)
    store.create()
    AccountSession.objects.update_or_create(
        session_key=store.session_key,
        defaults={"account": account},
    )
    return store.session_key


class Command(BaseCommand):
    help = "Seed a dedicated environment with synthetic release-performance data"

    def add_arguments(self, parser):
        parser.add_argument("--confirm", required=True)
        parser.add_argument("--namespace", required=True)
        parser.add_argument("--base-url", required=True)
        parser.add_argument("--session-output", required=True)
        parser.add_argument("--browser-state-output", required=True)
        parser.add_argument("--onboarding-state-output", required=True)
        parser.add_argument("--accounts", type=int, default=100)
        parser.add_argument("--documents-per-account", type=int, default=300)
        parser.add_argument("--pages-per-document", type=int, default=3)
        parser.add_argument("--characters-per-page", type=int, default=2000)

    def handle(self, *args, **options):
        if not getattr(settings, "ALLOW_PERFORMANCE_SEED", False):
            raise CommandError("performance seeding is disabled; enable it only in a dedicated synthetic environment")
        if options["confirm"] != CONFIRMATION:
            raise CommandError(f"--confirm must be exactly {CONFIRMATION}")
        namespace = options["namespace"]
        if _NAMESPACE_PATTERN.fullmatch(namespace) is None:
            raise CommandError("--namespace must be 1-32 lowercase letters, digits, or hyphens")
        accounts = options["accounts"]
        documents_per_account = options["documents_per_account"]
        pages_per_document = options["pages_per_document"]
        characters_per_page = options["characters_per_page"]
        if not 1 <= accounts <= 100:
            raise CommandError("--accounts must be between 1 and 100")
        if not 2 <= documents_per_account <= 300:
            raise CommandError("--documents-per-account must be between 2 and 300")
        if not 1 <= pages_per_document <= 3:
            raise CommandError("--pages-per-document must be between 1 and 3")
        if not 32 <= characters_per_page <= 2000:
            raise CommandError("--characters-per-page must be between 32 and 2000")

        origin, base_url = _validated_origin(options["base_url"])
        session_output = _outside_repository(options["session_output"], "--session-output")
        browser_output = _outside_repository(options["browser_state_output"], "--browser-state-output")
        onboarding_output = _outside_repository(options["onboarding_state_output"], "--onboarding-state-output")
        if len({session_output, browser_output, onboarding_output}) != 3:
            raise CommandError("credential output paths must be distinct")

        pdf_payload = _synthetic_pdf(pages_per_document)
        pdf_digest = hashlib.sha256(pdf_payload).hexdigest()
        now = timezone.now()
        old_completion = now - timedelta(days=30)
        dictionary_hash = hashlib.sha256(b"synthetic-performance-dictionary-v1").hexdigest()
        policies = consent_policies()
        account_model = get_user_model()

        session_rows = []
        first_session_key = None
        with transaction.atomic():
            for account_index in range(1, accounts + 1):
                account_id = _stable_id(namespace, "account", account_index)
                phone_hash = hashlib.sha256(
                    f"synthetic-performance/{namespace}/{account_index}".encode("utf-8")
                ).hexdigest()
                existing = account_model.objects.filter(pk=account_id).values("phone_hash").first()
                if existing is not None and existing["phone_hash"] != phone_hash:
                    raise CommandError("stable synthetic account identifier collides with existing data")
                account_model.objects.bulk_create(
                    [
                        account_model(
                            id=account_id,
                            phone_hash=phone_hash,
                            phone_encrypted="synthetic-performance-credential",
                            password="!",
                            is_active=True,
                        )
                    ],
                    ignore_conflicts=True,
                )
                account = account_model.objects.get(pk=account_id)
                patient_id = _stable_id(namespace, "patient", account_index)
                Patient.objects.bulk_create(
                    [Patient(id=patient_id, account_id=account_id, display_name=f"合成验收用户{account_index:03d}")],
                    ignore_conflicts=True,
                )
                patient = Patient.objects.get(pk=patient_id, account_id=account_id)
                ConsentRecord.objects.bulk_create(
                    [
                        ConsentRecord(
                            account_id=account_id,
                            consent_type=consent_type,
                            policy_version=policies[consent_type]["version"],
                            policy_digest=policies[consent_type]["digest"],
                            request_ip_hash="0" * 64,
                            user_agent_hash="1" * 64,
                        )
                        for consent_type in REQUIRED_CONSENT_TYPES
                    ],
                    ignore_conflicts=True,
                )
                PatientPreference.objects.bulk_create(
                    [PatientPreference(patient_id=patient_id)], ignore_conflicts=True
                )
                PatientUploadQuota.objects.bulk_create(
                    [
                        PatientUploadQuota(
                            patient_id=patient_id,
                            document_limit=min(65535, documents_per_account + 20),
                            page_limit=documents_per_account * pages_per_document + 60,
                        )
                    ],
                    ignore_conflicts=True,
                )

                batch_count = math.ceil(documents_per_account / 20)
                batch_ids = [_stable_id(namespace, "batch", account_index, number) for number in range(batch_count)]
                batches = []
                for batch_index, batch_id in enumerate(batch_ids):
                    count = min(20, documents_per_account - batch_index * 20)
                    batches.append(
                        UploadBatch(
                            id=batch_id,
                            patient_id=patient_id,
                            file_count=count,
                            page_count=count * pages_per_document,
                            byte_size=count * len(pdf_payload),
                            status=BatchStatus.COMPLETED,
                            completed_at=old_completion,
                        )
                    )
                UploadBatch.objects.bulk_create(batches, ignore_conflicts=True)

                documents = []
                items = []
                pages = []
                runs = []
                versions = []
                summaries = []
                ocr_blocks = []
                evidence_rows = []
                observation_rows = []
                for document_index in range(1, documents_per_account + 1):
                    document_id = _stable_id(namespace, "document", account_index, document_index)
                    run_id = _stable_id(namespace, "run", account_index, document_index)
                    version_id = _stable_id(namespace, "version", account_index, document_index)
                    digest = (
                        pdf_digest
                        if document_index == 1
                        else hashlib.sha256(
                            f"{namespace}/{account_index}/{document_index}".encode("utf-8")
                        ).hexdigest()
                    )
                    batch_index = (document_index - 1) // 20
                    batch_id = batch_ids[batch_index]
                    ordinal = (document_index - 1) % 20 + 1
                    object_key = f"originals/performance/{namespace}/{account_index:03d}/{document_id.hex}.pdf"
                    documents.append(
                        Document(
                            id=document_id,
                            patient_id=patient_id,
                            batch_id=batch_id,
                            display_filename=f"synthetic-{document_index:03d}.pdf",
                            content_type="application/pdf",
                            byte_size=len(pdf_payload),
                            page_count=pages_per_document,
                            sha256=digest,
                            original_object_key=object_key,
                            status=DocumentStatus.ORGANIZED,
                        )
                    )
                    items.append(
                        UploadItem(
                            id=_stable_id(namespace, "item", account_index, document_index),
                            batch_id=batch_id,
                            ordinal=ordinal,
                            display_filename=f"synthetic-{document_index:03d}.pdf",
                            byte_size=len(pdf_payload),
                            page_count=pages_per_document,
                            status=UploadItemStatus.CREATED,
                            document_id=document_id,
                        )
                    )
                    runs.append(
                        ProcessingRun(
                            id=run_id,
                            document_id=document_id,
                            parser_version="performance-v1",
                            task_type="synthetic-seed",
                            idempotency_key=f"performance:{namespace}:{document_id}",
                            stage=ProcessingStage.SUCCEEDED,
                            finished_at=now,
                            is_current=True,
                        )
                    )
                    versions.append(
                        ParsingVersion(
                            id=version_id,
                            document_id=document_id,
                            processing_run_id=run_id,
                            parser_version="performance-v1",
                            ocr_provider="synthetic",
                            ocr_provider_version="1.0.0",
                            dictionary_version="performance-v1",
                            dictionary_hash=dictionary_hash,
                            status=ParsingVersionStatus.PUBLISHED,
                            active=True,
                            diagnostics={"synthetic": True},
                            published_at=now,
                        )
                    )
                    document_date = date(
                        2020 + document_index % 6,
                        1 + document_index % 12,
                        1 + document_index % 27,
                    )
                    summaries.append(
                        DocumentSummary(
                            id=_stable_id(namespace, "summary", account_index, document_index),
                            parsing_version_id=version_id,
                            document_type=DocumentType.OTHER,
                            document_date_raw=document_date.isoformat(),
                            document_date=document_date,
                            date_precision=DatePrecision.DAY,
                            institution_raw="Synthetic Performance Facility",
                            confidence=Decimal("1.0000"),
                        )
                    )
                    first_page_id = None
                    first_block_id = None
                    for page_number in range(1, pages_per_document + 1):
                        page_id = _stable_id(namespace, "page", account_index, document_index, page_number)
                        block_id = _stable_id(namespace, "ocr", account_index, document_index, page_number)
                        pages.append(
                            DocumentPage(
                                id=page_id,
                                document_id=document_id,
                                page_number=page_number,
                                width=612,
                                height=792,
                                orientation="PORTRAIT",
                            )
                        )
                        ocr_blocks.append(
                            OcrBlock(
                                id=block_id,
                                parsing_version_id=version_id,
                                document_page_id=page_id,
                                reading_order=1,
                                text=_ocr_text(
                                    account_index,
                                    document_index,
                                    page_number,
                                    characters_per_page,
                                ),
                                polygon=_POLYGON,
                                confidence=Decimal("1.0000"),
                                provider_metadata={"synthetic": True},
                            )
                        )
                        if page_number == 1:
                            first_page_id = page_id
                            first_block_id = block_id
                    if document_index <= 2:
                        evidence_id = _stable_id(namespace, "evidence", account_index, document_index)
                        evidence_rows.append(
                            SourceEvidence(
                                id=evidence_id,
                                parsing_version_id=version_id,
                                document_page_id=first_page_id,
                                ocr_block_id=first_block_id,
                                polygon=_POLYGON,
                                source_text="synthetic metric",
                                confidence=Decimal("1.0000"),
                            )
                        )
                        observation_rows.append(
                            LabObservation(
                                id=_stable_id(namespace, "observation", account_index, document_index),
                                parsing_version_id=version_id,
                                document_page_id=first_page_id,
                                evidence_id=evidence_id,
                                reading_order=1,
                                raw_name="Synthetic metric",
                                standard_code=TREND_CODE,
                                standard_name="Synthetic metric",
                                raw_value=str(document_index),
                                result_type=ResultType.NUMERIC,
                                raw_unit="unit",
                                observation_date=document_date,
                                institution_raw="Synthetic Performance Facility",
                                method_raw="Synthetic Method",
                                capability_level=CapabilityLevel.STABLE,
                                dictionary_version="performance-v1",
                            )
                        )

                Document.objects.bulk_create(documents, batch_size=500, ignore_conflicts=True)
                UploadItem.objects.bulk_create(items, batch_size=500, ignore_conflicts=True)
                DocumentPage.objects.bulk_create(pages, batch_size=500, ignore_conflicts=True)
                ProcessingRun.objects.bulk_create(runs, batch_size=500, ignore_conflicts=True)
                ParsingVersion.objects.bulk_create(versions, batch_size=500, ignore_conflicts=True)
                DocumentSummary.objects.bulk_create(summaries, batch_size=500, ignore_conflicts=True)
                OcrBlock.objects.bulk_create(ocr_blocks, batch_size=250, ignore_conflicts=True)
                SourceEvidence.objects.bulk_create(evidence_rows, batch_size=100, ignore_conflicts=True)
                LabObservation.objects.bulk_create(observation_rows, batch_size=100, ignore_conflicts=True)

                expected_ids = [item.id for item in documents]
                if Document.objects.filter(patient_id=patient_id, id__in=expected_ids).count() != documents_per_account:
                    raise CommandError("synthetic document seed did not reach the requested count")

                first_document = documents[0]
                stored = get_object_store().put_staging(
                    io.BytesIO(pdf_payload),
                    expected_size=len(pdf_payload),
                    expected_sha256=pdf_digest,
                )
                get_object_store().promote_immutable(stored, first_document.original_object_key)

                session_key = _create_session(account)
                if first_session_key is None:
                    first_session_key = session_key
                session_rows.append(
                    {
                        "index": account_index,
                        "sessionid": session_key,
                        "document_count": documents_per_account,
                        "query": f"synthetic-token-{account_index:03d}",
                        "viewer_document_id": str(documents[0].id),
                        "delete_document_id": str(documents[1].id),
                        "trend_code": TREND_CODE,
                    }
                )

            onboarding_id = _stable_id(namespace, "onboarding-account")
            onboarding_hash = hashlib.sha256(
                f"synthetic-performance/{namespace}/onboarding".encode("utf-8")
            ).hexdigest()
            account_model.objects.bulk_create(
                [
                    account_model(
                        id=onboarding_id,
                        phone_hash=onboarding_hash,
                        phone_encrypted="synthetic-performance-credential",
                        password="!",
                        is_active=True,
                    )
                ],
                ignore_conflicts=True,
            )
            if Patient.objects.filter(account_id=onboarding_id).exists():
                raise CommandError("the synthetic onboarding account was already consumed; use a new namespace")
            onboarding_session_key = _create_session(account_model.objects.get(pk=onboarding_id))

        generated_at = timezone.now().isoformat()
        _write_private_json(
            session_output,
            {
                "schema_version": 1,
                "synthetic_only": True,
                "base_url": base_url,
                "generated_at": generated_at,
                "design": {
                    "accounts": accounts,
                    "documents_per_account": documents_per_account,
                    "pages_per_document": pages_per_document,
                    "characters_per_page": characters_per_page,
                },
                "sessions": session_rows,
            },
        )
        _write_private_json(browser_output, _storage_state(origin, first_session_key))
        _write_private_json(onboarding_output, _storage_state(origin, onboarding_session_key))
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {accounts} synthetic accounts and {accounts * documents_per_account} documents."
            )
        )
        self.stdout.write("Credential files were created outside the repository; delete them after verification.")
