import secrets

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from .alerts import evaluate_alerts
from .health import readiness
from .metrics import render_prometheus


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _authorized(request):
    expected = settings.OPERATIONS_METRICS_TOKEN
    header = request.headers.get("Authorization", "")
    if not expected or not header.startswith("Bearer "):
        return False
    supplied = header.removeprefix("Bearer ")
    return len(supplied) <= 512 and secrets.compare_digest(supplied, expected)


@require_GET
def live(request):
    return _no_store(JsonResponse({"status": "live"}))


@require_GET
def ready(request):
    result = readiness()
    return _no_store(
        JsonResponse(
            {"status": result.status, "dependencies": result.dependencies},
            status=result.http_status,
        )
    )


@require_GET
def metrics(request):
    if not _authorized(request):
        return _no_store(HttpResponse("Forbidden", status=403, content_type="text/plain"))
    return _no_store(
        HttpResponse(render_prometheus(), content_type="text/plain; version=0.0.4; charset=utf-8")
    )


@require_GET
def alerts(request):
    if not _authorized(request):
        return _no_store(JsonResponse({"error": "forbidden"}, status=403))
    return _no_store(
        JsonResponse(
            {
                "alerts": [
                    {"code": alert.code, "severity": alert.severity, "value": alert.value}
                    for alert in evaluate_alerts()
                ]
            }
        )
    )
