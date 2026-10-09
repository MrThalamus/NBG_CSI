"""
Read-side queries. All authoritative numbers come from PostgreSQL.

The summary is one SQL statement (scalar subqueries), so all seven counters
come from a single snapshot even under READ COMMITTED. duplicates, conflicts and
rejected_submissions count submission attempts, filtered by the attempt's own source.
"""

from contextlib import contextmanager

from django.db import connection, transaction

from production.models import AppliedReversal, ProductionEvent, SubmissionAttempt
from production.shared.clock import to_utc_iso
from production.shared.contracts import EventStatus, EventType, Outcome

SUMMARY_FIELDS = (
    "net_total", "processed_events", "pending_ack", "unresolved", "duplicates", "conflicts",
    "rejected_submissions",
)


def _summary_sql(filtered):
    events = ProductionEvent._meta.db_table
    reversals = AppliedReversal._meta.db_table
    attempts = SubmissionAttempt._meta.db_table
    ev = "AND e.source_id = %s" if filtered else ""
    at = "AND a.source_id = %s" if filtered else ""
    return f"""
        SELECT
            COALESCE((SELECT SUM(e.quantity) FROM {events} e
                      WHERE e.type = '{EventType.COUNT}' AND e.processing_status = '{EventStatus.ACCEPTED}' {ev}), 0)
          - COALESCE((SELECT SUM(r.quantity) FROM {reversals} r
                      JOIN {events} e ON e.id = r.target_id
                      WHERE TRUE {ev}), 0) AS net_total,
            (SELECT COUNT(*) FROM {events} e
             WHERE e.processing_status = '{EventStatus.ACCEPTED}' {ev}) AS processed_events,
            (SELECT COUNT(*) FROM {events} e
             WHERE e.processing_status = '{EventStatus.ACCEPTED}' AND e.acknowledged_at IS NULL {ev}) AS pending_ack,
            (SELECT COUNT(*) FROM {events} e
             WHERE e.processing_status = '{EventStatus.PENDING_REFERENCE}' {ev}) AS unresolved,
            (SELECT COUNT(*) FROM {attempts} a
             WHERE a.outcome = '{Outcome.DUPLICATE}' {at}) AS duplicates,
            (SELECT COUNT(*) FROM {attempts} a
             WHERE a.outcome = '{Outcome.CONFLICT}' {at}) AS conflicts,
            (SELECT COUNT(*) FROM {attempts} a
             WHERE a.outcome = '{Outcome.REJECTED}' {at}) AS rejected_submissions
    """


def get_summary(source_id=None):
    filtered = source_id is not None
    sql = _summary_sql(filtered)
    params = [source_id] * sql.count("%s")
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return {name: int(value) for name, value in zip(SUMMARY_FIELDS, row)}


@contextmanager
def read_snapshot():
    """
    Run several read queries against one snapshot (REPEATABLE READ, read only).
    Inside an existing transaction (e.g. tests, MQTT checkpoints) the caller's
    transaction is reused instead.
    """
    if connection.in_atomic_block:
        yield
        return
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield


def get_pending(source_id=None, limit=200):
    """Completed, unacknowledged COUNTs (including reversed ones), oldest first."""
    qs = (
        ProductionEvent.objects.filter(
            type=EventType.COUNT,
            processing_status=EventStatus.ACCEPTED,
            acknowledged_at__isnull=True,
        )
        .select_related("reversal__void")
        .order_by("pk")
    )
    if source_id is not None:
        qs = qs.filter(source_id=source_id)
    items = []
    for event in qs[:limit]:
        reversal = getattr(event, "reversal", None)
        items.append(
            {
                "event_id": event.event_id,
                "source_id": event.source_id,
                "type": event.type,
                "quantity": event.quantity,
                "event_time": to_utc_iso(event.event_time),
                "received_at": to_utc_iso(event.received_at),
                "status": event.processing_status,
                "reversed": reversal is not None,
                "reversed_by": reversal.void.event_id if reversal else None,
                "reversed_at": to_utc_iso(reversal.applied_at) if reversal else None,
            }
        )
    return items


def _payload_field(payload, name):
    return payload.get(name) if isinstance(payload, dict) else None


def get_exceptions(source_id=None, limit=200):
    """
    Unresolved VOIDs, VOIDs rejected during resolution, rejected submissions and
    conflict attempts; newest first. Each failure appears once: a rejected
    submission never creates a logical event, and a pending VOID's own attempt
    is PENDING_REFERENCE, not REJECTED.
    """
    entries = []

    with read_snapshot():
        events = ProductionEvent.objects.filter(
            processing_status__in=[EventStatus.PENDING_REFERENCE, EventStatus.REJECTED]
        )
        if source_id is not None:
            events = events.filter(source_id=source_id)
        for event in events.order_by("-pk")[:limit]:
            unresolved = event.processing_status == EventStatus.PENDING_REFERENCE
            occurred = event.received_at if unresolved else (event.processed_at or event.received_at)
            entries.append(
                (
                    occurred,
                    event.pk,
                    {
                        "kind": "UNRESOLVED_REFERENCE" if unresolved else "REJECTED_EVENT",
                        "ref": f"event:{event.event_id}",
                        "event_id": event.event_id,
                        "source_id": event.source_id,
                        "type": event.type,
                        "quantity": event.quantity,
                        "target_event_id": event.target_event_id,
                        "event_time": to_utc_iso(event.event_time),
                        "transport": None,
                        "error_code": "UNRESOLVED_REFERENCE" if unresolved else event.failure_code,
                        "reason": (
                            f"Waiting for target {event.target_event_id}"
                            if unresolved
                            else event.failure_reason
                        ),
                        "occurred_at": to_utc_iso(occurred),
                    },
                )
            )

        attempts = SubmissionAttempt.objects.filter(outcome__in=[Outcome.REJECTED, Outcome.CONFLICT])
        if source_id is not None:
            attempts = attempts.filter(source_id=source_id)
        for attempt in attempts.order_by("-pk")[:limit]:
            payload = attempt.raw_payload
            entries.append(
                (
                    attempt.received_at,
                    attempt.pk,
                    {
                        "kind": "CONFLICT" if attempt.outcome == Outcome.CONFLICT else "REJECTED_SUBMISSION",
                        "ref": f"attempt:{attempt.pk}",
                        "event_id": attempt.event_id,
                        "source_id": attempt.source_id,
                        "type": _payload_field(payload, "type"),
                        "quantity": _payload_field(payload, "quantity"),
                        "target_event_id": _payload_field(payload, "target_event_id"),
                        "event_time": _payload_field(payload, "event_time"),
                        "transport": attempt.transport,
                        "error_code": attempt.error_code,
                        "reason": attempt.reason,
                        "occurred_at": to_utc_iso(attempt.received_at),
                    },
                )
            )

    entries.sort(key=lambda entry: (entry[0], entry[2]["kind"], entry[1]), reverse=True)
    return [entry[2] for entry in entries[:limit]]
