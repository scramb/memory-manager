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

`JsonFormatter` additionally carries `trace_id`/`span_id` (#262, WP-31)
whenever an OTel span is current - `opentelemetry.trace.get_current_span()`,
the SDK's own ambient accessor, the same one `tracing.py`'s
`start_as_current_span` call sites feed via `contextvars` (that module's
own docstring). Importing `opentelemetry` is optional here too, same as
`tracing.py`: a bare `try/except ImportError` around the one call site,
never a module-level import, so this module (loaded on every process start,
stdio included) never requires the `otel` extra.
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
    "AccessLogQueryRedactionFilter",
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

#: Query-string keys whose value never belongs in a log line, even redirected
#: through `uvicorn.access` - OAuth/OIDC authorization codes, tokens and the
#: Hydra/OIDC login and consent challenges (#85), plus a plain `password`.
#: Matched case-insensitively; every other query parameter passes through.
_REDACTED_QUERY_KEYS = frozenset(
    {
        "code",
        "state",
        "code_verifier",
        "code_challenge",
        "token",
        "access_token",
        "refresh_token",
        "pending",
        "login_challenge",
        "consent_challenge",
        "client_secret",
        "password",
    }
)

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
        payload.update(_trace_context_fields())
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key == "request_id":
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, tz=UTC).isoformat(timespec="milliseconds")


def _trace_context_fields() -> dict[str, str]:
    """`{"trace_id": ..., "span_id": ...}` for the OTel span current right now, or
    `{}` whenever there is none - no span at all (tracing off, or outside any
    `start_as_current_span`), or the `otel` extra is not installed (module
    docstring).
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return {}
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return {}
    return {
        "trace_id": trace.format_trace_id(span_context.trace_id),
        "span_id": trace.format_span_id(span_context.span_id),
    }


def _redact_query_string(full_path: str) -> str:
    """`full_path` with every `_REDACTED_QUERY_KEYS` value replaced by `[redacted]`.

    `full_path` is the `path?query` uvicorn logs (`get_path_with_query_string`
    in its protocol implementations) - split on the first `?`, each
    `key=value` pair in the query is redacted independently, so a path with
    no query string or an unrelated query (`/login?pending=P` vs.
    `/static/app.js`) is affected exactly where it has a matching key.
    """
    path, sep, query = full_path.partition("?")
    if not sep:
        return full_path
    parts = []
    for part in query.split("&"):
        key, eq, _value = part.partition("=")
        if eq and key.lower() in _REDACTED_QUERY_KEYS:
            parts.append(f"{key}=[redacted]")
        else:
            parts.append(part)
    return f"{path}?{'&'.join(parts)}"


class AccessLogQueryRedactionFilter(logging.Filter):
    """Redacts OAuth/OIDC secrets from `uvicorn.access` request lines (#85).

    uvicorn logs one access line per request as
    `'%s - "%s %s HTTP/%s" %d'` with
    `args = (client_addr, method, full_path, http_version, status_code)`
    (`uvicorn.protocols.http.*`) - `full_path` (`args[2]`) carries the raw
    query string, which is exactly where an OAuth code or a Hydra login
    challenge would otherwise end up verbatim in every access log line.
    Rewriting `record.args` here (rather than the formatted message) keeps
    this working for any `Formatter`, including `JsonFormatter`, whose
    `getMessage()` call is what actually interpolates `record.msg %
    record.args`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > 2 and isinstance(args[2], str):
            redacted = _redact_query_string(args[2])
            if redacted != args[2]:
                record.args = (*args[:2], redacted, *args[3:])
        return True


def _attach_access_log_redaction() -> None:
    """Attach `AccessLogQueryRedactionFilter` to `uvicorn.access` exactly once.

    Idempotent, so it does not matter whether `configure_logging` runs
    before or after uvicorn creates its own `uvicorn.access` logger
    (`logging.getLogger` always returns the same singleton) or whether
    `configure_logging` itself is called more than once (e.g. across tests).
    """
    access_logger = logging.getLogger("uvicorn.access")
    if any(isinstance(f, AccessLogQueryRedactionFilter) for f in access_logger.filters):
        return
    access_logger.addFilter(AccessLogQueryRedactionFilter())


def configure_logging(*, level: str = _DEFAULT_LEVEL, json_format: bool = True) -> None:
    """Replace the root logger's handlers with exactly one stderr handler.

    Safe to call more than once (e.g. across tests): always starts from a
    clean handler list rather than accumulating one per call. Also attaches
    `AccessLogQueryRedactionFilter` to the `uvicorn.access` logger (#85), so
    secrets in the query string never reach either handler this sets up.
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if json_format else logging.Formatter(_TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    _attach_access_log_redaction()


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
