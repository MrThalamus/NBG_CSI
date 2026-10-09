"""
Protocol Buffers <-> JSON-contract conversion (see proto/production.proto).

Decoded events become the same dicts a JSON client would send, so the shared
event service applies identical validation, identity and duplicate rules.
"""

from google.protobuf.message import DecodeError

from production.modules.pb import production_pb2 as pb

CONTENT_TYPE = "application/x-protobuf"

_TYPE_TO_TEXT = {pb.EVENT_TYPE_COUNT: "COUNT", pb.EVENT_TYPE_VOID: "VOID"}


class BadProtobuf(ValueError):
    pass


def wants_protobuf(request):
    return CONTENT_TYPE in request.headers.get("Accept", "")


def decode_event_batch(body):
    """Return a list of JSON-contract event dicts, or raise BadProtobuf."""
    batch = pb.EventBatch()
    try:
        batch.ParseFromString(body)
    except DecodeError as exc:
        raise BadProtobuf(f"Malformed protobuf EventBatch: {exc}") from None
    return [_event_to_dict(event) for event in batch.events]


def _event_to_dict(event):
    item = {}
    # proto3 strings cannot be absent; an empty string means "not sent".
    for field in ("source_id", "event_id", "event_time"):
        value = getattr(event, field)
        if value != "":
            item[field] = value
    if event.type in _TYPE_TO_TEXT:
        item["type"] = _TYPE_TO_TEXT[event.type]
    item["quantity"] = event.quantity if event.HasField("quantity") else None
    item["target_event_id"] = event.target_event_id if event.HasField("target_event_id") else None
    return item


def encode_results(results):
    message = pb.EventResults()
    for result in results:
        entry = message.results.add(status=pb.ResultStatus.Value(f"RESULT_STATUS_{result.status}"),
                                    message=result.message)
        if result.event_id is not None:
            entry.event_id = result.event_id
        if result.error_code:
            entry.error_code = result.error_code
    return message.SerializeToString()


def encode_summary(summary):
    return pb.Summary(**summary).SerializeToString()
