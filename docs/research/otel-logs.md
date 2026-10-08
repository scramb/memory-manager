# OpenTelemetry Logs SDK status in the pinned `opentelemetry-sdk`/`opentelemetry-exporter-otlp`

Retrieved: 2026-10-08

Method: facts about the installed package come from introspecting the actual `opentelemetry-sdk`
1.45.1 / `opentelemetry-exporter-otlp` 1.45.1 modules in this repo's `.venv` (the versions
`pyproject.toml`'s `otel` extra pins, `>=1.45.1`) and cross-checked against the source at GitHub
tag `v1.45.1` of `open-telemetry/opentelemetry-python` (release published 2026-10-06 per the
GitHub Releases API) [R1], [R2]. `observability/audit_export.py` (#245) is built against exactly
what is documented here.

## 1. The Logs SDK is usable, but explicitly not stable

The Logs API/SDK ships inside the same `opentelemetry-api`/`opentelemetry-sdk` packages as traces
and metrics (no separate package to add), under the `opentelemetry.sdk._logs` private-looking
module path - the leading underscore is intentional upstream: logs are the one signal still marked
"not stable yet" in this SDK generation, several classes are already deprecated in favour of a
later redesign while remaining fully functional today. [R3], [R4]

## 2. Classes actually available at 1.45.1 (what `audit_export.py` uses)

| Symbol | Module | Status |
|---|---|---|
| `LoggerProvider` | `opentelemetry.sdk._logs` | current |
| `LoggingHandler` | `opentelemetry.sdk._logs` | **deprecated** (see §3) but present and functional |
| `BatchLogRecordProcessor` | `opentelemetry.sdk._logs.export` | current |
| `OTLPLogExporter` (grpc) | `opentelemetry.exporter.otlp.proto.grpc._log_exporter` | current |
| `InMemoryLogRecordExporter` | `opentelemetry.sdk._logs.export` | current (test-only, by its own docstring) |
| `ReadableLogRecord` | `opentelemetry.sdk._logs` | current |

`LoggerProvider.add_log_record_processor(processor)` is the method that wires a processor (here:
`BatchLogRecordProcessor(OTLPLogExporter(...))`) onto a provider. [R5]

## 3. `LoggingHandler` is deprecated, with no extra-package-free replacement yet

`LoggingHandler.__init__` (`opentelemetry/sdk/_logs/_internal/__init__.py:601-619` at tag
`v1.45.1`) unconditionally raises a `DeprecationWarning` on construction:

> "`LoggingHandler` in `opentelemetry-sdk` is deprecated. Use the handler from
> `opentelemetry-instrumentation-logging` instead." [R6]

`opentelemetry-instrumentation-logging` (latest: 0.66b1 on PyPI, 2026-10-08) [R7] is a **separate
package**, not part of the `otel` extra (`opentelemetry-sdk`, `opentelemetry-exporter-otlp`) this
repo already depends on - adding it would be a new dependency (CLAUDE.md: few dependencies, no
new dependency beyond the existing `otel` extra). `audit_export.py` therefore still uses the
deprecated `LoggingHandler`: it is the only stdlib-`logging`-to-OTel-Logs bridge available without
pulling in a second package, it still works exactly as documented, and the warning is harmless -
`tests/test_audit_export.py` surfaces it in `make check`'s warnings summary rather than silencing
it. Worth re-checking once this SDK's logs API stabilizes (tracked upstream; no fixed date found).

## 4. `InMemoryLogExporter` vs. `InMemoryLogRecordExporter`

`opentelemetry.sdk._logs.export.InMemoryLogExporter` is itself `@deprecated("Use
InMemoryLogRecordExporter. Since logs are not stable yet this WILL be removed in future
releases.")` and is now a one-line subclass of `InMemoryLogRecordExporter` that adds nothing.
[R8] `tests/test_audit_export.py` uses `InMemoryLogRecordExporter` (the non-deprecated name)
as the `otlp_exporter` test seam `AuditExporter.from_env` accepts, confirmed by constructing one
locally: it raised the deprecation warning from the `LogExporter` alias, not from
`LogRecordExporter`.

## 5. `LogRecord` was renamed; `ReadableLogRecord`/`ReadWriteLogRecord` are the current names

`opentelemetry.sdk._logs` at 1.45.1 has no `LogRecord` export at all - `dir()` on the installed
module lists `ReadableLogRecord` and `ReadWriteLogRecord` instead (defined at
`opentelemetry/sdk/_logs/_internal/__init__.py:229` and `:273` of tag `v1.45.1`). [R9] Anything
written against an older-tutorial `LogRecord` name (easy to find by searching the web, since it is
what pre-rename blog posts and Stack Overflow answers use) fails with
`ImportError: cannot import name 'LogRecord'` against this pinned version - confirmed locally.
`InMemoryLogRecordExporter.get_finished_logs()` returns a tuple of `ReadableLogRecord`, each
wrapping the actual `opentelemetry._logs.LogRecord` (API-level, not the SDK one) as
`.log_record`, plus `.resource` and `.instrumentation_scope`. [R10]

## 6. OTLP endpoint resolution order for logs specifically

`OTLPLogExporter.__init__` (grpc exporter) resolves its endpoint as: the `endpoint` constructor
argument, else `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` (log-signal-specific), both passed into the
shared `OTLPExporterMixin.__init__`, which itself falls back to `OTEL_EXPORTER_OTLP_ENDPOINT`
(the generic one `observability/tracing.py` already reads for traces) and finally hardcodes
`http://localhost:4317` if none of the above is set. [R11], [R12] `audit_export.py` passes
`environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None` as the `endpoint` argument - deliberately the
same env var `tracing.py` uses, not a new audit-specific one - so `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`
still works as an unprompted finer-grained override (inherited from the SDK's own fallback chain,
not implemented by this repo) if an operator sets it, and the SDK's own `localhost:4317` default
applies when neither is set.

## Sources
- [R1] https://api.github.com/repos/open-telemetry/opentelemetry-python/releases/tags/v1.45.1 (published_at 2026-10-06T17:33:17Z)
- [R2] https://pypi.org/pypi/opentelemetry-sdk/json ; https://pypi.org/pypi/opentelemetry-exporter-otlp/json ; https://pypi.org/pypi/opentelemetry-api/json (all report 1.45.1)
- [R3] https://github.com/open-telemetry/opentelemetry-python/tree/v1.45.1/opentelemetry-sdk/src/opentelemetry/sdk/_logs
- [R4] https://github.com/open-telemetry/opentelemetry-python/blob/v1.45.1/opentelemetry-sdk/src/opentelemetry/sdk/_logs/_internal/__init__.py (module docstring / stability notes)
- [R5] https://github.com/open-telemetry/opentelemetry-python/blob/v1.45.1/opentelemetry-sdk/src/opentelemetry/sdk/_logs/_internal/__init__.py#L983 (`LoggerProvider.add_log_record_processor`)
- [R6] https://github.com/open-telemetry/opentelemetry-python/blob/v1.45.1/opentelemetry-sdk/src/opentelemetry/sdk/_logs/_internal/__init__.py#L601-L619 (`LoggingHandler`)
- [R7] https://pypi.org/pypi/opentelemetry-instrumentation-logging/json
- [R8] https://github.com/open-telemetry/opentelemetry-python/blob/v1.45.1/opentelemetry-sdk/src/opentelemetry/sdk/_logs/_internal/export/in_memory_log_exporter.py
- [R9] https://github.com/open-telemetry/opentelemetry-python/blob/v1.45.1/opentelemetry-sdk/src/opentelemetry/sdk/_logs/_internal/__init__.py#L229 (`ReadableLogRecord`), #L273 (`ReadWriteLogRecord`)
- [R10] local introspection: `InMemoryLogRecordExporter().get_finished_logs()` against 1.45.1, 2026-10-08
- [R11] https://github.com/open-telemetry/opentelemetry-python/blob/v1.45.1/exporter/opentelemetry-exporter-otlp-proto-grpc/src/opentelemetry/exporter/otlp/proto/grpc/_log_exporter/__init__.py
- [R12] https://github.com/open-telemetry/opentelemetry-python/blob/v1.45.1/exporter/opentelemetry-exporter-otlp-proto-grpc/src/opentelemetry/exporter/otlp/proto/grpc/exporter.py#L286
