"""
MQTT challenge handling on top of the shared event service.

Durability rules:
- Each event item is processed in one transaction together with its
  submission attempt and the challenge checkpoint (results + next_index).
- Lock order: global mutation mutex, then the challenge row.
- The final response (with its state snapshot) is saved in the same
  transaction as the last item. Publication happens after commit, outside
  any transaction; unpublished saved responses can be retried.
- An identical terminal challenge replays its exact saved response text.
"""

import json
import logging
from dataclasses import dataclass
from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Count, F, Sum

from production.models import MqttChallenge, MqttChallengeLog, MqttRuntimeStatus
from production.modules.audit.service import storable
from production.modules.events.service import process_event
from production.modules.mqtt.protocol import (
    ChallengeRejected,
    build_response,
    serialize,
    topics,
    usable_challenge_id,
    validate_challenge,
)
from production.modules.state.queries import get_summary
from production.shared import clock
from production.shared.clock import to_utc_iso
from production.shared.contracts import ChallengeStatus, ErrorCode, Transport
from production.shared.locking import acquire_mutation_lock

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
STALE_AFTER_SECONDS = 45


@dataclass(frozen=True)
class ChallengeHandling:
    """What to publish for one delivery, and how it was handled."""

    response_text: str
    status: str
    kind: str  # NEW, RESUMED, REPLAY, CONFLICT, INVALID
    challenge_id: str | None
    challenge_pk: int | None = None


def _candidate():
    return str(settings.MQTT_CANDIDATE_ID)


def _unsaved_failure(challenge_id, code, message, kind):
    """A FAILED response that is not stored as the challenge's own response."""
    response = build_response(
        candidate_id=_candidate(),
        challenge_id=challenge_id,
        status=ChallengeStatus.FAILED,
        processed_at=clock.now(),
        results=[],
        state=get_summary(),
        error_code=code,
        message=message,
    )
    return ChallengeHandling(serialize(response), ChallengeStatus.FAILED, kind, challenge_id)


def _log(kind, challenge_id, code, reason, raw_text):
    MqttChallengeLog.objects.create(
        kind=kind,
        challenge_id=storable(challenge_id),
        error_code=code,
        reason=storable(reason),
        raw_body=storable(raw_text),
        received_at=clock.now(),
    )


def _ordered_result(result):
    """jsonb reorders keys; restore the documented field order for the response."""
    ordered = {key: result[key] for key in ("event_id", "status", "message") if key in result}
    ordered.update({k: v for k, v in result.items() if k not in ordered})
    return ordered


def _finish(challenge, status, code=None, message=None):
    """Save the terminal response with a state snapshot. Caller holds both locks."""
    processed_at = clock.now()
    response = build_response(
        candidate_id=challenge.candidate_id,
        challenge_id=challenge.challenge_id,
        status=status,
        processed_at=processed_at,
        results=[_ordered_result(r) for r in challenge.results],
        state=get_summary(),
        error_code=code,
        message=message,
    )
    challenge.status = status
    challenge.failure_code = code
    challenge.failure_message = message
    challenge.response_payload = serialize(response)
    challenge.completed_at = processed_at
    challenge.save()


