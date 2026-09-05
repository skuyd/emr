from django.test import override_settings
import pytest

from apps.operations.health import readiness


@override_settings(OPERATIONS_METRICS_TOKEN="test-metrics-token")
@pytest.mark.parametrize("authorization", ["", "Bearer wrong-token", "Basic ignored"])
def test_unauthorized_readiness_never_executes_dependency_probes(client, monkeypatch, authorization):
    def unexpected_probe():
        pytest.fail("Unauthorized requests must not probe dependencies")

    monkeypatch.setattr("apps.operations.views.readiness", unexpected_probe)
    response = client.get("/health/ready/", HTTP_AUTHORIZATION=authorization)

    assert response.status_code == 403
    assert response["Cache-Control"] == "no-store"


@override_settings(OPERATIONS_METRICS_TOKEN="test-metrics-token")
def test_authorized_readiness_returns_generic_dependency_state(client, monkeypatch):
    from apps.operations.health import Readiness

    monkeypatch.setattr("apps.operations.views.readiness", lambda: Readiness(
        "degraded", 503, {"database": "down", "cache": "up", "object_storage": "up"},
    ))
    response = client.get("/health/ready/", HTTP_AUTHORIZATION="Bearer test-metrics-token")

    assert response.status_code == 503
    assert response.json()["dependencies"]["database"] == "down"


def test_readiness_distinguishes_ready_and_dependency_degraded():
    healthy = readiness(
        probes={"database": lambda: True, "cache": lambda: True, "object_storage": lambda: True}
    )
    degraded = readiness(
        probes={"database": lambda: True, "cache": lambda: False, "object_storage": lambda: True}
    )

    assert healthy.status == "ready" and healthy.http_status == 200
    assert degraded.status == "degraded" and degraded.http_status == 503
    assert degraded.dependencies == {"database": "up", "cache": "down", "object_storage": "up"}


@pytest.mark.django_db
@override_settings(OPERATIONS_METRICS_TOKEN="test-metrics-token")
def test_health_is_generic_and_metrics_require_bearer_token(client):
    live = client.get("/health/live/")
    denied = client.get("/internal/metrics/")
    allowed = client.get("/internal/metrics/", HTTP_AUTHORIZATION="Bearer test-metrics-token")

    assert live.status_code == 200 and live.json() == {"status": "live"}
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed["Content-Type"].startswith("text/plain")
    assert allowed["Cache-Control"] == "no-store"
