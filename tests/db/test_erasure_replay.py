# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `storage.erasure_replay` and the `/readyz` gate it feeds (#233,
ADR-0007 §3 addendum 2026-10-08).

Two layers, cheapest first:

- `parse_replay_file`/`replay` directly against a migrated database, seeded by
  hand with the row(s) a restore would have brought back - `storage.
  erasure_replay._LOCK_KEY` stands in for a second replica the same way
  `tests/worker/test_singleton.py`'s foreign connection does for its own
  advisory lock.
- `create_app`'s `lifespan`/`_readyz`, in-process (`httpx.ASGITransport`, the
  same pattern `tests/test_http_app.py`'s own `_running_app` uses): a
  malformed file refuses startup outright, and a real database-held advisory
  lock keeps `/readyz` at 503 until replay actually finishes.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from memory_manager.app import open_services
from memory_manager.config import ServerConfig
from memory_manager.db.migrate import migrate
from memory_manager.http import READY_PATH, create_app
from memory_manager.observability.audit_export import AuditExporter
from memory_manager.observability.logging import JsonFormatter
from memory_manager.storage.erasure import erase_note
from memory_manager.storage.erasure_replay import (
    _LOCK_KEY,
    ErasureReplayFormatError,
    ReplayRecord,
    parse_replay_file,
    replay,
)


async def _seed_namespace(
    conn: asyncpg.Connection, kind: str, external_key: str, alias: str
) -> None:
    await conn.execute(
        "insert into namespaces (kind, external_key, alias) values ($1, $2, $3)",
        kind,
        external_key,
        alias,
    )


async def _seed_user(conn: asyncpg.Connection, oid: str, display_name: str) -> None:
    await conn.execute(
        "insert into users (oid, tid, display_name) values ($1, 'tenant-1', $2)", oid, display_name
    )


async def _seed_note(
    conn: asyncpg.Connection, note_id: str, namespace: str, *, author_oid: str | None = None
) -> None:
    """Insert one note across `vault_notes`/`notes`/`vault_revisions` - the
    minimum `storage.erasure.erase_note`/`erase_namespace`/`erase_user` need to
    find and remove it; chunks/links/jobs/audit_log stay empty, their counts
    simply come back zero (not this module's own concern - `tests/storage/
    test_erasure.py` already covers every table a real write touches)."""
    now = datetime.now(UTC)
    path = f"{namespace}/fact/{note_id}.md"
    content = b"content"
    await conn.execute(
        "insert into vault_notes (id, namespace, path, content, version, current_revision) "
        "values ($1, $2, $3, $4, 'v1', 1)",
        note_id,
        namespace,
        path,
        content,
    )
    await conn.execute(
        "insert into vault_revisions "
        "(note_id, revision, path, content, version, author, client, author_oid) "
        "values ($1, 1, $2, $3, 'v1', 'tester', 'pytest', $4)",
        note_id,
        path,
        content,
        author_oid,
    )
    await conn.execute(
        "insert into notes "
        "(id, path, namespace, type, slug, title, description, created, updated, file_hash) "
        "values ($1, $2, $3, 'fact', $4, 'title', 'description', $5, $5, 'deadbeef')",
        note_id,
        path,
        namespace,
        note_id,
        now,
    )


def _exported_line(fields: dict[str, object]) -> str:
    """`fields` run through the real `JsonFormatter` on an `AuditExporter.export`-shaped
    log record - what the `stdout` `AUDIT_EXPORT` target actually writes
    (`observability/audit_export.py`'s `_safe_emit`: `logger.info("audit", extra=fields)`),
    so `parse_replay_file` is checked against the real export format, not a
    hand-rolled stand-in for it."""
    record = logging.makeLogRecord({"msg": "audit", **fields})
    return JsonFormatter().format(record)


def _erasure_fields(
    *, actor: str, target_kind: str, target_ids: list[str], erasure_log_id: int = 1
) -> dict[str, object]:
    return {
        "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "actor": actor,
        "client": "erasure",
        "op": "erasure",
        "path": None,
        "outcome": "ok",
        "detail": {
            "erasure_log_id": erasure_log_id,
            "target_kind": target_kind,
            "target_ids": target_ids,
            "row_counts": {},
        },
        "request_id": None,
    }


