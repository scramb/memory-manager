# SPDX-License-Identifier: AGPL-3.0-only
"""Plan assertions for the Postgres-backend write path's `notes`/`links` queries (#98).

`Indexer.index_on_connection` runs `_upsert_note_rows`, `_refresh_links` and
`_heal_dangling_for_note` (via `_entries_for`) on the write's own connection,
inside the write's own transaction (indexer.py). A sequential scan over
`notes` or `links` on that path turns every write's cost into O(vault size)
instead of O(1) per write, which would blow through the write p95 < 200 ms
target at 1M notes (ADR-0007, F-01).

This seeds a representative-sized `notes`/`links` table with plain SQL (no
per-row Python round trip - fast enough to run as part of `make check`) and
asserts, via `EXPLAIN (FORMAT JSON)`, that the planner picks an index for
every one of those queries on its own, after `ANALYZE` - never with
`enable_seqscan` turned off, which would hide a missing index instead of
catching it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest_asyncio

from memory_manager.db.migrate import migrate

__all__: list[str] = []

# Large enough that a sequential scan is unmistakably worse than an index
# scan in the plan's cost estimate, small enough to seed in well under a
# second via `generate_series` (no per-row round trip).
_NOTE_COUNT = 20_000

_SCANNED_TABLES = frozenset({"notes", "links"})


async def _seed(conn: asyncpg.Connection) -> None:
    """`_NOTE_COUNT` notes, each with one link; ~20% of the links dangling.

    `note-<i>`'s slug and both of its aliases are distinct, case-varied
    strings so the slug and alias branches of `_entries_for`'s query each
    have real candidates to match; the dangling links (`target_path is
    null`) give `_heal_dangling_for_note`'s query a non-trivial partial
    index to use.
    """
    await conn.execute(
        """
        insert into notes (
            id, path, namespace, type, slug, title, description, aliases,
            created, updated, file_hash
        )
        select
            'n' || lpad(i::text, 8, '0'),
            'personal/fact/note-' || i || '.md',
            'personal',
            'fact',
            'note-' || i,
            'Note ' || i,
            'a description',
            array['alias-' || i, 'Alias-Other-' || i],
            now(),
            now(),
            md5(i::text)
        from generate_series(1, $1) as i
        """,
        _NOTE_COUNT,
    )
    await conn.execute(
        """
        insert into links (source_id, target_raw, target_path)
        select
            'n' || lpad(i::text, 8, '0'),
            'note-' || ((i % $1) + 1),
            case when i % 5 = 0 then null
                 else 'personal/fact/note-' || ((i % $1) + 1) || '.md'
            end
        from generate_series(1, $1) as i
        """,
        _NOTE_COUNT,
    )
    await conn.execute("analyze notes")
    await conn.execute("analyze links")


@pytest_asyncio.fixture
async def seeded_conn(conn: asyncpg.Connection) -> asyncpg.Connection:
    """`conftest.py`'s fresh, empty `conn`, migrated and seeded.

    Function-scoped, like `conn`/`test_database_url` themselves - every test
    below only ever plans a query (`EXPLAIN` without `ANALYZE`), never
    executes a write, so reseeding per test costs nothing beyond the
    `_NOTE_COUNT`-row `insert`s themselves (plain SQL, no per-row round
    trip).
    """
    await migrate(conn)
    await _seed(conn)
    return conn


def _seq_scans(node: object) -> list[dict[str, Any]]:
    """Every `Seq Scan` node on `notes`/`links` anywhere in an `EXPLAIN` tree."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("Node Type") == "Seq Scan" and node.get("Relation Name") in _SCANNED_TABLES:
            found.append(node)
        for value in node.values():
            found.extend(_seq_scans(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_seq_scans(item))
    return found


async def _assert_indexed(conn: asyncpg.Connection, sql: str, *params: Any) -> None:
    """Plan `sql` with `params` and fail if it contains a `notes`/`links` seq scan."""
    rows = await conn.fetch(f"explain (format json) {sql}", *params)
    raw_plan = rows[0]["QUERY PLAN"]
    plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
    scans = _seq_scans(plan)
    assert not scans, f"sequential scan on notes/links for {sql!r}: {scans}"


class TestWritePathPlans:
    """One test per query the write path (`index_on_connection`) runs."""

    async def test_upsert_note_delete_by_path_uses_an_index(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # `Indexer._upsert_note_rows`: frees `rel` if another note occupies it.
        await _assert_indexed(
            seeded_conn,
            "delete from notes where path = $1 and id <> $2",
            "personal/fact/note-100.md",
            "n99999999",
        )

    async def test_upsert_note_insert_on_conflict_uses_an_index(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # `Indexer._upsert_note_rows`: the main upsert, keyed on `id`.
        now = datetime.now(UTC)
        await _assert_indexed(
            seeded_conn,
            """
            insert into notes (
                id, path, namespace, type, slug, title, description, tags, aliases,
                created, updated, valid_from, valid_to, supersedes, source, archived,
                file_hash, indexed_at
            ) values (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                now()
            )
            on conflict (id) do update set
                path = excluded.path,
                namespace = excluded.namespace,
                type = excluded.type,
                slug = excluded.slug,
                title = excluded.title,
                description = excluded.description,
                tags = excluded.tags,
                aliases = excluded.aliases,
                created = excluded.created,
                updated = excluded.updated,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                supersedes = excluded.supersedes,
                source = excluded.source,
                archived = excluded.archived,
                file_hash = excluded.file_hash,
                indexed_at = excluded.indexed_at
            """,
            "n00000001",
            "personal/fact/note-1.md",
            "personal",
            "fact",
            "note-1",
            "Note 1",
            "a description",
            [],
            ["alias-1", "Alias-Other-1"],
            now,
            now,
            None,
            None,
            [],
            None,
            False,
            "deadbeef",
        )

    async def test_refresh_links_delete_by_source_uses_an_index(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # `Indexer._refresh_links`: drops a note's own outgoing links before
        # recomputing them.
        await _assert_indexed(seeded_conn, "delete from links where source_id = $1", "n00000001")

    async def test_refresh_links_insert_uses_an_index(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # `Indexer._refresh_links`: re-inserts the links just deleted above.
        await _assert_indexed(
            seeded_conn,
            "insert into links (source_id, target_raw, target_path) values ($1, $2, $3)",
            "n00000001",
            "note-5",
            "personal/fact/note-5.md",
        )

    async def test_entries_for_uses_an_index(self, seeded_conn: asyncpg.Connection) -> None:
        # `Indexer._entries_for`: the slug and alias lookup `_refresh_links`
        # and `_heal_dangling_for_note` both call to resolve a link's target.
        await _assert_indexed(
            seeded_conn,
            """
            select path, namespace, slug, aliases
            from notes
            where lower(slug) = any($1::text[])
               or lower_array(aliases) && $1::text[]
            """,
            ["note-5", "note-10", "alias-other-20"],
        )

    async def test_heal_dangling_for_note_uses_an_index(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # `Indexer._heal_dangling_for_note`: dangling links that could now
        # resolve to the note just written, scoped to its slug/aliases and
        # their namespace-qualified forms.
        await _assert_indexed(
            seeded_conn,
            """
            select links.source_id, links.target_raw, notes.namespace as source_namespace
            from links
            join notes on notes.id = links.source_id
            where links.target_path is null
              and lower(trim(links.target_raw)) = any($1::text[])
            """,
            [
                "note-5",
                "alias-5",
                "alias-other-5",
                "personal/note-5",
                "personal/alias-5",
                "personal/alias-other-5",
            ],
        )
