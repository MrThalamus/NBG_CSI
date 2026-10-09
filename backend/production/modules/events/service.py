"""
Event processing shared by REST and MQTT.

Each event is handled in its own transaction (a savepoint when the caller,
e.g. an MQTT challenge checkpoint, already holds one) under the global
mutation lock. The submission attempt is written in the same transaction as
the domain change, so audit and effect commit or roll back together.
"""

import logging

from django.db import IntegrityError, transaction

from production.models import AppliedReversal, ProductionEvent, ProductionSource
from production.modules.audit.service import record_submission_attempt
from production.modules.events.validation import (
    EventValidationError,
    normalize_and_validate_event,
    submitted_identity,
)
from production.shared import clock
from production.shared.contracts import (
    ErrorCode,
    EventResult,
    EventStatus,
    EventType,
    Outcome,
)
from production.shared.locking import acquire_mutation_lock

logger = logging.getLogger(__name__)


class _Decision:
    """What processing decided for one valid event, before auditing it."""

    def __init__(self, outcome, message, error_code=None, logical_event=None):
        self.outcome = outcome
        self.message = message
        self.error_code = error_code
        self.logical_event = logical_event


def process_batch(items, transport):
    """Process items in submitted order; one transaction per item (partial success)."""
    return [process_event(item, transport, item_index=index) for index, item in enumerate(items)]


def process_event(payload, transport, *, challenge=None, item_index=None):
    """Validate, apply and audit one submitted item. Returns an EventResult."""
    try:
        event = normalize_and_validate_event(payload)
    except EventValidationError as exc:
        event_id, _ = submitted_identity(payload)
        with transaction.atomic():
            acquire_mutation_lock()
            record_submission_attempt(
                transport=transport,
                outcome=Outcome.REJECTED,
                payload=payload,
                error_code=exc.code,
                reason=exc.message,
                challenge=challenge,
                item_index=item_index,
            )
        return EventResult(event_id, Outcome.REJECTED, exc.message, exc.code)

    with transaction.atomic():
        acquire_mutation_lock()
        decision = _apply_valid_event(event)
        record_submission_attempt(
            transport=transport,
            outcome=decision.outcome,
            payload=payload,
            error_code=decision.error_code,
            reason=decision.message,
            logical_event=decision.logical_event,
            challenge=challenge,
            item_index=item_index,
            event_id=event.event_id,
            source_id=event.source_id,
        )
    return EventResult(event.event_id, decision.outcome, decision.message, decision.error_code)


def _apply_valid_event(event):
    existing = ProductionEvent.objects.filter(event_id=event.event_id).first()
    if existing is not None:
        return _classify_existing(existing, event)
    if event.type == EventType.COUNT:
        return process_count(event)
    return process_void(event)


def _classify_existing(existing, event):
    if existing.normalized_payload == event.as_payload():
        return _Decision(
            Outcome.DUPLICATE,
            "Duplicate of an already stored event; not applied again",
            logical_event=existing,
        )
    return _Decision(
        Outcome.CONFLICT,
        "event_id already used by a different event; original kept unchanged",
        ErrorCode.EVENT_ID_CONFLICT,
        logical_event=existing,
    )


def _insert_event(event, **fields):
    """Insert inside a savepoint so an unexpected IntegrityError cannot poison the transaction."""
    ProductionSource.objects.get_or_create(source_id=event.source_id)
    with transaction.atomic():
        return ProductionEvent.objects.create(
            event_id=event.event_id,
            source_id=event.source_id,
            type=event.type,
            quantity=event.quantity,
            target_event_id=event.target_event_id,
            normalized_payload=event.as_payload(),
            event_time=event.event_time,
            received_at=clock.now(),
            **fields,
        )


def process_count(event):
    try:
        stored = _insert_event(event, processing_status=EventStatus.ACCEPTED, processed_at=clock.now())
    except IntegrityError:
        # Not expected under the global lock; classify against the row that won.
        logger.warning("IntegrityError inserting COUNT %s; reclassifying", event.event_id)
        return _classify_existing(ProductionEvent.objects.get(event_id=event.event_id), event)
    resolve_pending_voids(stored)
    return _Decision(Outcome.ACCEPTED, "COUNT accepted", logical_event=stored)


