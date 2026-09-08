import re
from datetime import timedelta

from django.test import Client
from django.urls import resolve
from django.utils import timezone
import pytest

from apps.core.route_security import application_routes
from apps.documents.models import UploadItem
from apps.exports.models import ExportJob
from apps.facts.models import Fact
from apps.notifications.models import NotificationKind, TaskNotification
from tests.documents.test_detail_viewer import _document, _parsed_document, _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PRIVATE_PREFIXES = (
    "uploads/",
    "trends/",
    "records/",
    "api/upload-batches/",
    "api/notifications/",
    "notifications/",
    "api/push-subscriptions/",
    "me/",
    "facts/",
    "visit/",
    "recycle-bin/",
    "self-records/",
    "treatments/",
)


def _application_routes():
    return tuple(route for route in application_routes() if not route.route.startswith("admin/"))


def _concrete_path(route):
    values = {
        "uuid": "00000000-0000-0000-0000-000000000001",
        "int": "1",
        "str": "CANARY_CODE",
        "slug": "canary-code",
    }

    def substitute(match):
        return values.get(match.group("converter") or "str", "canary")

    rendered = re.sub(
        r"<(?:(?P<converter>[a-zA-Z_][a-zA-Z0-9_]*):)?(?P<parameter>[a-zA-Z_][a-zA-Z0-9_]*)>",
        substitute,
        route,
    )
    return f"/{rendered}"


def test_every_application_route_declares_allowed_http_methods():
    missing = [(route.name, route.route) for route in _application_routes() if not route.methods]

    assert missing == []


def test_every_private_business_route_is_patient_scoped():
    routes = _application_routes()
    expected_private = [
        route
        for route in routes
        if (route.route in {"", "tasks/"} or route.route.startswith(PRIVATE_PREFIXES))
        and route.route != "me/delete-account/"  # Authenticated account-wide deletion, independent of patient selection.
    ]

    assert expected_private
    assert [(route.name, route.route) for route in expected_private if not route.patient_scoped] == []


def test_every_mutation_route_rejects_requests_without_csrf_token():
    client = Client(enforce_csrf_checks=True)
    failures = []
    mutations = []
    for route in _application_routes():
        for method in sorted(set(route.methods) & UNSAFE_METHODS):
            mutations.append((route, method))
            response = client.generic(
                method,
                _concrete_path(route.route),
                data=b"",
                content_type="application/octet-stream",
            )
            if route.csrf_exempt or response.status_code != 403:
                failures.append((route.name, route.route, method, route.csrf_exempt, response.status_code))

    assert mutations
    assert failures == []


