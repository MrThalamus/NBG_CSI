"""Acknowledgements, state views, source filtering and the documented demo sequence."""

import json

from django.test import TestCase

from production.models import ProductionEvent
from production.tests.helpers import ApiMixin, count, summary_dict, void


class AckTests(ApiMixin, TestCase):
    def test_t04_repeat_ack(self):
        self.submit(count("C-1", 5))
        self.assertEqual([r["status"] for r in self.ack(["C-1"])], ["ACKED"])
        self.assertEqual([r["status"] for r in self.ack(["C-1"])], ["ALREADY_ACKED"])
        self.assertEqual(self.summary(), summary_dict(5, 1, 0, 0, 0, 0))

    def test_t16_same_id_twice_in_one_request(self):
        self.submit([count("C-1", 5), count("C-2", 1)])
        results = self.ack(["C-1", "C-2", "C-1"])
        self.assertEqual(
            [(r["event_id"], r["status"]) for r in results],
            [("C-1", "ACKED"), ("C-2", "ACKED"), ("C-1", "ALREADY_ACKED")],
        )

    def test_t17_unknown_pending_rejected_and_completed_void(self):
        self.submit([
            count("C-1", 5),
            void("V-DONE", "C-1"),
            void("V-WAIT", "C-404"),
            void("V-LOSE", "C-2"),
            void("V-WIN-FIRST", "C-2", source_id="LINE-02"),
        ])
        self.submit(count("C-2", 2, source_id="LINE-02"))  # V-LOSE rejected (source), V-WIN-FIRST applied
        self.submit(count("C-BAD", -1))  # validation-rejected: no logical event
        results = {r["event_id"]: r["status"] for r in self.ack(["NOPE", "V-WAIT", "V-LOSE", "V-DONE", "C-BAD"])}
        self.assertEqual(results, {
            "NOPE": "NOT_FOUND",
            "V-WAIT": "NOT_READY",
            "V-LOSE": "NOT_READY",
            "V-DONE": "ALREADY_ACKED",
            "C-BAD": "NOT_FOUND",
        })

    def test_t18_ack_then_later_void(self):
        self.submit(count("C-1", 5))
        self.ack(["C-1"])
        self.assertEqual(self.statuses(void("V-1", "C-1")), ["ACCEPTED"])
        self.assertEqual(self.summary(), summary_dict(0, 2, 0, 0, 0, 0))
        self.assertIsNotNone(ProductionEvent.objects.get(event_id="C-1").acknowledged_at)

    def test_invalid_ack_shapes_return_400_without_partial_ack(self):
        self.submit(count("C-1", 5))
        bodies = [
            "not json",
            json.dumps(["C-1"]),
            json.dumps({"ids": ["C-1"]}),
            json.dumps({"event_ids": "C-1"}),
            json.dumps({"event_ids": ["C-1", 5]}),
            json.dumps({"event_ids": ["C-1", "  "]}),
            json.dumps({"event_ids": ["C-1"], "extra": 1}),
            json.dumps({"event_ids": ["C-1"] * 501}),
        ]
        for body in bodies:
            with self.subTest(body=body[:40]):
                response = self.client.post("/api/ack", body, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.assertIsNone(ProductionEvent.objects.get(event_id="C-1").acknowledged_at)


class StateViewTests(ApiMixin, TestCase):
    def test_summary_has_exactly_seven_fields_and_view_defaults(self):
        self.assertEqual(set(self.client.get("/api/state").json()), {
            "net_total", "processed_events", "pending_ack", "unresolved", "duplicates", "conflicts",
            "rejected_submissions",
        })
        self.assertEqual(self.client.get("/api/state", {"view": "totals"}).status_code, 400)
        self.assertEqual(self.client.get("/api/state", {"view": "pending", "limit": "0"}).status_code, 400)

    def test_pending_lists_unacked_counts_including_reversed(self):
        self.submit([count("C-1", 5), count("C-2", 3), void("V-1", "C-1")])
        self.ack(["C-2"])
        items = self.view("pending")
        self.assertEqual([i["event_id"] for i in items], ["C-1"])
        self.assertEqual(items[0]["reversed"], True)
        self.assertEqual(items[0]["reversed_by"], "V-1")
        self.assertEqual(items[0]["quantity"], 5)
        self.assertEqual(items[0]["event_time"], "2026-10-09T10:30:00Z")

    def test_exceptions_kinds_and_no_duplication(self):
        self.submit([
            void("V-WAIT", "C-404"),
            void("V-LOSE", "C-2", source_id="LINE-02"),
            count("C-1", 5),
            count("C-1", 6),
            count("C-BAD", -1),
        ])
        self.submit(count("C-2", 2))
        items = self.view("exceptions")
        kinds = sorted((i["kind"], i["event_id"]) for i in items)
        self.assertEqual(kinds, [
            ("CONFLICT", "C-1"),
            ("REJECTED_EVENT", "V-LOSE"),
            ("REJECTED_SUBMISSION", "C-BAD"),
            ("UNRESOLVED_REFERENCE", "V-WAIT"),
        ])
        refs = [i["ref"] for i in items]
        self.assertEqual(len(refs), len(set(refs)))
        rejected = next(i for i in items if i["event_id"] == "V-LOSE")
        self.assertEqual(rejected["error_code"], "SOURCE_MISMATCH")

    def test_t19_conflict_counted_under_attempt_source(self):
        self.submit(count("C-1", 5, source_id="LINE-01"))
        self.submit(count("C-1", 5, source_id="LINE-02"))
        self.assertEqual(self.summary("LINE-01"), summary_dict(5, 1, 1, 0, 0, 0))
        self.assertEqual(self.summary("LINE-02"), summary_dict(0, 0, 0, 0, 0, 1))
        self.assertEqual([i["kind"] for i in self.view("exceptions", "LINE-02")], ["CONFLICT"])
        self.assertEqual(self.view("exceptions", "LINE-01"), [])
        self.assertEqual([i["event_id"] for i in self.view("pending", "LINE-01")], ["C-1"])
        self.assertEqual(self.view("pending", "LINE-02"), [])

    def test_t20_missing_source_rejection_only_in_unfiltered_exceptions(self):
        payload = count("C-1", 5)
        del payload["source_id"]
        self.submit(payload)
        self.post_events("{broken", raw=True)
        unfiltered = self.view("exceptions")
        self.assertEqual(len(unfiltered), 2)
        self.assertTrue(all(i["source_id"] is None for i in unfiltered))
        self.assertEqual(self.view("exceptions", "LINE-01"), [])

    def test_source_filter_on_summary(self):
        self.submit([count("A-1", 5, source_id="LINE-01"), count("B-1", 7, source_id="LINE-02")])
        self.submit(count("A-1", 5, source_id="LINE-01"))
        self.assertEqual(self.summary("LINE-01"), summary_dict(5, 1, 1, 0, 1, 0))
        self.assertEqual(self.summary("LINE-02"), summary_dict(7, 1, 1, 0, 0, 0))
        self.assertEqual(self.summary(), summary_dict(12, 2, 2, 0, 1, 0))


class DemoSequenceTests(ApiMixin, TestCase):
    """Section 8 of the build guide: deterministic demo with expected counters."""

    def test_demo_sequence(self):
        steps = [
            (lambda: self.submit(count("C1", 5)), summary_dict(5, 1, 1, 0, 0, 0)),
            (lambda: self.submit(count("C1", 5)), summary_dict(5, 1, 1, 0, 1, 0)),
            (lambda: self.submit(count("C1", 6)), summary_dict(5, 1, 1, 0, 1, 1)),
            (lambda: self.submit(void("V2", "C2")), summary_dict(5, 1, 1, 1, 1, 1)),
            (lambda: self.submit(count("C2", 7)), summary_dict(5, 3, 2, 0, 1, 1)),
            (lambda: self.ack(["C1", "C2"]), summary_dict(5, 3, 0, 0, 1, 1)),
            (lambda: self.ack(["C1"]), summary_dict(5, 3, 0, 0, 1, 1)),
            (lambda: self.submit(void("V1", "C1")), summary_dict(0, 4, 0, 0, 1, 1)),
        ]
        for number, (action, expected) in enumerate(steps, start=1):
            action()
            self.assertEqual(self.summary(), expected, f"demo step {number}")
