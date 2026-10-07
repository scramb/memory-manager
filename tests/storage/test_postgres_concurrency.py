# SPDX-License-Identifier: AGPL-3.0-only
"""Two OS processes writing through one Postgres `PostgresBackend` (ADR-0007 §2,
WP-18, #99): the multi-replica shape the enterprise backend exists for, not
just the single-process concurrency `test_postgres_backend.py` already
covers with `asyncio.gather`. Each process gets its own connection pool and
its own `Indexer` wired the same way `app.py`'s `"postgres"` branch wires
one (ADR-0007 §4, #98) - so every write still indexes itself on the caller's
connection, exercised under real process-level contention rather than just
one event loop interleaving coroutines.

Scope (`Nicht dabei`): `write` only, no `edit`/`archive`/`supersede`/
`changes_since` under multi-process load (#106 covers the HTTP-replica
shape; those operations' own concurrency already has single-process
coverage). `"new"` writes never race each other on the same path: each
process mints its own ULID and its own unique path per such write, so that
race - already covered by `test_postgres_backend.py`'s
`test_concurrent_new_writes_on_one_path_leave_one_winner` - stays out of
scope here on purpose.

20 notes are seeded before either process starts: a shared block both
processes contend over, and a private block per process nothing else ever
touches. Each process then does `_WRITES_PER_PROCESS` writes, picking
either a known path (with a version it has itself observed - `"fresh"` its
own latest, `"stale"` a deliberately earlier one) or a brand-new path/id.
Every outcome is turned into a plain, picklable `_Record` *inside* the
child and sent back over a `multiprocessing.Queue` - never the exception
object itself (its `__reduce__` would not round-trip `VersionConflict`'s
constructor arguments) - so a result is only ever "success" or "conflict"
data, read back and checked against `vault_notes`/`vault_revisions`/`notes`
through a connection of the parent's own.

The race `postgres.py`'s `WHERE ... AND current_revision = $n` guard
(module docstring there, lines 25-33) exists for is narrow: both
processes' `SELECT` has to land before either commits its conditional
`UPDATE`/`INSERT`. Plain network-timing jitter between two real OS
processes rarely opens that window on its own, so each child installs
`_install_select_to_update_delay` on its own freshly spawned interpreter
(never the parent's or the sibling's module object) before its first
write: a short random pause inside `rules.prepare_write_or_edit`, which
runs synchronously between `postgres.py`'s read and its conditional write
and does no I/O of its own, widens that window on purpose. `if_version`
is carried on `_Record` precisely so `_assert_invariants`' (g) can tell a
genuinely guarded race from one the guard silently lost: two successes
for the same note that both claim to follow the same prior version can
only happen if that guard did not hold - the `if_version`/current-revision
match is exactly what is meant to serialize such claims, not timing.
Invariant (h) separately checks that the induced delay actually produced
a meaningful number of rejected "fresh" writes on shared notes, so this
test cannot quietly stop exercising the race it depends on.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import multiprocessing.synchronize
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
import pytest
from storage.contract import note_bytes

from memory_manager.db.migrate import migrate
from memory_manager.index.indexer import Indexer, VaultNotesSource
from memory_manager.storage import rules
from memory_manager.storage.base import Op, VersionConflict
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)

_CLIENTS = ("proc-a", "proc-b")
_SHARED_COUNT = 10
_PRIVATE_COUNT = 5
_WRITES_PER_PROCESS = 100
_JOIN_TIMEOUT = 60.0
_GET_TIMEOUT = 30.0

#: Chance a child picks a known path (shared or its own private block) over
#: minting a brand-new one; of those, the chance it deliberately goes
#: `"stale"` instead of `"fresh"` once it has at least two self-seen
#: versions to choose from.
_KNOWN_PATH_CHANCE = 0.75
_STALE_CHANCE = 0.5

#: Bounds (seconds) of the random pause `_install_select_to_update_delay` inserts
#: between a write's `SELECT` and its conditional `UPDATE`/`INSERT`, wide enough that
#: the two processes' reads of a shared row genuinely overlap before either commits.
_SELECT_TO_UPDATE_DELAY = (0.0, 0.03)

#: (h)'s floor: below this many guard-rejected "fresh" writes on shared notes, the
#: race window was not actually exercised and the invariant proves nothing.
_MIN_FRESH_CONFLICTS_ON_SHARED = 5


@dataclass(frozen=True)
class _NoteSeed:
    """One note already written before either child process starts."""

    path: str
    id: str
    version: str


@dataclass(frozen=True)
class _Record:
    """One child write's outcome, picklable on its own - never the raised exception."""

    client: str
    path: str
    note_id: str
    op_index: int
    kind: str  # "fresh" | "stale" | "new"
    outcome: str  # "success" | "conflict" | "error"
    if_version: str = ""
    version: str | None = None
    commit: str | None = None
    exc_type: str | None = None
    current_version: str | None = None
    reason: str | None = None
    start: float = 0.0
    end: float = 0.0


