"""
Pure normalization/validation of one event payload. No database access.

The normalized dict (all six fields, UTC time text, missing optionals as null)
is the identity used for duplicate/conflict comparison.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from production.shared.clock import to_utc_iso
from production.shared.contracts import (
    MAX_EVENT_TIME_LENGTH,
    MAX_ID_LENGTH,
    MAX_QUANTITY,
    ErrorCode,
    EventType,
)

EVENT_FIELDS = ("source_id", "event_id", "type", "quantity", "target_event_id", "event_time")


class EventValidationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class NormalizedEvent:
    source_id: str
    event_id: str
    type: str
    quantity: int | None
    target_event_id: str | None
    event_time: datetime

    def as_payload(self):
        return {
            "source_id": self.source_id,
            "event_id": self.event_id,
            "type": self.type,
            "quantity": self.quantity,
            "target_event_id": self.target_event_id,
            "event_time": to_utc_iso(self.event_time),
        }


def _required_id(payload, field):
    if field not in payload or payload[field] is None:
        raise EventValidationError(ErrorCode.MISSING_FIELD, f"{field} is required")
    value = payload[field]
    if not isinstance(value, str):
        raise EventValidationError(ErrorCode.INVALID_FIELD, f"{field} must be a string")
    value = value.strip()
    if not value:
        raise EventValidationError(ErrorCode.INVALID_FIELD, f"{field} must not be empty")
    if len(value) > MAX_ID_LENGTH:
        raise EventValidationError(
            ErrorCode.INVALID_FIELD, f"{field} must be at most {MAX_ID_LENGTH} characters"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise EventValidationError(ErrorCode.INVALID_FIELD, f"{field} must not contain control characters")
    return value


def _parse_event_time(payload):
    if "event_time" not in payload or payload["event_time"] is None:
        raise EventValidationError(ErrorCode.MISSING_FIELD, "event_time is required")
    raw = payload["event_time"]
    if not isinstance(raw, str):
        raise EventValidationError(ErrorCode.INVALID_EVENT_TIME, "event_time must be an ISO 8601 string")
    if len(raw) > MAX_EVENT_TIME_LENGTH:
        raise EventValidationError(ErrorCode.INVALID_EVENT_TIME, "event_time is too long")
    try:
        parsed = parse_aware_datetime(raw)
    except ValueError as exc:
        raise EventValidationError(ErrorCode.INVALID_EVENT_TIME, str(exc)) from None
    return parsed


def parse_aware_datetime(raw):
    """Parse ISO 8601 date-time text that includes a timezone; return it in UTC."""
    text = raw.strip()
    if "T" not in text and " " not in text:
        raise ValueError("timestamp must include a date and a time")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("timestamp is not valid ISO 8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone (e.g. Z or +00:00)")
    return parsed.astimezone(timezone.utc)


def _is_json_integer(value):
    # bool is a subclass of int in Python; it must not pass.
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_and_validate_event(payload):
    """Return a NormalizedEvent or raise EventValidationError."""
    if not isinstance(payload, dict):
        raise EventValidationError(ErrorCode.INVALID_ITEM, "Each event must be a JSON object")

    unknown = sorted(k for k in payload if k not in EVENT_FIELDS)
    if unknown:
        raise EventValidationError(
            ErrorCode.UNKNOWN_FIELD, f"Unsupported field(s): {', '.join(unknown)}"
        )

    source_id = _required_id(payload, "source_id")
    event_id = _required_id(payload, "event_id")

    event_type = payload.get("type")
    if event_type is None:
        raise EventValidationError(ErrorCode.MISSING_FIELD, "type is required")
    if not isinstance(event_type, str) or event_type not in (EventType.COUNT, EventType.VOID):
        raise EventValidationError(ErrorCode.INVALID_FIELD, "type must be exactly COUNT or VOID")

    quantity = payload.get("quantity")
    target = payload.get("target_event_id")

    if event_type == EventType.COUNT:
        if quantity is None:
            raise EventValidationError(ErrorCode.INVALID_QUANTITY, "COUNT requires a quantity")
        if not _is_json_integer(quantity):
            raise EventValidationError(
                ErrorCode.INVALID_QUANTITY, "quantity must be a whole JSON number (not text, boolean or fraction)"
            )
        if quantity <= 0:
            raise EventValidationError(ErrorCode.INVALID_QUANTITY, "quantity must be greater than zero")
        if quantity > MAX_QUANTITY:
            raise EventValidationError(
                ErrorCode.INVALID_QUANTITY, f"quantity must be at most {MAX_QUANTITY}"
            )
        if target is not None:
            raise EventValidationError(ErrorCode.INVALID_FIELD, "COUNT must not have a target_event_id")
    else:
        if quantity is not None:
            raise EventValidationError(
                ErrorCode.INVALID_QUANTITY, "VOID must not have a quantity; the target COUNT's quantity is used"
            )
        if target is None:
            raise EventValidationError(ErrorCode.MISSING_FIELD, "VOID requires target_event_id")
        target = _required_id(payload, "target_event_id")

    event_time = _parse_event_time(payload)

    return NormalizedEvent(
        source_id=source_id,
        event_id=event_id,
        type=str(event_type),
        quantity=quantity,
        target_event_id=target,
        event_time=event_time,
    )


def submitted_identity(payload):
    """Best-effort (event_id, source_id) from an item for auditing, even when invalid."""
    if not isinstance(payload, dict):
        return None, None

    def usable(value):
        if isinstance(value, str):
            value = value.strip()
            if value and len(value) <= 255:
                return value
        return None

    return usable(payload.get("event_id")), usable(payload.get("source_id"))
