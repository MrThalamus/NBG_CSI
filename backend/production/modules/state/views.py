from django.http import JsonResponse

from production.modules.state.queries import get_exceptions, get_pending, get_summary
from production.shared.contracts import ErrorCode
from production.shared.http import api_endpoint, json_error

VIEWS = ("summary", "pending", "exceptions")
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


@api_endpoint(["GET"])
def state_view(request):
    view = request.GET.get("view", "").strip() or "summary"
    if view not in VIEWS:
        return json_error(400, ErrorCode.INVALID_REQUEST, f"view must be one of: {', '.join(VIEWS)}")

    source_id = request.GET.get("source_id", "").strip() or None

    if view == "summary":
        return JsonResponse(get_summary(source_id))

    raw_limit = request.GET.get("limit", "").strip()
    if raw_limit:
        if not (raw_limit.isascii() and raw_limit.isdigit()) or not 1 <= int(raw_limit) <= MAX_LIMIT:
            return json_error(400, ErrorCode.INVALID_REQUEST, f"limit must be an integer from 1 to {MAX_LIMIT}")
        limit = int(raw_limit)
    else:
        limit = DEFAULT_LIMIT

    if view == "pending":
        return JsonResponse({"items": get_pending(source_id, limit)})
    return JsonResponse({"items": get_exceptions(source_id, limit)})
