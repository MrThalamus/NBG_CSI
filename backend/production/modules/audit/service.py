"""Durable submission audit records."""

from production.models import SubmissionAttempt
from production.modules.events.validation import submitted_identity
from production.shared import clock


def storable(value):
    """PostgreSQL text/jsonb cannot hold NUL characters; escape them so audits never fail."""
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, list):
        return [storable(v) for v in value]
    if isinstance(value, dict):
        return {storable(k): storable(v) for k, v in value.items()}
    return value


def record_submission_attempt(
    *,
    transport,
    outcome,
    payload=None,
    raw_body=None,
    error_code=None,
    reason="",
    logical_event=None,
    challenge=None,
    item_index=None,
    event_id=None,
    source_id=None,
):
    """
    Store one attempt. When IDs are not given explicitly, they are taken from
    the submitted payload (trimmed) so invalid items stay filterable by source.
    """
    if event_id is None and source_id is None:
        event_id, source_id = submitted_identity(payload)
    return SubmissionAttempt.objects.create(
        transport=transport,
        outcome=outcome,
        error_code=error_code,
        reason=storable(reason),
        raw_payload=storable(payload),
        raw_body=storable(raw_body),
        event_id=storable(event_id),
        source_id=storable(source_id),
        logical_event=logical_event,
        challenge=challenge,
        item_index=item_index,
        received_at=clock.now(),
    )
