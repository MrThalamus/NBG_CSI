"""Stable vocabulary shared by REST, MQTT, services and the frontend contract."""

from dataclasses import dataclass

from django.db import models


class EventType(models.TextChoices):
    COUNT = "COUNT"
    VOID = "VOID"


class EventStatus(models.TextChoices):
    """Current state of a stored logical event."""

    ACCEPTED = "ACCEPTED"
    PENDING_REFERENCE = "PENDING_REFERENCE"
    REJECTED = "REJECTED"


class Outcome(models.TextChoices):
    """Historical result of one submission attempt."""

    ACCEPTED = "ACCEPTED"
    PENDING_REFERENCE = "PENDING_REFERENCE"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"


class Transport(models.TextChoices):
    REST = "REST"
    MQTT = "MQTT"


class AckOutcome(models.TextChoices):
    ACKED = "ACKED"
    ALREADY_ACKED = "ALREADY_ACKED"
    NOT_READY = "NOT_READY"
    NOT_FOUND = "NOT_FOUND"


class ChallengeStatus(models.TextChoices):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ErrorCode:
    # Request level
    MALFORMED_JSON = "MALFORMED_JSON"
    INVALID_REQUEST = "INVALID_REQUEST"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    # Item validation
    INVALID_ITEM = "INVALID_ITEM"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_FIELD = "INVALID_FIELD"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    INVALID_EVENT_TIME = "INVALID_EVENT_TIME"
    # Business rules
    EVENT_ID_CONFLICT = "EVENT_ID_CONFLICT"
    SELF_REFERENCE = "SELF_REFERENCE"
    TARGET_NOT_COUNT = "TARGET_NOT_COUNT"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    ALREADY_REVERSED = "ALREADY_REVERSED"
    # MQTT challenge level
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CANDIDATE_MISMATCH = "CANDIDATE_MISMATCH"
    UNSUPPORTED_PROTOCOL = "UNSUPPORTED_PROTOCOL"
    CHALLENGE_EXPIRED = "CHALLENGE_EXPIRED"
    CHALLENGE_CONFLICT = "CHALLENGE_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class EventResult:
    """Per-item result returned by REST and embedded in MQTT responses."""

    event_id: str | None
    status: str
    message: str
    error_code: str | None = None

    def as_dict(self):
        data = {"event_id": self.event_id, "status": self.status, "message": self.message}
        if self.error_code:
            data["error_code"] = self.error_code
        return data


# Documented input limits.
MAX_ID_LENGTH = 64
MAX_QUANTITY = 1_000_000
MAX_EVENT_TIME_LENGTH = 64
MAX_BATCH_ITEMS = 500
MAX_ACK_IDS = 500
MAX_CHALLENGE_EVENTS = 1000
