# SPDX-License-Identifier: AGPL-3.0-only
"""Exports every audit record to stdout and/or an OTLP logs endpoint for a SIEM (#245).

`AuditWriter.record` (`audit.py`) calls `AuditExporter.export` once per
`audit_log` row it inserts, after the DB insert - `AUDIT_EXPORT` (parsed by
`config.audit_export_targets_from_env`) picks which of the two targets
below (both, one, or neither - the default) get that call:

- `"stdout"`: one JSON line per record, via `observability.logging.
  JsonFormatter` on a dedicated logger - written to **stderr**, not the
  real `stdout` file descriptor, because `observability.logging`'s own
  module docstring reserves actual stdout for the stdio transport's MCP
  wire protocol (a stray line there would corrupt it); a container's log
  collector scrapes stderr exactly the same way it scrapes stdout, so the
  SIEM-ingestion use case this target exists for is unaffected.
- `"otlp"`: the existing optional `otel` extra (`opentelemetry-sdk`,
  `opentelemetry-exporter-otlp`), following the off-unless-installed
  pattern of `observability/tracing.py` - except that here, unlike
  tracing, `otlp` without the extra installed is a startup error
  (`AuditConfigError`, raised from `from_env`) rather than a silent no-op:
  an operator who asked for SIEM export must not end up quietly getting
  none. Bridges a dedicated `logging.Logger` into the OTel Logs SDK via
  `opentelemetry.sdk._logs.LoggingHandler`, the SDK's own stdlib-logging
  integration, onto a `LoggerProvider`/`BatchLogRecordProcessor`/
  `OTLPLogExporter` pointed at `OTEL_EXPORTER_OTLP_ENDPOINT` (the same
  env var `tracing.py` reads, no second one invented for this).

`export()` never raises: a SIEM outage must never fail or undo the audit
write it describes, the same contract `AuditWriter.record` already gives
the DB insert itself. Records carry metadata only (`audit.py`'s own
docstring) - this module adds nothing from note content to any record, it
only moves whatever `AuditWriter.record` already built along.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

from memory_manager.config import AuditConfigError, audit_export_targets_from_env
from memory_manager.observability.logging import JsonFormatter

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import LogRecordExporter

__all__ = ["AuditExporter"]

_logger = logging.getLogger(__name__)

#: The standard OTel env var `tracing.py` already reads for span export;
#: reused verbatim here rather than inventing an audit-specific one.
_OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
_SERVICE_NAME = "memory-manager"
_STDOUT_LOGGER_NAME = "memory_manager.audit.export.stdout"
_OTLP_LOGGER_NAME = "memory_manager.audit.export.otlp"


class AuditExporter:
    """Exports one structured event per `export()` call to every configured target.

    Built once per `AuditWriter` (`from_env`, normally at process startup)
    and reused for every record; `otlp_provider` is kept only so `flush()`
    (tests, and a graceful shutdown) can force the OTLP batch processor to
    send what it is still holding before the process exits.
    """

    def __init__(
        self,
        *,
        stdout_logger: logging.Logger | None,
        otlp_logger: logging.Logger | None,
        otlp_provider: LoggerProvider | None = None,
    ) -> None:
        self._stdout_logger = stdout_logger
        self._otlp_logger = otlp_logger
        self._otlp_provider = otlp_provider

    @classmethod
    def from_env(
        cls, environ: dict[str, str], *, otlp_exporter: LogRecordExporter | None = None
    ) -> AuditExporter:
        """Build from `AUDIT_EXPORT` (`config.audit_export_targets_from_env`).

        Raises `AuditConfigError` if `AUDIT_EXPORT` names `"otlp"` and the
        `otel` extra is not installed - before any audit record is ever
        exported, not discovered only once the first write happens.

        `otlp_exporter` is a test seam: given, it replaces the real network
        `OTLPLogExporter` (an in-memory one in `tests/test_audit_export.py`)
        - production code never passes it.
        """
        targets = audit_export_targets_from_env(environ)
        stdout_logger = _build_stdout_logger() if "stdout" in targets else None
        otlp_logger: logging.Logger | None = None
        otlp_provider: LoggerProvider | None = None
        if "otlp" in targets:
            otlp_logger, otlp_provider = _build_otlp_logger(environ, otlp_exporter=otlp_exporter)
        return cls(
            stdout_logger=stdout_logger, otlp_logger=otlp_logger, otlp_provider=otlp_provider
        )

    def export(self, fields: dict[str, Any]) -> None:
        """Emit `fields` (one `audit_log` row's worth) to every configured target.

        Never raises: a failure on one target (or both) is logged and
        swallowed, same as a failed DB write in `AuditWriter.record`.
        """
        if self._stdout_logger is not None:
            _safe_emit(self._stdout_logger, fields)
        if self._otlp_logger is not None:
            _safe_emit(self._otlp_logger, fields)

    def flush(self) -> None:
        """Force the OTLP batch processor to send what it is still holding; a no-op
        for every other configuration (including no `otlp` target at all)."""
        if self._otlp_provider is not None:
            self._otlp_provider.force_flush()


def _safe_emit(logger: logging.Logger, fields: dict[str, Any]) -> None:
    try:
        logger.info("audit", extra=fields)
    except Exception:
        _logger.exception("audit export to %s failed", logger.name)


def _build_stdout_logger() -> logging.Logger:
    """A dedicated logger, `JsonFormatter`-formatted, on stderr (see module docstring
    for why stderr and not the real stdout)."""
    logger = logging.getLogger(_STDOUT_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def _build_otlp_logger(
    environ: dict[str, str], *, otlp_exporter: LogRecordExporter | None
) -> tuple[logging.Logger, LoggerProvider]:
    """A dedicated logger bridged into the OTel Logs SDK via `LoggingHandler`.

    Raises `AuditConfigError` if the `otel` extra is not installed - this is
    the "refuses startup" half of the module docstring's otlp contract.
    """
    try:
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource

        if otlp_exporter is None:
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    except ImportError as exc:
        raise AuditConfigError(
            "AUDIT_EXPORT names 'otlp', but the 'otel' extra is not installed - install "
            "it to enable SIEM export via OTLP, e.g. `uv sync --extra otel` or "
            "`pip install 'memory-manager[otel]'`"
        ) from exc

    provider = LoggerProvider(resource=Resource.create({SERVICE_NAME: _SERVICE_NAME}))
    exporter = (
        otlp_exporter
        if otlp_exporter is not None
        else OTLPLogExporter(endpoint=environ.get(_OTLP_ENDPOINT_ENV) or None)
    )
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    logger = logging.getLogger(_OTLP_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))
    return logger, provider
