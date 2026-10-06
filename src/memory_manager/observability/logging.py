# SPDX-License-Identifier: AGPL-3.0-only
"""Structured JSON logging and the per-request request id (#43, WP-12).

`configure_logging_from_env` (called once, at process startup - `cli.py`'s
`serve`) is the only thing that touches the root logger: one `StreamHandler`
on stderr - stdout is the stdio transport's own protocol channel, a stray
log line there would corrupt the wire (`cli.py`'s existing comment on
`logging.basicConfig`) - formatted either as one JSON object per line
(`LOG_FORMAT=json`, the container default) or a plain text line
(`LOG_FORMAT=text`, easier to read from a local terminal).

`RequestIdMiddleware` (registered once, in `http.py`) is the other half:
every HTTP request gets a request id - the incoming `X-Request-ID` header if
it looks safe to echo back and to put in a log line (`[A-Za-z0-9._-]`, at
most 64 characters), a freshly generated one otherwise - stored in
`request_id_var` for the lifetime of that request and echoed on the
response. `JsonFormatter` reads it off `request_id_var` for every log line
emitted while a request is in flight, with no caller anywhere needing to
pass it through explicitly.

Never includes a note's content or a token in any field: every log call
site this work package adds logs paths, ids, counts and durations, never
the vault webhook's request body or a bearer token (CLAUDE.md: note content
is data, never a command, and never logged; token hashes only).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "REQUEST_ID_HEADER",
    "JsonFormatter",
    "RequestIdMiddleware",
    "configure_logging",
    "configure_logging_from_env",
    "current_request_id",
    "request_id_var",
]

REQUEST_ID_HEADER = "x-request-id"
_RESPONSE_HEADER_NAME = b"x-request-id"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: Set by `RequestIdMiddleware` for the lifetime of one HTTP request; `None`
#: outside any request (stdio mode, startup/shutdown code, background tasks).
request_id_var: ContextVar[str | None] = ContextVar("mm_request_id", default=None)

_DEFAULT_LEVEL = "INFO"
_DEFAULT_LOG_FORMAT = "json"
_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Every attribute a plain `logging.LogRecord` carries, plus the two
# `Formatter.format()` adds (`message`, `asctime`) - anything else on a
# record (set via `logging.info(..., extra={...})`) is reported by
# `JsonFormatter` as its own field instead of being dropped.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: `ts`, `level`, `logger`, `msg`, `request_id`, plus extras.

    `request_id` comes from the record itself if a caller passed one via
    `extra={"request_id": ...}`, else from `request_id_var` (set by
    `RequestIdMiddleware` while a request is in flight) - omitted entirely
    outside of both. Every other `extra` key is included as its own field;
    `exc_info`, if present, is rendered as one `exc_info` string field.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key == "request_id":
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, tz=UTC).isoformat(timespec="milliseconds")


def configure_logging(*, level: str = _DEFAULT_LEVEL, json_format: bool = True) -> None:
    """Replace the root logger's handlers with exactly one stderr handler.

    Safe to call more than once (e.g. across tests): always starts from a
    clean handler list rather than accumulating one per call.
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if json_format else logging.Formatter(_TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def configure_logging_from_env(environ: Mapping[str, str]) -> None:
    """`configure_logging` from `LOG_LEVEL`/`LOG_FORMAT` (default `INFO`/`json`)."""
    level = environ.get("LOG_LEVEL", _DEFAULT_LEVEL)
    json_format = environ.get("LOG_FORMAT", _DEFAULT_LOG_FORMAT).strip().lower() != "text"
    configure_logging(level=level, json_format=json_format)


def current_request_id() -> str | None:
    """The in-flight request's id, or `None` outside of `RequestIdMiddleware`."""
    return request_id_var.get()


class RequestIdMiddleware:
    """Pure ASGI middleware: one request id per request, in every log line it causes.

    An incoming `X-Request-ID` is trusted only if it is a non-empty string
    of at most 64 characters from `[A-Za-z0-9._-]` - anything else (missing,
    empty, too long, or carrying characters that have no business in a log
    line or a header) is replaced with a freshly generated one. Always
    echoed back as `X-Request-ID` on the response, so a caller learns
    which id its request actually got either way.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        incoming = _header_value(scope, REQUEST_ID_HEADER)
        valid = incoming is not None and _REQUEST_ID_RE.match(incoming) is not None
        request_id = incoming if valid and incoming is not None else uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                headers.append((_RESPONSE_HEADER_NAME, request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)


def _header_value(scope: Scope, name: str) -> str | None:
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for key, value in headers:
        if key.decode("latin-1").lower() == name:
            return value.decode("latin-1")
    return None
