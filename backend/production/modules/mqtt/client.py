"""
Long-lived MQTT worker (run via `python manage.py run_mqtt`).

Paho callbacks run on Paho's network thread and only enqueue small control
messages; the main thread does all database work and publishing, so callbacks
stay short and Django connections are used from one thread.
"""

import logging
import queue
import secrets
import threading
import time
from datetime import timedelta

from django.db import DatabaseError, close_old_connections, connection

from production.modules.mqtt import service
from production.modules.mqtt.protocol import status_message, topics
from production.shared import clock

logger = logging.getLogger(__name__)

QOS = 1
PUBLISH_TIMEOUT_SECONDS = 10
RETRY_UNPUBLISHED_SECONDS = 30


def _paho_client(client_id):
    import paho.mqtt.client as mqtt

    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
        clean_session=True,
    )


def _db_housekeeping():
    """Drop broken/expired DB connections between units of work (never inside a transaction)."""
    if not connection.in_atomic_block:
        close_old_connections()


def _sanitize(error):
    text = str(error).replace("\n", " ").strip()
    return text[:300] or error.__class__.__name__


class MqttWorker:
    def __init__(self, *, host, port, candidate_id, keepalive=30, client_factory=_paho_client,
                 heartbeat_seconds=service.HEARTBEAT_SECONDS, queue_size=200):
        self.host = host
        self.port = port
        self.candidate_id = str(candidate_id)
        self.keepalive = keepalive
        self.heartbeat_interval = timedelta(seconds=heartbeat_seconds)
        self.client_id = f"fse01-{self.candidate_id}-{secrets.token_hex(3)}"
        self.topics = topics(self.candidate_id)
        self.inbox = queue.Queue(maxsize=queue_size)
        self.stop_requested = threading.Event()
        self.subscribed = False
        self.last_heartbeat_sent = None
        self.last_retry_at = None

        self.client = client_factory(self.client_id)
        self.client.will_set(self.topics["status"], self._status_payload("OFFLINE"), qos=QOS, retain=False)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    # ----------------------------------------------------------- paho callbacks (network thread)

    def _enqueue(self, item):
        try:
            self.inbox.put_nowait(item)
        except queue.Full:
            logger.error("MQTT inbox full; dropped %s", item[0])

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._enqueue(("connected", reason_code))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._enqueue(("disconnected", reason_code))

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        self._enqueue(("subscribed", list(reason_codes)))

    def _on_message(self, client, userdata, message):
        if message.topic != self.topics["challenge"]:
            logger.warning("Ignoring message on unexpected topic %s", message.topic)
            return
        self._enqueue(("message", bytes(message.payload)))

    # ----------------------------------------------------------- lifecycle (main thread)

    def start(self):
        logger.info("Connecting to %s:%s as %s", self.host, self.port, self.client_id)
        self._persist(client_id=self.client_id, connection_state="CONNECTING", last_error=None)
        self.client.connect_async(self.host, self.port, keepalive=self.keepalive)
        self.client.loop_start()

    def request_stop(self):
        self.stop_requested.set()

    def shutdown(self):
        if self.subscribed:
            self._publish(self.topics["status"], self._status_payload("OFFLINE"), wait=True)
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()
        self.subscribed = False
        self._persist(connection_state="DISCONNECTED", disconnected_at=clock.now())
        logger.info("MQTT worker stopped")

    def step(self, timeout=0.0):
        """Handle at most one queued event, then periodic duties. Used by run_forever and tests."""
        try:
            kind, data = self.inbox.get(timeout=timeout) if timeout else self.inbox.get_nowait()
        except queue.Empty:
            kind = None
        _db_housekeeping()
        try:
            if kind == "connected":
                self._handle_connected(data)
            elif kind == "subscribed":
                self._handle_subscribed(data)
            elif kind == "disconnected":
                self._handle_disconnected(data)
            elif kind == "message":
                self._handle_message(data)
            self._periodic()
        except DatabaseError as exc:
            logger.exception("Database error in MQTT worker")
            self._persist(last_error=f"Database error: {_sanitize(exc)}", last_error_at=clock.now())
        finally:
            _db_housekeeping()

    # ----------------------------------------------------------- handlers

    def _handle_connected(self, reason_code):
        if getattr(reason_code, "is_failure", False):
            logger.error("MQTT connect refused: %s", reason_code)
            self._persist(connection_state="DISCONNECTED", last_error=f"Connect refused: {reason_code}",
                          last_error_at=clock.now())
            return
        logger.info("Connected; subscribing to %s", self.topics["challenge"])
        self.client.subscribe(self.topics["challenge"], qos=QOS)

    def _handle_subscribed(self, reason_codes):
        if any(getattr(code, "is_failure", False) for code in reason_codes):
            logger.error("Subscription refused: %s", reason_codes)
            self._persist(last_error=f"Subscription refused: {reason_codes}", last_error_at=clock.now())
            return
        self.subscribed = True
        now = clock.now()
        self._persist(connection_state="CONNECTED", connected_at=now, last_heartbeat_at=now)
        self._publish(self.topics["status"], self._status_payload("ONLINE"))
        self.last_heartbeat_sent = now
        logger.info("Subscribed; published ONLINE")
        self._retry_unpublished()

    def _handle_disconnected(self, reason_code):
        self.subscribed = False
        now = clock.now()
        unexpected = getattr(reason_code, "is_failure", False)
        fields = {"connection_state": "DISCONNECTED", "disconnected_at": now}
        if unexpected:
            fields.update(last_error=f"Disconnected: {reason_code}", last_error_at=now)
        self._persist(**fields)
        logger.warning("MQTT disconnected (%s); Paho will reconnect with backoff", reason_code)

    def _handle_message(self, payload):
        received = clock.now()
        try:
            handling = service.handle_mqtt_challenge(payload)
        except DatabaseError:
            raise
        except Exception as exc:  # never let one bad message kill the worker
            logger.exception("Unexpected error handling challenge")
            self._persist(last_error=f"Challenge handling error: {_sanitize(exc)}", last_error_at=clock.now())
            handling = service.internal_failure(payload)
        self._persist(last_challenge_id=handling.challenge_id, last_challenge_at=received)
        published = self._publish(self.topics["response"], handling.response_text, wait=True)
        if handling.challenge_pk is not None:
            service.record_publish_result(handling.challenge_pk, published)
        fields = {"last_response_status": handling.status}
        if published:
            fields["last_response_at"] = clock.now()
        self._persist(**fields)
        lateness = (clock.now() - received).total_seconds()
        logger.info("Challenge %s: %s %s (published=%s, %.2fs)",
                    handling.challenge_id, handling.kind, handling.status, published, lateness)

    def _periodic(self):
        now = clock.now()
        if self.subscribed and (self.last_heartbeat_sent is None or now - self.last_heartbeat_sent >= self.heartbeat_interval):
            self.last_heartbeat_sent = now
            if self._publish(self.topics["status"], self._status_payload("HEARTBEAT")):
                self._persist(last_heartbeat_at=now)
        if self.subscribed and (self.last_retry_at is None or (now - self.last_retry_at).total_seconds() >= RETRY_UNPUBLISHED_SECONDS):
            self._resume_incomplete()
            self._retry_unpublished()

    def _resume_incomplete(self):
        try:
            resumed = service.resume_incomplete_challenges()
        except DatabaseError:
            raise
        except Exception:
            logger.exception("Could not resume interrupted challenges")
            return
        if resumed:
            logger.info("Resumed %s interrupted challenge(s) from checkpoints", len(resumed))

    def _retry_unpublished(self):
        self.last_retry_at = clock.now()
        if not self.subscribed:
            return
        for pk, challenge_id, status, payload in service.unpublished_responses():
            published = self._publish(self.topics["response"], payload, wait=True)
            service.record_publish_result(pk, published)
            if published:
                self._persist(last_response_status=status, last_response_at=clock.now())
            logger.info("Retried saved response for %s (published=%s)", challenge_id, published)

    # ----------------------------------------------------------- helpers

    def _status_payload(self, status):
        return status_message(candidate_id=self.candidate_id, client_id=self.client_id, status=status, sent_at=clock.now())

    def _publish(self, topic, payload, wait=False):
        """Publish with QoS 1, retain=False. True only when PUBACK arrived (transport ack, not acceptance)."""
        try:
            info = self.client.publish(topic, payload, qos=QOS, retain=False)
            if wait:
                info.wait_for_publish(timeout=PUBLISH_TIMEOUT_SECONDS)
            return info.is_published() if wait else info.rc == 0
        except (RuntimeError, ValueError, OSError) as exc:
            logger.warning("Publish to %s failed: %s", topic, exc)
            return False

    def _persist(self, **fields):
        try:
            service.update_runtime_status(**fields)
        except DatabaseError:
            logger.exception("Could not persist MQTT runtime status")


def run_worker(worker, max_runtime=None):
    """Run until stopped (or max_runtime seconds, for smoke tests); always shut down cleanly."""
    started = time.monotonic()
    worker.start()
    worker._resume_incomplete()
    try:
        while not worker.stop_requested.is_set():
            worker.step(timeout=1.0)
            if max_runtime is not None and time.monotonic() - started >= max_runtime:
                break
    finally:
        worker.shutdown()
