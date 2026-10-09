"""GET /api/status and `manage.py status`: true/false health checks."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase, override_settings

from production.modules.mqtt import service
from production.tests.test_mqtt import NOW, FakeClock


@override_settings(MQTT_CANDIDATE_ID="15")
class HealthTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch("production.shared.clock.now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect_worker(self):
        service.update_runtime_status(connection_state="CONNECTED", connected_at=NOW, last_heartbeat_at=NOW)

    def test_never_connected_worker_is_not_ok(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual((body["ok"], body["database"], body["mqtt_connected"]), (False, True, False))
        self.assertEqual(body["mqtt_state"], "NEVER_CONNECTED")

    def test_connected_worker_with_fresh_heartbeat_is_ok(self):
        self.connect_worker()
        self.clock.advance(10)
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["ok"], body["database"], body["mqtt_connected"], body["mqtt_state"]),
                         (True, True, True, "CONNECTED"))
        self.assertEqual(body["heartbeat_age_seconds"], 10.0)

    def test_stale_or_disconnected_worker_is_not_ok(self):
        self.connect_worker()
        self.clock.advance(service.STALE_AFTER_SECONDS + 1)
        body = self.client.get("/api/status").json()
        self.assertEqual((body["ok"], body["mqtt_connected"], body["mqtt_state"]), (False, False, "STALE"))
        service.update_runtime_status(connection_state="DISCONNECTED")
        self.assertEqual(self.client.get("/api/status").json()["mqtt_state"], "DISCONNECTED")

    def test_database_down_reports_false_without_leaking_details(self):
        with mock.patch("production.modules.health.service.connection.cursor",
                        side_effect=OperationalError("password=hunter2")), \
                self.assertLogs("production.modules.health.service", level="ERROR"):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual((body["ok"], body["database"], body["mqtt_connected"], body["mqtt_state"]),
                         (False, False, False, None))
        self.assertNotIn("hunter2", response.content.decode())

    def test_only_get_allowed(self):
        self.assertEqual(self.client.post("/api/status").status_code, 405)

    def test_status_command_prints_checks_and_exit_code(self):
        self.connect_worker()
        out = StringIO()
        call_command("status", stdout=out)
        text = out.getvalue()
        self.assertIn("database        true", text)
        self.assertIn("mqtt_connected  true", text)
        self.assertIn("ok              true", text)

        self.clock.advance(service.STALE_AFTER_SECONDS + 1)
        out = StringIO()
        with self.assertRaises(SystemExit) as ctx:
            call_command("status", stdout=out)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("mqtt_connected  false  (STALE", out.getvalue())