def test_every_dynamic_patient_route_rejects_foreign_resources(django_user_model):
    intruder, _intruder_patient = _patient(django_user_model, "u")
    _owner, owner_patient = _patient(django_user_model, "v")
    document, _first_evidence, _second_evidence = _parsed_document(owner_patient)
    item = UploadItem.objects.create(
        batch=document.batch,
        ordinal=1,
        display_filename="CANARY_FOREIGN_ITEM.pdf",
    )
    notification = TaskNotification.objects.create(
        patient=owner_patient,
        batch=document.batch,
        kind=NotificationKind.COMPLETED,
    )
    fact_document, version = parsed_facts(owner_patient, ["诊断：合成诊断。"])
    fact = Fact.objects.get(parsing_version=version)
    from apps.facts.clinical_extraction import extract_clinical_version
    from tests.facts.test_clinical_foundation import CT
    clinical_document, clinical_version = parsed_facts(owner_patient, CT, document_type="IMAGING")
    extract_clinical_version(clinical_version)
    clinical_report = clinical_version.clinical_reports.get()
    from datetime import date
    from tests.labs.test_trends import _observation
    _, observation = _observation(owner_patient, date(2026, 8, 1), "4")
    job = ExportJob.objects.create(patient=owner_patient, expires_at=timezone.now() + timedelta(hours=24))
    from uuid import uuid4
    from apps.self_records.services import create_record
    from tests.self_records.test_payloads import payload
    daily_record = create_record(owner_patient, owner_patient.account, payload(), creation_key=uuid4()).record
    from tests.treatments.test_manual_events import create as create_treatment
    from tests.treatments.test_regimens import regimen
    from tests.treatments.test_cycle_decisions import cycle
    treatment = create_treatment(owner_patient, owner_patient.account)
    scheme = regimen(owner_patient, treatment)
    treatment_cycle = cycle(owner_patient, [treatment], regimen_id=scheme.pk)
    trashed, _ = _document(owner_patient)
    trashed.deleted_at = trashed.trashed_at = timezone.now()
    trashed.trash_expires_at = trashed.trashed_at + timedelta(days=30)
    trashed.save(update_fields=["deleted_at", "trashed_at", "trash_expires_at"])

    matrix = {
        "treatments:event": [(method, f"/treatments/events/{treatment.pk}/") for method in ("GET", "POST")],
        "treatments:regimen": [(method, f"/treatments/regimens/{scheme.pk}/") for method in ("GET", "POST")],
        "treatments:cycle": [(method, f"/treatments/cycles/{treatment_cycle.pk}/") for method in ("GET", "POST")],
        "treatments:split": [(method, f"/treatments/cycles/{treatment_cycle.pk}/split/") for method in ("GET", "POST")],
        "treatments:assign": [(method, f"/treatments/cycles/{treatment_cycle.pk}/records/") for method in ("GET", "POST")],
        "self_records:detail": [("GET", f"/self-records/{daily_record.pk}/")],
        "self_records:edit": [(method, f"/self-records/{daily_record.pk}/edit/") for method in ("GET", "POST")],
        "self_records:delete": [("POST", f"/self-records/{daily_record.pk}/delete/")],
        "self_records:undo": [("POST", f"/self-records/{daily_record.pk}/undo/")],
        "labs:observation": [(method, f"/labs/observations/{observation.pk}/") for method in ("GET", "POST")],
        "labs:create_task": [("POST", f"/labs/observations/{observation.pk}/review/")],
        "labs:observation_source": [("GET", f"/labs/observations/{observation.pk}/source/raw_value/")],
        "labs:observation_source_image": [("GET", f"/labs/observations/{observation.pk}/source/raw_value/image/")],
        "labs:activate_version": [("POST", f"/labs/versions/{observation.parsing_version_id}/activate/")],
        "facts:document": [(method, f"/facts/documents/{fact_document.pk}/") for method in ("GET", "POST")],
        "facts:detail": [(method, f"/facts/{fact.pk}/") for method in ("GET", "POST")],
        "facts:reports": [(method, f"/facts/documents/{clinical_document.pk}/reports/") for method in ("GET", "POST")],
        "facts:report": [(method, f"/facts/reports/{clinical_report.pk}/") for method in ("GET", "POST")],
        "exports:preview": [(method, f"/visit/{job.pk}/") for method in ("GET", "POST")],
        "exports:pdf": [("GET", f"/visit/{job.pk}/pdf/")],
        "exports:download": [("GET", f"/visit/{job.pk}/download/")],
        "exports:cancel": [("POST", f"/visit/{job.pk}/cancel/")],
        "documents:document_restore": [("POST", f"/recycle-bin/{trashed.pk}/restore/")],
        "documents:document_permanent_delete": [
            (method, f"/recycle-bin/{trashed.pk}/delete/") for method in ("GET", "POST")
        ],
        "documents:indicator_trend": [("GET", "/trends/LAB_WBC/")],
        "documents:document_summary": [("GET", f"/records/{document.pk}/")],
        "documents:document_feedback": [("POST", f"/records/{document.pk}/feedback/")],
        "documents:document_reprocess": [("POST", f"/records/{document.pk}/reprocess/")],
        "documents:document_material": [("POST", f"/records/{document.pk}/material/")],
        "documents:document_delete": [
            ("GET", f"/records/{document.pk}/delete/"),
            ("POST", f"/records/{document.pk}/delete/"),
        ],
        "documents:document_viewer": [("GET", f"/records/{document.pk}/viewer/")],
        "documents:document_page_image": [("GET", f"/records/{document.pk}/pages/1/image/")],
        "documents:document_thumbnail_sheet": [
            ("GET", f"/records/{document.pk}/thumbnails/sheet/")
        ],
        "documents:document_original": [("GET", f"/records/{document.pk}/original/")],
        "documents:upload_item_content": [
            ("POST", f"/api/upload-batches/{document.batch_id}/items/{item.pk}/content/")
        ],
        "documents:remove_upload_item": [
            ("POST", f"/api/upload-batches/{document.batch_id}/items/{item.pk}/remove/")
        ],
        "documents:batch_status": [("GET", f"/api/upload-batches/{document.batch_id}/status/")],
        "notifications:mark_read": [("POST", f"/api/notifications/{notification.pk}/read/")],
        "notifications:open": [("GET", f"/notifications/{notification.pk}/open/")],
    }
    dynamic_routes = {
        route.name for route in _application_routes() if route.patient_scoped and "<" in route.route
    }

    assert dynamic_routes == set(matrix)
    failures = []
    for route_name, probes in matrix.items():
        for method, path in probes:
            assert resolve(path).view_name == route_name
            response = intruder.generic(method, path, data=b"", content_type="application/octet-stream")
            if response.status_code != 404:
                failures.append((route_name, method, path, response.status_code))

    assert failures == []
