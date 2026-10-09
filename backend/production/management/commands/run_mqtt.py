import logging
import signal

from django.conf import settings
from django.core.management.base import BaseCommand

from production.modules.mqtt.client import MqttWorker, run_worker

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the MQTT challenge worker for the configured candidate (Ctrl+C to stop)."

    def add_arguments(self, parser):
        parser.add_argument("--max-runtime", type=float, default=None,
                            help="Stop after this many seconds (smoke tests).")

    def handle(self, *args, **options):
        worker = MqttWorker(
            host=settings.MQTT_HOST,
            port=settings.MQTT_PORT,
            candidate_id=settings.MQTT_CANDIDATE_ID,
            keepalive=settings.MQTT_KEEPALIVE,
        )
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: worker.request_stop())
        self.stdout.write(
            f"MQTT worker {worker.client_id} -> {settings.MQTT_HOST}:{settings.MQTT_PORT}, "
            f"subscribing to {worker.topics['challenge']}"
        )
        run_worker(worker, max_runtime=options["max_runtime"])
