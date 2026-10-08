# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for audit export to stdout/OTLP for a SIEM (#245).

`AuditExporter`'s stdout target writes to `sys.stderr` (see its module
docstring for why - real stdout is the stdio transport's wire protocol),
so every stdout-target test here reads `capsys.readouterr().err`, not
`.out` - and uses `capsys`, not a hand-rolled `sys.stderr` monkeypatch:
pytest's own capture manager reinstalls its capture object on `sys.stderr`
at the start of every test's call phase, which would silently discard a
`StreamHandler` built against a `sys.stderr` swapped in during fixture
setup instead. The otlp tests use `opentelemetry.sdk._logs.export.
InMemoryLogRecordExporter` as the `otlp_exporter` test seam instead of a
real network endpoint.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Sequence
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from memory_manager.audit import AuditWriter
from memory_manager.config import AuditConfigError, audit_export_targets_from_env
from memory_manager.db.migrate import migrate
from memory_manager.observability.audit_export import AuditExporter

try:
    import opentelemetry.sdk  # noqa: F401
except ImportError:
    _OTEL_INSTALLED = False
else:
    _OTEL_INSTALLED = True

# --- config.audit_export_targets_from_env -----------------------------------


class TestAuditExportTargetsFromEnv:
    def test_defaults_to_off(self) -> None:
        assert audit_export_targets_from_env({}) == frozenset()

    def test_off_is_explicit_too(self) -> None:
        assert audit_export_targets_from_env({"AUDIT_EXPORT": "off"}) == frozenset()

    def test_single_target(self) -> None:
        assert audit_export_targets_from_env({"AUDIT_EXPORT": "stdout"}) == frozenset({"stdout"})

    def test_both_targets_comma_separated(self) -> None:
        assert audit_export_targets_from_env({"AUDIT_EXPORT": "stdout,otlp"}) == frozenset(
            {"stdout", "otlp"}
        )

    def test_unknown_target_raises(self) -> None:
        with pytest.raises(AuditConfigError, match="AUDIT_EXPORT"):
            audit_export_targets_from_env({"AUDIT_EXPORT": "syslog"})

    def test_one_unknown_entry_among_valid_ones_raises(self) -> None:
        with pytest.raises(AuditConfigError):
            audit_export_targets_from_env({"AUDIT_EXPORT": "stdout,syslog"})


# --- AuditExporter: stdout target --------------------------------------------


def _record_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "at": "2026-10-08T00:00:00.000+00:00",
        "actor": "alice",
        "client": "claude.ai",
        "op": "write",
        "path": "personal/fact/x.md",
        "outcome": "ok",
        "detail": {"version": 2},
        "request_id": None,
    }
    fields.update(overrides)
    return fields


