"""Small JSON-API helpers: method checks, safe JSON parsing and sanitized errors."""

import functools
import json
import logging

from django.db import DatabaseError
from django.http import JsonResponse

logger = logging.getLogger("production.api")


class BadJson(ValueError):
    pass


def _reject_constant(name):
    raise BadJson(f"Invalid JSON value: {name}")


def parse_json_bytes(body):
    """Strict JSON parsing (UTF-8, no NaN/Infinity). Raises BadJson."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise BadJson("Request body must be UTF-8 encoded JSON") from None
    if not text.strip():
        raise BadJson("Request body is empty")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except BadJson:
        raise
    except ValueError as exc:
        raise BadJson(f"Malformed JSON: {exc}") from None


def json_error(status, code, message):
    return JsonResponse({"error": code, "message": message}, status=status)


def api_endpoint(methods):
    """Restrict methods and turn unexpected failures into sanitized JSON errors."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                response = json_error(405, "METHOD_NOT_ALLOWED", f"Use {', '.join(methods)}")
                response["Allow"] = ", ".join(methods)
                return response
            try:
                return view(request, *args, **kwargs)
            except DatabaseError:
                logger.exception("Database error in %s %s", request.method, request.path)
                return json_error(503, "DATABASE_UNAVAILABLE", "The database is unavailable; please retry")
            except Exception:
                logger.exception("Unhandled error in %s %s", request.method, request.path)
                return json_error(500, "INTERNAL_ERROR", "Internal server error")

        return wrapper

    return decorator
