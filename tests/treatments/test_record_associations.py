import uuid

import pytest
from django.core.exceptions import PermissionDenied

from apps.treatments.models import CycleRecordLink
from tests.documents.test_detail_viewer import _document, _patient
from tests.treatments.test_cycle_decisions import cycle
from tests.treatments.test_manual_events import create

pytestmark = pytest.mark.django_db


def assign(patient, item, document, **kwargs):
    from apps.treatments.records import assign_record, record_state
    row = record_state(patient, actor=patient.account, kind="document", identity=document.pk)
    values = dict(actor=patient.account, kind="document", identity=document.pk, expected_source=row["source_token"],
                  expected_assignment=row["assignment_token"],
                  expected_revision=item.revision_number, checked_original=True, operation_id=uuid.uuid4(), assigned=True)
    values.update(kwargs)
    return assign_record(patient, item.pk, **values)


def test_manual_record_assignment_is_persistent_and_audits_actual_actor(django_user_model):
    _, patient = _patient(django_user_model, "cycle-record-assign")
    document, _ = _document(patient)
    item = cycle(patient, [create(patient, patient.account)])
    revision = assign(patient, item, document)
    link = CycleRecordLink.objects.get(cycle=item, document=document)
    assert link.origin == "USER" and link.assigned and link.active
    assert revision.author_id == patient.account_id and revision.after["record_assignment"]["source_id"] == str(document.pk)


def test_reassignment_preserves_old_decision_and_explicit_unassigned_is_not_automatic(django_user_model):
    from apps.treatments.records import association_material
    _, patient = _patient(django_user_model, "cycle-record-reassign")
    document, _ = _document(patient)
    one, two = cycle(patient, [create(patient, patient.account)]), cycle(patient, [create(patient, patient.account)])
    first = assign(patient, one, document)
    second = assign(patient, two, document)
    assert first.after["record_assignment"]["source_id"] == second.after["record_assignment"]["source_id"]
    assert CycleRecordLink.objects.filter(document=document).count() == 2
    assert list(CycleRecordLink.objects.filter(document=document, active=True).values_list("cycle_id", flat=True)) == [two.pk]
    two.refresh_from_db()
    assign(patient, two, document, assigned=False)
    rows = association_material(patient, actor=patient.account)
    assert len(rows) == 1 and rows[0]["assigned"] is False and rows[0]["reason"] == "user_left_unassigned"


def test_record_read_and_assignment_reject_foreign_document_and_changed_source(django_user_model):
    from apps.treatments.records import assign_record, record_state
    from apps.treatments.services import TreatmentConflict
    _, patient = _patient(django_user_model, "cycle-record-source")
    _, other = _patient(django_user_model, "cycle-record-foreign")
    foreign, _ = _document(other)
    item = cycle(patient, [create(patient, patient.account)])
    with pytest.raises(PermissionDenied):
        record_state(patient, actor=patient.account, kind="document", identity=foreign.pk)
    with pytest.raises(PermissionDenied):
        assign_record(patient, item.pk, actor=patient.account, kind="document", identity=foreign.pk, expected_source="a" * 64,
            expected_revision=1, checked_original=True, operation_id=uuid.uuid4(), assigned=True)
    own, _ = _document(patient)
    token = record_state(patient, actor=patient.account, kind="document", identity=own.pk)["source_token"]
    own.material_revision += 1
    own.save(update_fields=["material_revision", "updated_at"])
    with pytest.raises(TreatmentConflict):
        assign(patient, item, own, expected_source=token)


def test_split_allows_explicit_record_assignments_and_leaves_others_unassigned(django_user_model):
    from apps.treatments.cycles import split_cycle
    from apps.treatments.records import association_material
    _, patient = _patient(django_user_model, "cycle-record-split")
    one, two = create(patient, patient.account), create(patient, patient.account)
    parent = cycle(patient, [one, two])
    document, _ = _document(patient)
    assign(patient, parent, document)
    parent.refresh_from_db()
    split_cycle(patient, parent.pk, actor=patient.account, expected_revision=parent.revision_number, checked_original=True,
        operation_id=uuid.uuid4(), parts=[
            {"event_ids": [str(one.pk)], "record_link_ids": [], "anchor": None, "anchor_precision": "UNKNOWN", "ordinal": None},
            {"event_ids": [str(two.pk)], "record_link_ids": [], "anchor": None, "anchor_precision": "UNKNOWN", "ordinal": None},
        ])
    rows = association_material(patient, actor=patient.account)
    assert len(rows) == 1 and rows[0]["assigned"] is False
    assert rows[0]["reason"] == "user_left_unassigned"


def test_report_association_uses_its_confirmed_typed_date_and_not_document_header(django_user_model):
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact
    from apps.treatments.records import record_state
    from tests.facts.test_clinical_foundation import clinical_fixture
    _, patient, document, _, _ = clinical_fixture(django_user_model, name="cycle-record-report")
    report = document.clinical_reports.get()
    before = record_state(patient, actor=patient.account, kind="report", identity=report.pk)
    assert before["date"] is None
    field = report.fields.get(field_key="report.exam_date")
    revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(field)["current_source_token"], checked_original=True)
    after = record_state(patient, actor=patient.account, kind="report", identity=report.pk)
    assert after["date"] == "2026-08-17" and after["date_precision"] == "DAY"
    assert before["source_token"] != after["source_token"]


def test_assignment_change_from_another_tab_is_not_overwritten(django_user_model):
    from apps.treatments.records import record_state
    from apps.treatments.services import TreatmentConflict
    _, patient = _patient(django_user_model, "cycle-record-concurrent")
    document, _ = _document(patient)
    one, two = cycle(patient, [create(patient, patient.account)]), cycle(patient, [create(patient, patient.account)])
    old = record_state(patient, actor=patient.account, kind="document", identity=document.pk)
    assign(patient, one, document)
    with pytest.raises(TreatmentConflict):
        assign(patient, two, document, expected_assignment=old["assignment_token"])
    assert list(CycleRecordLink.objects.filter(document=document, active=True).values_list("cycle_id", flat=True)) == [one.pk]


def test_material_includes_changed_assignment_and_preserved_revision_history(django_user_model):
    from apps.treatments.readmodels import treatment_material
    _, patient = _patient(django_user_model, "cycle-record-history")
    document, _ = _document(patient)
    one, two = cycle(patient, [create(patient, patient.account)]), cycle(patient, [create(patient, patient.account)])
    assign(patient, one, document)
    before = treatment_material(patient, actor=patient.account, include_history=True)
    assign(patient, two, document)
    after = treatment_material(patient, actor=patient.account, include_history=True)
    assert before["fingerprint"] != after["fingerprint"]
    assert len(after["record_associations"]) == 1 and after["record_associations"][0]["cycle_id"] == str(two.pk)
    history = next(row for row in after["history"] if row["kind"] == "cycle" and row["id"] == str(one.pk))
    assert history["initial_content"]["anchor"] == "2024-02-29"
    assert history["revisions"][-1]["after"]["record_assignment"]["source_id"] == str(document.pk)
