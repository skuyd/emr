from datetime import date
import uuid

import pytest
from django.core.exceptions import PermissionDenied

from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation
from tests.treatments.test_cycle_decisions import cycle
from tests.treatments.test_manual_events import create

pytestmark = pytest.mark.django_db


def test_real_neutrophil_count_maps_to_anc_without_using_neutrophil_percentage(django_user_model):
    from apps.treatments.workspace import workspace_material
    from apps.treatments.timeline import build_cycle_timeline
    from apps.treatments.overlays import build_cycle_overlays
    _, patient = _patient(django_user_model, "cycle-actual-anc")
    cycle(patient, [create(patient, patient.account)])
    _, count = _observation(patient, date(2024, 3, 1), "2.1", code="LAB_NEUT_COUNT", raw_name="NEU#", standard_name="中性粒细胞计数")
    _, percentage = _observation(patient, date(2024, 3, 1), "50", code="LAB_NEUT_PERCENT", raw_name="NEUT%", raw_unit="%")
    source = workspace_material(patient, actor=patient.account)
    result = build_cycle_overlays(build_cycle_timeline(source), source)
    assert [row["observation_id"] for row in result["points"]] == [str(count.pk)]
    assert result["points"][0]["standard_code"] == "LAB_NEUT_COUNT"
    assert str(percentage.pk) in {row["id"] for row in source["records"]}


def test_patient_lab_material_keeps_precise_personal_change_sources_from_full_context(django_user_model):
    from apps.treatments.workspace import workspace_material
    _, patient = _patient(django_user_model, "cycle-personal-data")
    rows = [_observation(patient, date(2024, 3, day), value)[1] for day, value in [(1, "2"), (2, "4"), (3, "6"), (13, "12")]]
    material = workspace_material(patient, actor=patient.account)
    latest = next(row for row in material["personal_changes"] if row["observation_id"] == str(rows[-1].pk))
    assert latest["daily_change"] == "0.6" and latest["baseline_mean"] == "4"
    assert latest["baseline_observation_ids"] == [str(row.pk) for row in rows[:3]]
    assert latest["previous_observation_id"] == str(rows[2].pk) and latest["elapsed_days"] == 10


def test_source_date_month_precision_is_not_silently_converted_to_a_cycle_day(django_user_model):
    from apps.treatments.records import record_state
    from apps.treatments.workspace import workspace_material
    _, patient = _patient(django_user_model, "cycle-lab-month")
    _, observation = _observation(patient, date(2024, 3, 1), "2.1", code="LAB_NEUT_COUNT", precision="MONTH")
    row = record_state(patient, actor=patient.account, kind="observation", identity=observation.pk)
    assert row["date"] == "2024-03" and row["date_precision"] == "MONTH"
    material = workspace_material(patient, actor=patient.account)
    current = next(row for row in material["records"] if row["id"] == str(observation.pk))
    assert not current["trend_eligible"] and current["date_precision"] == "MONTH"


def test_current_lab_revision_invalidates_automatic_proposal_input(django_user_model):
    from apps.labs.revisions import revise_observation
    from apps.treatments.derivations import persist_proposals, proposal_preview
    from apps.treatments.services import TreatmentConflict
    _, patient = _patient(django_user_model, "cycle-lab-proposal-input")
    _, observation = _observation(patient, date(2024, 3, 1), "2.1", code="LAB_NEUT_COUNT")
    create(patient, patient.account, occurred_on="2024-03-01")
    preview = proposal_preview(patient, actor=patient.account)
    revise_observation(patient.account, observation.pk, action="CORRECT", expected_revision=0, changes={"raw_value": "2.2"})
    with pytest.raises(TreatmentConflict):
        persist_proposals(patient, actor=patient.account, expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4())


def test_workspace_material_checks_actual_actor_and_excludes_other_patient(django_user_model):
    from apps.treatments.workspace import workspace_material
    _, patient = _patient(django_user_model, "cycle-material-owner")
    _, other = _patient(django_user_model, "cycle-material-other")
    _, own = _observation(patient, date(2024, 3, 1), "2.1")
    _, foreign = _observation(other, date(2024, 3, 1), "999")
    with pytest.raises(PermissionDenied):
        workspace_material(patient, actor=other.account)
    result = workspace_material(patient, actor=patient.account)
    ids = {row["id"] for row in result["records"]}
    assert str(own.pk) in ids and str(foreign.pk) not in ids
