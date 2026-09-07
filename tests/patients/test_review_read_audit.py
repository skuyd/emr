"""Professional review reads use task grants and each displayed patient's audit."""

import pytest
from django.test import Client

from apps.labs import views
from apps.labs.review import create_review_task, transition_review_task
from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from apps.patients.models import Patient
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_phase_two_comparison import row as make_row
from tests.labs.test_phase_two_workflows import _reviewer, _withdraw_reviewer_authority


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("page", ["task", "queue"])
@pytest.mark.parametrize("withdrawal", ["task", "is_staff", "permission", "is_active"])
def test_review_read_discards_body_after_authority_changes(django_user_model, monkeypatch, page, withdrawal):
    _, patient = _patient(django_user_model, f"review-read-{page}-{withdrawal}")
    _, observation = make_row(patient)
    reviewer = _reviewer(django_user_model)
    task = create_review_task(patient.account, observation.pk, reviewer=reviewer)
    client = Client()
    client.force_login(reviewer)
    original = views._render
    def render_then_withdraw(*args, **kwargs):
        response = original(*args, **kwargs)
        if args[1] == "labs/error.html":
            return response
        if withdrawal == "task":
            transition_review_task(patient.account, task.pk, action="REVOKE", expected_revision=0)
        else:
            _withdraw_reviewer_authority(reviewer, withdrawal)
        return response
    monkeypatch.setattr(views, "_render", render_then_withdraw)
    response = client.get(f"/labs/reviews/{task.pk}/" if page == "task" else "/labs/reviews/")
    assert response.status_code == 403
    assert observation.raw_name not in response.content.decode()
    assert AuditEvent.objects.filter(action="review_viewed", patient_hash=_hash("patient", patient.pk),
                                     actor_hash=_hash("actor", reviewer.pk), result="denied").exists()


def test_review_queue_audits_only_actual_displayed_tasks_in_each_patient(django_user_model):
    reviewer = _reviewer(django_user_model)
    client = Client()
    client.force_login(reviewer)
    unrelated = Patient.objects.create(account=reviewer, display_name="无关活动患者")
    session = client.session
    session["active_patient_id"] = str(unrelated.pk)
    session.save()
    expected = set()
    for index in range(2):
        _, patient = _patient(django_user_model, f"review-queue-patient-{index}")
        _, observation = make_row(patient)
        task = create_review_task(patient.account, observation.pk, reviewer=reviewer)
        expected.add((_hash("patient", patient.pk), _hash("target", task.pk)))
    assert client.get("/labs/reviews/").status_code == 200
    events = AuditEvent.objects.filter(route_name="labs:reviews")
    assert set(events.values_list("patient_hash", "target_hash")) == expected
    assert set(events.values_list("action", "result", "actor_hash")) == {
        ("review_viewed", "succeeded", _hash("actor", reviewer.pk))
    }
