"""
MQTT challenge protocol: envelope validation, canonical digest and response
builders. Pure functions; no network or database access.

Challenge identity (replay vs conflict) compares the canonical JSON of the
whole parsed challenge body: object keys sorted, array order and all values
significant. This is separate from per-event normalization, which is applied
later to each item by the shared event service.
"""

import hashlib
import json
from dataclasses import dataclass

from production.modules.events.validation import parse_aware_datetime
from production.shared.clock import to_utc_iso
from production.shared.contracts import MAX_CHALLENGE_EVENTS, ErrorCode

PROTOCOL_VERSION = "1.0"
COMMAND_PROCESS_EVENTS = "PROCESS_EVENTS"
MAX_CHALLENGE_ID_LENGTH = 128


class ChallengeRejected(Exception):
    """Envelope-level failure. challenge_id is set only when it was usable."""

    def __init__(self, code, message, challenge_id=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.challenge_id = challenge_id


@dataclass(frozen=True)
class ChallengeEnvelope:
    challenge_id: str
    candidate_id: str
    sent_at: object
    expires_at: object
    events: list
    canonical_body: str
    digest: str


def topics(candidate_id):
    base = f"fse-01/{candidate_id}"
    return {
        "challenge": f"{base}/challenge",
        "response": f"{base}/response",
        "status": f"{base}/status",
    }


def canonicalize(parsed):
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(canonical_body):
    return hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()


def _usable_challenge_id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CHALLENGE_ID_LENGTH:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value


def _candidate_matches(value, expected):
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return value == expected
    if isinstance(value, int):
        return str(value) == expected
    return False


def _timestamp(body, field, challenge_id):
    raw = body.get(field)
    if not isinstance(raw, str):
        raise ChallengeRejected(ErrorCode.VALIDATION_ERROR, f"{field} must be an ISO 8601 string", challenge_id)
    try:
        return parse_aware_datetime(raw)
    except ValueError as exc:
        raise ChallengeRejected(ErrorCode.VALIDATION_ERROR, f"{field}: {exc}", challenge_id) from None


def validate_challenge(raw, candidate_id):
    """Return a ChallengeEnvelope or raise ChallengeRejected. No production processing happens here."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise ChallengeRejected(ErrorCode.VALIDATION_ERROR, "Challenge is not UTF-8 text") from None
    try:
        body = json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        raise ChallengeRejected(ErrorCode.VALIDATION_ERROR, "Challenge is not valid JSON") from None
    if not isinstance(body, dict):
        raise ChallengeRejected(ErrorCode.VALIDATION_ERROR, "Challenge must be a JSON object")

    challenge_id = _usable_challenge_id(body.get("challenge_id"))
    if challenge_id is None:
        raise ChallengeRejected(ErrorCode.VALIDATION_ERROR, "challenge_id must be a non-empty string")

    if body.get("protocol_version") != PROTOCOL_VERSION:
        raise ChallengeRejected(
            ErrorCode.UNSUPPORTED_PROTOCOL,
            f"protocol_version must be {PROTOCOL_VERSION}",
            challenge_id,
        )
    if not _candidate_matches(body.get("candidate_id"), candidate_id):
        raise ChallengeRejected(
            ErrorCode.CANDIDATE_MISMATCH,
            f"Challenge is not addressed to candidate {candidate_id}",
            challenge_id,
        )
    if body.get("command") != COMMAND_PROCESS_EVENTS:
        raise ChallengeRejected(
            ErrorCode.VALIDATION_ERROR, f"command must be {COMMAND_PROCESS_EVENTS}", challenge_id
        )

    sent_at = _timestamp(body, "sent_at", challenge_id)
    expires_at = _timestamp(body, "expires_at", challenge_id)
    if expires_at <= sent_at:
        raise ChallengeRejected(ErrorCode.VALIDATION_ERROR, "expires_at must be after sent_at", challenge_id)

    events = body.get("events")
    if not isinstance(events, list):
        raise ChallengeRejected(ErrorCode.VALIDATION_ERROR, "events must be an array", challenge_id)
    if len(events) > MAX_CHALLENGE_EVENTS:
        raise ChallengeRejected(
            ErrorCode.VALIDATION_ERROR, f"events may contain at most {MAX_CHALLENGE_EVENTS} items", challenge_id
        )

    canonical = canonicalize(body)
    return ChallengeEnvelope(
        challenge_id=challenge_id,
        candidate_id=candidate_id,
        sent_at=sent_at,
        expires_at=expires_at,
        events=events,
        canonical_body=canonical,
        digest=digest_of(canonical),
    )


def _reject_constant(name):
    raise ValueError(f"Invalid JSON value: {name}")


def build_response(*, candidate_id, challenge_id, status, processed_at, results, state, error_code=None, message=None):
    response = {
        "protocol_version": PROTOCOL_VERSION,
        "candidate_id": candidate_id,
        "challenge_id": challenge_id,
        "status": status,
        "processed_at": to_utc_iso(processed_at),
        "results": results,
        "state": state,
    }
    if error_code:
        response["error_code"] = error_code
        response["message"] = message
    return response


def serialize(message):
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


def status_message(*, candidate_id, client_id, status, sent_at):
    return serialize(
        {
            "protocol_version": PROTOCOL_VERSION,
            "candidate_id": candidate_id,
            "client_id": client_id,
            "status": status,
            "sent_at": to_utc_iso(sent_at),
        }
    )