def _reversal_problem(void_source_id, target):
    """Return (error_code, reason) if this VOID may not reverse target, else None."""
    if target.type != EventType.COUNT:
        return ErrorCode.TARGET_NOT_COUNT, f"Target {target.event_id} is a VOID, not a COUNT"
    if target.source_id != void_source_id:
        return (
            ErrorCode.SOURCE_MISMATCH,
            f"Target {target.event_id} belongs to source {target.source_id}, not {void_source_id}",
        )
    existing = AppliedReversal.objects.filter(target=target).select_related("void").first()
    if existing is not None:
        return (
            ErrorCode.ALREADY_REVERSED,
            f"Target {target.event_id} was already reversed by {existing.void.event_id}",
        )
    return None


def _apply_reversal(void, target):
    now = clock.now()
    AppliedReversal.objects.create(target=target, void=void, quantity=target.quantity, applied_at=now)
    void.processing_status = EventStatus.ACCEPTED
    void.processed_at = now
    void.acknowledged_at = now  # completed VOIDs are auto-acknowledged
    void.failure_code = None
    void.failure_reason = None
    void.save(update_fields=["processing_status", "processed_at", "acknowledged_at", "failure_code", "failure_reason"])


def process_void(event):
    if event.target_event_id == event.event_id:
        return _Decision(Outcome.REJECTED, "A VOID cannot target itself", ErrorCode.SELF_REFERENCE)

    target = (
        ProductionEvent.objects.select_for_update().filter(event_id=event.target_event_id).first()
    )
    if target is not None:
        problem = _reversal_problem(event.source_id, target)
        if problem:
            code, reason = problem
            return _Decision(Outcome.REJECTED, reason, code)
        try:
            void = _insert_event(event, processing_status=EventStatus.PENDING_REFERENCE)
        except IntegrityError:
            logger.warning("IntegrityError inserting VOID %s; reclassifying", event.event_id)
            return _classify_existing(ProductionEvent.objects.get(event_id=event.event_id), event)
        _apply_reversal(void, target)
        resolve_pending_voids(void)
        return _Decision(
            Outcome.ACCEPTED,
            f"VOID applied; reversed {target.event_id} (-{target.quantity})",
            logical_event=void,
        )

    try:
        void = _insert_event(event, processing_status=EventStatus.PENDING_REFERENCE)
    except IntegrityError:
        logger.warning("IntegrityError inserting VOID %s; reclassifying", event.event_id)
        return _classify_existing(ProductionEvent.objects.get(event_id=event.event_id), event)
    resolve_pending_voids(void)
    return _Decision(
        Outcome.PENDING_REFERENCE,
        f"Target {event.target_event_id} not received yet; VOID will apply when it arrives",
        logical_event=void,
    )


def resolve_pending_voids(arrived):
    """
    Resolve VOIDs waiting for `arrived.event_id`, in durable insertion order.

    The earliest valid waiting VOID wins; invalid earlier candidates do not block
    it. Every other waiting VOID is rejected with a reason. Runs inside the
    caller's transaction so target processing and resolution commit together.
    """
    waiting = list(
        ProductionEvent.objects.select_for_update()
        .filter(
            type=EventType.VOID,
            processing_status=EventStatus.PENDING_REFERENCE,
            target_event_id=arrived.event_id,
        )
        .exclude(pk=arrived.pk)
        .order_by("pk")
    )
    for void in waiting:
        problem = _reversal_problem(void.source_id, arrived)
        if problem is None:
            _apply_reversal(void, arrived)
            continue
        code, reason = problem
        void.processing_status = EventStatus.REJECTED
        void.failure_code = code
        void.failure_reason = reason
        void.processed_at = clock.now()
        void.save(update_fields=["processing_status", "failure_code", "failure_reason", "processed_at"])