def _build_backend(pool: asyncpg.Pool) -> tuple[PostgresBackend, Indexer]:
    """A `PostgresBackend` wired with an `Indexer` exactly like `app.py`'s
    `"postgres"` branch wires one (ADR-0007 §4, #98), `provider=None` - the
    index path runs on every write, embeddings stay a no-op.
    """
    indexer = Indexer(pool, VaultNotesSource())
    backend = PostgresBackend(
        pool,
        index_hook=indexer.index_on_connection,
        index_commit_hook=indexer.schedule_embeddings,
    )
    return backend, indexer


def _install_select_to_update_delay(rng: random.Random) -> None:
    """Widen `postgres.py`'s `SELECT`-to-conditional-write window on *this* interpreter.

    `_write_or_edit_inner` reads `current_revision`, calls `rules.check_version`, then
    `rules.prepare_write_or_edit` - synchronous, I/O-free (rules.py docstring) - and only
    then issues the `UPDATE ... WHERE current_revision = $n`/`INSERT ... ON CONFLICT`
    that module docstring (postgres.py:25-33) says is what actually makes a lost write
    impossible under `READ COMMITTED`, not timing. Without something holding that
    window open, two real OS processes rarely get both `SELECT`s in before either
    commits, so the guard is never actually put to the test. Patching the module-level
    function here reaches `postgres.py` too - it imported the `rules` module object
    itself (`from memory_manager.storage import rules`), not the function by name - and
    is safe precisely because `multiprocessing`'s `spawn` context gives every child its
    own fresh interpreter: the parent process and the sibling child each import their
    own, unpatched copy of `memory_manager.storage.rules`.
    """
    original = rules.prepare_write_or_edit

    def _delayed(
        op: Op,
        path: str,
        content: bytes | None,
        old_str: str | None,
        new_str: str | None,
        current: bytes | None,
    ) -> bytes:
        time.sleep(rng.uniform(*_SELECT_TO_UPDATE_DELAY))
        return original(op, path, content, old_str, new_str, current)

    rules.prepare_write_or_edit = _delayed


async def _seed_notes(
    backend: PostgresBackend,
) -> tuple[list[_NoteSeed], dict[str, list[_NoteSeed]]]:
    """20 notes up front: a shared block, plus one private block per client."""
    shared: list[_NoteSeed] = []
    for i in range(_SHARED_COUNT):
        path = f"shared/fact/note-{i}.md"
        note_id = new_ulid(_CREATED)
        content = note_bytes(id=note_id, title=f"Shared note {i}", body=f"Shared seed body {i}.\n")
        result = await backend.write(path, content, if_version="new", client="seed")
        shared.append(_NoteSeed(path=path, id=note_id, version=result.version))

    private: dict[str, list[_NoteSeed]] = {}
    for client in _CLIENTS:
        notes: list[_NoteSeed] = []
        for i in range(_PRIVATE_COUNT):
            path = f"{client}/fact/private-{i}.md"
            note_id = new_ulid(_CREATED)
            content = note_bytes(
                id=note_id, title=f"{client} private {i}", body=f"{client} seed body {i}.\n"
            )
            result = await backend.write(path, content, if_version="new", client="seed")
            notes.append(_NoteSeed(path=path, id=note_id, version=result.version))
        private[client] = notes
    return shared, private


def _run_child_process(
    database_url: str,
    client: str,
    seed: int,
    shared: list[_NoteSeed],
    private: list[_NoteSeed],
    writes: int,
    result_queue: multiprocessing.Queue[_Record],
    barrier: multiprocessing.synchronize.Barrier,
) -> None:
    """`multiprocessing.Process` target (spawn): build the child's own event loop."""
    asyncio.run(
        _child_main(database_url, client, seed, shared, private, writes, result_queue, barrier)
    )


