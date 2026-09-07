"""Owner-visible review feedback must reflect persisted state, not a URL flag."""

import pytest

from apps.labs.revisions import effective_observation, revise_observation
from apps.labs.validation import validate_observation
from tests.labs.test_phase_two_workflows import case, _new_version


pytestmark = pytest.mark.django_db


def test_confirmation_shows_saved_state_without_changing_values_or_clearing_issues(case):
    client, _, document, row = case
    row.evidence.confidence = "0.7000"
    row.evidence.save(update_fields=["confidence"])
    row.raw_unit = "unverified-unit"
    row.save(update_fields=["raw_unit"])
    before = {issue["code"] for issue in validate_observation(effective_observation(row))}
    url = f"/labs/observations/{row.pk}/"

    response = client.post(url, {"action": "CONFIRM", "expected_revision": 0}, follow=True)

    assert response.status_code == 200
    assert 'role="status"' in response.content.decode()
    assert "已保存核对结果" in response.content.decode()
    row.refresh_from_db()
    current = effective_observation(row)
    assert current.raw_value == "62" and current.raw_unit == "unverified-unit"
    assert current.review_state == "CONFIRM" and row.revisions.count() == 1
    assert {issue["code"] for issue in validate_observation(current)} == before
    assert {"recognition_uncertain", "unit_unknown"} <= before

    refreshed = client.get(url)
    assert refreshed.context["review_status"]["label"] == "已核对"
    assert "已核对" in refreshed.content.decode()
    assert "已保存核对结果" not in refreshed.content.decode()
    detail = client.get(f"/records/{document.pk}/")
    assert detail.context["observations"][0].review_status["label"] == "已核对"
    assert "已核对" in detail.content.decode()


@pytest.mark.parametrize(
    ("action", "changes", "label"),
    [("REPORT_ERROR", {}, "已标记识别有误"), ("DEFER", {}, "暂不处理"),
     ("CORRECT", {"raw_value": "6.2"}, "已更正")],
)
def test_action_feedback_and_undo_restore_the_visible_state(case, action, changes, label):
    client, _, _, row = case
    url = f"/labs/observations/{row.pk}/"
    initial = client.get(url)
    assert initial.context["review_status"]["label"] == "待核对"
    response = client.post(url, {"action": action, "expected_revision": 0, **changes}, follow=True)
    assert response.status_code == 200
    assert response.context["review_status"]["label"] == label
    assert label in response.content.decode()
    assert 'role="status"' in response.content.decode()
    assert client.get(url).context["review_status"]["label"] == label

    undone = client.post(url, {"action": "UNDO", "expected_revision": 1}, follow=True)
    assert undone.context["review_status"]["label"] == "待核对"
    assert "已撤销上次操作" in undone.content.decode()
    assert undone.context["observation"].raw_value == "62"


def test_stale_or_invalid_action_never_claims_it_was_saved(case):
    client, _, _, row = case
    url = f"/labs/observations/{row.pk}/"
    assert client.post(url, {"action": "CONFIRM", "expected_revision": 0}, follow=True).status_code == 200
    stale = client.post(url, {"action": "DEFER", "expected_revision": 0}, follow=True)
    assert stale.status_code == 409
    assert "已保存" not in stale.content.decode()
    invalid = client.post(url, {"action": "UNKNOWN", "expected_revision": 1}, follow=True)
    assert invalid.status_code == 400
    assert "已保存" not in invalid.content.decode()
    assert client.get(url).context["review_status"]["label"] == "已核对"


def test_confirmation_does_not_present_unresolved_reparse_conflict_as_verified(case):
    client, patient, document, row = case
    revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
    current = _new_version(document, row)
    url = f"/labs/observations/{current.pk}/"
    response = client.post(url, {"action": "CONFIRM", "expected_revision": 0}, follow=True)
    assert response.context["review_status"]["tone"] == "warning"
    assert response.context["review_status"]["label"] == "待核对版本冲突"
    assert response.context["observation"].revision_conflict is True
    assert "核对后采用本次识别" in response.content.decode()
    detail = client.get(f"/records/{document.pk}/")
    assert detail.context["observations"][0].review_status["tone"] == "warning"
    chosen = client.post(url, {"action": "USE_AUTOMATIC", "expected_revision": 1}, follow=True)
    assert chosen.context["review_status"]["label"] == "已核对，采用本次识别"
    assert chosen.context["observation"].raw_value == "63"
    assert chosen.context["observation"].revision_conflict is False
