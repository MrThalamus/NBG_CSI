"""Optional Protocol Buffers encoding (bonus): same rules as JSON, JSON unchanged."""

from django.test import TestCase

from production.models import ProductionEvent, SubmissionAttempt
from production.modules.pb import production_pb2 as pb
from production.tests.helpers import ApiMixin, count, summary_dict

PROTOBUF = "application/x-protobuf"


def pb_event(event_id, quantity=None, type_=pb.EVENT_TYPE_COUNT, target=None, source="LINE-01",
             event_time="2026-10-09T10:30:00Z"):
    event = pb.Event(source_id=source, event_id=event_id, type=type_, event_time=event_time)
    if quantity is not None:
        event.quantity = quantity
    if target is not None:
        event.target_event_id = target
    return event


class ProtobufApiTests(ApiMixin, TestCase):
    def post_pb(self, *events):
        response = self.client.post("/api/events", pb.EventBatch(events=events).SerializeToString(),
                                    content_type=PROTOBUF)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Content-Type"], PROTOBUF)
        return pb.EventResults.FromString(response.content).results

    def test_count_and_void_round_trip(self):
        results = self.post_pb(pb_event("C-1", 5), pb_event("V-1", type_=pb.EVENT_TYPE_VOID, target="C-9"))
        self.assertEqual([pb.ResultStatus.Name(r.status) for r in results],
                         ["RESULT_STATUS_ACCEPTED", "RESULT_STATUS_PENDING_REFERENCE"])
        self.assertEqual([r.event_id for r in results], ["C-1", "V-1"])
        self.assertFalse(results[0].HasField("error_code"))
        self.assertEqual(self.summary(), summary_dict(5, 1, 1, 1, 0, 0))

    def test_identity_is_shared_with_json(self):
        self.post_pb(pb_event("C-1", 5))
        self.assertEqual(self.statuses(count("C-1", 5)), ["DUPLICATE"])  # same event via JSON
        self.assertEqual(self.statuses(count("C-1", 6)), ["CONFLICT"])
        results = self.post_pb(pb_event("C-1", 5, event_time="2026-10-09T12:30:00+02:00"))
        self.assertEqual(pb.ResultStatus.Name(results[0].status), "RESULT_STATUS_DUPLICATE")
        self.assertEqual(ProductionEvent.objects.count(), 1)

    def test_absent_fields_follow_json_validation(self):
        results = self.post_pb(
            pb_event("C-1"),                         # COUNT without quantity
            pb_event("", 5),                         # empty event_id == missing
            pb_event("C-2", 5, type_=pb.EVENT_TYPE_UNSPECIFIED),
            pb_event("C-3", 0),                      # zero is a set value, still invalid
            pb_event("V-1", 3, type_=pb.EVENT_TYPE_VOID, target="C-1"),  # VOID with quantity
        )
        self.assertEqual([r.error_code for r in results],
                         ["INVALID_QUANTITY", "MISSING_FIELD", "MISSING_FIELD", "INVALID_QUANTITY", "INVALID_QUANTITY"])
        self.assertFalse(results[1].HasField("event_id"))
        self.assertEqual(ProductionEvent.objects.count(), 0)
        self.assertEqual(SubmissionAttempt.objects.filter(outcome="REJECTED").count(), 5)

    def test_malformed_protobuf_is_400_and_audited(self):
        response = self.client.post("/api/events", b"\x0a\x05abc", content_type=PROTOBUF)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "MALFORMED_PROTOBUF")
        attempt = SubmissionAttempt.objects.get()
        self.assertEqual(attempt.error_code, "MALFORMED_PROTOBUF")
        self.assertTrue(attempt.raw_body.startswith("base64:"))

    def test_summary_as_protobuf_matches_json(self):
        self.post_pb(pb_event("C-1", 5), pb_event("C-1", 5))
        response = self.client.get("/api/state", {"view": "summary"}, HTTP_ACCEPT=PROTOBUF)
        self.assertEqual(response["Content-Type"], PROTOBUF)
        summary = pb.Summary.FromString(response.content)
        self.assertEqual(
            {field: getattr(summary, field) for field in summary_dict(0, 0, 0, 0, 0, 0)}, self.summary()
        )
        # JSON stays the default.
        self.assertEqual(self.client.get("/api/state")["Content-Type"], "application/json")
