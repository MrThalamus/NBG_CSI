"""
Real concurrency tests: separate threads, separate PostgreSQL connections,
released together by a barrier. TransactionTestCase so commits are real.
"""

import json
import threading
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import Client, TransactionTestCase, override_settings

from production.models import AppliedReversal, MqttChallenge, ProductionEvent, SubmissionAttempt
from production.modules.ack.service import acknowledge_events
from production.modules.events.service import process_event
from production.modules.mqtt import service
from production.modules.state.queries import get_summary
from production.tests.helpers import BASE_TIME, count, iso, void

WORKERS = 8


def run_concurrently(target, n=WORKERS):
    """Run target(index) in n threads started together; return results in index order."""
    barrier = threading.Barrier(n)
    results = [None] * n
    errors = []

    def runner(index):
        try:
            barrier.wait(timeout=10)
            results[index] = target(index)
        except Exception as exc:  # surfaced in the test thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    if errors:
        raise errors[0]
    return results


class ConcurrencyTests(TransactionTestCase):
    serialized_rollback = True  # restores the migration-seeded ProcessingMutex row

    def test_t30_concurrent_identical_counts(self):
        payload = json.dumps(count("C-1", 5))

        def submit(_):
            response = Client().post("/api/events", payload, content_type="application/json")
            return response.json()["results"][0]["status"]

        statuses = run_concurrently(submit)
        self.assertEqual(sorted(statuses), ["ACCEPTED"] + ["DUPLICATE"] * (WORKERS - 1))
        self.assertEqual(ProductionEvent.objects.count(), 1)
        self.assertEqual(SubmissionAttempt.objects.count(), WORKERS)
        self.assertEqual(get_summary()["net_total"], 5)
        self.assertEqual(get_summary()["duplicates"], WORKERS - 1)

    def test_t31_concurrent_competing_voids(self):
        process_event(count("C-1", 5), "REST")
        statuses = run_concurrently(lambda i: process_event(void(f"V-{i}", "C-1"), "REST").status)
        self.assertEqual(sorted(statuses), ["ACCEPTED"] + ["REJECTED"] * (WORKERS - 1))
        self.assertEqual(AppliedReversal.objects.count(), 1)
        self.assertEqual(get_summary()["net_total"], 0)

    def test_concurrent_pending_voids_and_target_count(self):
        # Pending VOIDs and the target COUNT race; whatever the interleaving, at most one reversal.
        def act(i):
            if i == 0:
                return process_event(count("C-1", 4), "REST").status
            return process_event(void(f"V-{i}", "C-1"), "REST").status

        run_concurrently(act)
        self.assertEqual(AppliedReversal.objects.count(), 1)
        self.assertEqual(get_summary()["net_total"], 0)
        self.assertEqual(ProductionEvent.objects.filter(type="VOID", processing_status="ACCEPTED").count(), 1)
        self.assertEqual(ProductionEvent.objects.filter(processing_status="PENDING_REFERENCE").count(), 0)

    def test_t32_concurrent_acks(self):
        process_event(count("C-1", 5), "REST")
        statuses = run_concurrently(lambda _: acknowledge_events(["C-1"])[0]["status"])
        self.assertEqual(sorted(statuses), ["ACKED"] + ["ALREADY_ACKED"] * (WORKERS - 1))
        self.assertEqual(get_summary()["pending_ack"], 0)

    @override_settings(MQTT_CANDIDATE_ID="15")
    def test_t33_concurrent_identical_challenge_deliveries(self):
        sent = BASE_TIME
        body = json.dumps({
            "protocol_version": "1.0",
            "candidate_id": "15",
            "challenge_id": "CH-RACE",
            "command": "PROCESS_EVENTS",
            "sent_at": iso(sent),
            "expires_at": iso(sent + timedelta(seconds=15)),
            "events": [count("A", 1), count("B", 2), void("V", "A"), count("C", 3)],
        }).encode()
        with mock.patch("production.shared.clock.now", lambda: sent + timedelta(seconds=1)):
            handlings = run_concurrently(lambda _: service.handle_mqtt_challenge(body))
        self.assertEqual(len({h.response_text for h in handlings}), 1)
        response = json.loads(handlings[0].response_text)
        self.assertEqual(response["status"], "COMPLETED")
        self.assertEqual([r["status"] for r in response["results"]], ["ACCEPTED"] * 4)
        # One processing sequence: one attempt per item, no duplicates from racing deliveries.
        self.assertEqual(SubmissionAttempt.objects.count(), 4)
        stored = MqttChallenge.objects.get()
        self.assertEqual(stored.delivery_count, WORKERS)
        self.assertEqual(get_summary()["net_total"], 5)
