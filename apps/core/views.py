from django.shortcuts import redirect
from django.views.decorators.http import require_GET


@require_GET
def favicon(request):
    return redirect("/static/favicon.svg", permanent=True)
