"""
Persistence model for production events, audit attempts and MQTT challenges.

Model declarations live together to keep migrations simple; business
operations live in `production.modules.*`.
"""

from django.db import models
from django.db.models import Q

from production.shared.contracts import (
    ChallengeStatus,
    EventStatus,
    EventType,
    Outcome,
    Transport,
)

ID_MAX_LENGTH = 64
ATTEMPT_ID_MAX_LENGTH = 255


class ProductionSource(models.Model):
    """A production line/device. Created when its first valid event is stored."""

    source_id = models.CharField(max_length=ID_MAX_LENGTH, unique=True)
    display_name = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["source_id"]

    def __str__(self):
        return self.source_id


class ProductionEvent(models.Model):
    """
    A logical event: an accepted COUNT, or a VOID that was accepted (applied),
    is waiting for its target, or was rejected while resolving.

    The auto-increment primary key is the durable insertion sequence used to
    order competing pending VOIDs.
    """

    event_id = models.CharField(max_length=ID_MAX_LENGTH, unique=True)
    source = models.ForeignKey(
        ProductionSource,
        to_field="source_id",
        on_delete=models.PROTECT,
        related_name="events",
    )
    type = models.CharField(max_length=8, choices=EventType.choices)
    quantity = models.PositiveIntegerField(null=True, blank=True)
    # Plain string: a pending VOID may name an event that does not exist yet.
    target_event_id = models.CharField(max_length=ID_MAX_LENGTH, null=True, blank=True)
    normalized_payload = models.JSONField()
    event_time = models.DateTimeField()
    received_at = models.DateTimeField()
    processing_status = models.CharField(max_length=20, choices=EventStatus.choices)
    failure_code = models.CharField(max_length=40, null=True, blank=True)
    failure_reason = models.TextField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["source", "processing_status"], name="event_source_status_idx"),
            models.Index(fields=["processing_status", "acknowledged_at"], name="event_status_ack_idx"),
            models.Index(
                fields=["target_event_id"],
                name="event_pending_target_idx",
                condition=Q(processing_status="PENDING_REFERENCE"),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                name="event_type_valid",
                condition=Q(type__in=[EventType.COUNT, EventType.VOID]),
            ),
            models.CheckConstraint(
                name="event_status_valid",
                condition=Q(processing_status__in=[s.value for s in EventStatus]),
            ),
            models.CheckConstraint(
                name="event_count_shape",
                condition=~Q(type=EventType.COUNT)
                | Q(quantity__isnull=False, quantity__gt=0, target_event_id__isnull=True),
            ),
            models.CheckConstraint(
                name="event_void_shape",
                condition=~Q(type=EventType.VOID)
                | Q(quantity__isnull=True, target_event_id__isnull=False),
            ),
            models.CheckConstraint(
                name="event_count_always_accepted",
                condition=~Q(type=EventType.COUNT) | Q(processing_status=EventStatus.ACCEPTED),
            ),
            models.CheckConstraint(
                name="event_ack_only_when_accepted",
                condition=Q(acknowledged_at__isnull=True) | Q(processing_status=EventStatus.ACCEPTED),
            ),
        ]

    def __str__(self):
        return f"{self.type} {self.event_id} ({self.processing_status})"


class AppliedReversal(models.Model):
    """Links one accepted VOID to the one COUNT it reversed."""

    target = models.OneToOneField(
        ProductionEvent, on_delete=models.PROTECT, related_name="reversal"
    )
    void = models.OneToOneField(
        ProductionEvent, on_delete=models.PROTECT, related_name="applied_reversal"
    )
    # Copied from the target COUNT at reversal time; never from the VOID payload.
    quantity = models.PositiveIntegerField()
    applied_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(name="reversal_not_self", condition=~Q(target=models.F("void"))),
        ]


class SubmissionAttempt(models.Model):
    """Every submission (REST or MQTT, valid or not) and its outcome."""

    transport = models.CharField(max_length=8, choices=Transport.choices)
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    error_code = models.CharField(max_length=40, null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    # Parsed JSON item as submitted, or the raw body when it could not be parsed.
    raw_payload = models.JSONField(null=True, blank=True)
    raw_body = models.TextField(null=True, blank=True)
    # IDs as submitted (trimmed) when usable; kept even for rejected items.
    event_id = models.CharField(max_length=ATTEMPT_ID_MAX_LENGTH, null=True, blank=True)
    source_id = models.CharField(max_length=ATTEMPT_ID_MAX_LENGTH, null=True, blank=True)
    # The stored logical event this attempt created or matched (duplicate/conflict).
    logical_event = models.ForeignKey(
        ProductionEvent,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    challenge = models.ForeignKey(
        "MqttChallenge",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    item_index = models.PositiveIntegerField(null=True, blank=True)
    received_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["outcome", "source_id"], name="attempt_outcome_source_idx"),
            models.Index(fields=["event_id"], name="attempt_event_id_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                name="attempt_outcome_valid",
                condition=Q(outcome__in=[o.value for o in Outcome]),
            ),
        ]


class MqttChallenge(models.Model):
    """A validly-enveloped challenge, its checkpoint and its saved response."""

    challenge_id = models.CharField(max_length=128, unique=True)
    candidate_id = models.CharField(max_length=64)
    canonical_body = models.TextField()
    body_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=ChallengeStatus.choices)
    sent_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    total_items = models.PositiveIntegerField()
    next_index = models.PositiveIntegerField(default=0)
    results = models.JSONField(default=list)
    # Exact serialized response text; replays publish these bytes unchanged.
    response_payload = models.TextField(null=True, blank=True)
    failure_code = models.CharField(max_length=40, null=True, blank=True)
    failure_message = models.TextField(null=True, blank=True)
    received_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    last_delivery_at = models.DateTimeField()
    delivery_count = models.PositiveIntegerField(default=1)
    replay_count = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)
    publish_attempts = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["status"], name="challenge_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                name="challenge_status_valid",
                condition=Q(status__in=[s.value for s in ChallengeStatus]),
            ),
            models.CheckConstraint(
                name="challenge_progress_bounded",
                condition=Q(next_index__lte=models.F("total_items")),
            ),
            models.CheckConstraint(
                name="challenge_terminal_has_response",
                condition=Q(status=ChallengeStatus.PROCESSING) | Q(response_payload__isnull=False),
            ),
        ]


class MqttChallengeLog(models.Model):
    """Inbound MQTT messages that did not become (or changed) a stored challenge."""

    class Kind(models.TextChoices):
        INVALID = "INVALID"
        CONFLICT = "CONFLICT"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    challenge_id = models.CharField(max_length=ATTEMPT_ID_MAX_LENGTH, null=True, blank=True)
    error_code = models.CharField(max_length=40)
    reason = models.TextField()
    raw_body = models.TextField()
    received_at = models.DateTimeField()


class MqttRuntimeStatus(models.Model):
    """Operational status written by the MQTT worker and read by the HTTP process."""

    candidate_id = models.CharField(max_length=64, unique=True)
    client_id = models.CharField(max_length=128, blank=True, default="")
    connection_state = models.CharField(max_length=16, default="DISCONNECTED")
    connected_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    last_challenge_id = models.CharField(max_length=ATTEMPT_ID_MAX_LENGTH, null=True, blank=True)
    last_challenge_at = models.DateTimeField(null=True, blank=True)
    last_response_status = models.CharField(max_length=16, null=True, blank=True)
    last_response_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class ProcessingMutex(models.Model):
    """
    One seeded row. Every state-changing transaction locks it first
    (SELECT ... FOR UPDATE), serializing mutations across processes.
    """

    name = models.CharField(max_length=32, unique=True)
