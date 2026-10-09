import logging

from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError
from django.http import JsonResponse

from production.modules.audit.service import record_submission_attempt
from production.modules.events.service import process_batch
from production.shared.contracts import MAX_BATCH_ITEMS, ErrorCode, Outcome, Transport
from production.shared.http import BadJson, api_endpoint, json_error, parse_json_bytes

logger = logging.getLogger(__name__)


def _audit_request_failure(error_code, reason, raw_body=None, payload=None):
    """Audit a request-level rejection; never let an audit failure hide the 4xx answer."""
    try:
        record_submission_attempt(
            transport=Transport.REST,
            outcome=Outcome.REJECTED,
            error_code=error_code,
            reason=reason,
            raw_body=raw_body,
            payload=payload,
            event_id=None,
            source_id=None,
        )
    except DatabaseError:
        logger.exception("Could not audit rejected request (%s)", error_code)


@api_endpoint(["POST"])
def events_view(request):
    try:
        body = request.body
    except RequestDataTooBig:
        reason = "Request body is too large"
        _audit_request_failure(ErrorCode.PAYLOAD_TOO_LARGE, reason)
        return json_error(413, ErrorCode.PAYLOAD_TOO_LARGE, reason)

    raw_text = body.decode("utf-8", errors="replace")
    try:
        data = parse_json_bytes(body)
    except BadJson as exc:
        _audit_request_failure(ErrorCode.MALFORMED_JSON, str(exc), raw_body=raw_text)
        return json_error(400, ErrorCode.MALFORMED_JSON, str(exc))

    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        reason = "Body must be a JSON object (one event) or an array of events"
        _audit_request_failure(ErrorCode.INVALID_REQUEST, reason, raw_body=raw_text)
        return json_error(400, ErrorCode.INVALID_REQUEST, reason)

    if len(items) > MAX_BATCH_ITEMS:
        reason = f"A batch may contain at most {MAX_BATCH_ITEMS} events"
        _audit_request_failure(ErrorCode.PAYLOAD_TOO_LARGE, reason, raw_body=raw_text)
        return json_error(413, ErrorCode.PAYLOAD_TOO_LARGE, reason)

    results = process_batch(items, Transport.REST)
    return JsonResponse({"results": [result.as_dict() for result in results]})