def handle_mqtt_challenge(raw):
    """Handle one delivered challenge message and return what to publish."""
    raw_text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    candidate = _candidate()

    try:
        envelope = validate_challenge(raw, candidate)
    except ChallengeRejected as exc:
        with transaction.atomic():
            _log(MqttChallengeLog.Kind.INVALID, exc.challenge_id, exc.code, exc.message, raw_text)
            return _unsaved_failure(exc.challenge_id, exc.code, exc.message, "INVALID")

    with transaction.atomic():
        acquire_mutation_lock()
        challenge = (
            MqttChallenge.objects.select_for_update().filter(challenge_id=envelope.challenge_id).first()
        )
        now = clock.now()
        if challenge is not None:
            if challenge.body_digest != envelope.digest or challenge.canonical_body != envelope.canonical_body:
                message = "challenge_id was already used with a different body; original kept"
                _log(MqttChallengeLog.Kind.CONFLICT, envelope.challenge_id, ErrorCode.CHALLENGE_CONFLICT, message, raw_text)
                return _unsaved_failure(envelope.challenge_id, ErrorCode.CHALLENGE_CONFLICT, message, "CONFLICT")
            challenge.delivery_count += 1
            challenge.last_delivery_at = now
            if challenge.status != ChallengeStatus.PROCESSING:
                challenge.replay_count += 1
                challenge.save(update_fields=["delivery_count", "last_delivery_at", "replay_count"])
                return ChallengeHandling(
                    challenge.response_payload, challenge.status, "REPLAY", challenge.challenge_id, challenge.pk
                )
            challenge.save(update_fields=["delivery_count", "last_delivery_at"])
            kind = "RESUMED"
        else:
            challenge = MqttChallenge.objects.create(
                challenge_id=envelope.challenge_id,
                candidate_id=candidate,
                canonical_body=envelope.canonical_body,
                body_digest=envelope.digest,
                status=ChallengeStatus.PROCESSING,
                sent_at=envelope.sent_at,
                expires_at=envelope.expires_at,
                total_items=len(envelope.events),
                received_at=now,
                last_delivery_at=now,
            )
            kind = "NEW"
            if now >= envelope.expires_at:
                _finish(challenge, ChallengeStatus.FAILED, ErrorCode.CHALLENGE_EXPIRED,
                        "Challenge expired before processing started; no events were processed")
            elif challenge.total_items == 0:
                _finish(challenge, ChallengeStatus.COMPLETED)

    if challenge.status == ChallengeStatus.PROCESSING:
        _process_safely(challenge.pk)
    challenge.refresh_from_db()
    return ChallengeHandling(challenge.response_payload, challenge.status, kind, challenge.challenge_id, challenge.pk)


def run_challenge(challenge_pk):
    """
    Process remaining items from the last committed checkpoint. Safe to call
    concurrently or after a crash: every step re-reads the checkpoint under lock.
    """
    events = None
    while True:
        with transaction.atomic():
            acquire_mutation_lock()
            challenge = MqttChallenge.objects.select_for_update().get(pk=challenge_pk)
            if challenge.status != ChallengeStatus.PROCESSING:
                return
            if events is None:
                events = json.loads(challenge.canonical_body)["events"]
            if clock.now() >= challenge.expires_at:
                done = challenge.next_index
                _finish(
                    challenge,
                    ChallengeStatus.FAILED,
                    ErrorCode.CHALLENGE_EXPIRED,
                    f"Challenge expired after {done} of {challenge.total_items} events; "
                    "committed results are kept and remaining events were not processed",
                )
                logger.warning("Challenge %s expired after %s/%s items", challenge.challenge_id, done, challenge.total_items)
                return
            index = challenge.next_index
            result = process_event(events[index], Transport.MQTT, challenge=challenge, item_index=index)
            challenge.results = [*challenge.results, result.as_dict()]
            challenge.next_index = index + 1
            if challenge.next_index >= challenge.total_items:
                _finish(challenge, ChallengeStatus.COMPLETED)
                return
            challenge.save(update_fields=["results", "next_index"])


def resume_incomplete_challenges():
    """Finish challenges interrupted by a crash/restart; returns their pks."""
    pks = list(
        MqttChallenge.objects.filter(status=ChallengeStatus.PROCESSING).order_by("pk").values_list("pk", flat=True)
    )
    for pk in pks:
        _process_safely(pk)
    return pks


def _process_safely(challenge_pk):
    """
    Run a challenge. An unexpected (non-database) error finalizes it as
    FAILED / INTERNAL_ERROR with its committed results, so a failing item is not
    retried forever and the simulator still gets an answer. Database errors
    propagate: the challenge stays PROCESSING and is resumed once the database
    is reachable again.
    """
    try:
        run_challenge(challenge_pk)
    except DatabaseError:
        raise
    except Exception:
        logger.exception("Internal error processing challenge pk=%s", challenge_pk)
        _fail_internal(challenge_pk)


def _fail_internal(challenge_pk):
    with transaction.atomic():
        acquire_mutation_lock()
        challenge = MqttChallenge.objects.select_for_update().get(pk=challenge_pk)
        if challenge.status != ChallengeStatus.PROCESSING:
            return
        _finish(
            challenge,
            ChallengeStatus.FAILED,
            ErrorCode.INTERNAL_ERROR,
            f"Internal error while processing event {challenge.next_index + 1} of {challenge.total_items}; "
            "committed results are kept and remaining events were not processed",
        )


