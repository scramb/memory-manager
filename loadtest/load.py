# SPDX-License-Identifier: AGPL-3.0-only
"""Bulk-load a synthetic vault (`loadtest.generate`, #107) into Postgres under
RLS, with registered principals (#108, #124), and prepare k6's side inputs.

Usage::

    python -m loadtest.load --vault ./loadtest-vault \\
        --admin-url postgresql://mm:mm@localhost:55432/mm \\
        --context-out ./loadtest-vault/k6-context.json --base-url http://127.0.0.1:18080/mcp

Since WP-19/ADR-0008's addendum, a request transaction never touches content
as the owner: it switches to a non-superuser app role and the caller's own
`oid`/`roles` identity (`db.rls.request_identity`), and every content table
is under `FORCE ROW LEVEL SECURITY` (`migrations/0005_rls.sql`). This module
sets both halves up, not just the data:

- `build_rows` walks `<vault>/vault/**.md`, sorted, and builds one `_Row` per
  note exactly as a `write(if_version="new")` through `storage.postgres.
  PostgresBackend` would: `id` from the frontmatter, `namespace` from
  `vault.paths.parse_note_path`, `version` the sha256 `vault.note.version`
  computes over the file's own bytes (the generator already writes the
  canonical byte form `vault.note.serialize` produces, so this never needs
  re-serializing), `current_revision`/`revision` fixed at 1, `author`/
  `client` the marker `"loadtest"`, `message` naming the path.
  `created_at`/`updated_at`/`xid` are left to the tables' own `default
  now()`/`default pg_current_xact_id()` (migration `0004_vault.sql`) - this
  never sets them, only the columns named in `_VAULT_NOTES_COLUMNS`/
  `_VAULT_REVISIONS_COLUMNS` are part of either `COPY`.
- `ensure_app_role` creates the non-owner app role (`--app-role`, default
  `mm_loadtest_app`) idempotently and grants it to the admin connection's own
  `current_user` - the role `db.rls.check_app_role`/`grant_app_role` (run by
  `app.py` at `serve` startup) need to already exist and already be held by
  the owner; this module never grants the table/function privileges itself,
  that stays `serve`'s own startup gate.
- `populate_registry` fills `users`/`namespaces`/`user_groups`/
  `project_members` (migration `0005_rls.sql`'s membership tables) from the
  generated vault's own `namespaces.json`: every `user-*` alias becomes a
  synthetic, deterministic `oid` (`f"oid-{alias}"`, CLAUDE.md "no real
  personal data") and a `namespaces` row of kind `'user'` whose `alias` is
  already the generator's own alias - so `mm_ensure_personal_ns()` only
  ever confirms it (`migrations/0009_namespace_resolution.sql` ~56-100's
  "if v_alias is null" branch never fires for a row that already carries
  one), never invents the production `u-<id>` alias instead. Group aliases
  and `user_groups` membership follow the same way; `proj-*` aliases (ADR-
  0008) get a `namespaces` row of kind `'project'` and a `project_members`
  row per member (`_PROJECT_MEMBER_ROLE`), so both `mm_readable_ns`/
  `mm_writable_ns` and ADR-0016's `mm_namespace_kind` resolve them exactly
  like a real project namespace would; `org` gets one fixed row.
- `load` is the async part: (re)creates a fixed-name database (`--db-name`,
  default `mm_loadtest` - never a random name, unlike `tests/conftest.py`'s
  `test_database_url`, so a repeated `make loadtest-smoke` run always starts
  from a clean slate instead of accumulating databases on `mm-pg`), ensures
  the app role, migrates the fresh database, bulk-inserts every row via
  `load_vault` (`asyncpg.Pool.copy_records_to_table`), populates the
  registry, creates one static token per sampled synthetic principal
  (`namespaces.json`'s `user-*` aliases, via `create_principal_tokens`) with
  an owner principal (`owner_oid`/`roles=["Memory.User"]`) and
  `namespaces=["*"]` (ADR scope: the RLS matrix, not the token's own
  namespaces claim, is what actually narrows a static token under this
  module - per-namespace *token* claims are #109's job, not this one), and
  writes a JSON side file for k6: the server's base URL and the token list,
  each carrying its own sample of readable note paths rewritten to `me/...`
  (its own personal notes) or kept at `org/...` (never a group path: a
  static token carries no `groups` claim, so `namespaces.Resolution.
  readable()` never adds one for it either).

Nothing here starts a server or runs k6 - `scripts/loadtest-smoke.sh` does
both, after this has run.

`--with-chunks` (#267) additionally fills `chunks` for the whole vault and
builds the ADR-0016 vector index, without ever going through
`reindex --full` (too slow at 1M+ notes, #267's own Context) or a real
embedding call (costs money, adds nothing to a latency measurement):

- `load_vault(..., backend="postgres")` runs the ADR-0016 migration
  (`migrations/postgres/0012_vector_layout.sql`), which creates `chunks`
  already partitioned by `namespace_kind` - and, on that still-empty table,
  its own three multi-owner HNSW indexes.
- `build_chunk_rows` chunks every loaded note with the production chunker
  (`index.chunker.chunk_note`), exactly as `index/indexer.py`'s own
  `_upsert_note_rows` would for the same bytes, and assigns each chunk's
  `namespace_kind` straight from the generated vault's own `namespaces.json`
  (`namespace_kinds`) rather than through the registry's `mm_namespace_kind`
  - this module never depends on `populate_registry` having run first.
  `build_chunk_rows_parallel` spreads the same work (CPU-bound: chunking
  plus one `synthetic_vector` call per chunk) over `--chunk-workers`
  processes (`concurrent.futures.ProcessPoolExecutor`, stdlib only),
  yielding one shard's own chunk rows at a time, in deterministic,
  `rows`-order - `load_chunks_streaming` `COPY`s each shard the moment it
  is ready rather than waiting for the whole vault to finish chunking.
- `load_chunks_with_index` drops the migration's own, still-empty HNSW
  indexes (`drop_hnsw_indexes`), bulk-`COPY`s every chunk row - each with a
  deterministic synthetic embedding (`loadtest.vectors.synthetic_vector`,
  keyed by `"{note_id}:{ord}"`) - into its own partition table
  (`chunks_user`/`chunks_group`/`chunks_project`/`chunks_org`), then rebuilds
  the same three indexes (`build_hnsw_indexes`) under a raised
  `maintenance_work_mem` for that session only. "Index build happens after
  the bulk load, never before" (#267's own Context): building an HNSW index
  against an empty table only to throw it away and build it again once the
  data is in would be strictly slower. `m`/`ef_construction` are read
  straight out of `0012_vector_layout.sql`'s own text
  (`hnsw_indexes_from_migration`) - never a second, independently-typed copy
  of those numbers in this module.
- `analyze_chunks` runs `ANALYZE chunks` once the index build is done
  (ADR-0007 addendum #117), the same step `index/indexer.py`'s own
  `reindex(full=True)` takes for a `_VaultNotesReader` source.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import multiprocessing
import os
import random
import re
import sys
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import asyncpg

from loadtest.vector_index import format_vector
from loadtest.vectors import synthetic_vector
from memory_manager.auth.tokens import ALL_NAMESPACES, create_token
from memory_manager.db.migrate import migrate
from memory_manager.index.chunker import chunk_note
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.vault.note import parse, version
from memory_manager.vault.paths import PathRejected, parse_note_path

__all__ = [
    "analyze_chunks",
    "build_chunk_rows",
    "build_chunk_rows_parallel",
    "build_hnsw_indexes",
    "build_rows",
    "create_principal_tokens",
    "drop_hnsw_indexes",
    "ensure_app_role",
    "hnsw_indexes_from_migration",
    "load",
    "load_chunks",
    "load_chunks_streaming",
    "load_chunks_with_index",
    "load_notes",
    "load_vault",
    "main",
    "namespace_kinds",
    "populate_registry",
]

_DEFAULT_DB_NAME = "mm_loadtest"
_DEFAULT_APP_ROLE = "mm_loadtest_app"
_DEFAULT_TOKEN_COUNT = 50
_DEFAULT_PER_TOKEN_READ_PATHS = 20
_DEFAULT_BASE_URL = "http://127.0.0.1:8080/mcp"
_DEFAULT_SEED = 1

#: The one role (ADR-0008 addendum, #115) every synthetic principal's static
#: token carries - `create_principal_tokens`'s own `roles` argument to
#: `auth.tokens.create_token`. No test here needs `Memory.Curator`/`Admin`:
#: #124's own scope is "the smoke stays green", not the permission matrix's
#: write-side branches (that is `tests/mcp/test_permission_matrix.py`'s job).
_TOKEN_ROLES = ("Memory.User",)

#: Synthetic Entra tenant id for every `users` row this module inserts
#: (CLAUDE.md "no real personal data") - one fixed value, never read by any
#: RLS function, just `users.tid`'s `not null` constraint.
_SYNTHETIC_TID = "tid-loadtest"

# A fixed marker, never a per-row value: every bulk-loaded revision was
# written by this loader, not by a real client - the same way `loadtest.
# generate`'s bodies are made-up words, not real user content.
_LOADTEST_MARKER = "loadtest"

_VAULT_NOTES_COLUMNS = ("id", "namespace", "path", "content", "version", "current_revision")
_VAULT_REVISIONS_COLUMNS = (
    "note_id",
    "revision",
    "path",
    "content",
    "version",
    "author",
    "client",
    "message",
)

#: ADR-0016's `chunks.embedding` is a fixed `halfvec(1024)`
#: (`migrations/postgres/0012_vector_layout.sql`) - not a knob this loader
#: chooses on its own: every synthetic vector written below must carry
#: exactly this many components, or the `COPY` into `chunks` fails at the
#: column's own type level (halfvec's input function enforces its typmod).
_CHUNK_VECTOR_DIMENSION = 1024

#: Recorded on every synthetic chunk's own `model` column - distinguishes a
#: loadtest-attached vector (`loadtest.vectors.synthetic_vector`) from one
#: `index/indexer.py` would later write for a real embedding model.
_CHUNK_VECTOR_MODEL = "loadtest-synthetic"

#: `namespaces.json`'s own `kind` vocabulary (`loadtest.generate`, "personal"/
#: "group"/"project"/"org") mapped onto ADR-0016's `chunks.namespace_kind`
#: check constraint (`migrations/postgres/0012_vector_layout.sql`: "user"/
#: "group"/"project"/"org") - "personal" is the one entry that actually
#: differs; `populate_registry` above makes the same translation for
#: `namespaces.kind`.
_NAMESPACE_KIND_BY_GENERATOR_KIND = {
    "personal": "user",
    "group": "group",
    "project": "project",
    "org": "org",
}

#: Every ADR-0016 partition table suffix, in the order `0012_vector_layout.sql`
#: creates them.
_PARTITION_KINDS = ("user", "group", "project", "org")

#: Notes per shard for `build_chunk_rows_parallel` - chunking a note
#: (`index.chunker.chunk_note`) plus one `loadtest.vectors.synthetic_vector`
#: call per chunk is CPU-bound, not I/O-bound (the #267 follow-up's own
#: ~50k-chunk measurement: ~784 rows/s, Python-side), so it is worth
#: spreading over `ProcessPoolExecutor`'s worker processes. Small enough
#: that one slow shard never stalls the others for long, large enough that
#: inter-process pickling overhead stays a small fraction of the work each
#: shard actually does.
_CHUNK_SHARD_SIZE = 200

_VECTOR_LAYOUT_MIGRATION_PACKAGE = "memory_manager.db.migrations.postgres"
_VECTOR_LAYOUT_MIGRATION_FILE = "0012_vector_layout.sql"

#: Matches the three multi-owner HNSW index statements
#: `0012_vector_layout.sql` itself creates (`chunks_group_embedding_hnsw_idx`
#: etc.) - read from that file's own text (`hnsw_indexes_from_migration`)
#: rather than hard-coded a second time here, so `m`/`ef_construction` can
#: never silently drift between the migration and this loader's own
#: rebuild-after-bulk-load (#267: "no second copy of the numbers").
_HNSW_INDEX_DDL_RE = re.compile(
    r"create index (chunks_\w+_embedding_hnsw_idx) on (chunks_\w+)\s+"
    r"(using hnsw \([^)]*\) with \([^)]*\));"
)

#: `build_hnsw_indexes`' own default `maintenance_work_mem` - raised for that
#: session only (`set`, not `set session`/a server-wide change), gone again
#: once the connection closes. `docs/research/vector-index.md` §1's own
#: 1M-row spike used `maintenance_work_mem=4GB`; this loader's default is
#: smaller because the test-scale default run (#267's own DoD) never needs
#: that much - `--maintenance-work-mem` overrides it for a real target-size
#: run.
_DEFAULT_MAINTENANCE_WORK_MEM = "1GB"

#: `chunks.note_id` (both migration `0001_index_schema.sql` and ADR-0016's
#: `0012_vector_layout.sql`) is `references notes (id)` - `load_vault` only
#: ever fills `vault_notes`/`vault_revisions` (the `"postgres"` backend's
#: own source of truth, ADR-0007 §2), never the derived `notes` table a real
#: write's `IndexHook` would also populate. `--with-chunks` bulk-inserts
#: `notes` itself (`_note_table_records`/`load_notes`) rather than running
#: the real `Indexer` for it (too slow at this scale, the same reason
#: `reindex --full` is out of scope here, #267's own Context) - the same
#: columns `index/indexer.py`'s own `_upsert_note_rows` insert names, minus
#: `indexed_at` (left to its own `default now()`).
_NOTES_TABLE_COLUMNS = (
    "id",
    "path",
    "namespace",
    "type",
    "slug",
    "title",
    "description",
    "tags",
    "aliases",
    "created",
    "updated",
    "valid_from",
    "valid_to",
    "supersedes",
    "source",
    "archived",
    "file_hash",
)

_CHUNK_COPY_COLUMNS = (
    "note_id",
    "ord",
    "heading_path",
    "text",
    "lang",
    "namespace",
    "namespace_kind",
    "embedding",
    "model",
    "dimension",
)

_COPY_NULL = "\\N"


@dataclass(frozen=True)
class _Row:
    """One note, ready to insert into `vault_notes`/`vault_revisions`."""

    id: str
    namespace: str
    path: str
    content: bytes
    version: str


@dataclass(frozen=True)
class _ChunkRow:
    """One note's one chunk, ready for ADR-0016's `chunks` layout - everything
    `index/indexer.py`'s own `chunk_note` call would produce for the same
    note, the `namespace`/`namespace_kind` this loader (not the registry's
    `mm_namespace_kind`, #267's own scope) assigns it, and its own
    synthetic embedding, already rendered as the `'[...]'` text literal
    `halfvec`'s input function parses (`format_vector`).

    `embedding_literal` is computed once, here, rather than later in
    `_chunk_copy_lines` - `synthetic_vector`'s 1024 `random.gauss` draws
    and `format_vector`'s own per-component formatting are this loader's
    actual CPU cost (measured, not assumed: the #267 follow-up's own
    ~50k-chunk run spent ~39s/~24s of a ~64s total in exactly those two
    calls) - keeping both inside the one function `build_chunk_rows_
    parallel` spreads across worker processes is what makes that
    parallelisation actually pay off, rather than parallelising only the
    cheaper `chunk_note` call and leaving the expensive part serial in the
    main process.
    """

    note_id: str
    ord: int
    heading_path: str
    text: str
    lang: str | None
    namespace: str
    namespace_kind: str
    embedding_literal: str


@dataclass(frozen=True)
class _HnswIndex:
    """One HNSW index statement read straight out of `0012_vector_layout.sql`
    (`hnsw_indexes_from_migration`)."""

    name: str
    table: str
    options: str  # "using hnsw (...) with (...)", no trailing ";"


def _iter_note_files(vault_dir: Path) -> list[Path]:
    """Every `*.md` file under `vault_dir`, sorted for a deterministic load order."""
    return sorted(vault_dir.rglob("*.md"))


def build_rows(vault_dir: Path) -> list[_Row]:
    """Build one `_Row` per note under `vault_dir` (a generated vault's `vault/` directory).

    Raises `ValueError` if a file's path relative to `vault_dir` is not a
    valid vault-relative note path - `loadtest.generate` never produces one,
    so this only ever fires against a hand-edited or unrelated directory.
    """
    rows: list[_Row] = []
    for file_path in _iter_note_files(vault_dir):
        relative = file_path.relative_to(vault_dir).as_posix()
        try:
            note_path = parse_note_path(relative)
        except PathRejected as exc:
            raise ValueError(f"{file_path} is not a valid vault-relative note path") from exc
        data = file_path.read_bytes()
        note = parse(data)
        rows.append(
            _Row(
                id=note.id,
                namespace=note_path.namespace,
                path=relative,
                content=data,
                version=version(data),
            )
        )
    return rows


def namespace_kinds(namespaces: Mapping[str, Any]) -> dict[str, str]:
    """`namespaces.json`'s own alias -> ADR-0016 `chunks.namespace_kind` map
    (`_NAMESPACE_KIND_BY_GENERATOR_KIND`), `build_chunk_rows`'s own input."""
    return {
        alias: _NAMESPACE_KIND_BY_GENERATOR_KIND.get(info["kind"], "org")
        for alias, info in namespaces["namespaces"].items()
    }


def build_chunk_rows(rows: Sequence[_Row], kinds: Mapping[str, str]) -> list[_ChunkRow]:
    """Chunk every `rows` entry with the production chunker (`index.chunker.
    chunk_note`), exactly the way `index/indexer.py`'s own `_upsert_note_rows`
    chunks the same note bytes - so a `tests/loadtest/test_load.py`
    comparison against a real `Indexer` run never diverges on chunk text,
    heading path or ordinal (#267) - and attaches each chunk's own
    synthetic embedding (`loadtest.vectors.synthetic_vector`, keyed by
    `"{note_id}:{ord}"`, already rendered to its `halfvec` text literal via
    `format_vector`), the two calls that actually dominate this loader's
    own CPU cost (see `_ChunkRow`'s own docstring) - done here, not later,
    so `build_chunk_rows_parallel` spreading this function across worker
    processes actually parallelises the expensive part, not just chunking.

    `kinds` (`namespace_kinds`' own return value) assigns each chunk's
    `namespace_kind`; an alias with no entry falls back to `"org"`,
    mirroring `_upsert_note_rows`'s own `coalesce(mm_namespace_kind($6),
    'org')` - this loader never calls that function itself, so it keeps the
    same fallback by hand instead.
    """
    chunk_rows: list[_ChunkRow] = []
    for row in rows:
        note = parse(row.content)
        kind = kinds.get(row.namespace, "org")
        for chunk in chunk_note(
            note.title,
            note.body,
            description=note.description,
            aliases=note.aliases,
            tags=note.tags,
        ):
            vector = synthetic_vector(f"{note.id}:{chunk.ord}", _CHUNK_VECTOR_DIMENSION)
            chunk_rows.append(
                _ChunkRow(
                    note_id=note.id,
                    ord=chunk.ord,
                    heading_path=chunk.heading_path,
                    text=chunk.text,
                    lang=chunk.lang,
                    namespace=row.namespace,
                    namespace_kind=kind,
                    embedding_literal=format_vector(vector),
                )
            )
    return chunk_rows


def _chunk_rows_for_shard(shard: Sequence[_Row], kinds: Mapping[str, str]) -> list[_ChunkRow]:
    """`build_chunk_rows` for one shard - module-level (not a closure) so
    `ProcessPoolExecutor` can pickle it by its own qualified name."""
    return build_chunk_rows(shard, kinds)


def build_chunk_rows_parallel(
    rows: Sequence[_Row],
    kinds: Mapping[str, str],
    *,
    max_workers: int | None = None,
    shard_size: int = _CHUNK_SHARD_SIZE,
) -> Iterable[list[_ChunkRow]]:
    """`build_chunk_rows`, spread over up to `max_workers` processes
    (`concurrent.futures.ProcessPoolExecutor`, stdlib only - CLAUDE.md "few
    dependencies"): chunking a note plus one `loadtest.vectors.
    synthetic_vector` call per chunk is CPU-bound enough at this scale
    (#267's own ~50k-chunk measurement) to be worth more than one core, on
    the 1M-note/5M-chunk target run this loader exists for.

    `rows` is split into consecutive shards of `shard_size` notes each
    (`_CHUNK_SHARD_SIZE`'s own rationale); yields one shard's own chunk rows
    at a time, **in `rows`' own order** - `ProcessPoolExecutor.map` returns
    results in submission order regardless of which worker finishes first,
    so this is deterministic the same way the single-process
    `build_chunk_rows` already is, never a hidden source of run-to-run
    divergence. Yielding per shard (rather than returning one combined
    list) lets a caller (`load_chunks_streaming`) start `COPY`ing a shard's
    rows the moment they are ready, instead of holding every chunk row for
    the whole vault in memory before the first `COPY` starts.
    """
    shards = [rows[i : i + shard_size] for i in range(0, len(rows), shard_size)]
    if not shards:
        return
    # `"spawn"`, not the platform default (`"fork"` on Linux): forking a
    # process that already has other threads running (an event loop's own
    # executor thread, `asyncpg`'s - every caller here runs under
    # `asyncio.run`/pytest-asyncio) only copies the forking thread, not the
    # others, which `multiprocessing`'s own docs name as a deadlock risk.
    # `"spawn"` starts each worker fresh instead - slower per worker, never
    # unsafe.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=max_workers, mp_context=context) as pool:
        yield from pool.map(_chunk_rows_for_shard, shards, itertools.repeat(kinds))


def _note_table_records(rows: Sequence[_Row]) -> list[tuple[Any, ...]]:
    """`rows` as `notes` table records (`_NOTES_TABLE_COLUMNS`), the exact
    fields `index/indexer.py`'s own `_upsert_note_rows` insert names, minus
    `indexed_at` (left to its own `default now()`)."""
    records: list[tuple[Any, ...]] = []
    for row in rows:
        note = parse(row.content)
        note_path = parse_note_path(row.path, allow_archive=True)
        records.append(
            (
                note.id,
                row.path,
                note_path.namespace,
                note_path.type,
                note_path.slug,
                note.title,
                note.description,
                list(note.tags),
                list(note.aliases),
                note.created,
                note.updated,
                note.valid_from,
                note.valid_to,
                list(note.supersedes),
                note.source,
                note_path.archived,
                row.version,
            )
        )
    return records


async def load_notes(conn: asyncpg.Connection, rows: Sequence[_Row]) -> int:
    """Bulk-insert `rows` into `notes` (`_note_table_records`) - the FK
    `chunks.note_id references notes (id)` needs a matching row before any
    chunk can be `COPY`'d in, and `load_vault` never writes one (it only
    fills `vault_notes`/`vault_revisions`, ADR-0007 §2's own source of
    truth). Returns the row count copied.
    """
    status = await conn.copy_records_to_table(
        "notes", records=_note_table_records(rows), columns=list(_NOTES_TABLE_COLUMNS)
    )
    return _parse_affected(status) if status else len(rows)


def _escape_copy_text(value: str) -> str:
    """Escape `value` for Postgres `COPY ... (format text)`: backslash, tab,
    newline, carriage return. Chunk `text` routinely contains the latter
    three (`index.chunker.chunk_note` joins title/heading/content with
    `"\\n"`), unlike `loadtest.vector_index.copy_source`'s own columns,
    which never do.
    """
    return (
        value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    )


async def _chunk_copy_lines(
    rows: Sequence[_ChunkRow], *, batch_rows: int = 2000
) -> AsyncIterator[bytes]:
    """`rows` as `COPY ... (format text)` lines. `row.embedding_literal` is
    already computed (`build_chunk_rows`, possibly on a worker process via
    `build_chunk_rows_parallel`) - this only ever formats, never calls
    `synthetic_vector`/`format_vector` itself, so this step stays cheap
    regardless of how many chunk rows there are.
    """
    buf: list[str] = []
    for row in rows:
        fields: tuple[str | None, ...] = (
            row.note_id,
            str(row.ord),
            row.heading_path,
            row.text,
            row.lang,
            row.namespace,
            row.namespace_kind,
            row.embedding_literal,
            _CHUNK_VECTOR_MODEL,
            str(_CHUNK_VECTOR_DIMENSION),
        )
        buf.append(
            "\t".join(_COPY_NULL if f is None else _escape_copy_text(f) for f in fields) + "\n"
        )
        if len(buf) >= batch_rows:
            yield "".join(buf).encode("utf-8")
            buf.clear()
    if buf:
        yield "".join(buf).encode("utf-8")


def _parse_affected(status: str) -> int:
    """Parse the row count out of a `COPY`/`asyncpg` command tag like `'COPY 3'`."""
    return int(status.rsplit(" ", 1)[-1])


async def load_chunks_streaming(
    conn: asyncpg.Connection, shards: Iterable[Sequence[_ChunkRow]]
) -> dict[str, int]:
    """`COPY` each of `shards` (e.g. `build_chunk_rows_parallel`'s own yield)
    into its matching ADR-0016 partition table
    (`chunks_user`/`chunks_group`/`chunks_project`/`chunks_org`, never the
    partitioned parent `chunks` directly - this module already knows each
    row's `namespace_kind`) as soon as that shard is ready, rather than
    requiring every chunk row for the whole vault in memory before the
    first `COPY` starts. Several `COPY` statements against the same table
    (one per shard per partition) are exactly as valid as one big one -
    Postgres has no notion of "still open" across them. Returns the row
    count actually copied, per partition table, summed across every shard.
    """
    counts: dict[str, int] = {}
    for shard in shards:
        for kind in _PARTITION_KINDS:
            subset = [row for row in shard if row.namespace_kind == kind]
            if not subset:
                continue
            table = f"chunks_{kind}"
            status = await conn.copy_to_table(
                table,
                source=_chunk_copy_lines(subset),
                columns=list(_CHUNK_COPY_COLUMNS),
                format="text",
            )
            copied = _parse_affected(status) if status else len(subset)
            counts[table] = counts.get(table, 0) + copied
    return counts


async def load_chunks(conn: asyncpg.Connection, chunk_rows: Sequence[_ChunkRow]) -> dict[str, int]:
    """`load_chunks_streaming` for one, already-complete batch of
    `chunk_rows` (`build_chunk_rows`'s own output) - the non-parallel,
    non-streaming case, kept for callers (and tests) that already have
    every chunk row in hand.
    """
    return await load_chunks_streaming(conn, [chunk_rows])


def hnsw_indexes_from_migration() -> list[_HnswIndex]:
    """Every multi-owner HNSW index `0012_vector_layout.sql` itself creates,
    read from that file's own text (`_HNSW_INDEX_DDL_RE`) - never a second,
    independently-typed copy of `m`/`ef_construction` kept in this module.
    """
    sql = (
        resources.files(_VECTOR_LAYOUT_MIGRATION_PACKAGE)
        .joinpath(_VECTOR_LAYOUT_MIGRATION_FILE)
        .read_text(encoding="utf-8")
    )
    indexes = [
        _HnswIndex(name=m.group(1), table=m.group(2), options=m.group(3))
        for m in _HNSW_INDEX_DDL_RE.finditer(sql)
    ]
    if not indexes:
        raise RuntimeError(
            f"found no HNSW index statements in {_VECTOR_LAYOUT_MIGRATION_FILE} - "
            "has its own DDL shape changed?"
        )
    return indexes


async def drop_hnsw_indexes(conn: asyncpg.Connection, indexes: Sequence[_HnswIndex]) -> None:
    """Drop `indexes` (built empty by the migration) before the bulk `COPY` -
    building an HNSW index against an empty table only to throw it away and
    build it again once the data is in would be strictly slower (#267's own
    "index build happens after the bulk load, never before").
    """
    for index in indexes:
        await conn.execute(f"drop index if exists {index.name}")


async def build_hnsw_indexes(
    conn: asyncpg.Connection,
    indexes: Sequence[_HnswIndex],
    *,
    maintenance_work_mem: str | None,
    max_parallel_maintenance_workers: int | None,
) -> list[dict[str, object]]:
    """Recreate `indexes` with their own migration-sourced DDL, after raising
    `maintenance_work_mem`/`max_parallel_maintenance_workers` for this
    session only (`set`, not `set session` or a server-wide change - gone
    the moment `conn` closes). Returns one build-time/size record per index -
    the results side file's own per-index entries (#267's own checklist:
    "logs build time and index size (`pg_relation_size`) into the results
    side file").
    """
    if maintenance_work_mem:
        await conn.execute(f"set maintenance_work_mem = '{maintenance_work_mem}'")
    if max_parallel_maintenance_workers is not None:
        await conn.execute(
            f"set max_parallel_maintenance_workers = {max_parallel_maintenance_workers}"
        )
    results: list[dict[str, object]] = []
    for index in indexes:
        start = time.monotonic()
        await conn.execute(f"create index {index.name} on {index.table} {index.options}")
        elapsed = time.monotonic() - start
        index_bytes = await conn.fetchval("select pg_relation_size($1::regclass)", index.name)
        results.append(
            {
                "index": index.name,
                "table": index.table,
                "build_seconds": round(elapsed, 2),
                "index_bytes": int(index_bytes or 0),
            }
        )
    return results


async def analyze_chunks(conn: asyncpg.Connection) -> None:
    """`ANALYZE chunks` (ADR-0007 addendum #117) on the partitioned parent -
    recurses into every partition on its own, the same step `index/
    indexer.py`'s own `_analyze_chunks` takes after a `reindex(full=True)`.
    """
    await conn.execute("analyze chunks")


async def load_chunks_with_index(
    database_url: str,
    rows: Sequence[_Row],
    namespaces: Mapping[str, Any],
    *,
    maintenance_work_mem: str | None = _DEFAULT_MAINTENANCE_WORK_MEM,
    max_parallel_maintenance_workers: int | None = None,
    chunk_workers: int | None = None,
) -> dict[str, object]:
    """Chunk `rows` across up to `chunk_workers` processes
    (`build_chunk_rows_parallel`), `COPY`ing each shard into the ADR-0016
    partitions as it is ready (`load_chunks_streaming`), then build the
    HNSW index after the fact (`drop_hnsw_indexes`/`build_hnsw_indexes`) and
    `ANALYZE chunks` (#267). `chunk_workers=None` uses
    `ProcessPoolExecutor`'s own default (`os.process_cpu_count()` as of
    Python 3.13, `os.cpu_count()` before - every core on the loader's own
    host).

    `database_url` must already be `backend="postgres"`-migrated
    (`load_vault(..., backend="postgres")`) - this never migrates on its
    own. Also bulk-inserts `notes` itself (`load_notes`): `chunks.note_id`'s
    foreign key needs a matching row, and `load_vault` never writes one
    (ADR-0007 §2: `vault_notes`/`vault_revisions` are the `"postgres"`
    backend's own source of truth, `notes` is the derived index). Returns a
    JSON-serialisable summary: chunk counts per partition, load throughput,
    and one build-time/size record per HNSW index - the results side file
    `load`'s own `--with-chunks` writes.
    """
    kinds = namespace_kinds(namespaces)
    indexes = hnsw_indexes_from_migration()

    conn = await asyncpg.connect(database_url)
    try:
        await drop_hnsw_indexes(conn, indexes)

        load_start = time.monotonic()
        await load_notes(conn, rows)
        shards = build_chunk_rows_parallel(rows, kinds, max_workers=chunk_workers)
        partition_counts = await load_chunks_streaming(conn, shards)
        load_seconds = time.monotonic() - load_start

        hnsw_start = time.monotonic()
        hnsw_results = await build_hnsw_indexes(
            conn,
            indexes,
            maintenance_work_mem=maintenance_work_mem,
            max_parallel_maintenance_workers=max_parallel_maintenance_workers,
        )
        hnsw_seconds = time.monotonic() - hnsw_start

        await analyze_chunks(conn)
    finally:
        await conn.close()

    total_chunks = sum(partition_counts.values())
    return {
        "chunks": total_chunks,
        "chunks_per_partition": partition_counts,
        "load_seconds": round(load_seconds, 2),
        "rows_per_second": round(total_chunks / load_seconds, 1) if load_seconds else None,
        "hnsw_indexes": hnsw_results,
        "hnsw_index_count": len(hnsw_results),
        "hnsw_build_seconds": round(hnsw_seconds, 2),
    }


def _notes_records(rows: Sequence[_Row]) -> list[tuple[Any, ...]]:
    return [(row.id, row.namespace, row.path, row.content, row.version, 1) for row in rows]


def _revisions_records(rows: Sequence[_Row]) -> list[tuple[Any, ...]]:
    return [
        (
            row.id,
            1,
            row.path,
            row.content,
            row.version,
            _LOADTEST_MARKER,
            _LOADTEST_MARKER,
            f"write {row.path}",
        )
        for row in rows
    ]


async def load_vault(database_url: str, rows: Sequence[_Row], *, backend: str = "git") -> None:
    """Migrate `database_url` and bulk-insert `rows` into `vault_notes`/`vault_revisions`.

    One transaction for both tables: a failure on either `COPY` leaves
    neither behind, never a `vault_notes` row with no matching revision.

    `backend` (`"git"`, the default, or `"postgres"`) is passed straight
    through to `db.migrate.migrate`: `load`'s own `--with-chunks` (#267)
    needs the ADR-0016 `chunks` layout (`migrations/postgres/
    0012_vector_layout.sql`), which only `backend="postgres"` applies.
    """
    conn = await asyncpg.connect(database_url)
    try:
        await migrate(conn, backend=backend)
        async with conn.transaction():
            await conn.copy_records_to_table(
                "vault_notes", records=_notes_records(rows), columns=list(_VAULT_NOTES_COLUMNS)
            )
            await conn.copy_records_to_table(
                "vault_revisions",
                records=_revisions_records(rows),
                columns=list(_VAULT_REVISIONS_COLUMNS),
            )
    finally:
        await conn.close()


async def _recreate_database(admin_url: str, db_name: str) -> str:
    """Drop `db_name` if it exists and create it fresh; return its own connection URL."""
    admin_conn = await asyncpg.connect(admin_url)
    try:
        await admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'drop database if exists "{db_name}"')
        await admin_conn.execute(f'create database "{db_name}"')
    finally:
        await admin_conn.close()
    base, _, _ = admin_url.rpartition("/")
    return f"{base}/{db_name}"


async def ensure_app_role(admin_url: str, role: str) -> None:
    """Idempotently create `role` (`NOLOGIN NOSUPERUSER NOBYPASSRLS`) and grant
    it to the admin connection's own `current_user`.

    Roles are cluster-wide, not per-database (unlike `_recreate_database`'s
    `db_name`), so this runs against `admin_url` directly, before the loaded
    database even exists. Only ever creates the role and its membership -
    never the table/function privileges `db.rls.grant_app_role` grants,
    which is `serve`'s own startup gate (`app.py`, run once `DATABASE_APP_ROLE`
    is set): `db.rls.check_app_role`'s membership check
    (`pg_has_role(current_user, role, 'MEMBER')`) needs that membership to
    already be there before `serve` can switch to `role` for a request.

    Safe to call again: an existing role is left alone, and granting a
    membership a second time is a Postgres no-op (`GRANT ... TO` on an
    already-held membership, not an error) - the same idempotency
    `db.rls.grant_app_role` itself relies on for repeated `serve` starts.
    """
    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval("select 1 from pg_roles where rolname = $1", role)
        if not exists:
            await conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
        owner = await conn.fetchval("select current_user")
        await conn.execute(f'grant "{role}" to "{owner}"')
    finally:
        await conn.close()


def _synthetic_oid(alias: str) -> str:
    """A deterministic, synthetic `users.oid` for a generator `user-*` alias.

    Never a real Entra object id (CLAUDE.md "no real personal data") - just
    the alias itself under a fixed prefix, reproducible across runs and
    legible by eye while debugging a failed load or token.
    """
    return f"oid-{alias}"


def _synthetic_group_id(alias: str) -> str:
    """A deterministic, synthetic Entra group object id for a generator
    `group-*` alias - `_synthetic_oid`'s own idea, for `user_groups.group_id`."""
    return f"gid-{alias}"


def _synthetic_project_id(alias: str) -> str:
    """A deterministic, synthetic external id for a generator `proj-*` alias
    - `_synthetic_oid`'s own idea, for `namespaces.external_key` (ADR-0008's
    `project` kind; `project_members` below references the namespace by its
    generated `id`, not by this key)."""
    return f"pid-{alias}"


#: Every generated project member becomes able to write their project
#: namespace by default: `namespace_settings.project_write` defaults to
#: `'writers'` (`migrations/0005_rls.sql`'s own `coalesce(s.project_write,
#: 'writers')`) when this loader writes no `namespace_settings` row at all,
#: so `mm_writable_ns()`'s `project_write` CTE only grants write through
#: `pm.role in ('writer', 'owner')` - never through the `'readers'` branch.
#: `group_write`'s own default (`'members'`, any member may write) reaches
#: the same "every generated member can write" outcome without needing a
#: per-row role at all; `project_members` has no such default, so every row
#: this module inserts carries `'writer'` by hand instead.
_PROJECT_MEMBER_ROLE = "writer"


def _org_alias(namespaces: Mapping[str, Any]) -> str | None:
    """The one `kind: "org"` alias in a generated `namespaces.json`, or `None`.

    `loadtest.generate` always writes exactly one, but this module does not
    assume that - a hand-built `namespaces.json` (this file's own tests) may
    omit it.
    """
    for alias, info in namespaces["namespaces"].items():
        if info["kind"] == "org":
            return str(alias)
    return None


async def populate_registry(pool: asyncpg.Pool, namespaces: Mapping[str, Any]) -> None:
    """Insert every `namespaces.json` alias into the registry tables
    `migrations/0005_rls.sql`'s `mm_readable_ns`/`mm_writable_ns` (and
    ADR-0016's `mm_namespace_kind`) and `migrations/0009_namespace_
    resolution.sql`'s `mm_principal_namespaces` read: `users`, `namespaces`,
    `user_groups`, `project_members`. Without this, every one of those
    functions sees an empty registry and resolves every identity to zero
    namespaces - RLS hides the whole, already-loaded vault - and
    `mm_namespace_kind` resolves every alias to `null`, so a real indexing
    write into any namespace this function left out falls back to the
    `'org'` partition instead of its own.

    Every personal namespace keeps the exact alias `loadtest.generate`
    already wrote the vault under (`namespaces.alias` is set directly, by
    the insert below, not left `null` for `mm_ensure_personal_ns()` to
    invent the production `u-<id>` form for). Project namespaces (ADR-0008)
    get both a `namespaces` row and `project_members` membership rows (one
    per `namespaces.json` member, `_PROJECT_MEMBER_ROLE`) - the `project_
    members.namespace_id` foreign key needs the `namespaces` insert's own
    generated `id`, so this reads it back (`select ... where kind =
    'project'`) rather than carrying it across in Python. Runs once, against
    a freshly (re)created and still-empty database (`load`'s own
    `_recreate_database` always runs first) - no row here can conflict with
    another, so none of the inserts need an `on conflict` clause.
    """
    entries = namespaces["namespaces"]
    personal = sorted((a, i) for a, i in entries.items() if i["kind"] == "personal")
    groups = sorted((a, i) for a, i in entries.items() if i["kind"] == "group")
    projects = sorted((a, i) for a, i in entries.items() if i["kind"] == "project")
    orgs = sorted((a, i) for a, i in entries.items() if i["kind"] == "org")

    user_rows = [
        (_synthetic_oid(alias), _SYNTHETIC_TID, f"loadtest user {alias}") for alias, _ in personal
    ]
    namespace_rows = (
        [("user", _synthetic_oid(alias), alias) for alias, _ in personal]
        + [("group", _synthetic_group_id(alias), alias) for alias, _ in groups]
        + [("project", _synthetic_project_id(alias), alias) for alias, _ in projects]
        + [("org", alias, alias) for alias, _ in orgs]
    )
    membership_rows = [
        (_synthetic_oid(member), _synthetic_group_id(alias))
        for alias, info in groups
        for member in info["members"]
    ]

    async with pool.acquire() as conn, conn.transaction():
        if user_rows:
            await conn.copy_records_to_table(
                "users", records=user_rows, columns=["oid", "tid", "display_name"]
            )
        if namespace_rows:
            await conn.copy_records_to_table(
                "namespaces", records=namespace_rows, columns=["kind", "external_key", "alias"]
            )
        if membership_rows:
            await conn.copy_records_to_table(
                "user_groups", records=membership_rows, columns=["oid", "group_id"]
            )
        if projects:
            project_rows = await conn.fetch(
                "select id, alias from namespaces where kind = 'project'"
            )
            namespace_id_by_alias = {row["alias"]: row["id"] for row in project_rows}
            project_member_rows = [
                (namespace_id_by_alias[alias], "user", _synthetic_oid(member), _PROJECT_MEMBER_ROLE)
                for alias, info in projects
                for member in info["members"]
            ]
            if project_member_rows:
                await conn.copy_records_to_table(
                    "project_members",
                    records=project_member_rows,
                    columns=["namespace_id", "principal_kind", "principal_id", "role"],
                )


def _personal_aliases(namespaces: Mapping[str, Any]) -> list[str]:
    """Every `user-*` alias in a generated `namespaces.json` document, sorted."""
    return sorted(
        alias
        for alias, info in namespaces["namespaces"].items()
        if alias.startswith("user-") and info["kind"] == "personal"
    )


def _membership_namespaces(alias: str, namespaces: Mapping[str, Any]) -> list[str]:
    """Every namespace alias `alias` is a member of, sorted (its own personal
    namespace, any sampled group, and `org`)."""
    return sorted(
        ns_alias for ns_alias, info in namespaces["namespaces"].items() if alias in info["members"]
    )


def _sample(items: Sequence[str], count: int, rng: random.Random) -> list[str]:
    if count >= len(items):
        return list(items)
    return rng.sample(list(items), count)


def _rewrite_to_me(path: str) -> str:
    """`path` (a generator-written vault path) with its namespace segment
    replaced by `me` - the pseudo-alias every client-facing path uses for
    its own personal namespace (`mcp.namespaces.ME_ALIAS`)."""
    _namespace, _, rest = path.partition("/")
    return f"me/{rest}"


def _per_token_read_paths(
    alias: str,
    rows: Sequence[_Row],
    org_alias: str | None,
    count: int,
    rng: random.Random,
) -> list[str]:
    """`count` read paths `alias`'s own static token may always read: its own
    personal notes (rewritten to `me/...`) plus the org's notes (kept at
    their real `org/...` path, never rewritten - `org` is not the caller's
    own personal namespace).

    Never a group path: a static token carries no `groups` claim
    (`auth.verifier`'s own module docstring), so `namespaces.Resolution.
    readable()` never adds one for it either - sampling one here would only
    ever produce a `memory_read` call this module's own canary
    (`loadtest/k6/read.js`) is built to catch as a silent `NotFound`.
    """
    own = [_rewrite_to_me(row.path) for row in rows if row.namespace == alias]
    org = [row.path for row in rows if org_alias is not None and row.namespace == org_alias]
    return sorted(_sample(own + org, count, rng))


async def create_principal_tokens(
    pool: asyncpg.Pool,
    aliases: Sequence[str],
    namespaces: Mapping[str, Any],
    rows: Sequence[_Row],
    *,
    rng: random.Random,
    read_path_count: int = _DEFAULT_PER_TOKEN_READ_PATHS,
) -> list[dict[str, Any]]:
    """One static, read+write, every-namespace token per `aliases` entry, each
    with an owner principal (`owner_oid`/`roles=["Memory.User"]`, ADR-0008
    addendum #115) and its own sample of readable paths.

    `namespaces=["*"]` (`tokens.ALL_NAMESPACES`) still means "no restriction
    from the token's own claim" - what actually narrows a static token under
    RLS is the owner principal's matrix (`namespaces.Resolution.readable()`),
    computed fresh per request from `populate_registry`'s rows, not a
    per-namespace token claim (#109's scope, not this one's).

    Each entry's `namespaces` records the alias's actual membership from
    `namespaces` (`namespaces.json`, including any group) for k6's own
    bookkeeping; `read_paths` is `_per_token_read_paths`'s own sample -
    `me/...`/`org/...` only, matching what the owner principal can actually
    read end to end through the MCP tools, not just through raw SQL.
    """
    org_alias = _org_alias(namespaces)
    tokens: list[dict[str, Any]] = []
    for alias in aliases:
        plaintext, _info = await create_token(
            pool,
            f"loadtest-{alias}",
            scopes=[READ_SCOPE, WRITE_SCOPE],
            namespaces=[ALL_NAMESPACES],
            owner_oid=_synthetic_oid(alias),
            roles=list(_TOKEN_ROLES),
        )
        tokens.append(
            {
                "alias": alias,
                "token": plaintext,
                "namespaces": _membership_namespaces(alias, namespaces),
                "read_paths": _per_token_read_paths(alias, rows, org_alias, read_path_count, rng),
            }
        )
    return tokens


async def load(
    *,
    vault_out: Path,
    admin_url: str,
    db_name: str,
    app_role: str,
    token_count: int,
    base_url: str,
    context_out: Path,
    seed: int,
    with_chunks: bool = False,
    results_out: Path | None = None,
    maintenance_work_mem: str | None = _DEFAULT_MAINTENANCE_WORK_MEM,
    max_parallel_maintenance_workers: int | None = None,
    chunk_workers: int | None = None,
) -> None:
    """Load `vault_out` (a `loadtest.generate` output directory) into `db_name`
    under RLS, with a registered principal per sampled token, and write
    `context_out`, the JSON side file `loadtest/k6/lib.js` reads.

    `with_chunks` (#267) additionally fills `chunks` and builds the
    ADR-0016 vector index (`load_chunks_with_index`) - see this module's own
    docstring. `results_out`, if given, gets that call's own JSON summary
    (chunk counts, load throughput, HNSW build time/size); ignored when
    `with_chunks` is `False`.
    """
    vault_dir = vault_out / "vault"
    rows = build_rows(vault_dir)
    if not rows:
        raise ValueError(f"no notes found under {vault_dir}")

    namespaces = json.loads((vault_out / "namespaces.json").read_text(encoding="utf-8"))

    await ensure_app_role(admin_url, app_role)
    database_url = await _recreate_database(admin_url, db_name)
    await load_vault(database_url, rows, backend="postgres" if with_chunks else "git")

    rng = random.Random(seed)  # noqa: S311 - deterministic sampling, not a secret
    sampled_aliases = _sample(_personal_aliases(namespaces), token_count, rng)

    pool = await asyncpg.create_pool(database_url)
    try:
        await populate_registry(pool, namespaces)
        tokens = await create_principal_tokens(pool, sampled_aliases, namespaces, rows, rng=rng)
    finally:
        await pool.close()

    context = {"base_url": base_url, "tokens": tokens}
    context_out.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"loaded {len(rows)} notes into {db_name!r}; wrote {len(tokens)} tokens to {context_out}")

    if with_chunks:
        chunk_results = await load_chunks_with_index(
            database_url,
            rows,
            namespaces,
            maintenance_work_mem=maintenance_work_mem,
            max_parallel_maintenance_workers=max_parallel_maintenance_workers,
            chunk_workers=chunk_workers,
        )
        if results_out is not None:
            results_out.write_text(
                json.dumps(chunk_results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(
            f"loaded {chunk_results['chunks']} chunks "
            f"({chunk_results['rows_per_second']} rows/s); "
            f"built {chunk_results['hnsw_index_count']} HNSW indexes "
            f"in {chunk_results['hnsw_build_seconds']}s"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadtest.load",
        description="bulk-load a synthetic vault into Postgres and prepare k6's side inputs (#108)",
    )
    parser.add_argument(
        "--vault", type=Path, required=True, help="a loadtest.generate output directory"
    )
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("MM_TEST_DATABASE_URL"),
        help="admin Postgres URL used to (re)create --db-name (defaults to $MM_TEST_DATABASE_URL)",
    )
    parser.add_argument("--db-name", default=_DEFAULT_DB_NAME, help="database to drop and recreate")
    parser.add_argument(
        "--app-role",
        default=_DEFAULT_APP_ROLE,
        help="non-owner role request transactions switch to under RLS (ensure_app_role)",
    )
    parser.add_argument(
        "--tokens",
        type=int,
        default=_DEFAULT_TOKEN_COUNT,
        help="number of synthetic principals to create a static token for",
    )
    parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help="the MCP endpoint k6 should call, recorded in --context-out",
    )
    parser.add_argument(
        "--context-out", type=Path, required=True, help="where to write the k6 side file (JSON)"
    )
    parser.add_argument(
        "--seed", type=int, default=_DEFAULT_SEED, help="seed for token/read-path sampling"
    )
    parser.add_argument(
        "--with-chunks",
        action="store_true",
        help="also fill chunks and build the ADR-0016 HNSW index (#267)",
    )
    parser.add_argument(
        "--results-out",
        type=Path,
        default=None,
        help="where to write --with-chunks' own JSON summary (chunk counts, "
        "load throughput, HNSW build time/size); ignored without --with-chunks",
    )
    parser.add_argument(
        "--maintenance-work-mem",
        default=_DEFAULT_MAINTENANCE_WORK_MEM,
        help="raised for the HNSW index build session only (--with-chunks)",
    )
    parser.add_argument(
        "--max-parallel-maintenance-workers",
        type=int,
        default=None,
        help="raised for the HNSW index build session only (--with-chunks); "
        "unset leaves the server's own default",
    )
    parser.add_argument(
        "--chunk-workers",
        type=int,
        default=None,
        help="process-pool size for chunking+synthetic-vector generation (--with-chunks); "
        "unset uses every core on this host",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` (`sys.argv[1:]` if omitted) and run `load`."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.admin_url:
        print(
            "loadtest.load: --admin-url is required (or set MM_TEST_DATABASE_URL)",
            file=sys.stderr,
        )
        return 2
    asyncio.run(
        load(
            vault_out=args.vault,
            admin_url=args.admin_url,
            db_name=args.db_name,
            app_role=args.app_role,
            token_count=args.tokens,
            base_url=args.base_url,
            context_out=args.context_out,
            seed=args.seed,
            with_chunks=args.with_chunks,
            results_out=args.results_out,
            maintenance_work_mem=args.maintenance_work_mem,
            max_parallel_maintenance_workers=args.max_parallel_maintenance_workers,
            chunk_workers=args.chunk_workers,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