class TestAuditExporterStdout:
    def test_one_json_line_per_record(self, capsys: pytest.CaptureFixture[str]) -> None:
        exporter = AuditExporter.from_env({"AUDIT_EXPORT": "stdout"})

        exporter.export(_record_fields())
        exporter.export(_record_fields(op="archive"))

        lines = capsys.readouterr().err.strip().splitlines()
        assert len(lines) == 2
        first, second = (json.loads(line) for line in lines)
        assert first["op"] == "write"
        assert second["op"] == "archive"

    def test_record_fields_are_all_present(self, capsys: pytest.CaptureFixture[str]) -> None:
        exporter = AuditExporter.from_env({"AUDIT_EXPORT": "stdout"})

        exporter.export(_record_fields(request_id="req-1"))

        line = json.loads(capsys.readouterr().err.strip())
        for key in ("at", "actor", "client", "op", "path", "outcome", "detail", "request_id"):
            assert key in line, f"{key!r} missing from exported record"
        assert line["detail"] == {"version": 2}
        assert line["request_id"] == "req-1"

    def test_no_target_configured_exports_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        exporter = AuditExporter.from_env({})

        exporter.export(_record_fields())

        assert capsys.readouterr().err == ""

    def test_never_carries_a_note_content_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`AuditWriter.record` never receives note content in the first place
        (CLAUDE.md: note content is never logged); this guards that the export
        path adds nothing content-shaped either."""
        exporter = AuditExporter.from_env({"AUDIT_EXPORT": "stdout"})

        exporter.export(_record_fields())

        line = json.loads(capsys.readouterr().err.strip())
        for forbidden in ("content", "body", "old_str", "new_str", "current_content"):
            assert forbidden not in line


# --- AuditExporter: otlp target -----------------------------------------------


class TestAuditExporterOtlp:
    # Skips every test in this class, cleanly, the moment the `otel` extra is
    # missing (`tests/test_tracing.py`'s own module-level `pytestmark`, here
    # scoped to just this class instead of the whole file) - the stdout and
    # config tests above have no such dependency and must keep running.
    pytestmark = pytest.mark.skipif(not _OTEL_INSTALLED, reason="the 'otel' extra is not installed")

    def test_exports_one_log_record_with_the_expected_attributes(self) -> None:
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

        in_memory = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call, unused-ignore]
        exporter = AuditExporter.from_env({"AUDIT_EXPORT": "otlp"}, otlp_exporter=in_memory)

        exporter.export(_record_fields(request_id="req-otlp"))
        exporter.flush()

        logs = in_memory.get_finished_logs()
        assert len(logs) == 1
        attributes = logs[0].log_record.attributes
        assert attributes is not None
        assert attributes["actor"] == "alice"
        assert attributes["client"] == "claude.ai"
        assert attributes["op"] == "write"
        assert attributes["path"] == "personal/fact/x.md"
        assert attributes["outcome"] == "ok"
        assert attributes["detail"] == {"version": 2}
        assert attributes["request_id"] == "req-otlp"

    def test_both_targets_export_independently(self, capsys: pytest.CaptureFixture[str]) -> None:
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

        in_memory = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call, unused-ignore]
        exporter = AuditExporter.from_env({"AUDIT_EXPORT": "stdout,otlp"}, otlp_exporter=in_memory)

        exporter.export(_record_fields())
        exporter.flush()

        assert len(capsys.readouterr().err.strip().splitlines()) == 1
        assert len(in_memory.get_finished_logs()) == 1

    def test_otlp_without_the_extra_refuses_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "opentelemetry.sdk._logs", None)

        with pytest.raises(AuditConfigError, match="otel"):
            AuditExporter.from_env({"AUDIT_EXPORT": "otlp"})

    def test_export_failure_is_swallowed_not_raised(self) -> None:
        from opentelemetry.sdk._logs._internal import ReadableLogRecord
        from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult

        class _BoomExporter(LogRecordExporter):  # type: ignore[misc, unused-ignore]
            def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
                raise RuntimeError("SIEM is unreachable")

            def shutdown(self) -> None:
                pass

            def force_flush(self, timeout_millis: int = 30000) -> bool:
                return True

        exporter = AuditExporter.from_env({"AUDIT_EXPORT": "otlp"}, otlp_exporter=_BoomExporter())

        # Must not raise, even though the underlying exporter always does.
        exporter.export(_record_fields())
        exporter.flush()


# --- AuditWriter: wired to a real audit_log table -----------------------------


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    migration_conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    created_pool = await asyncpg.create_pool(test_database_url)
    try:
        yield created_pool
    finally:
        await created_pool.close()


class _RecordingExporter(AuditExporter):
    """An `AuditExporter` stand-in that records every call instead of exporting."""

    def __init__(self) -> None:
        super().__init__(stdout_logger=None, otlp_logger=None)
        self.records: list[dict[str, Any]] = []

    def export(self, fields: dict[str, Any]) -> None:
        self.records.append(fields)


class _FailingExporter(AuditExporter):
    """An `AuditExporter` stand-in whose `export()` always raises - `AuditWriter.
    record` must swallow this, same as a DB failure."""

    def __init__(self) -> None:
        super().__init__(stdout_logger=None, otlp_logger=None)

    def export(self, fields: dict[str, Any]) -> None:
        raise RuntimeError("SIEM is unreachable")


class TestAuditWriterExport:
    async def test_record_is_inserted_and_exported(self, pool: asyncpg.Pool) -> None:
        exporter = _RecordingExporter()
        writer = AuditWriter(pool, exporter=exporter)

        await writer.record(
            actor="alice",
            client="claude.ai",
            op="write",
            path="personal/fact/x.md",
            commit_sha="deadbeef",
            outcome="ok",
            detail={"version": 2},
        )

        rows = await pool.fetch("select actor, client, op, path, outcome, detail from audit_log")
        assert len(rows) == 1
        row = rows[0]
        assert row["actor"] == "alice"
        assert row["outcome"] == "ok"

        assert len(exporter.records) == 1
        exported = exporter.records[0]
        assert exported["actor"] == "alice"
        assert exported["client"] == "claude.ai"
        assert exported["op"] == "write"
        assert exported["path"] == "personal/fact/x.md"
        assert exported["outcome"] == "ok"
        assert exported["detail"] == {"version": 2}
        assert "at" in exported
        assert "request_id" in exported

    async def test_exporter_failure_leaves_the_write_and_db_row_intact(
        self, pool: asyncpg.Pool
    ) -> None:
        writer = AuditWriter(pool, exporter=_FailingExporter())

        # Must not raise, and the DB row must still be there afterwards.
        await writer.record(
            actor="bob",
            client="claude.ai",
            op="archive",
            path="personal/fact/y.md",
            commit_sha="cafef00d",
            outcome="ok",
            detail=None,
        )

        rows = await pool.fetch("select actor, op from audit_log")
        assert len(rows) == 1
        assert rows[0]["actor"] == "bob"

    async def test_db_failure_still_exports(self, pool: asyncpg.Pool) -> None:
        await pool.close()  # the pool is now unusable, every execute() will fail

        exporter = _RecordingExporter()
        writer = AuditWriter(pool, exporter=exporter)

        # Must not raise despite the dead pool, and the export must still fire.
        await writer.record(
            actor="carol",
            client="claude.ai",
            op="write",
            path="personal/fact/z.md",
            commit_sha=None,
            outcome="failed",
            detail={"error": "VersionConflict"},
        )

        assert len(exporter.records) == 1
        assert exporter.records[0]["actor"] == "carol"