async def _child_main(
    database_url: str,
    client: str,
    seed: int,
    shared: list[_NoteSeed],
    private: list[_NoteSeed],
    writes: int,
    result_queue: multiprocessing.Queue[_Record],
    barrier: multiprocessing.synchronize.Barrier,
) -> None:
    """One process's share of the load: its own pool, its own `Indexer`, `writes` writes.

    `min_size=1, max_size=2` mirrors a lean replica, not `app.py`'s default pool -
    this process never needs more than one write in flight at a time.
    """
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    backend, indexer = _build_backend(pool)
    try:
        # Block until the sibling process is also connected and ready, so the
        # measured write windows actually overlap (invariant (f)) instead of
        # one process finishing before the other even starts.
        await asyncio.to_thread(barrier.wait)

        rng = random.Random(seed)  # noqa: S311 - deterministic test fixture, not crypto
        _install_select_to_update_delay(rng)
        versions: dict[str, list[str]] = {n.path: [n.version] for n in (*shared, *private)}
        ids: dict[str, str] = {n.path: n.id for n in (*shared, *private)}
        known_paths = [n.path for n in (*shared, *private)]

        for i in range(writes):
            kind: str
            path: str
            note_id: str
            if_version: str

            if known_paths and rng.random() < _KNOWN_PATH_CHANCE:
                path = rng.choice(known_paths)
                history = versions[path]
                if len(history) >= 2 and rng.random() < _STALE_CHANCE:
                    kind = "stale"
                    if_version = rng.choice(history[:-1])
                else:
                    kind = "fresh"
                    if_version = history[-1]
                note_id = ids[path]
                content = note_bytes(
                    id=note_id,
                    title=f"{client} write {path}",
                    body=f"Written by {client}, op {i}, kind {kind}.\n",
                )
            else:
                kind = "new"
                path = f"{client}/fact/new-{i}.md"
                note_id = new_ulid()
                if_version = "new"
                content = note_bytes(
                    id=note_id,
                    title=f"{client} new note {i}",
                    body=f"Created by {client}, op {i}.\n",
                )

            start = time.monotonic()
            record: _Record
            try:
                result = await backend.write(path, content, if_version=if_version, client=client)
            except VersionConflict as exc:
                record = _Record(
                    client=client,
                    path=path,
                    note_id=note_id,
                    op_index=i,
                    kind=kind,
                    outcome="conflict",
                    if_version=if_version,
                    exc_type=type(exc).__name__,
                    current_version=exc.current_version,
                    reason=str(exc),
                    start=start,
                    end=time.monotonic(),
                )
            except Exception as exc:  # every failure becomes a record, never a silent crash
                record = _Record(
                    client=client,
                    path=path,
                    note_id=note_id,
                    op_index=i,
                    kind=kind,
                    outcome="error",
                    if_version=if_version,
                    exc_type=type(exc).__name__,
                    reason=str(exc),
                    start=start,
                    end=time.monotonic(),
                )
            else:
                versions.setdefault(path, []).append(result.version)
                record = _Record(
                    client=client,
                    path=path,
                    note_id=note_id,
                    op_index=i,
                    kind=kind,
                    outcome="success",
                    if_version=if_version,
                    version=result.version,
                    commit=result.commit,
                    start=start,
                    end=time.monotonic(),
                )
            await asyncio.to_thread(result_queue.put, record)
    finally:
        await indexer.aclose()
        await pool.close()


