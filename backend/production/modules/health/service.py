"""
System health as plain true/false checks, shared by GET /api/status and
`python manage.py status`.

- database: PostgreSQL answers a trivial query.
- mqtt_connected: the MQTT worker reports CONNECTED with a fresh heartbeat
  (same freshness rule as /api/mqtt/status, reused rather than duplicated).
- ok: both are true.
"""

import logging

from django.db import DatabaseError, connection

from production.modules.mqtt.service import get_mqtt_status
from production.shared import clock
from production.shared.clock import to_utc_iso

logger = logging.getLogger(__name__)


def _database_ok():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)
    except DatabaseError:
        logger.exception("Health check: database unreachable")
        return False


def get_health():
    database = _database_ok()
    mqtt_state = None
    heartbeat_age = None
    if database:
        try:
            mqtt = get_mqtt_status()
            mqtt_state = mqtt["connection_state"]
            heartbeat_age = mqtt["heartbeat_age_seconds"]
        except DatabaseError:
            logger.exception("Health check: could not read MQTT status")
            database = False
    mqtt_connected = mqtt_state == "CONNECTED"
    return {
        "ok": database and mqtt_connected,
        "database": database,
        "mqtt_connected": mqtt_connected,
        "mqtt_state": mqtt_state,  # CONNECTED, STALE, DISCONNECTED, CONNECTING, NEVER_CONNECTED, or null
        "heartbeat_age_seconds": heartbeat_age,
        "checked_at": to_utc_iso(clock.now()),
    }
