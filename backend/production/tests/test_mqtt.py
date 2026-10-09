"""MQTT challenge service and worker tests (fake broker client; no network)."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings

from production.models import MqttChallenge, MqttChallengeLog, ProductionEvent, SubmissionAttempt
from production.modules.mqtt import service
from production.modules.mqtt.client import MqttWorker
from production.tests.helpers import BASE_TIME, ApiMixin, count, iso, summary_dict, void

NOW = BASE_TIME + timedelta(minutes=15)


class FakeClock:
    def __init__(self, start=NOW):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def challenge(challenge_id="CH-1", events=None, sent=NOW, ttl=15, **overrides):
    body = {
        "protocol_version": "1.0",
        "candidate_id": "15",
        "challenge_id": challenge_id,
        "command": "PROCESS_EVENTS",
        "sent_at": iso(sent),
        "expires_at": iso(sent + timedelta(seconds=ttl)),
        "events": events if events is not None else [count("EV-101", 5)],
    }
    body.update(overrides)
    return json.dumps(body).encode()


@override_settings(MQTT_CANDIDATE_ID="15")
class ChallengeServiceTestBase(ApiMixin, TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch("production.shared.clock.now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def handle(self, raw):
        handling = service.handle_mqtt_challenge(raw)
        return handling, json.loads(handling.response_text)


class ChallengeServiceTests(ChallengeServiceTestBase):
    def test_new_challenge_completes_with_results_and_state(self):
        handling, response = self.handle(challenge())
        self.assertEqual(handling.kind, "NEW")
        self.assertEqual(response, {
            "protocol_version": "1.0",
            "candidate_id": "15",
            "challenge_id": "CH-1",
            "status": "COMPLETED",
            "processed_at": iso(NOW),
            "results": [{"event_id": "EV-101", "status": "ACCEPTED", "message": "COUNT accepted"}],
            "state": summary_dict(5, 1, 1, 0, 0, 0),
        })
        self.assertEqual(SubmissionAttempt.objects.get().transport, "MQTT")

    def test_t05_identical_challenge_twice_replays_exact_response(self):
        first, _ = self.handle(challenge())
        self.clock.advance(5)
        self.submit(count("EV-OTHER", 3))  # later unrelated change must not alter the replay
        attempts_before = SubmissionAttempt.objects.count()

        second, replayed = self.handle(challenge())
        self.assertEqual(second.kind, "REPLAY")
        self.assertEqual(second.response_text, first.response_text)
        self.assertEqual(replayed["state"]["net_total"], 5)
        self.assertEqual(SubmissionAttempt.objects.count(), attempts_before)
        self.assertEqual(ProductionEvent.objects.count(), 2)
        stored = MqttChallenge.objects.get()
        self.assertEqual((stored.delivery_count, stored.replay_count), (2, 1))

    def test_t24_changed_body_same_id_conflicts_and_preserves_original(self):
        first, _ = self.handle(challenge())
        handling, response = self.handle(challenge(events=[count("EV-101", 9)]))
        self.assertEqual((response["status"], response["error_code"]), ("FAILED", "CHALLENGE_CONFLICT"))
        self.assertEqual(response["challenge_id"], "CH-1")
        self.assertEqual(MqttChallenge.objects.get().response_payload, first.response_text)
        self.assertEqual(MqttChallengeLog.objects.get().kind, "CONFLICT")
        self.assertEqual(ProductionEvent.objects.get().quantity, 5)
        # The original is still replayable.
        self.assertEqual(self.handle(challenge())[0].response_text, first.response_text)

    def test_key_order_does_not_change_identity_but_array_order_does(self):
        self.handle(challenge(events=[count("A", 1), count("B", 2)]))
        reordered = json.dumps(dict(reversed(list(json.loads(challenge(events=[count("A", 1), count("B", 2)])).items()))))
        self.assertEqual(self.handle(reordered.encode())[0].kind, "REPLAY")
        self.assertEqual(self.handle(challenge(events=[count("B", 2), count("A", 1)]))[0].kind, "CONFLICT")

    def test_t25_new_expired_challenge_fails_without_processing(self):
        handling, response = self.handle(challenge(sent=NOW - timedelta(seconds=60)))
        self.assertEqual((response["status"], response["error_code"]), ("FAILED", "CHALLENGE_EXPIRED"))
        self.assertEqual(response["results"], [])
        self.assertEqual(response["state"], summary_dict(0, 0, 0, 0, 0, 0))
        self.assertEqual(ProductionEvent.objects.count(), 0)
        self.assertEqual(SubmissionAttempt.objects.count(), 0)

    def test_t26_completed_challenge_replayed_after_expiry(self):
        first, _ = self.handle(challenge())
        self.clock.advance(3600)
        second, _ = self.handle(challenge())
        self.assertEqual((second.kind, second.response_text), ("REPLAY", first.response_text))

    def test_t27_envelope_failures_have_stable_codes_and_no_processing(self):
        cases = [
            (challenge(candidate_id="17"), "CANDIDATE_MISMATCH", "CH-1"),
            (challenge(protocol_version="2.0"), "UNSUPPORTED_PROTOCOL", "CH-1"),
            (challenge(command="DELETE_EVENTS"), "VALIDATION_ERROR", "CH-1"),
            (challenge(events="EV-1"), "VALIDATION_ERROR", "CH-1"),
            (challenge(sent_at="2026-10-09T10:45:00"), "VALIDATION_ERROR", "CH-1"),
            (challenge(challenge_id=""), "VALIDATION_ERROR", None),
            (b"{not json", "VALIDATION_ERROR", None),
            (b"[1, 2]", "VALIDATION_ERROR", None),
        ]
        for raw, code, challenge_id in cases:
            with self.subTest(code=code, raw=raw[:30]):
                handling, response = self.handle(raw)
                self.assertEqual((handling.kind, response["status"], response["error_code"]), ("INVALID", "FAILED", code))
                self.assertEqual(response["challenge_id"], challenge_id)
        self.assertEqual(ProductionEvent.objects.count(), 0)
        self.assertEqual(SubmissionAttempt.objects.count(), 0)
        self.assertEqual(MqttChallenge.objects.count(), 0)
        self.assertEqual(MqttChallengeLog.objects.filter(kind="INVALID").count(), len(cases))

    def test_integer_candidate_id_is_accepted(self):
        _, response = self.handle(challenge(candidate_id=15))
        self.assertEqual(response["status"], "COMPLETED")

    def test_t28_rejected_item_inside_completed_challenge(self):
        events = [count("C-1", 5), count("C-2", -1), void("V-9", "C-404"), count("C-1", 5)]
        _, response = self.handle(challenge(events=events))
        self.assertEqual(response["status"], "COMPLETED")
        self.assertEqual([r["status"] for r in response["results"]],
                         ["ACCEPTED", "REJECTED", "PENDING_REFERENCE", "DUPLICATE"])
        self.assertEqual(response["state"], summary_dict(5, 1, 1, 1, 1, 0))
        self.assertEqual(
            list(SubmissionAttempt.objects.order_by("item_index").values_list("item_index", flat=True)), [0, 1, 2, 3]
        )

    def test_t29_rest_then_mqtt_is_duplicate(self):
        self.submit(count("EV-101", 5))
        _, response = self.handle(challenge())
        self.assertEqual(response["results"][0]["status"], "DUPLICATE")
        self.assertEqual(response["state"], summary_dict(5, 1, 1, 0, 1, 0))

    def test_empty_challenge_completes(self):
        _, response = self.handle(challenge(events=[]))
        self.assertEqual((response["status"], response["results"]), ("COMPLETED", []))

    def test_expiry_between_items_keeps_committed_results(self):
        original = service.process_event

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock.advance(10)
            return result

        with mock.patch("production.modules.mqtt.service.process_event", side_effect=slow):
            _, response = self.handle(challenge(events=[count("A", 1), count("B", 2), count("C", 3)]))
        self.assertEqual((response["status"], response["error_code"]), ("FAILED", "CHALLENGE_EXPIRED"))
        self.assertEqual([r["event_id"] for r in response["results"]], ["A", "B"])
        self.assertEqual(response["state"]["net_total"], 3)
        self.assertFalse(ProductionEvent.objects.filter(event_id="C").exists())

    def test_t35_crash_after_checkpoint_resumes_without_repeating(self):
        original = service.process_event
        calls = {"n": 0}

        def crash_on_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash")
            return original(*args, **kwargs)

        raw = challenge(events=[count("A", 1), count("B", 2), count("C", 3)])
        with mock.patch("production.modules.mqtt.service.process_event", side_effect=crash_on_second):
            with self.assertRaises(RuntimeError):
                self.handle(raw)
        stored = MqttChallenge.objects.get()
        self.assertEqual((stored.status, stored.next_index), ("PROCESSING", 1))
        self.assertEqual(SubmissionAttempt.objects.count(), 1)

        # Restart: resume from the committed checkpoint.
        self.assertEqual(service.resume_incomplete_challenges(), [stored.pk])
        stored.refresh_from_db()
        response = json.loads(stored.response_payload)
        self.assertEqual(response["status"], "COMPLETED")
        self.assertEqual([r["event_id"] for r in response["results"]], ["A", "B", "C"])
        self.assertEqual([r["status"] for r in response["results"]], ["ACCEPTED"] * 3)
        self.assertEqual(SubmissionAttempt.objects.filter(event_id="A").count(), 1)
        # Not yet published: still available for retry, and a redelivery replays it.
        self.assertEqual([row[0] for row in service.unpublished_responses()], [stored.pk])
        self.assertEqual(self.handle(raw)[0].response_text, stored.response_payload)

    def test_resumed_challenge_past_deadline_fails_with_committed_results(self):
        original = service.process_event

        def crash_on_second_item(*args, **kwargs):
            if kwargs.get("item_index") == 1:
                raise RuntimeError("simulated crash")
            return original(*args, **kwargs)

        with mock.patch("production.modules.mqtt.service.process_event", side_effect=crash_on_second_item):
            with self.assertRaises(RuntimeError):
                self.handle(challenge(events=[count("A", 1), count("B", 2)]))
        self.clock.advance(60)
        service.resume_incomplete_challenges()
        response = json.loads(MqttChallenge.objects.get().response_payload)
        self.assertEqual((response["status"], response["error_code"]), ("FAILED", "CHALLENGE_EXPIRED"))
        self.assertEqual([r["event_id"] for r in response["results"]], ["A"])
        self.assertFalse(ProductionEvent.objects.filter(event_id="B").exists())


class FakeInfo:
    def __init__(self, ok=True):
        self.ok = ok
        self.rc = 0 if ok else 4

    def wait_for_publish(self, timeout=None):
        if not self.ok:
            raise RuntimeError("not connected")

    def is_published(self):
        return self.ok


class FakeClient:
    def __init__(self, client_id):
        self.client_id = client_id
        self.published = []
        self.subscriptions = []
        self.will = None
        self.publish_ok = True
        self.connected_to = None

    def will_set(self, topic, payload, qos, retain):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay, max_delay):
        self.backoff = (min_delay, max_delay)

    def connect_async(self, host, port, keepalive):
        self.connected_to = (host, port, keepalive)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))

    def publish(self, topic, payload, qos, retain):
        if self.publish_ok:
            self.published.append((topic, payload, qos, retain))
        return FakeInfo(self.publish_ok)


OK = SimpleNamespace(is_failure=False, value=0)
LOST = SimpleNamespace(is_failure=True, value=0x80)


@override_settings(MQTT_CANDIDATE_ID="15")
class WorkerTests(ChallengeServiceTestBase):
    def make_worker(self):
        worker = MqttWorker(host="broker.test", port=1883, candidate_id="15", client_factory=FakeClient)
        worker.start()
        return worker

    def connect(self, worker):
        worker.client.on_connect(worker.client, None, None, OK, None)
        worker.step()
        worker.client.on_subscribe(worker.client, None, 1, [SimpleNamespace(is_failure=False, value=1)], None)
        worker.step()

    def status_kinds(self, worker):
        return [json.loads(p)["status"] for t, p, q, r in worker.client.published if t.endswith("/status")]

    def test_client_identity_topics_will_and_online(self):
        worker = self.make_worker()
        self.assertRegex(worker.client_id, r"^fse01-15-[0-9a-f]{6}$")
        self.assertEqual(worker.client.will[0], "fse-01/15/status")
        self.assertEqual(json.loads(worker.client.will[1])["status"], "OFFLINE")
        self.assertEqual(worker.client.will[2:], (1, False))
        self.connect(worker)
        self.assertEqual(worker.client.subscriptions, [("fse-01/15/challenge", 1)])
        self.assertEqual(self.status_kinds(worker), ["ONLINE"])
        self.assertEqual(service.get_mqtt_status()["connection_state"], "CONNECTED")

    def test_challenge_message_is_processed_and_response_published(self):
        worker = self.make_worker()
        self.connect(worker)
        worker.client.on_message(worker.client, None, SimpleNamespace(topic="fse-01/15/challenge", payload=challenge()))
        worker.step()
        topic, payload, qos, retain = worker.client.published[-1]
        self.assertEqual((topic, qos, retain), ("fse-01/15/response", 1, False))
        self.assertEqual(json.loads(payload)["status"], "COMPLETED")
        self.assertIsNotNone(MqttChallenge.objects.get().published_at)
        status = service.get_mqtt_status()
        self.assertEqual((status["last_challenge_id"], status["last_response_status"]), ("CH-1", "COMPLETED"))
        self.assertEqual(status["challenges"]["completed"], 1)

    def test_messages_on_other_topics_are_ignored(self):
        worker = self.make_worker()
        worker.client.on_message(worker.client, None, SimpleNamespace(topic="fse-01/17/challenge", payload=challenge()))
        self.assertTrue(worker.inbox.empty())

    def test_t36_heartbeat_disconnect_stale_and_resubscribe(self):
        worker = self.make_worker()
        self.connect(worker)
        self.clock.advance(16)
        worker.step()
        self.assertEqual(self.status_kinds(worker), ["ONLINE", "HEARTBEAT"])

        worker.client.on_disconnect(worker.client, None, None, LOST, None)
        worker.step()
        status = service.get_mqtt_status()
        self.assertEqual(status["connection_state"], "DISCONNECTED")
        self.assertIn("Disconnected", status["last_error"])
        self.clock.advance(20)
        worker.step()  # no heartbeat while disconnected
        self.assertEqual(self.status_kinds(worker), ["ONLINE", "HEARTBEAT"])

        self.connect(worker)  # Paho reconnects; worker resubscribes
        self.assertEqual(worker.client.subscriptions, [("fse-01/15/challenge", 1)] * 2)
        self.assertEqual(self.status_kinds(worker)[-1], "ONLINE")

    def test_crashed_worker_becomes_stale(self):
        worker = self.make_worker()
        self.connect(worker)
        self.assertEqual(service.get_mqtt_status()["connection_state"], "CONNECTED")
        self.clock.advance(service.STALE_AFTER_SECONDS + 1)  # no more steps: process "died"
        status = service.get_mqtt_status()
        self.assertEqual((status["connection_state"], status["is_fresh"]), ("STALE", False))

    def test_failed_publish_is_retried_without_reprocessing(self):
        worker = self.make_worker()
        self.connect(worker)
        worker.client.publish_ok = False
        worker.client.on_message(worker.client, None, SimpleNamespace(topic="fse-01/15/challenge", payload=challenge()))
        worker.step()
        stored = MqttChallenge.objects.get()
        self.assertIsNone(stored.published_at)
        attempts = SubmissionAttempt.objects.count()

        worker.client.publish_ok = True
        self.clock.advance(31)
        worker.step()
        stored.refresh_from_db()
        self.assertIsNotNone(stored.published_at)
        self.assertEqual(worker.client.published[-1][1], stored.response_payload)
        self.assertEqual(SubmissionAttempt.objects.count(), attempts)

    def test_graceful_shutdown_publishes_offline(self):
        worker = self.make_worker()
        self.connect(worker)
        worker.shutdown()
        self.assertEqual(self.status_kinds(worker)[-1], "OFFLINE")
        self.assertEqual(service.get_mqtt_status()["connection_state"], "DISCONNECTED")

    def test_status_api(self):
        response = self.client.get("/api/mqtt/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["candidate_id"], body["connection_state"]), ("15", "NEVER_CONNECTED"))
        self.assertEqual(body["topics"]["challenge"], "fse-01/15/challenge")
