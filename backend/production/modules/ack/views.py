from django.http import JsonResponse

from production.modules.ack.service import acknowledge_events
from production.shared.contracts import MAX_ACK_IDS, MAX_ID_LENGTH, ErrorCode
from production.shared.http import BadJson, api_endpoint, json_error, parse_json_bytes


def _parse_ack_ids(data):
    """Return a list of trimmed IDs, or raise ValueError describing the bad shape."""
    if not isinstance(data, dict) or set(data) != {"event_ids"}:
        raise ValueError('Body must be an object with exactly one key: "event_ids"')
    ids = data["event_ids"]
    if not isinstance(ids, list):
        raise ValueError("event_ids must be an array of strings")
    if len(ids) > MAX_ACK_IDS:
        raise ValueError(f"event_ids may contain at most {MAX_ACK_IDS} IDs")
    cleaned = []
    for position, value in enumerate(ids):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"event_ids[{position}] must be a non-empty string")
        if len(value.strip()) > MAX_ID_LENGTH:
            raise ValueError(f"event_ids[{position}] must be at most {MAX_ID_LENGTH} characters")
        cleaned.append(value.strip())
    return cleaned


@api_endpoint(["POST"])
def ack_view(request):
    try:
        data = parse_json_bytes(request.body)
        event_ids = _parse_ack_ids(data)
    except BadJson as exc:
        return json_error(400, ErrorCode.MALFORMED_JSON, str(exc))
    except ValueError as exc:
        return json_error(400, ErrorCode.INVALID_REQUEST, str(exc))
    return JsonResponse({"results": acknowledge_events(event_ids)})