def internal_failure(raw):
    """Unsaved FAILED / INTERNAL_ERROR response for an error before a challenge was stored."""
    challenge_id = None
    try:
        body = json.loads(raw)
        if isinstance(body, dict):
            challenge_id = usable_challenge_id(body.get("challenge_id"))
    except (ValueError, TypeError):
        pass
    try:
        state = get_summary()
    except DatabaseError:
        state = None
    response = build_response(
        candidate_id=_candidate(),
        challenge_id=challenge_id,
        status=ChallengeStatus.FAILED,
        processed_at=clock.now(),
        results=[],
        state=state,
        error_code=ErrorCode.INTERNAL_ERROR,
        message="Internal error while handling the challenge",
    )
    return ChallengeHandling(serialize(response), ChallengeStatus.FAILED, "INTERNAL_ERROR", challenge_id)


def unpublished_responses():
    return list(
        MqttChallenge.objects.filter(response_payload__isnull=False, published_at__isnull=True)
        .order_by("pk")
        .values_list("pk", "challenge_id", "status", "response_payload")
    )


def record_publish_result(challenge_pk, published):
    updates = {"publish_attempts": F("publish_attempts") + 1}
    if published:
        updates["published_at"] = clock.now()
    MqttChallenge.objects.filter(pk=challenge_pk).update(**updates)


# ---------------------------------------------------------------- runtime status


def update_runtime_status(**fields):
    status, _ = MqttRuntimeStatus.objects.get_or_create(candidate_id=_candidate())
    for name, value in fields.items():
        setattr(status, name, storable(value) if isinstance(value, str) else value)
    status.save()
    return status


def get_mqtt_status():
    candidate = _candidate()
    now = clock.now()
    runtime = MqttRuntimeStatus.objects.filter(candidate_id=candidate).first()

    by_status = dict(MqttChallenge.objects.order_by().values_list("status").annotate(n=Count("pk")))
    deliveries = MqttChallenge.objects.aggregate(total=Sum("delivery_count"), replays=Sum("replay_count"))
    logs = dict(MqttChallengeLog.objects.order_by().values_list("kind").annotate(n=Count("pk")))

    if runtime is None:
        state = "NEVER_CONNECTED"
        heartbeat_age = None
    else:
        heartbeat_age = (now - runtime.last_heartbeat_at).total_seconds() if runtime.last_heartbeat_at else None
        if runtime.connection_state != "CONNECTED":
            state = runtime.connection_state
        elif heartbeat_age is None or heartbeat_age > STALE_AFTER_SECONDS:
            state = "STALE"
        else:
            state = "CONNECTED"

    def field(name):
        return getattr(runtime, name) if runtime else None

    return {
        "candidate_id": candidate,
        "client_id": field("client_id") or None,
        "connection_state": state,
        "reported_state": field("connection_state"),
        "is_fresh": state == "CONNECTED",
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
        "connected_at": to_utc_iso(field("connected_at")),
        "disconnected_at": to_utc_iso(field("disconnected_at")),
        "last_heartbeat_at": to_utc_iso(field("last_heartbeat_at")),
        "last_challenge_id": field("last_challenge_id"),
        "last_challenge_at": to_utc_iso(field("last_challenge_at")),
        "last_response_status": field("last_response_status"),
        "last_response_at": to_utc_iso(field("last_response_at")),
        "last_error": field("last_error"),
        "last_error_at": to_utc_iso(field("last_error_at")),
        "challenges": {
            "total": sum(by_status.values()),
            "completed": by_status.get(ChallengeStatus.COMPLETED, 0),
            "failed": by_status.get(ChallengeStatus.FAILED, 0),
            "processing": by_status.get(ChallengeStatus.PROCESSING, 0),
        },
        "deliveries": {
            "stored_challenge_deliveries": deliveries["total"] or 0,
            "replays": deliveries["replays"] or 0,
            "conflicts": logs.get(MqttChallengeLog.Kind.CONFLICT, 0),
            "invalid": logs.get(MqttChallengeLog.Kind.INVALID, 0),
        },
        "topics": topics(candidate),
        "server_time": to_utc_iso(now),
    }
