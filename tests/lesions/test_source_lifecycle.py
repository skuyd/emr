from apps.facts.laterality import review_parent_arguments
import json

import pytest

from apps.documents.deletion import DeletionOutcome, purge_document_deletion, request_document_deletion
from apps.documents.lifecycle import move_to_trash, restore_document
from apps.documents.models import Document
from apps.facts.clinical_extraction import extract_clinical_version
from apps.facts.models import Fact
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.lesions.models import Lesion, LesionObservation, LesionOperation
from apps.lesions.readmodels import review_observations
from tests.documents.fakes import InMemoryObjectStore
from tests.facts.factories import parsed_facts
from .test_relationships import matched_pair


pytestmark = pytest.mark.django_db


def test_restore_from_trash_does_not_refresh_old_relation_confirmation(django_user_model):
    patient, _, rows = matched_pair(django_user_model, "lesion-recycle-source")
    selected = rows[1]
    move_to_trash(patient, selected["document_id"], actor=patient.account)
    unavailable = next(row for row in review_observations(patient, actor=patient.account, include_unavailable=True)
                       if row["id"] == selected["id"])
    assert unavailable["status"] == "UNAVAILABLE" and not unavailable["usable"]
    restore_document(patient, selected["document_id"], actor=patient.account)
    current = next(row for row in review_observations(patient, actor=patient.account) if row["id"] == selected["id"])
    assert current["status"] == "STALE" and not current["source_usable"] and not current["usable"]
    assert all(field["status"] == "PENDING" and not field["usable"] for field in current["fields"])
    assert current["assignment"]["source_binding"] == selected["source_binding"]
    for field in Fact.objects.filter(clinical_report_id=selected["report_id"]):
        state = effective_fact(field)
        revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=field.revision_number,
                    expected_source=state["current_source_token"], checked_original=True, **review_parent_arguments(field))
    rechecked = next(row for row in review_observations(patient, actor=patient.account) if row["id"] == selected["id"])
    assert rechecked["source_usable"] and rechecked["status"] == "STALE" and not rechecked["usable"]
    assert rechecked["assignment"]["source_binding"] == selected["source_binding"]


def test_reparse_creates_unassigned_local_identity_and_keeps_old_link_as_unavailable_history(django_user_model):
    patient, _, rows = matched_pair(django_user_model, "lesion-reparse-source")
    selected = rows[1]
    document = Document.objects.get(pk=selected["document_id"])
    previous = document.parsing_versions.get(active=True)
    _, current = parsed_facts(patient, ["合成医院 CT诊断报告书", "检查日期：2026-09-01 检查项目：胸部CT平扫",
        "影像表现：左肺上叶见结节，长径15mm。", "诊断意见：建议随访。"],
        document=document, previous=previous, document_type="IMAGING")
    extract_clinical_version(current)
    material = review_observations(patient, actor=patient.account, include_unavailable=True)
    old = next(row for row in material if row["id"] == selected["id"])
    new = next(row for row in material if row["document_id"] == selected["document_id"] and row["id"] != selected["id"])
    assert old["status"] == "UNAVAILABLE" and old["lesion_id"] == selected["lesion_id"]
    assert new["status"] == "UNASSIGNED" and new["lesion_id"] is None and not new["usable"]
    assert LesionObservation.objects.filter(patient=patient).count() == 2


def test_source_purge_removes_medical_fields_but_retains_opaque_link_history_until_patient_purge(django_user_model):
    patient, operation, rows = matched_pair(django_user_model, "lesion-purge-source")
    selected = rows[1]
    job = request_document_deletion(patient, selected["document_id"], actor=patient.account, dispatch=lambda _: None)
    result = purge_document_deletion(job.pk, InMemoryObjectStore())
    assert result.outcome == DeletionOutcome.PURGED
    record = LesionObservation.objects.get(pk=selected["id"])
    assert record.document_id is None and record.report_id is None
    assert str(record.original_report_id) == selected["report_id"]
    unavailable = next(row for row in review_observations(patient, actor=patient.account, include_unavailable=True)
                       if row["id"] == selected["id"])
    assert unavailable["fields"] == [] and unavailable["context_fields"] == [] and not unavailable["usable"]
    assert unavailable["status"] == "UNAVAILABLE" and unavailable["source_binding"] is None
    assert "15mm" not in json.dumps(list(operation.observation_revisions.values_list("after", flat=True)))
    patient.delete()
    assert not Lesion.objects.exists() and not LesionObservation.objects.exists() and not LesionOperation.objects.exists()
