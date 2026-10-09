"""REST event processing: COUNT, duplicates, conflicts, batches and request errors."""

import json
from unittest import mock

from django.db import OperationalError
from django.test import Client, TestCase

from production.models import ProductionEvent, ProductionSource, SubmissionAttempt
from production.tests.helpers import ApiMixin, count, summary_dict, void


class CountAndDuplicateTests(ApiMixin, TestCase):
    def test_t01_new_count(self):
        results = self.submit(count("EV-101", 5))
        self.assertEqual(results, [{"event_id": "EV-101", "status": "ACCEPTED", "message": "COUNT accepted"}])
        self.assertEqual(self.summary(), summary_dict(5, 1, 1, 0, 0, 0))
        event = ProductionEvent.objects.get(event_id="EV-101")
        self.assertEqual((event.type, event.quantity, event.source_id), ("COUNT", 5, "LINE-01"))
        attempt = SubmissionAttempt.objects.get()
        self.assertEqual((attempt.outcome, attempt.transport, attempt.logical_event_id), ("ACCEPTED", "REST", event.pk))

    def test_t02_identical_count_twice_is_duplicate(self):
        self.assertEqual(self.statuses(count("EV-101", 5)), ["ACCEPTED"])
        self.assertEqual(self.statuses(count("EV-101", 5)), ["DUPLICATE"])
        self.assertEqual(self.summary(), summary_dict(5, 1, 1, 0, 1, 0))
        self.assertEqual(ProductionEvent.objects.count(), 1)
        self.assertEqual(SubmissionAttempt.objects.count(), 2)

    def test_t06_changed_quantity_source_or_time_conflicts(self):
        self.submit(count("EV-1", 5))
        variants = [count("EV-1", 6), count("EV-1", 5, minutes=3), count("EV-1", 5, source_id="LINE-02")]
        for variant in variants:
            result = self.submit(variant)[0]
            self.assertEqual(result["status"], "CONFLICT")
            self.assertEqual(result["error_code"], "EVENT_ID_CONFLICT")
        original = ProductionEvent.objects.get(event_id="EV-1")
        self.assertEqual((original.quantity, original.source_id), (5, "LINE-01"))
        self.assertEqual(self.summary(), summary_dict(5, 1, 1, 0, 0, 3))
        self.assertEqual(SubmissionAttempt.objects.filter(outcome="CONFLICT").count(), 3)

    def test_t07_same_id_on_different_source_conflicts(self):
        self.submit(count("EV-1", 5, source_id="LINE-01"))
        self.assertEqual(self.statuses(count("EV-1", 5, source_id="LINE-02")), ["CONFLICT"])
        self.assertFalse(ProductionSource.objects.filter(source_id="LINE-02").exists())

    def test_t21_equivalent_timestamp_and_null_vs_missing_is_duplicate(self):
        self.submit(count("EV-1", 5))
        replay = count("EV-1", 5, event_time="2026-10-09T12:30:00+02:00")
        del replay["target_event_id"]
        self.assertEqual(self.statuses(replay), ["DUPLICATE"])
        # Key order is irrelevant too.
        reordered = dict(reversed(list(count("EV-1", 5).items())))
        self.assertEqual(self.statuses(reordered), ["DUPLICATE"])

    def test_t22_bad_quantities_rejected_without_logical_event(self):
        for index, bad in enumerate([True, "5", 2.5, -1, 0]):
            result = self.submit(count(f"EV-{index}", bad))[0]
            self.assertEqual(result["status"], "REJECTED")
            self.assertEqual(result["error_code"], "INVALID_QUANTITY")
        self.assertEqual(ProductionEvent.objects.count(), 0)
        self.assertEqual(ProductionSource.objects.count(), 0)
        self.assertEqual(SubmissionAttempt.objects.filter(outcome="REJECTED").count(), 5)

    def test_t23_corrected_rejected_event_id_can_be_reused(self):
        self.assertEqual(self.statuses(count("EV-1", "5")), ["REJECTED"])
        self.assertEqual(self.statuses(count("EV-1", 5)), ["ACCEPTED"])
        self.assertEqual(SubmissionAttempt.objects.filter(event_id="EV-1").count(), 2)

    def test_source_created_on_first_valid_event_only(self):
        self.submit(count("EV-1", -5, source_id="LINE-09"))
        self.assertFalse(ProductionSource.objects.exists())
        self.submit(count("EV-2", 5, source_id="LINE-09"))
        self.assertTrue(ProductionSource.objects.filter(source_id="LINE-09").exists())

    def test_rejected_invalid_with_existing_id_is_rejected_not_conflict(self):
        self.submit(count("EV-1", 5))
        self.assertEqual(self.statuses(count("EV-1", "5")), ["REJECTED"])
        self.assertEqual(self.summary()["conflicts"], 0)


