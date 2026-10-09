"""Single source of 'now' so tests can freeze or advance time."""

from datetime import timezone as dt_timezone

from django.utils import timezone


def now():
    return timezone.now()


def to_utc_iso(value):
    """Canonical UTC ISO 8601 text, e.g. 2026-10-09T10:30:00Z (microseconds only if present)."""
    if value is None:
        return None
    value = value.astimezone(dt_timezone.utc)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
