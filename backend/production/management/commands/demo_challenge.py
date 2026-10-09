import json
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand

from production.modules.mqtt.service import handle_mqtt_challenge
from production.shared import clock
from production.shared.clock import to_utc_iso


class Command(BaseCommand):
    help = (
        "Print a challenge with fresh timestamps; with --process, run it through the same "
        "challenge service the MQTT worker uses (no network). Running the same --challenge-id "
        "and --body-file twice demonstrates exact replay."
    )

    def add_arguments(self, parser):
        parser.add_argument("--challenge-id", default=None)
        parser.add_argument("--ttl", type=int, default=15, help="Seconds until expires_at.")
        parser.add_argument("--body-file", help="Reuse a saved challenge body instead of generating one.")
        parser.add_argument("--save", help="Write the generated challenge body to this file.")
        parser.add_argument("--process", action="store_true", help="Process locally and print the response.")

    def handle(self, *args, **options):
        if options["body_file"]:
            with open(options["body_file"], encoding="utf-8") as handle:
                body = json.load(handle)
        else:
            now = clock.now()
            stamp = now.strftime("%H%M%S")
            body = {
                "protocol_version": "1.0",
                "candidate_id": str(settings.MQTT_CANDIDATE_ID),
                "challenge_id": options["challenge_id"] or f"CH-LOCAL-{stamp}",
                "command": "PROCESS_EVENTS",
                "sent_at": to_utc_iso(now),
                "expires_at": to_utc_iso(now + timedelta(seconds=options["ttl"])),
                "events": [
                    {"source_id": "LINE-01", "event_id": f"M-{stamp}-1", "type": "COUNT", "quantity": 5,
                     "target_event_id": None, "event_time": to_utc_iso(now)},
                    {"source_id": "LINE-01", "event_id": f"M-{stamp}-1", "type": "COUNT", "quantity": 5,
                     "target_event_id": None, "event_time": to_utc_iso(now)},
                    {"source_id": "LINE-01", "event_id": f"M-{stamp}-2", "type": "VOID", "quantity": None,
                     "target_event_id": f"M-{stamp}-1", "event_time": to_utc_iso(now)},
                ],
            }
        text = json.dumps(body, indent=2)
        if options["save"]:
            with open(options["save"], "w", encoding="utf-8") as handle:
                handle.write(text)
        self.stdout.write("Challenge:\n" + text)
        if options["process"]:
            handling = handle_mqtt_challenge(json.dumps(body).encode())
            self.stdout.write(f"\nHandled as {handling.kind} -> {handling.status}. Response:")
            self.stdout.write(json.dumps(json.loads(handling.response_text), indent=2))
