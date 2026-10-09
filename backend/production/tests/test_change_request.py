"""Change request: COUNT quantity limit 1-500 and the rejected_submissions summary counter."""

import json

from django.test import TestCase

from production.models import ProductionEvent, SubmissionAttempt
from production.modules.mqtt import service
from production.shared.contracts import QUANTITY_RULE
from production.tests.helpers import ApiMixin, count, summary_dict, void
from production.tests.test_mqtt import ChallengeServiceTestBase, challenge


class QuantityLimitTests(ApiMixin, TestCase):
    def test_boundary_quantities_1_450_500_accepted(self):
        results = self.submit([count("Q-1", 1), count("Q-450", 450), count("Q-500", 500)])
        self.assertEqual([r["status"] for r in results], ["ACCEPTED"] * 3)
        self.assertEqual(self.summary()["net_total"], 951)

    def test_501_rejected_with_reason_audit_and_no_total_change(self):
        self.submit(count("Q-450", 450))
        before = self.summary()
        result = self.submit(count("Q-501", 501))[0]
        self.assertEqual(result, {"event_id": "Q-501", "status": "REJECTED", "message": QUANTITY_RULE,
                                  "error_code": "INVALID_QUANTITY"})
        self.assertEqual(QUANTITY_RULE, "COUNT quantity must be an integer between 1 and 500.")
        attempt = SubmissionAttempt.objects.get(event_id="Q-501")
        self.assertEqual((attempt.outcome, attempt.error_code, attempt.reason, attempt.transport),
                         ("REJECTED", "INVALID_QUANTITY", QUANTITY_RULE, "REST"))
        self.assertIsNone(attempt.logical_event_id)
        self.assertFalse(ProductionEvent.objects.filter(event_id="Q-501").exists())
        after = self.summary()
        self.assertEqual(after["net_total"], before["net_total"])
        self.assertEqual(after["rejected_submissions"], before["rejected_submissions"] + 1)
        self.assertEqual(self.statuses(count("Q-1000000", 1_000_000)), ["REJECTED"])

    def test_existing_invalid_quantity_types_still_rejected(self):
        for index, bad in enumerate([0, -1, 2.5, 5.0, "5", True, False, None]):
            with self.subTest(quantity=bad):
                result = self.submit(count(f"Q-BAD-{index}", bad))[0]
                self.assertEqual((result["status"], result["error_code"]), ("REJECTED", "INVALID_QUANTITY"))
        raw = '{"source_id":"LINE-01","event_id":"Q-FRAC","type":"COUNT","quantity":5.0,"event_time":"2026-10-09T10:30:00Z"}'
        self.assertEqual(self.post_events(raw, raw=True).json()["results"][0]["status"], "REJECTED")
        self.assertEqual(ProductionEvent.objects.count(), 0)

    def test_mixed_batch_keeps_valid_items_and_order(self):
        results = self.submit([count("M-1", 450), count("M-2", 501), count("M-3", 500), count("M-4", 9999)])
        self.assertEqual([(r["event_id"], r["status"]) for r in results],
                         [("M-1", "ACCEPTED"), ("M-2", "REJECTED"), ("M-3", "ACCEPTED"), ("M-4", "REJECTED")])
        self.assertEqual(self.summary()["net_total"], 950)
        self.assertEqual(self.summary()["rejected_submissions"], 2)
        self.assertEqual(list(SubmissionAttempt.objects.order_by("pk").values_list("item_index", flat=True)), [0, 1, 2, 3])

    def test_limit_never_invalidates_existing_records(self):
        self.submit(count("OLD-1", 5))
        # VOID, ACK and duplicate semantics are unaffected by the new limit.
        self.assertEqual(self.statuses(void("V-OLD", "OLD-1")), ["ACCEPTED"])
        self.assertEqual(self.statuses(count("OLD-1", 5)), ["DUPLICATE"])
        self.assertEqual(self.statuses(count("OLD-1", 501)), ["REJECTED"])  # invalid -> REJECTED, not CONFLICT
        self.assertEqual(self.summary(), summary_dict(0, 2, 1, 0, 1, 0, rejected=1))


