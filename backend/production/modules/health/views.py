from django.http import JsonResponse

from production.modules.health.service import get_health
from production.shared.http import api_endpoint


@api_endpoint(["GET"])
def status_view(request):
    """200 when everything is healthy, 503 otherwise, so monitors can rely on the status code alone."""
    health = get_health()
    return JsonResponse(health, status=200 if health["ok"] else 503)