class BatchTests(ApiMixin, TestCase):
    def test_t08_valid_invalid_valid_batch(self):
        results = self.submit([count("EV-1", 5), count("EV-2", -1), count("EV-3", 2)])
        self.assertEqual([(r["event_id"], r["status"]) for r in results],
                         [("EV-1", "ACCEPTED"), ("EV-2", "REJECTED"), ("EV-3", "ACCEPTED")])
        self.assertEqual(self.summary()["net_total"], 7)
        self.assertEqual(
            list(SubmissionAttempt.objects.order_by("pk").values_list("event_id", "outcome", "item_index")),
            [("EV-1", "ACCEPTED", 0), ("EV-2", "REJECTED", 1), ("EV-3", "ACCEPTED", 2)],
        )

    def test_t10_non_object_element_rejected_others_continue(self):
        results = self.submit([count("EV-1", 5), 42, None, "x", count("EV-2", 1)])
        self.assertEqual([r["status"] for r in results], ["ACCEPTED", "REJECTED", "REJECTED", "REJECTED", "ACCEPTED"])
        self.assertIsNone(results[1]["event_id"])
        self.assertEqual(results[1]["error_code"], "INVALID_ITEM")

    def test_batch_items_processed_in_order(self):
        results = self.submit([void("V-1", "C-1"), count("C-1", 4), count("C-1", 4)])
        self.assertEqual([r["status"] for r in results], ["PENDING_REFERENCE", "ACCEPTED", "DUPLICATE"])
        self.assertEqual(self.summary(), summary_dict(0, 2, 1, 0, 1, 0))

    def test_t37_empty_array_and_unknown_source(self):
        self.assertEqual(self.submit([]), [])
        self.assertEqual(self.summary(), summary_dict(0, 0, 0, 0, 0, 0))
        self.submit(count("EV-1", 5))
        self.assertEqual(self.summary("NO-SUCH-LINE"), summary_dict(0, 0, 0, 0, 0, 0))
        self.assertEqual(self.view("pending", "NO-SUCH-LINE"), [])
        self.assertEqual(self.view("exceptions", "NO-SUCH-LINE"), [])
        self.assertEqual(self.ack([]), [])

    def test_batch_size_limit(self):
        response = self.post_events([count(f"EV-{i}", 1) for i in range(501)])
        self.assertEqual(response.status_code, 413)
        self.assertEqual(ProductionEvent.objects.count(), 0)
        self.assertEqual(SubmissionAttempt.objects.get().error_code, "PAYLOAD_TOO_LARGE")


class RequestErrorTests(ApiMixin, TestCase):
    def test_t09_malformed_json_returns_400_and_is_audited(self):
        response = self.post_events('{"event_id": "EV-1", ', raw=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "MALFORMED_JSON")
        attempt = SubmissionAttempt.objects.get()
        self.assertEqual((attempt.outcome, attempt.error_code), ("REJECTED", "MALFORMED_JSON"))
        self.assertEqual(attempt.raw_body, '{"event_id": "EV-1", ')

    def test_t09_top_level_scalar_or_null_returns_400(self):
        for body in ("5", "null", '"text"', "true"):
            with self.subTest(body=body):
                response = self.post_events(body, raw=True)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "INVALID_REQUEST")
        self.assertEqual(SubmissionAttempt.objects.count(), 4)

    def test_nan_and_empty_body_are_malformed(self):
        for body in ('{"quantity": NaN}', "", "   "):
            self.assertEqual(self.post_events(body, raw=True).status_code, 400)

    def test_nul_characters_do_not_break_audit(self):
        results = self.submit({**count("EV\u00001", 5), "source_id": "LINE\u0000X"})
        self.assertEqual(results[0]["status"], "REJECTED")
        self.assertTrue(SubmissionAttempt.objects.filter(outcome="REJECTED").exists())

    def test_wrong_method_returns_405(self):
        self.assertEqual(self.client.get("/api/events").status_code, 405)
        self.assertEqual(self.client.post("/api/state").status_code, 405)

    def test_t38_internal_errors_are_sanitized(self):
        secret = "password=hunter2 host=db.internal"
        with mock.patch(
            "production.modules.events.views.process_batch", side_effect=OperationalError(secret)
        ), self.assertLogs("production.api", level="ERROR") as logs:
            response = self.post_events(count("EV-1", 5))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("hunter2", response.content.decode())
        self.assertIn("hunter2", "\n".join(logs.output))

        with mock.patch(
            "production.modules.state.views.get_summary", side_effect=RuntimeError(secret)
        ), self.assertLogs("production.api", level="ERROR"):
            response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "INTERNAL_ERROR", "message": "Internal server error"})


class CsrfTests(TestCase):
    def test_post_requires_csrf_token_and_accepts_valid_one(self):
        client = Client(enforce_csrf_checks=True)
        body = json.dumps(count("EV-1", 5))
        self.assertEqual(client.post("/api/events", body, content_type="application/json").status_code, 403)

        client.get("/")  # dashboard sets the csrftoken cookie
        token = client.cookies["csrftoken"].value
        response = client.post("/api/events", body, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["status"], "ACCEPTED")
