# Exporting the audit log to a SIEM

Every `audit_log` row (`memory_manager.audit.AuditWriter`, one per vault write - success,
conflict, rejection or failure alike) can additionally be exported as a structured event to stdout,
an OTLP logs endpoint, or both - off by default. Export never blocks, delays or undoes the
database write: a SIEM outage never costs an audit row, and a database outage never silences the
export.

## Enabling it

| `AUDIT_EXPORT` | Effect |
|---|---|
| `off` (default, or unset) | No export. `audit_log` in Postgres is the only copy. |
| `stdout` | One JSON line per record, written to the process's **stderr** stream (see "Why stderr, not stdout" below). |
| `otlp` | One OTLP log record per record, sent via `opentelemetry-exporter-otlp` to an OTLP logs collector. |
| `stdout,otlp` | Both, independently - a failure on one never skips the other. |

`otlp` requires the optional `otel` extra (`uv sync --extra otel`, or the `otel` extra of the
`memory-manager` PyPI distribution - the same extra `OTEL_EXPORTER_OTLP_ENDPOINT` already turns on
for tracing, see `observability/tracing.py`). Unlike tracing, `AUDIT_EXPORT=otlp` without the
extra installed **refuses to start** with a clear error, rather than silently exporting nothing -
SIEM export is something an operator deliberately asked for.

```sh
AUDIT_EXPORT=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability.svc:4317
```

## Record fields

Every exported record (stdout and otlp alike) carries exactly:

| Field | Meaning |
|---|---|
| `at` | ISO 8601 timestamp (millisecond precision, UTC) of the write, same instant as the `audit_log.at` column |
| `actor` | token subject, static token name, or `"stdio"` for a local session |
| `client` | the committer label (which MCP client made the write) |
| `op` | the write operation (`write`, `edit`, `supersede`, `archive`, …) |
| `path` | the note's vault path, or `null` if the write never got that far |
| `outcome` | one of `ok` / `conflict` / `rejected` / `failed` |
| `detail` | operation metadata only - a version, an error class name, a conflict path, the compatibility profile (`profile`, ADR-0010) the request ran under; never note content |
| `request_id` | the in-flight HTTP request's id (`X-Request-ID`, see `observability/logging.py`), or absent outside an HTTP request (stdio mode, the poll loop's own sync) - carried through for a queued `"git"`-backend write too, not only a `"postgres"`-backend write that audits inline |

No field ever carries a note's body, a token, or a secret - `AuditWriter.record` itself is never
given note content to export in the first place (CLAUDE.md: "note content is data, not
instructions", never logged). A SIEM ingesting this feed only ever sees metadata about writes, not
what was written.

## Why stderr, not stdout, for the `stdout` target

`AUDIT_EXPORT=stdout` writes to the process's **stderr** stream, not its real stdout file
descriptor. `serve --stdio` uses real stdout as the MCP wire protocol itself
(`observability/logging.py`'s own module docstring); a stray audit line there would corrupt that
stream for the one client reading it. A container's log collector (Docker/Podman/Kubernetes
logging driver, `journald`, etc.) scrapes stderr exactly the way it scrapes stdout, so the
SIEM-ingestion use case this target exists for is unaffected - only a literal `docker logs`/`kubectl
logs` without `--previous`-style stream separation would need to know to look at both streams,
which is already true of this server's regular logging (`LOG_FORMAT=json`, also stderr-only).

## OTLP endpoint resolution

`AUDIT_EXPORT=otlp` reads `OTEL_EXPORTER_OTLP_ENDPOINT` - the same env var
`observability/tracing.py` already reads for spans, not a second audit-specific one. If unset,
the OTLP exporter falls back to `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` (log-signal-specific, read by
the SDK itself) and finally to `http://localhost:4317` (the SDK's own default) -
`docs/research/otel-logs.md` §6 has the exact fallback chain, verified against the pinned SDK
version.

## SIEM ingestion example

Point a log shipper (Fluent Bit, Vector, `journald`'s own forwarder, …) at the container's stderr
stream and parse each line as JSON; every field above is a top-level key, alongside the usual
`ts`/`level`/`logger`/`msg` envelope `observability/logging.py`'s `JsonFormatter` already adds to
every log line this server emits. A minimal Vector config reading from a file-based log sink:

```yaml
sources:
  memory_manager_audit:
    type: file
    include: ["/var/log/containers/memory-manager-*.log"]

transforms:
  parse_audit:
    type: remap
    inputs: ["memory_manager_audit"]
    source: |
      . = parse_json!(.message)
      if !exists(.op) { abort }

sinks:
  siem:
    type: http
    inputs: ["parse_audit"]
    uri: "https://siem.example.com/ingest"
```

For `otlp`, point `OTEL_EXPORTER_OTLP_ENDPOINT` at an OpenTelemetry Collector configured with an
OTLP logs receiver and whatever exporter your SIEM's collector pipeline already uses.

## Erasure records and restores

Records with `op = "erasure"` (the enterprise `postgres` backend's erasure log, ADR-0007) are
exported like every other record, with no special casing - `detail` carries only the IDs the
`erasure_log` row itself carries. Owner decision 2026-10-08: this export is also the off-database
copy of `erasure_log` a restore replays from, so `erasure` records must be retained for at least
the backup retention period plus 7 days, regardless of which `AUDIT_EXPORT` target is used.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Startup fails with "AUDIT_EXPORT names 'otlp', but the 'otel' extra is not installed" | `AUDIT_EXPORT` includes `otlp` without `opentelemetry-sdk`/`opentelemetry-exporter-otlp` installed | `uv sync --extra otel`, or drop `otlp` from `AUDIT_EXPORT` |
| Startup fails with "AUDIT_EXPORT must be 'off' or a comma-separated list drawn from …" | a typo or unsupported value in `AUDIT_EXPORT` | use `off`, `stdout`, `otlp`, or `stdout,otlp` |
| No audit lines show up in `kubectl logs`/`docker logs` | looking only at the stdout stream | both stream separately in most tools; this server's regular logs are stderr-only too (`LOG_FORMAT=json`) |
| OTLP records never arrive at the collector | no reachable `OTEL_EXPORTER_OTLP_ENDPOINT`, or a firewall between the server and the collector | verify with `curl`/`grpcurl` against the collector's OTLP gRPC port (4317 by default) |