class RejectedSubmissionsSummaryTests(ApiMixin, TestCase):
    def test_zero_when_empty_and_counts_only_rejected_attempts(self):
        self.assertEqual(self.summary()["rejected_submissions"], 0)
        self.assertIsInstance(self.summary()["rejected_submissions"], int)
        self.submit([
            count("C-1", 5),
            count("C-1", 5),              # DUPLICATE: excluded
            count("C-1", 6),              # CONFLICT: excluded
            void("V-1", "C-9"),           # PENDING_REFERENCE: excluded
            count("C-2", 501),            # REJECTED
            void("V-SELF", "V-SELF"),     # REJECTED (business rule)
        ])
        self.submit(void("V-2", "C-9"))               # second pending VOID
        self.submit(count("C-9", 2, source_id="LINE-02"))  # both pending VOIDs now REJECTED as logical events...
        summary = self.summary()
        # ...but their attempts stay PENDING_REFERENCE, so they are not rejected *attempts*.
        self.assertEqual(summary["rejected_submissions"], 2)
        self.assertEqual((summary["duplicates"], summary["conflicts"]), (1, 1))

    def test_counts_attempts_not_distinct_event_ids(self):
        self.submit([count("R-1", 501), count("R-1", 501), count("R-1", "x")])
        self.assertEqual(self.summary()["rejected_submissions"], 3)

    def test_source_filter_uses_attempt_source(self):
        self.submit([count("A-1", 501, source_id="LINE-01"), count("A-2", 0, source_id="LINE-01"),
                     count("B-1", 999, source_id="LINE-02")])
        no_source = count("N-1", 5)
        del no_source["source_id"]
        self.submit(no_source)                              # no usable source
        self.post_events("{not json", raw=True)             # request-level rejection, no source
        self.assertEqual(self.summary("LINE-01")["rejected_submissions"], 2)
        self.assertEqual(self.summary("LINE-02")["rejected_submissions"], 1)
        self.assertEqual(self.summary("NO-SUCH")["rejected_submissions"], 0)
        self.assertEqual(self.summary()["rejected_submissions"], 5)

    def test_other_six_fields_unchanged(self):
        self.submit([count("C-1", 5), count("C-2", 501)])
        self.assertEqual(self.summary(), summary_dict(5, 1, 1, 0, 0, 0, rejected=1))


class MqttChangeRequestTests(ChallengeServiceTestBase):
    def test_mqtt_enforces_same_limit_and_reports_new_field(self):
        handling = service.handle_mqtt_challenge(
            challenge(events=[count("MQ-1", 500), count("MQ-2", 501), count("MQ-3", 450)])
        )
        response = json.loads(handling.response_text)
        self.assertEqual(response["status"], "COMPLETED")
        self.assertEqual([(r["event_id"], r["status"]) for r in response["results"]],
                         [("MQ-1", "ACCEPTED"), ("MQ-2", "REJECTED"), ("MQ-3", "ACCEPTED")])
        self.assertEqual(response["results"][1]["message"], QUANTITY_RULE)
        self.assertEqual(response["state"], summary_dict(950, 2, 2, 0, 0, 0, rejected=1))
        attempt = SubmissionAttempt.objects.get(event_id="MQ-2")
        self.assertEqual((attempt.transport, attempt.outcome, attempt.reason), ("MQTT", "REJECTED", QUANTITY_RULE))

    def test_rest_and_mqtt_reject_identically(self):
        rest = self.submit(count("SAME-1", 501))[0]
        mqtt = json.loads(service.handle_mqtt_challenge(
            challenge(events=[count("SAME-2", 501)])).response_text)["results"][0]
        self.assertEqual((rest["status"], rest["error_code"], rest["message"]),
                         (mqtt["status"], mqtt["error_code"], mqtt["message"]))

    def test_replay_creates_no_extra_rejection_attempts(self):
        raw = challenge(events=[count("RP-1", 501), count("RP-2", 5)])
        first = service.handle_mqtt_challenge(raw)
        attempts = SubmissionAttempt.objects.count()
        rejected = self.summary()["rejected_submissions"]
        replay = service.handle_mqtt_challenge(raw)
        self.assertEqual((replay.kind, replay.response_text), ("REPLAY", first.response_text))
        self.assertEqual(SubmissionAttempt.objects.count(), attempts)
        self.assertEqual(self.summary()["rejected_submissions"], rejected)
