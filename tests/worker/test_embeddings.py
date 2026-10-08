# SPDX-License-Identifier: AGPL-3.0-only
"""The embedding queue (#219, ADR-0007 §4): a `"postgres"`-mode write commits
its note, revision and full-text chunks and returns - `index.indexer.Indexer.
index_on_connection` enqueues an `"embed_note"` job on the write's own
connection instead of embedding inline, and `worker.build_job_handlers`'s
`"embed_note"` handler is what a `memory-manager worker` process claims that
job with (`Indexer.embed_note_job`).

Each test wires a `PostgresBackend` with its own api-side `Indexer` (the
`index_hook` only - never `index_commit_hook`, exactly like `app.py`'s
`"postgres"` branch wires one since #219) plus a second, worker-side
`Indexer` fed to `build_job_handlers` and run inside a real
`worker.consume_jobs` loop (`tests/jobs/test_queue.py`'s own `_start_worker`
shape) - proving the whole chain from a write through a claimed-and-run job,
not just `Indexer.embed_note_job` called directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import asyncpg
import pytest
import pytest_asyncio
from storage.contract import note_bytes

from memory_manager.db.migrate import migrate
from memory_manager.index.embeddings import EmbeddingError
from memory_manager.index.indexer import Indexer, VaultNotesSource
from memory_manager.search import hybrid_search
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.ulid import new_ulid
from memory_manager.worker import build_job_handlers, consume_jobs

__all__: list[str] = []

_POLL_SECONDS = 0.1
_WAIT_TIMEOUT_SECONDS = 5.0


@dataclass
class _FakeProvider:
    """A deterministic embedding provider for these tests - no network, mirrors
    `tests/index/test_indexer_postgres_source.py`'s own `_FakeProvider`.
    """

    model: str = "fake-v1"
    dimension: int = 4
    fail: bool = False
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise EmbeddingError("fake provider configured to fail")
        return [[float(len(text) + i) for i in range(self.dimension)] for text in texts]


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    migration_conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    created_pool = await asyncpg.create_pool(test_database_url, min_size=1, max_size=10)
    try:
        yield created_pool
    finally:
        await created_pool.close()


@dataclass
class _Worker:
    """One `consume_jobs` task plus the `stop` event and dedicated `LISTEN`
    connection it owns, exactly like `tests/jobs/test_queue.py`'s own `_Worker`.
    """

    task: asyncio.Task[None]
    stop: asyncio.Event
    listen_conn: asyncpg.Connection

    async def stop_and_join(self, *, timeout: float = _WAIT_TIMEOUT_SECONDS) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=timeout)
        await self.listen_conn.close()


async def _start_embedding_worker(
    test_database_url: str, pool: asyncpg.Pool, worker_indexer: Indexer
) -> _Worker:
    """Start a real `consume_jobs` loop against `worker_indexer`'s `"embed_note"`
    handler (`build_job_handlers`), on its own dedicated `LISTEN` connection.
    """
    listen_conn = await asyncpg.connect(test_database_url)
    handlers = build_job_handlers(worker_indexer)
    stop = asyncio.Event()
    task = asyncio.create_task(
        consume_jobs(
            pool,
            listen_conn,
            handlers,
            kinds=tuple(handlers),
            stop=stop,
            poll_seconds=_POLL_SECONDS,
        )
    )
    return _Worker(task=task, stop=stop, listen_conn=listen_conn)


async def _wait_until(
    condition: Callable[[], Awaitable[bool]], *, timeout: float = _WAIT_TIMEOUT_SECONDS
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if await condition():
            return
        if loop.time() >= deadline:
            pytest.fail("condition was never satisfied in time")
        await asyncio.sleep(0.05)


async def _chunks_for(pool: asyncpg.Pool, note_id: str) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "select embedding, model from chunks where note_id = $1 order by ord", note_id
        )


async def _job_row(pool: asyncpg.Pool, note_id: str) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "select state, attempts, last_error from jobs "
            "where kind = 'embed_note' and payload->>'note_id' = $1 "
            "order by created_at desc limit 1",
            note_id,
        )


async def test_write_is_found_by_search_while_its_embedding_does_not_exist_yet(
    pool: asyncpg.Pool,
) -> None:
    """A write commits full-text chunks and returns; nothing embeds them inline
    any more (#219) - `hybrid_search` (`memory_search`'s own ranking) still
    finds the note through full text, and exactly one `"embed_note"` job is
    left `pending` for a worker to pick up.
    """
    provider = _FakeProvider()
    api_indexer = Indexer(pool, VaultNotesSource(), provider)
    backend = PostgresBackend(pool, index_hook=api_indexer.index_on_connection)

    note_id = new_ulid()
    content = note_bytes(
        id=note_id, title="Giraffe Habits", body="Giraffes eat leaves high in acacia trees.\n"
    )
    await backend.write("personal/fact/giraffe.md", content, if_version="new", client="ci")

    hits = await hybrid_search(pool, "giraffe", provider=provider)
    assert {hit.path for hit in hits} == {"personal/fact/giraffe.md"}
    assert provider.calls == [["giraffe"]]  # only the query was ever embedded so far

    chunks = await _chunks_for(pool, note_id)
    assert chunks
    assert all(chunk["embedding"] is None for chunk in chunks)

    job = await _job_row(pool, note_id)
    assert job is not None
    assert job["state"] == "pending"
    assert job["attempts"] == 0


async def test_one_worker_run_embeds_every_chunk(
    test_database_url: str, pool: asyncpg.Pool
) -> None:
    """Once a worker claims and runs the job, every chunk the write produced
    has an embedding stamped with the provider's current model.
    """
    provider = _FakeProvider()
    api_indexer = Indexer(pool, VaultNotesSource(), provider)
    backend = PostgresBackend(pool, index_hook=api_indexer.index_on_connection)

    note_id = new_ulid()
    content = note_bytes(
        id=note_id,
        title="Multi Section",
        body="# First\nSome text in the first section.\n\n# Second\nMore text here.\n",
    )
    await backend.write("personal/fact/multi.md", content, if_version="new", client="ci")

    worker_indexer = Indexer(pool, VaultNotesSource(), provider)
    worker = await _start_embedding_worker(test_database_url, pool, worker_indexer)
    try:

        async def _all_embedded() -> bool:
            chunks = await _chunks_for(pool, note_id)
            return bool(chunks) and all(chunk["embedding"] is not None for chunk in chunks)

        await _wait_until(_all_embedded)
    finally:
        await worker.stop_and_join()

    chunks = await _chunks_for(pool, note_id)
    assert chunks
    assert all(chunk["model"] == provider.model for chunk in chunks)

    job = await _job_row(pool, note_id)
    assert job is not None
    assert job["state"] == "done"


async def test_edit_before_the_job_runs_embeds_only_the_new_version(
    test_database_url: str, pool: asyncpg.Pool
) -> None:
    """An edit that lands before the first write's own job is claimed leaves that
    job's note version stale: `embed_note_job` skips it without ever calling the
    provider, and only the edited version's chunks are ever embedded.
    """
    provider = _FakeProvider()
    api_indexer = Indexer(pool, VaultNotesSource(), provider)
    backend = PostgresBackend(pool, index_hook=api_indexer.index_on_connection)

    note_id = new_ulid()
    original = note_bytes(id=note_id, title="Edit Target", body="Old phrase zephyrfoo.\n")
    written = await backend.write(
        "personal/fact/edit-me.md", original, if_version="new", client="ci"
    )

    edited = note_bytes(id=note_id, title="Edit Target", body="New phrase quokkabar.\n")
    await backend.write("personal/fact/edit-me.md", edited, if_version=written.version, client="ci")

    async with pool.acquire() as conn:
        enqueued = await conn.fetch(
            "select payload->>'version' as version from jobs "
            "where kind = 'embed_note' and payload->>'note_id' = $1 order by created_at",
            note_id,
        )
    assert len(enqueued) == 2  # one job per write; the first is now stale

    worker_indexer = Indexer(pool, VaultNotesSource(), provider)
    worker = await _start_embedding_worker(test_database_url, pool, worker_indexer)
    try:

        async def _all_embedded() -> bool:
            chunks = await _chunks_for(pool, note_id)
            return bool(chunks) and all(chunk["embedding"] is not None for chunk in chunks)

        await _wait_until(_all_embedded)

        async def _both_jobs_done() -> bool:
            async with pool.acquire() as conn:
                count = await conn.fetchval(
                    "select count(*) from jobs where kind = 'embed_note' "
                    "and payload->>'note_id' = $1 and state = 'done'",
                    note_id,
                )
            return int(count) == 2

        await _wait_until(_both_jobs_done)
    finally:
        await worker.stop_and_join()

    # Exactly one embedding call ever happened - the stale job's own version
    # check short-circuited before it ever read a chunk or touched the provider.
    assert len(provider.calls) == 1
    embedded_texts = provider.calls[0]
    assert any("quokkabar" in text for text in embedded_texts)
    assert not any("zephyrfoo" in text for text in embedded_texts)


async def test_provider_outage_leaves_the_job_retrying_and_search_still_works(
    test_database_url: str, pool: asyncpg.Pool
) -> None:
    """A provider that only ever fails leaves the job retrying with backoff
    (never `failed` outright after a single attempt) and chunks `NULL` -
    `hybrid_search` still finds the note through its full-text fallback.
    """
    provider = _FakeProvider(fail=True)
    api_indexer = Indexer(pool, VaultNotesSource(), provider)
    backend = PostgresBackend(pool, index_hook=api_indexer.index_on_connection)

    note_id = new_ulid()
    content = note_bytes(
        id=note_id, title="Outage Note", body="Some searchable outageword987 text.\n"
    )
    await backend.write("personal/fact/outage.md", content, if_version="new", client="ci")

    worker_indexer = Indexer(pool, VaultNotesSource(), provider)
    worker = await _start_embedding_worker(test_database_url, pool, worker_indexer)
    try:

        async def _retried_at_least_once() -> bool:
            job = await _job_row(pool, note_id)
            return job is not None and job["attempts"] >= 1

        await _wait_until(_retried_at_least_once)
    finally:
        await worker.stop_and_join()

    job = await _job_row(pool, note_id)
    assert job is not None
    assert job["state"] == "pending"  # retried with backoff, not failed outright
    assert job["last_error"] is not None

    chunks = await _chunks_for(pool, note_id)
    assert chunks
    assert all(chunk["embedding"] is None for chunk in chunks)

    hits = await hybrid_search(pool, "outageword987", provider=provider)
    assert {hit.path for hit in hits} == {"personal/fact/outage.md"}
