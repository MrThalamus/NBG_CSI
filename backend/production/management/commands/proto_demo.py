import http.cookiejar
import urllib.request
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from production.modules.pb import production_pb2 as pb
from production.modules.pb.codec import CONTENT_TYPE


class Command(BaseCommand):
    help = (
        "Demonstrate the optional Protocol Buffers encoding against a running server over real HTTP: "
        "POST an EventBatch to /api/events and read the summary as a protobuf Summary. "
        "Creates events with unique PB-* IDs on source LINE-PB."
    )

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="http://127.0.0.1:8001")

    def handle(self, *args, **options):
        base = options["base_url"].rstrip("/")
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.open(f"{base}/").read()  # sets the csrftoken cookie
        token = next(c.value for c in jar if c.name == "csrftoken")

        now = datetime.now(timezone.utc)
        stamp = now.strftime("%H%M%S")
        when = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        batch = pb.EventBatch(events=[
            pb.Event(source_id="LINE-PB", event_id=f"PB-{stamp}-1", type=pb.EVENT_TYPE_COUNT, quantity=4, event_time=when),
            pb.Event(source_id="LINE-PB", event_id=f"PB-{stamp}-1", type=pb.EVENT_TYPE_COUNT, quantity=4, event_time=when),
            pb.Event(source_id="LINE-PB", event_id=f"PB-{stamp}-2", type=pb.EVENT_TYPE_VOID,
                     target_event_id=f"PB-{stamp}-1", event_time=when),
        ])
        body = batch.SerializeToString()
        request = urllib.request.Request(f"{base}/api/events", data=body, method="POST", headers={
            "Content-Type": CONTENT_TYPE, "Accept": CONTENT_TYPE, "X-CSRFToken": token,
        })
        with opener.open(request) as response:
            results = pb.EventResults.FromString(response.read()).results
        self.stdout.write(f"POST /api/events ({len(body)} bytes protobuf):")
        for result in results:
            status = pb.ResultStatus.Name(result.status).removeprefix("RESULT_STATUS_")
            self.stdout.write(f"  {result.event_id:<14} {status:<18} {result.message}")

        request = urllib.request.Request(f"{base}/api/state?view=summary&source_id=LINE-PB",
                                         headers={"Accept": CONTENT_TYPE})
        with opener.open(request) as response:
            summary = pb.Summary.FromString(response.read())
        self.stdout.write("GET /api/state?view=summary&source_id=LINE-PB (protobuf Summary):")
        for field in ("net_total", "processed_events", "pending_ack", "unresolved", "duplicates", "conflicts"):
            self.stdout.write(f"  {field:<17} {getattr(summary, field)}")
