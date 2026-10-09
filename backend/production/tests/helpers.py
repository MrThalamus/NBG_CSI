import json
from datetime import datetime, timedelta, timezone

from django.test import Client

BASE_TIME = datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def count(event_id, quantity=5, source_id="LINE-01", minutes=0, **extra):
    payload = {
        "source_id": source_id,
        "event_id": event_id,
        "type": "COUNT",
        "quantity": quantity,
        "target_event_id": None,
        "event_time": iso(BASE_TIME + timedelta(minutes=minutes)),
    }
    payload.update(extra)
    return payload


def void(event_id, target_event_id, source_id="LINE-01", minutes=1, **extra):
    payload = {
        "source_id": source_id,
        "event_id": event_id,
        "type": "VOID",
        "quantity": None,
        "target_event_id": target_event_id,
        "event_time": iso(BASE_TIME + timedelta(minutes=minutes)),
    }
    payload.update(extra)
    return payload


class ApiMixin:
    """Thin JSON helpers around the Django test client."""

    def post_events(self, body, raw=False):
        data = body if raw else json.dumps(body)
        return self.client.post("/api/events", data=data, content_type="application/json")

    def submit(self, body):
        response = self.post_events(body)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def statuses(self, body):
        return [r["status"] for r in self.submit(body)]

    def ack(self, ids):
        response = self.client.post(
            "/api/ack", data=json.dumps({"event_ids": ids}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def summary(self, source_id=None):
        params = {"view": "summary"}
        if source_id is not None:
            params["source_id"] = source_id
        response = self.client.get("/api/state", params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def view(self, name, source_id=None):
        params = {"view": name}
        if source_id is not None:
            params["source_id"] = source_id
        response = self.client.get("/api/state", params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["items"]


def summary_dict(net, processed, pending_ack, unresolved, duplicates, conflicts):
    return {
        "net_total": net,
        "processed_events": processed,
        "pending_ack": pending_ack,
        "unresolved": unresolved,
        "duplicates": duplicates,
        "conflicts": conflicts,
    }


__all__ = ["ApiMixin", "BASE_TIME", "Client", "count", "iso", "summary_dict", "void"]
