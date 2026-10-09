import sys

from django.core.management.base import BaseCommand

from production.modules.health.service import get_health


class Command(BaseCommand):
    help = "Print system health (database, MQTT worker) as true/false. Exit code 0 when ok, 1 otherwise."

    def handle(self, *args, **options):
        health = get_health()
        mqtt_note = health["mqtt_state"] or "unknown"
        if health["heartbeat_age_seconds"] is not None:
            mqtt_note += f", heartbeat {health['heartbeat_age_seconds']:.0f} s ago"
        rows = [
            ("database", health["database"], ""),
            ("mqtt_connected", health["mqtt_connected"], f"({mqtt_note})"),
            ("ok", health["ok"], ""),
        ]
        for name, value, note in rows:
            text = f"{name:<16}{str(value).lower():<7}{note}".rstrip()
            self.stdout.write(self.style.SUCCESS(text) if value else self.style.ERROR(text))
        if not health["ok"]:
            sys.exit(1)
