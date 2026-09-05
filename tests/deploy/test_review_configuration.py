from pathlib import Path

from celery import Celery
import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_ocr_and_control_tasks_have_dedicated_consumers(settings):
    app = Celery("route-contract", set_as_current=False)
    app.config_from_object(settings, namespace="CELERY")
    try:
        routes = app.amqp.router
        assert routes.route({}, "processing.process_document")["queue"].name == "ocr"
        for task in ("accounts.deliver_sms", "accounts.recover_sms_deliveries",
                     "documents.purge_deleted_document", "accounts.purge_deleted_account",
                     "notifications.deliver_push", "processing.recover_stale_runs"):
            assert routes.route({}, task)["queue"].name == "control"
    finally:
        app.close()
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert "--queues=ocr" in services["worker"]["command"]
    assert "--queues=control" in services["control-worker"]["command"]


def test_public_proxy_is_isolated_from_backing_services_and_ready_probe():
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services["caddy"]["networks"]) == {"proxy", "edge"}
    assert "proxy" in services["web"]["networks"]
    assert compose["networks"]["proxy"]["internal"] is True
    assert "TRUSTED_PROXY_NETWORKS" in services["web"]["environment"]
    private_matcher = next(line for line in (ROOT / "deploy/Caddyfile").read_text().splitlines()
                           if "@private_operations path " in line)
    assert "/health/ready /health/ready/*" in private_matcher


def test_production_lock_covers_transitive_dependencies_and_enforces_hashes():
    lock = (ROOT / "requirements-prod.lock").read_text(encoding="utf-8")
    entries = [line for line in lock.splitlines() if line and not line[0].isspace() and not line.startswith("#")]
    assert len(entries) > 80
    for entry in entries:
        assert "==" in entry and entry.endswith("\\")
    assert lock.count("--hash=sha256:") >= len(entries)
    assert "--require-hashes" in (ROOT / "deploy/Dockerfile").read_text()
