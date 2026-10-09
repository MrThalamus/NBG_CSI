"""Acknowledgement (supervisor review). Never changes totals or history."""

from django.db import transaction

from production.models import ProductionEvent
from production.shared import clock
from production.shared.contracts import AckOutcome, EventStatus
from production.shared.locking import acquire_mutation_lock


def acknowledge_events(event_ids):
    """
    Acknowledge IDs sequentially in one transaction, preserving request order
    (a repeated ID yields ACKED then ALREADY_ACKED).
    """
    results = []
    with transaction.atomic():
        acquire_mutation_lock()
        for event_id in event_ids:
            event = ProductionEvent.objects.select_for_update().filter(event_id=event_id).first()
            if event is None:
                results.append(_result(event_id, AckOutcome.NOT_FOUND, "No stored event with this ID"))
            elif event.processing_status != EventStatus.ACCEPTED:
                state = "waiting for its target" if event.processing_status == EventStatus.PENDING_REFERENCE else "rejected"
                results.append(_result(event_id, AckOutcome.NOT_READY, f"Event is not completed ({state})"))
            elif event.acknowledged_at is not None:
                results.append(_result(event_id, AckOutcome.ALREADY_ACKED, "Event was already acknowledged"))
            else:
                event.acknowledged_at = clock.now()
                event.save(update_fields=["acknowledged_at"])
                results.append(_result(event_id, AckOutcome.ACKED, "Event acknowledged"))
    return results


def _result(event_id, status, message):
    return {"event_id": event_id, "status": status, "message": message}
