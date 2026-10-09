"""
T34: durable state survives a restart. Data is really committed (TransactionTestCase),
then the database connection is closed and fresh client/service objects are used,
which is what a process restart amounts to: nothing is kept in process memory.
"""

import json
from datetime import timedelta

from django.db import connection
from django.test import Client, TransactionTestCase, override_settings

from production.models import MqttChallenge, SubmissionAttempt
from production.modules.mqtt import service
from production.shared import clock
from production.shared.clock import to_utc_iso
from production.tests.helpers import count, void


def fresh_challenge(challenge_id, events):
    now = clock.now()
    return json.dumps({
        "protocol_version": "1.0",
        "candidate_id": "15",
        "challenge_id": challenge_id,
        "command": "PROCESS_EVENTS",
        "sent_at": to_utc_iso(now),
        "expires_at": to_utc_iso(now + timedelta(seconds=60)),
        "events": events,
    }).encode()


@override_settings(MQTT_CANDIDATE_ID="15")
class RestartTests(TransactionTestCase):
    serialized_rollback = True  # restores the migration-seeded ProcessingMutex row

    def snapshot(self, client):
        return {view: client.get("/api/state", {"view": view}).content for view in ("summary", "pending", "exceptions")}

    def test_t34_state_pending_void_and_challenge_replay_survive_restart(self):
        before = Client()
        before.post("/api/events", json.dumps([count("C-1", 5), void("V-2", "C-2"), count("C-1", 6)]),
                    content_type="application/json")
        before.post("/api/ack", json.dumps({"event_ids": ["C-1"]}), content_type="application/json")
        raw = fresh_challenge("CH-RESTART", [count("M-1", 3, source_id="LINE-02")])
        first = service.handle_mqtt_challenge(raw)
        state_before = self.snapshot(before)

        connection.close()  # "restart": new database session, new objects, no cached state
        after = Client()

        self.assertEqual(self.snapshot(after), state_before)
        summary = json.loads(state_before["summary"])
        self.assertEqual((summary["net_total"], summary["unresolved"], summary["pending_ack"]), (8, 1, 1))

        # The pending VOID still resolves after the restart.
        after.post("/api/events", json.dumps(count("C-2", 7)), content_type="application/json")
        summary = after.get("/api/state").json()
        self.assertEqual((summary["net_total"], summary["unresolved"], summary["processed_events"]), (8, 0, 4))

        # The stored challenge replays its original response byte for byte, adding no attempts.
        attempts = SubmissionAttempt.objects.count()
        replay = service.handle_mqtt_challenge(raw)
        self.assertEqual((replay.kind, replay.response_text), ("REPLAY", first.response_text))
        self.assertEqual(SubmissionAttempt.objects.count(), attempts)

    def test_t34_interrupted_challenge_resumes_after_restart(self):
        raw = fresh_challenge("CH-INTERRUPTED", [count("A", 1), count("B", 2)])
        original = service.process_event

        class ProcessKilled(BaseException):
            pass

        def killed_on_second(*args, **kwargs):
            if kwargs.get("item_index") == 1:
                raise ProcessKilled()
            return original(*args, **kwargs)

        service.process_event = killed_on_second
        try:
            with self.assertRaises(ProcessKilled):
                service.handle_mqtt_challenge(raw)
        finally:
            service.process_event = original

        connection.close()  # restart
        stored = MqttChallenge.objects.get()
        self.assertEqual((stored.status, stored.next_index), ("PROCESSING", 1))

        self.assertEqual(service.resume_incomplete_challenges(), [stored.pk])  # what the worker does on startup
        stored.refresh_from_db()
        response = json.loads(stored.response_payload)
        self.assertEqual(response["status"], "COMPLETED")
        self.assertEqual([r["event_id"] for r in response["results"]], ["A", "B"])
        self.assertEqual(SubmissionAttempt.objects.filter(event_id="A").count(), 1)
