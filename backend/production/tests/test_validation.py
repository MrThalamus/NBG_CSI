from datetime import datetime, timezone

from django.test import SimpleTestCase

from production.modules.events.validation import (
    EventValidationError,
    normalize_and_validate_event,
)
from production.shared.contracts import ErrorCode
from production.tests.helpers import count, void


class ValidationTests(SimpleTestCase):
    def assertRejected(self, payload, code=None):
        with self.assertRaises(EventValidationError) as ctx:
            normalize_and_validate_event(payload)
        if code:
            self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        return ctx.exception

    def test_valid_count_normalizes(self):
        event = normalize_and_validate_event(count("  EV-1  ", 5, source_id=" LINE-01 "))
        self.assertEqual(event.event_id, "EV-1")
        self.assertEqual(event.source_id, "LINE-01")
        self.assertEqual(event.as_payload()["event_time"], "2026-10-09T10:30:00Z")

    def test_ids_are_case_sensitive(self):
        a = normalize_and_validate_event(count("ev-1"))
        b = normalize_and_validate_event(count("EV-1"))
        self.assertNotEqual(a.as_payload(), b.as_payload())

    def test_missing_ids(self):
        for field in ("event_id", "source_id"):
            payload = count("EV-1")
            del payload[field]
            self.assertRejected(payload, ErrorCode.MISSING_FIELD)
            self.assertRejected({**count("EV-1"), field: "   "}, ErrorCode.INVALID_FIELD)
            self.assertRejected({**count("EV-1"), field: 12}, ErrorCode.INVALID_FIELD)

    def test_id_length_limit(self):
        self.assertRejected(count("E" * 65), ErrorCode.INVALID_FIELD)
        normalize_and_validate_event(count("E" * 64))

    def test_control_characters_rejected(self):
        self.assertRejected(count("EV\x001"), ErrorCode.INVALID_FIELD)

    def test_quantity_rules(self):
        for bad in (True, False, "5", 5.0, 2.5, 0, -3, None, 1_000_001, [5]):
            with self.subTest(quantity=bad):
                self.assertRejected(count("EV-1", bad), ErrorCode.INVALID_QUANTITY)

    def test_type_must_be_exact(self):
        for bad in ("count", " COUNT", "ADJUST", 1, None):
            with self.subTest(type=bad):
                self.assertRejected({**count("EV-1"), "type": bad})

    def test_count_with_target_rejected(self):
        self.assertRejected(count("EV-1", target_event_id="EV-0"), ErrorCode.INVALID_FIELD)

    def test_void_shape(self):
        normalize_and_validate_event(void("V-1", "EV-1"))
        self.assertRejected(void("V-1", "EV-1", quantity=5), ErrorCode.INVALID_QUANTITY)
        self.assertRejected(void("V-1", None), ErrorCode.MISSING_FIELD)
        self.assertRejected(void("V-1", "  "), ErrorCode.INVALID_FIELD)
        self.assertRejected(void("V-1", 7), ErrorCode.INVALID_FIELD)

    def test_event_time_requires_timezone(self):
        for bad in ("2026-10-09T10:30:00", "2026-10-09", "yesterday", "", 1696847400, None):
            with self.subTest(event_time=bad):
                self.assertRejected({**count("EV-1"), "event_time": bad})

    def test_equivalent_timestamps_normalize_equal(self):
        variants = [
            "2026-10-09T10:30:00Z",
            "2026-10-09T10:30:00+00:00",
            "2026-10-09T12:30:00+02:00",
            "2026-10-09T10:30:00.000Z",
        ]
        payloads = {
            str(normalize_and_validate_event({**count("EV-1"), "event_time": v}).as_payload())
            for v in variants
        }
        self.assertEqual(len(payloads), 1)

    def test_null_and_omitted_optionals_equal(self):
        explicit = count("EV-1")
        omitted = {k: v for k, v in explicit.items() if k != "target_event_id"}
        self.assertEqual(
            normalize_and_validate_event(explicit).as_payload(),
            normalize_and_validate_event(omitted).as_payload(),
        )
        void_explicit = void("V-1", "EV-1")
        void_omitted = {k: v for k, v in void_explicit.items() if k != "quantity"}
        self.assertEqual(
            normalize_and_validate_event(void_explicit).as_payload(),
            normalize_and_validate_event(void_omitted).as_payload(),
        )

    def test_unknown_fields_rejected(self):
        self.assertRejected({**count("EV-1"), "shift": "A"}, ErrorCode.UNKNOWN_FIELD)

    def test_non_object_items_rejected(self):
        for bad in (None, 5, "EV-1", [count("EV-1")]):
            self.assertRejected(bad, ErrorCode.INVALID_ITEM)

    def test_event_time_kept_in_utc(self):
        event = normalize_and_validate_event({**count("EV-1"), "event_time": "2026-10-09T12:30:00+02:00"})
        self.assertEqual(event.event_time, datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc))