def _capturing_stdout_exporter() -> tuple[AuditExporter, io.StringIO]:
    """An `AuditExporter` whose `"stdout"` target writes `JsonFormatter`-formatted
    lines into an in-memory buffer instead of the real stderr stream
    (`AuditExporter.from_env`'s own `_build_stdout_logger`) - built directly via
    `AuditExporter.__init__` rather than `from_env`, which hardcodes stderr."""
    buffer = io.StringIO()
    logger = logging.getLogger(f"test.erasure_replay.audit_export.{id(buffer)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream=buffer)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return AuditExporter(stdout_logger=logger, otlp_logger=None), buffer


# --- parse_replay_file -------------------------------------------------------


class TestParseReplayFile:
    def test_parses_a_real_stdout_exported_line(self, tmp_path: Path) -> None:
        line = _exported_line(
            _erasure_fields(actor="admin", target_kind="note", target_ids=["note-1"])
        )
        path = tmp_path / "replay.jsonl"
        path.write_text(line + "\n")

        records = parse_replay_file(path)

        assert records == [ReplayRecord(actor="admin", target_kind="note", target_ids=("note-1",))]

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        line = _exported_line(
            _erasure_fields(actor="admin", target_kind="namespace", target_ids=["alice"])
        )
        path = tmp_path / "replay.jsonl"
        path.write_text(f"\n{line}\n\n")

        records = parse_replay_file(path)

        assert records == [
            ReplayRecord(actor="admin", target_kind="namespace", target_ids=("alice",))
        ]

    def test_rejects_invalid_json_naming_the_line_number(self, tmp_path: Path) -> None:
        good = _exported_line(_erasure_fields(actor="admin", target_kind="note", target_ids=["n"]))
        path = tmp_path / "replay.jsonl"
        path.write_text(f"{good}\nnot json at all\n")

        with pytest.raises(ErasureReplayFormatError, match=r":2: not valid JSON"):
            parse_replay_file(path)

    def test_rejects_a_non_erasure_op_naming_the_line_number(self, tmp_path: Path) -> None:
        line = json.dumps({"op": "write", "actor": "admin", "detail": {}})
        path = tmp_path / "replay.jsonl"
        path.write_text(f"{line}\n")

        with pytest.raises(ErasureReplayFormatError, match=r":1: expected op='erasure'"):
            parse_replay_file(path)

    def test_rejects_a_missing_target_kind(self, tmp_path: Path) -> None:
        line = json.dumps({"op": "erasure", "actor": "admin", "detail": {"target_ids": ["n"]}})
        path = tmp_path / "replay.jsonl"
        path.write_text(f"{line}\n")

        with pytest.raises(ErasureReplayFormatError, match="target_kind"):
            parse_replay_file(path)

    def test_rejects_an_empty_target_ids_list(self, tmp_path: Path) -> None:
        line = json.dumps(
            {"op": "erasure", "actor": "admin", "detail": {"target_kind": "note", "target_ids": []}}
        )
        path = tmp_path / "replay.jsonl"
        path.write_text(f"{line}\n")

        with pytest.raises(ErasureReplayFormatError, match="target_ids"):
            parse_replay_file(path)


# --- replay -------------------------------------------------------------------


class TestReplay:
    async def test_erases_a_note_that_reappeared_and_is_a_no_op_the_second_time(
        self, conn: asyncpg.Connection, test_database_url: str
    ) -> None:
        await migrate(conn, backend="postgres")
        await _seed_namespace(conn, "user", "oid-alice", "alice")
        await _seed_note(conn, "note-1", "alice", author_oid="oid-alice")

        records = [ReplayRecord(actor="admin", target_kind="note", target_ids=("note-1",))]
        pool = await asyncpg.create_pool(test_database_url)
        try:
            stats = await replay(pool, records)
            assert stats.applied == 1
            assert stats.skipped == 0
            remaining = await conn.fetchval("select count(*) from vault_notes where id = 'note-1'")
            assert remaining == 0
            log_count = await conn.fetchval("select count(*) from erasure_log")
            assert log_count == 1

            stats_again = await replay(pool, records)
            assert stats_again.applied == 0
            assert stats_again.skipped == 1
            log_count_again = await conn.fetchval("select count(*) from erasure_log")
            assert log_count_again == 1
        finally:
            await pool.close()

    async def test_erases_a_namespace_that_reappeared_and_is_a_no_op_the_second_time(
        self, conn: asyncpg.Connection, test_database_url: str
    ) -> None:
        await migrate(conn, backend="postgres")
        await _seed_namespace(conn, "user", "oid-bob", "bob")
        await _seed_note(conn, "note-bob", "bob", author_oid="oid-bob")

        records = [ReplayRecord(actor="admin", target_kind="namespace", target_ids=("bob",))]
        pool = await asyncpg.create_pool(test_database_url)
        try:
            stats = await replay(pool, records)
            assert stats.applied == 1
            remaining_ns = await conn.fetchval(
                "select count(*) from namespaces where alias = 'bob'"
            )
            assert remaining_ns == 0

            stats_again = await replay(pool, records)
            assert stats_again.applied == 0
            assert stats_again.skipped == 1
        finally:
            await pool.close()

    async def test_erases_a_user_that_reappeared_and_re_applies_pseudonymization(
        self, conn: asyncpg.Connection, test_database_url: str
    ) -> None:
        await migrate(conn, backend="postgres")
        await _seed_namespace(conn, "user", "oid-carol", "carol")
        await _seed_namespace(conn, "group", "grp-1", "payments")
        await _seed_user(conn, "oid-carol", "Carol")
        await _seed_note(conn, "note-personal", "carol", author_oid="oid-carol")
        await _seed_note(conn, "note-shared", "payments", author_oid="oid-carol")

        records = [ReplayRecord(actor="admin", target_kind="user", target_ids=("oid-carol",))]
        pool = await asyncpg.create_pool(test_database_url)
        try:
            stats = await replay(pool, records)
            assert stats.applied == 1

            personal_remaining = await conn.fetchval(
                "select count(*) from vault_notes where namespace = 'carol'"
            )
            assert personal_remaining == 0

            shared_remaining = await conn.fetchval(
                "select count(*) from vault_notes where id = 'note-shared'"
            )
            assert shared_remaining == 1
            author = await conn.fetchrow(
                "select author, author_oid from vault_revisions where note_id = 'note-shared'"
            )
            assert author is not None
            assert author["author"] == "erased"
            assert author["author_oid"] == "erased"

            user_remaining = await conn.fetchval(
                "select count(*) from users where oid = 'oid-carol'"
            )
            assert user_remaining == 0

            stats_again = await replay(pool, records)
            assert stats_again.applied == 0
            assert stats_again.skipped == 1
        finally:
            await pool.close()

    async def test_replay_is_a_no_op_for_a_target_already_gone(
        self, conn: asyncpg.Connection, test_database_url: str
    ) -> None:
        """A restore that never actually reintroduced the target (or a previous,
        interrupted replay already handled it) - `replay` must not insert a
        second, empty `erasure_log` row for it."""
        await migrate(conn, backend="postgres")

        records = [ReplayRecord(actor="admin", target_kind="note", target_ids=("no-such-note",))]
        pool = await asyncpg.create_pool(test_database_url)
        try:
            stats = await replay(pool, records)
            assert stats.applied == 0
            assert stats.skipped == 1
            log_count = await conn.fetchval("select count(*) from erasure_log")
            assert log_count == 0
        finally:
            await pool.close()

    async def test_a_second_replica_waits_for_the_lock_then_still_replays(
        self, conn: asyncpg.Connection, test_database_url: str
    ) -> None:
        await migrate(conn, backend="postgres")
        await _seed_namespace(conn, "user", "oid-dave", "dave")
        await _seed_note(conn, "note-dave", "dave", author_oid="oid-dave")

        records = [ReplayRecord(actor="admin", target_kind="note", target_ids=("note-dave",))]
        pool = await asyncpg.create_pool(test_database_url)
        foreign_conn = await asyncpg.connect(test_database_url)
        foreign_tx = foreign_conn.transaction()
        await foreign_tx.start()
        try:
            await foreign_conn.fetchval("select pg_advisory_xact_lock($1)", _LOCK_KEY)

            task = asyncio.create_task(replay(pool, records))
            await asyncio.sleep(0.2)
            assert not task.done(), "replay must wait for the foreign lock holder"

            await foreign_tx.rollback()
            await foreign_conn.close()

            stats = await asyncio.wait_for(task, timeout=5.0)
            assert stats.applied == 1
            remaining = await conn.fetchval(
                "select count(*) from vault_notes where id = 'note-dave'"
            )
            assert remaining == 0
        finally:
            if not foreign_conn.is_closed():
                await foreign_conn.close()
            await pool.close()


# --- /readyz -------------------------------------------------------------


@pytest_asyncio.fixture
async def app_role(admin_database_url: str, test_database_url: str) -> AsyncIterator[str]:
    """A disposable, non-owner, non-superuser role for the RLS request path - see
    `tests/test_http_app.py`'s identical fixture for why `DATABASE_APP_ROLE` is
    required at all for `STORAGE_BACKEND=postgres`."""
    role = f"mm_test_app_{secrets.token_hex(8)}"
    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
    finally:
        await admin_conn.close()
    try:
        yield role
    finally:
        owned_conn: asyncpg.Connection | None
        try:
            owned_conn = await asyncpg.connect(test_database_url)
        except asyncpg.PostgresError:
            owned_conn = None
        if owned_conn is not None:
            try:
                await owned_conn.execute(f'drop owned by "{role}"')
            finally:
                await owned_conn.close()
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(f'drop role if exists "{role}"')
        finally:
            await admin_conn.close()


@asynccontextmanager
async def _running_app(
    environ: dict[str, str], config: ServerConfig
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    app = create_app(lambda: open_services(environ), config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield app, client


def _environ(test_database_url: str, app_role: str) -> dict[str, str]:
    return {
        "STORAGE_BACKEND": "postgres",
        "DATABASE_URL": test_database_url,
        "DATABASE_APP_ROLE": app_role,
    }


class TestReadyz:
    async def test_is_503_while_a_foreign_connection_holds_the_replay_lock_then_erases(
        self, test_database_url: str, app_role: str, tmp_path: Path
    ) -> None:
        seed_conn = await asyncpg.connect(test_database_url)
        try:
            await migrate(seed_conn, backend="postgres")
            await _seed_namespace(seed_conn, "user", "oid-erin", "erin")
            await _seed_note(seed_conn, "note-http-1", "erin", author_oid="oid-erin")
        finally:
            await seed_conn.close()

        replay_file = tmp_path / "replay.jsonl"
        replay_file.write_text(
            _exported_line(
                _erasure_fields(actor="admin", target_kind="note", target_ids=["note-http-1"])
            )
            + "\n"
        )
        config = ServerConfig(
            public_url="https://mm.example.test", erasure_log_replay_file=replay_file
        )

        foreign_conn = await asyncpg.connect(test_database_url)
        foreign_tx = foreign_conn.transaction()
        await foreign_tx.start()
        await foreign_conn.fetchval("select pg_advisory_xact_lock($1)", _LOCK_KEY)
        try:
            async with _running_app(_environ(test_database_url, app_role), config) as (
                _app,
                client,
            ):
                response = await client.get(READY_PATH)
                assert response.status_code == 503
                assert response.json()["erasure_replay"] is False

                await foreign_tx.rollback()
                await foreign_conn.close()

                for _ in range(50):
                    response = await client.get(READY_PATH)
                    if response.status_code == 200:
                        break
                    await asyncio.sleep(0.1)

                assert response.status_code == 200
                assert response.json() == {
                    "ready": True,
                    "vault": True,
                    "database": True,
                    "draining": False,
                    "erasure_replay": True,
                }
        finally:
            if not foreign_conn.is_closed():
                await foreign_conn.close()

        check_conn = await asyncpg.connect(test_database_url)
        try:
            remaining = await check_conn.fetchval(
                "select count(*) from vault_notes where id = 'note-http-1'"
            )
            assert remaining == 0
        finally:
            await check_conn.close()

    async def test_create_app_refuses_startup_with_a_malformed_replay_file(
        self, test_database_url: str, app_role: str, tmp_path: Path
    ) -> None:
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text("not json at all\n")
        config = ServerConfig(
            public_url="https://mm.example.test", erasure_log_replay_file=bad_file
        )
        app = create_app(lambda: open_services(_environ(test_database_url, app_role)), config)

        with pytest.raises(ErasureReplayFormatError):
            async with app.router.lifespan_context(app):
                pass


# --- end-to-end: erase_note's own AUDIT_EXPORT feeds parse_replay_file/replay ----


class TestExportThenReplayEndToEnd:
    async def test_erase_note_export_round_trips_through_replay_on_a_restored_db(
        self, conn: asyncpg.Connection, test_database_url: str, tmp_path: Path
    ) -> None:
        """Closes the gap `erasure.py`'s own module docstring used to leave open
        (#233 fix for #231): `erase_note`'s `audit_export` now feeds the exact
        SIEM export `storage.erasure_replay.parse_replay_file` already expects -
        captured here (`AUDIT_EXPORT=stdout`'s own `JsonFormatter` shape, an
        in-memory stand-in for the real stderr stream) rather than hand-built,
        so a drift between what `erase_note` exports and what `parse_replay_file`
        accepts would show up as a test failure, not silently in production."""
        await migrate(conn, backend="postgres")
        await _seed_namespace(conn, "user", "oid-frank", "frank")
        await _seed_note(conn, "note-e2e", "frank", author_oid="oid-frank")

        exporter, buffer = _capturing_stdout_exporter()
        result = await erase_note(
            conn, "note-e2e", actor="admin", reason="gdpr-request", audit_export=exporter
        )
        assert result.target_kind == "note"

        audit_rows = await conn.fetchval("select count(*) from audit_log where client = 'erasure'")
        assert audit_rows == 1, "export must not insert the audit row a second time"

        exported_lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        assert len(exported_lines) == 1

        # A restore brings the note back - the backup it restored from predates
        # this erasure.
        await _seed_note(conn, "note-e2e", "frank", author_oid="oid-frank")
        reappeared = await conn.fetchval("select count(*) from vault_notes where id = 'note-e2e'")
        assert reappeared == 1

        replay_file = tmp_path / "replay.jsonl"
        replay_file.write_text(exported_lines[0] + "\n")
        records = parse_replay_file(replay_file)

        pool = await asyncpg.create_pool(test_database_url)
        try:
            stats = await replay(pool, records)
        finally:
            await pool.close()

        assert stats.applied == 1
        remaining = await conn.fetchval("select count(*) from vault_notes where id = 'note-e2e'")
        assert remaining == 0