async def _assert_invariants(
    conn: asyncpg.Connection,
    records: list[_Record],
    shared: list[_NoteSeed],
    private_by_client: dict[str, list[_NoteSeed]],
) -> None:
    seeded_ids = {n.id for n in shared}
    for notes in private_by_client.values():
        seeded_ids.update(n.id for n in notes)
    private_paths_by_client = {
        client: {n.path for n in notes} for client, notes in private_by_client.items()
    }

    successes = [r for r in records if r.outcome == "success"]
    conflicts = [r for r in records if r.outcome == "conflict"]

    # (a) every failure is a VersionConflict - never a WriteFailed/InvalidNote/anything else.
    for record in records:
        assert record.outcome in ("success", "conflict"), (
            f"{record.client} {record.path} op {record.op_index} failed with "
            f"{record.exc_type}: {record.reason}"
        )

    # (b) every success has exactly one matching vault_revisions row, no duplicates.
    seen_pairs: set[tuple[str, int]] = set()
    for record in successes:
        assert record.commit is not None and record.version is not None
        commit_note_id, _, revision_str = record.commit.partition("@")
        assert commit_note_id == record.note_id, (
            f"{record.client} {record.path} op {record.op_index}: commit {record.commit!r} "
            f"does not name the note it wrote ({record.note_id})"
        )
        revision = int(revision_str)
        row = await conn.fetchrow(
            "select version from vault_revisions where note_id = $1 and revision = $2",
            record.note_id,
            revision,
        )
        assert row is not None, (
            f"{record.client} {record.path} op {record.op_index}: no vault_revisions row for "
            f"{record.commit}"
        )
        assert row["version"] == record.version, (
            f"{record.client} {record.path} op {record.op_index}: vault_revisions version "
            f"differs from the reported write result"
        )
        pair = (record.note_id, revision)
        assert pair not in seen_pairs, f"(note, revision) {pair} reported by two successes"
        seen_pairs.add(pair)

    # (c) revisions are gapless 1..current_revision, successes account for exactly
    # that many (plus the one seed write for notes that existed up front), and
    # vault_notes.version matches the last revision's version.
    success_counts: dict[str, int] = {}
    for record in successes:
        success_counts[record.note_id] = success_counts.get(record.note_id, 0) + 1

    all_note_ids = {r.note_id for r in successes} | {r.note_id for r in conflicts} | seeded_ids
    for note_id in all_note_ids:
        note_row = await conn.fetchrow(
            "select current_revision, version from vault_notes where id = $1", note_id
        )
        assert note_row is not None, f"note {note_id} is missing from vault_notes"
        revision_rows = await conn.fetch(
            "select revision, version from vault_revisions where note_id = $1 order by revision",
            note_id,
        )
        revision_numbers = [r["revision"] for r in revision_rows]
        assert revision_numbers == list(range(1, len(revision_numbers) + 1)), (
            f"note {note_id} has a gap in its revisions: {revision_numbers}"
        )
        assert note_row["current_revision"] == len(revision_numbers), (
            f"note {note_id}: current_revision {note_row['current_revision']} does not match "
            f"{len(revision_numbers)} vault_revisions rows"
        )
        base = 1 if note_id in seeded_ids else 0
        expected = len(revision_numbers) - base
        assert success_counts.get(note_id, 0) == expected, (
            f"note {note_id}: {success_counts.get(note_id, 0)} successes recorded, expected "
            f"{expected} from {len(revision_numbers)} revisions (seeded={note_id in seeded_ids})"
        )
        assert note_row["version"] == revision_rows[-1]["version"], (
            f"note {note_id}: vault_notes.version does not match its last revision's version"
        )

    # (d) every reported conflict's current_version is real and belongs to that note.
    for record in conflicts:
        assert record.current_version is not None, (
            f"{record.client} {record.path} op {record.op_index}: conflict with no "
            "current_version, but the note already existed"
        )
        own_versions = {
            r["version"]
            for r in await conn.fetch(
                "select version from vault_revisions where note_id = $1", record.note_id
            )
        }
        assert record.current_version in own_versions, (
            f"{record.client} {record.path} op {record.op_index}: reported current_version "
            f"{record.current_version} never appears among {record.note_id}'s own revisions"
        )

    # (e) private notes: a fresh write always succeeds, a provably stale write always conflicts.
    for record in records:
        if record.path not in private_paths_by_client.get(record.client, set()):
            continue
        if record.kind == "fresh":
            assert record.outcome == "success", (
                f"{record.client} {record.path} op {record.op_index}: a fresh write to a "
                f"private note was rejected ({record.exc_type}: {record.reason})"
            )
        elif record.kind == "stale":
            assert record.outcome == "conflict", (
                f"{record.client} {record.path} op {record.op_index}: a provably stale write "
                f"to a private note did not conflict (outcome={record.outcome})"
            )

    # (f) the two processes' write windows actually overlapped.
    windows: dict[str, tuple[float, float]] = {}
    for client in _CLIENTS:
        client_records = [r for r in records if r.client == client]
        assert client_records, f"no records at all from {client}"
        windows[client] = (
            min(r.start for r in client_records),
            max(r.end for r in client_records),
        )
    (a_start, a_end), (b_start, b_end) = windows[_CLIENTS[0]], windows[_CLIENTS[1]]
    assert a_start < b_end and b_start < a_end, (
        f"the two processes' write windows never overlapped: "
        f"{_CLIENTS[0]}={windows[_CLIENTS[0]]}, {_CLIENTS[1]}={windows[_CLIENTS[1]]}"
    )

    # (g) no two successes on the same note both claim to follow the same prior
    # version. Exactly one writer can ever legitimately advance a note away from a
    # given `current_revision` - that is the DB-side guard's entire job
    # (postgres.py:25-33) - so a second success sharing that same `if_version` means
    # the guard let a second, stale writer through instead of raising
    # `VersionConflict`: the lost-write `postgres.py`'s own docstring says cannot
    # happen under `READ COMMITTED`. `"new"` is excluded - it names "no current row
    # yet", not one specific prior version, and that race is explicitly out of scope
    # (module docstring above).
    consumed: dict[tuple[str, str], list[_Record]] = {}
    for record in successes:
        if record.if_version == "new":
            continue
        consumed.setdefault((record.note_id, record.if_version), []).append(record)
    for (note_id, base_version), writers in consumed.items():
        assert len(writers) == 1, (
            f"note {note_id}: {len(writers)} successful writes all claimed to follow "
            f"version {base_version!r} "
            f"({[(w.client, w.op_index) for w in writers]}) - the DB-side "
            "current_revision guard let more than one through"
        )

    # (h) the induced SELECT-to-write delay actually produced real races: a
    # meaningful number of "fresh" writes to *shared* notes were rejected anyway,
    # proving the race window (invariant (g) covers whether the guard actually held
    # it) was genuinely exercised rather than this test silently degrading back into
    # one that only ever hits `rules.check_version`'s already-stale-at-read case.
    shared_paths = {n.path for n in shared}
    fresh_conflicts_on_shared = [
        r
        for r in records
        if r.outcome == "conflict" and r.kind == "fresh" and r.path in shared_paths
    ]
    assert len(fresh_conflicts_on_shared) >= _MIN_FRESH_CONFLICTS_ON_SHARED, (
        f"only {len(fresh_conflicts_on_shared)} 'fresh' writes to shared notes were "
        f"rejected (need >= {_MIN_FRESH_CONFLICTS_ON_SHARED}) - the SELECT-to-write "
        "race window was never actually exercised, so this run cannot tell a working "
        "guard apart from a missing one"
    )

    # Index check: `notes` (the derived index `index_on_connection` writes in the
    # same transaction) stays in step with `vault_notes` (the source of truth)
    # for every note either process touched - `file_hash` is `notes`' freshness
    # marker, `vault_notes.version` is the matching one (ADR-0007 §4, #98).
    notes_set = {
        (r["id"], r["path"], r["file_hash"])
        for r in await conn.fetch("select id, path, file_hash from notes")
    }
    vault_set = {
        (r["id"], r["path"], r["version"])
        for r in await conn.fetch("select id, path, version from vault_notes")
    }
    assert notes_set == vault_set, (
        f"the index is out of step with vault_notes: only in notes={notes_set - vault_set}, "
        f"only in vault_notes={vault_set - notes_set}"
    )


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_two_processes_writing_through_postgres_lose_no_write_and_report_every_conflict(
    seed: int, test_database_url: str
) -> None:
    migration_conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    seed_pool = await asyncpg.create_pool(test_database_url)
    seed_backend, seed_indexer = _build_backend(seed_pool)
    try:
        shared, private_by_client = await _seed_notes(seed_backend)
    finally:
        await seed_indexer.aclose()
        await seed_pool.close()

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(len(_CLIENTS))
    result_queue: multiprocessing.Queue[_Record] = ctx.Queue()

    root_rng = random.Random(seed)  # noqa: S311 - deterministic test fixture, not crypto
    child_seeds = {client: root_rng.getrandbits(64) for client in _CLIENTS}

    processes = [
        ctx.Process(
            target=_run_child_process,
            args=(
                test_database_url,
                client,
                child_seeds[client],
                shared,
                private_by_client[client],
                _WRITES_PER_PROCESS,
                result_queue,
                barrier,
            ),
        )
        for client in _CLIENTS
    ]
    for process in processes:
        process.start()

    total_expected = len(_CLIENTS) * _WRITES_PER_PROCESS
    records: list[_Record] = []
    try:
        while len(records) < total_expected:
            record = await asyncio.to_thread(result_queue.get, True, _GET_TIMEOUT)
            records.append(record)
    finally:
        for process in processes:
            await asyncio.to_thread(process.join, _JOIN_TIMEOUT)
        result_queue.close()

    for process in processes:
        assert process.exitcode == 0, f"{process.name} exited with code {process.exitcode}"

    conn = await asyncpg.connect(test_database_url)
    try:
        await _assert_invariants(conn, records, shared, private_by_client)
    finally:
        await conn.close()
