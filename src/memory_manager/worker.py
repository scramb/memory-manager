# SPDX-License-Identifier: AGPL-3.0-only
"""`memory-manager worker` - a separate process that runs periodic singleton
jobs exactly once across however many replicas of it run (#217, ADR-0009 §4:
"worker: embedding queue, Graph delta sync, retention, OAuth cleanup, with
its own HPA").

Builds on the one pattern already proven for this in `http.py` (#106):
`_cleanup_loop`/`_run_cleanup_iteration` sleep on a fixed interval, then try
a non-blocking Postgres advisory lock (`pg_try_advisory_xact_lock`) before
doing anything - whichever replica wins the race runs the job this round,
every other replica sees the lock held and skips it. This module turns that
one-off shape into a small, reusable registry (`Job`, `_job_loop`) instead of
a single hardcoded loop, and derives each job's own lock key from its name
(`_lock_key`) rather than one fixed constant per job - a second job
registered here later never needs a lock key of its own picked by hand.

Only one job is registered today (`_cleanup_job`, `build_jobs`): the OAuth
authorization server's stale state (`auth.store.cleanup`) and
`PostgresSharedState`'s own expired `rate_limits` rows
(`PostgresSharedState.sweep_expired_windows`) - exactly what `http.py`'s own
`_run_cleanup_iteration` already swept, moved here because this worker, not
every `api` replica, is now the one process that runs it for the `"postgres"`
backend (`http.py`'s own loop keeps running for the `"git"` backend
unchanged, ADR-0009 §6 - see that module's `lifespan` for the gating). Run
unconditionally (not gated on whether an OAuth authorization server or a
`PostgresSharedState` backend is actually configured, unlike `http.py`'s
narrower optimization): both halves are idempotent no-ops against an empty
table, and a worker process has no cheap way to know an `api` replica's own
`LOGIN_MODE`/`VALKEY_URL` without duplicating `http.py`'s own config
resolution for no benefit beyond skipping an occasional empty `DELETE`.

The `jobs` outbox consumer (#218, `consume_jobs` below) is a second,
unrelated loop shape this module runs, for a different problem: several
worker replicas claiming disjoint rows from `jobs` concurrently, rather than
one replica winning an exclusive lock. `build_job_handlers` registers one
job kind against it today, `"embed_note"` (#219): a Postgres-mode write
(`index.indexer.Indexer.index_on_connection`) commits its note, revision and
full-text chunks and enqueues this job rather than embedding inline, so
`memory_search` already finds a fresh note through full text before its
embedding exists; `_embed_note_job` below is the handler, and
`enqueue_pending_embeddings` is this process's startup catch-up for any
chunk a lost job, a provider outage or a model change left stale.

`build_jobs` also registers `retention` (#240, ADR-0008 "Deprovisioned
users") as a second periodic singleton job, next to `_cleanup_job` -
unconditionally, every `WorkerConfig.personal_retention_days`/
`retention_sweep_seconds` (defaults 30 days / once a day): `_retention_job`
below erases the personal namespace and identity of every user still
disabled at or past that horizon, through `storage.postgres.PostgresBackend.
erase` (`storage/erasure.py`'s own `erase_user`) with actor
`"system:retention"` - the same erasure primitive #231's admin-triggered
path already uses, so `erasure_log` and the SIEM export happen identically.

`build_jobs` also registers `entra_delta_sync` (#223, ADR-0006 §6) as a
third periodic singleton job, next to `_cleanup_job`/`retention` - but only when
`graph_client` is given (`cli.py`'s `_serve_worker` passes `auth.graph.
GraphClient.from_env(os.environ)`, `None` for a deployment that never
configured Entra at all): one Graph `users/delta` round per
`WorkerConfig.entra_delta_sync_seconds` (default 300s), applied to `users`
through `auth.users.disable_user`/`enable_user` - `_entra_delta_sync_job`
below is the job body, `entra_delta_cursor` (migration 0014) the single-row
cursor it reads and advances. Like `_cleanup_job`, a failed run is simply
retried next interval (`_job_loop`'s own guarantee) - this job additionally
leaves `entra_delta_cursor.delta_link` exactly where it was on any failure,
so a retry re-fetches the identical round rather than skipping ahead
(`auth.graph.GraphClient.users_delta`'s own docstring: "a partial round is
never half-applied").

This process always connects to Postgres as the owner, never switching to
`DATABASE_APP_ROLE` (ADR-0008 addendum: "the worker is a system identity,
like `reindex`/`import`" - the same connection shape `cli.py`'s
`_open_migrated_pool` already gives `reindex`/`token create|list|revoke`),
since every job here touches operational tables (`oauth_pending`,
`oauth_auth_codes`, `oauth_tokens`, `oauth_clients`, `rate_limits`), never a
`FORCE ROW LEVEL SECURITY` content table that would need a request principal
to switch to instead.

Refuses to run at all against `STORAGE_BACKEND=git` (`cli.py`'s `worker`
subcommand) - that backend is still the single, un-replicated `api` process
`http.py`'s own loop already covers (ADR-0009 §6); a worker process would
have no job to run for it today and no principled way to run one safely
tomorrow (there is no shared Postgres to take an advisory lock against in
the first place unless `DATABASE_URL` happens to be set for the derived
index, which `"git"` does not require).

Exposes the same three paths `http.py` does (`/healthz`, `/readyz`,
`/metrics`) on its own port (`WorkerConfig.port`, default 8090) through
`create_worker_app`, served with `http.py`'s own `GracefulShutdownServer` -
the identical `SIGTERM` -> `app.state.draining = True` -> `/readyz` 503
mechanism, reused rather than reimplemented. `/readyz` additionally reports
whether every job's scheduling loop is still alive (`jobs_running`) - a
crashed loop (every iteration already guards its own job against raising,
so only a bug in the loop itself, not a failing job, could cause this) means
this worker is no longer doing anything useful, the same "not ready" signal
a database outage gives.

A third, unrelated loop `create_worker_app` always starts alongside the
above (`_metrics_refresh_loop`, #261): `mm_jobs_pending`/`mm_jobs_oldest_
pending_age_seconds` (`observability.metrics.set_jobs_pending`, one call per
`job_handlers` kind every round) and, only while an embedding provider is
actually configured (`_embedding_provider_configured`, read directly from
the environment the same way `observability.metrics.metrics_enabled`
already does - cheaper than threading a new parameter through `cli.py`'s
`_serve_worker` just for this), `mm_embedding_lag_seconds`
(`observability.metrics.set_embedding_lag_seconds`) - both from the single
`jobs.pending_stats` call `_refresh_metrics` makes each round, bounded by
`jobs_claimable_idx`'s own partial index rather than by the size of `jobs`
or `chunks`: the embedding lag is the age of the oldest still-pending
`"embed_note"` job, the same backlog `mm_jobs_pending{kind="embed_note"}`
already reports, not a separate `notes`/`chunks` scan. Unlike the
singleton `Job`s above, this loop takes no advisory lock: every worker
replica's own `/metrics` must reflect the current state on its own, not
whichever replica happened to win a race this round. Deliberately left out
of `app.state.tasks`/`/readyz`'s own `jobs_running` check - a worker with no
registered `Job` and no `jobs` outbox handler is still "not ready" by that
check's own existing contract (`tests/worker/test_singleton.py`), which this
always-on loop must not change.

Graceful shutdown (ADR-0009 §1/§5, mirrored from `http.py`'s own): on
shutdown, `create_worker_app`'s `lifespan` stops every job loop from
scheduling another run and waits up to `WorkerConfig.shutdown_grace_seconds`
for whatever job is already running to finish on its own - only past that
deadline are the loops cancelled outright, so this process never hangs
forever on a job that never finishes. The process as a whole still ends
with `-SIGTERM`, not a plain `0`: `http.GracefulShutdownServer` is uvicorn's
own signal-handling machinery underneath, the same reused mechanism that
already makes the `api` process exit the identical way on `SIGTERM`
(`tests/test_shutdown.py`'s own `-signal.SIGTERM` assertion for it) - this
worker's graceful, bounded drain happens first, inside that same exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from memory_manager import __commit__, __version__, jobs
from memory_manager.auth import store
from memory_manager.auth.graph import GraphClient, GraphDeltaExpired
from memory_manager.auth.shared_state import PostgresSharedState
from memory_manager.auth.users import disable_user, enable_user, get_user
from memory_manager.config import (
    EmbeddingConfig,
    EmbeddingConfigError,
    ServerConfig,
    WorkerConfig,
    rate_limit_sweep_floor_seconds,
)
from memory_manager.index.indexer import Indexer
from memory_manager.observability.metrics import (
    metrics_endpoint,
    set_embedding_lag_seconds,
    set_jobs_pending,
)
from memory_manager.observability.tracing import job_span
from memory_manager.storage.postgres import PostgresBackend

__all__ = [
    "Job",
    "JobHandler",
    "build_job_handlers",
    "build_jobs",
    "consume_jobs",
    "create_worker_app",
    "enqueue_pending_embeddings",
]

_logger = logging.getLogger(__name__)

HEALTH_PATH = "/healthz"
READY_PATH = "/readyz"
METRICS_PATH = "/metrics"

_SOURCE_URL = "https://github.com/scramb/memory-manager"

#: Same interval `http.py`'s own `_CLEANUP_INTERVAL_SECONDS` used for this
#: sweep (#106) - not configurable, for the identical reason given there: the
#: advisory lock makes it a singleton regardless of how many worker replicas
#: run it, so a shorter interval would only cost extra skipped lock attempts,
#: never duplicate work.
_CLEANUP_INTERVAL_SECONDS = 60 * 60

#: `_metrics_refresh_loop`'s own default interval (#261's "configurable,
#: default 15 s") - a plain module constant, like `_CLEANUP_INTERVAL_SECONDS`
#: above, rather than a new `WORKER_*` environment variable: `create_worker_
#: app`'s own `metrics_refresh_seconds` parameter is the configuration seam
#: (`cli.py`'s `_serve_worker` could thread a variable into it later without
#: any further change here), this is only ever this module's own fallback.
_METRICS_REFRESH_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True)
class Job:
    """One periodic singleton job: `run` is awaited at most every `interval_seconds`,
    and at most by one worker replica at a time (`_lock_key(name)`'s advisory lock).
    """

    name: str
    interval_seconds: float
    run: Callable[[asyncpg.Pool], Awaitable[None]]


def _lock_key(name: str) -> int:
    """The advisory lock key a job's every run takes, derived from its own `name` -
    not a fixed constant per job (`http.py`'s `_CLEANUP_LOCK_KEY` is one such
    constant, picked by hand for the one job that module runs): two jobs with
    different names can never collide on the same key, and a new job registered
    here later needs nothing picked for it beyond a name.
    """
    return zlib.crc32(f"memory_manager:worker:{name}".encode())


async def _run_singleton(pool: asyncpg.Pool, job: Job) -> None:
    """Run `job.run(pool)` iff this call wins `job`'s advisory lock this round -
    the non-blocking counterpart to `db/migrate.py`'s blocking one, same
    reasoning as `http.py`'s `_run_cleanup_iteration`: nothing waits on a
    singleton job, so a replica that loses the race skips this round outright.

    Takes the lock on its own connection, held only for as long as acquiring it
    takes (`http.py`'s `_run_cleanup_iteration` does the same) - `job.run` itself
    makes its own pool connections for whatever it actually does, same as
    `auth.store.cleanup`/`PostgresSharedState.sweep_expired_windows` always have.
    """
    async with pool.acquire() as conn, conn.transaction():
        acquired = await conn.fetchval("select pg_try_advisory_xact_lock($1)", _lock_key(job.name))
        if not acquired:
            _logger.debug(
                "worker job %r: advisory lock held elsewhere, skipping this round", job.name
            )
            return
        # `job.run` is held inside this same lock-holding transaction - still
        # on `conn`'s own connection only for the lock itself, with `job.run`
        # free to acquire whatever other pool connections it needs for its
        # own work (same shape as `http.py`'s `_run_cleanup_iteration`): the
        # lock is released only once this whole block ends, at the earliest.
        await job.run(pool)


async def _job_loop(pool: asyncpg.Pool, job: Job, stop: asyncio.Event) -> None:
    """Run `_run_singleton(pool, job)` every `job.interval_seconds`, until `stop` is
    set - mirrors `http.py`'s own `_cleanup_loop`: a failed run is logged and
    retried next interval, never allowed to crash this loop (or the process).

    `stop` is checked by racing it against the interval sleep, not polled -
    setting it while a run is already in flight has no effect on that run (it
    is let finish, `create_worker_app`'s `lifespan` docstring explains why);
    it only ever stops *another* run from being scheduled.
    """
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=job.interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            await _run_singleton(pool, job)
        except Exception:
            _logger.exception("worker job %r failed", job.name)


# --- the metrics-refresh loop (#261) ----------------------------------------
#
# Unrelated to both loop shapes above: no advisory lock (module docstring -
# every worker replica's own `/metrics` must reflect the current state on
# its own), and no `jobs`-outbox claiming either. Just a plain interval loop
# that re-runs one cheap read-only query (`jobs.pending_stats`, already
# bounded by `jobs_claimable_idx`'s own partial-index predicate, not by the
# size of `jobs` as a whole, let alone `chunks` - a first version of this
# loop computed `mm_embedding_lag_seconds` from a `notes`/`chunks` join on
# `embedding is null` instead; rejected in review (#261) for exactly that
# reason - an unindexed predicate over a table that can hold millions of
# rows, scanned by every worker replica every 15s) and pushes its result
# into `observability.metrics`' gauges.

#: The one `jobs.kind` `mm_embedding_lag_seconds` is derived from - the same
#: literal `index.indexer.Indexer.index_on_connection`/`build_job_handlers`
#: already use for this job kind, not re-exported as a shared constant
#: there: this is the only place outside those two that needs to name it.
_EMBED_NOTE_KIND = "embed_note"


def _embedding_provider_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Whether `EMBEDDING_PROVIDER` names a real provider, not `"none"`
    (`config.EmbeddingConfig`'s own default) - read directly from the
    environment, the same `environ: Mapping[str, str] | None = None` shape
    `observability.metrics.metrics_enabled` already uses, rather than
    threading a new parameter through `cli.py`'s `_serve_worker` just for
    this one boolean (`EmbeddingConfig.from_env` reads a handful of
    `EMBEDDING_*` variables already read there a second time - no network
    call, nothing expensive to repeat here). An invalid configuration
    (`EmbeddingConfigError` - `_serve_worker` itself already refuses to start
    over the identical error) is treated as "no provider": this function
    only ever decides whether `mm_embedding_lag_seconds` is worth computing
    at all, never anything that should itself fail a worker round over a
    config problem belonging to a different code path entirely.
    """
    raw = dict(environ) if environ is not None else dict(os.environ)
    try:
        return EmbeddingConfig.from_env(raw).provider != "none"
    except EmbeddingConfigError:
        return False


async def _refresh_metrics(
    pool: asyncpg.Pool, *, kinds: Sequence[str], embedding_lag_enabled: bool
) -> None:
    """One round of #261's metrics refresh - a single `jobs.pending_stats` call
    (bounded by `jobs_claimable_idx`'s own partial index, never by `jobs`'s
    total size) feeds both gauges:

    - `mm_jobs_pending`/`mm_jobs_oldest_pending_age_seconds` for every `kinds`
      entry, with `(0, 0.0)` for a `kind` the query no longer returns - the
      backlog it just finished draining.
    - `mm_embedding_lag_seconds`, iff `embedding_lag_enabled`: the same
      `_EMBED_NOTE_KIND` entry `pending_stats` already computed - "how far
      behind embeddings are" is exactly the age of the oldest still-pending
      (or retrying; `jobs.fail_or_retry` keeps a retried job `'pending'`)
      `"embed_note"` job, `0.0` once none is. Reset to absent
      (`set_embedding_lag_seconds(None)`) while disabled instead - never left
      at a stale value from an earlier round in the (production-impossible,
      but test-observable) case this flag ever flips.
    """
    stats = await jobs.pending_stats(pool)
    for kind in kinds:
        pending, oldest_age_seconds = stats.get(kind, (0, 0.0))
        set_jobs_pending(kind, pending, oldest_age_seconds)

    if not embedding_lag_enabled:
        set_embedding_lag_seconds(None)
        return

    _pending, oldest_age_seconds = stats.get(_EMBED_NOTE_KIND, (0, 0.0))
    set_embedding_lag_seconds(oldest_age_seconds)


async def _metrics_refresh_loop(
    pool: asyncpg.Pool,
    stop: asyncio.Event,
    *,
    interval_seconds: float,
    kinds: Sequence[str],
    embedding_lag_enabled: bool,
) -> None:
    """Run `_refresh_metrics` every `interval_seconds` until `stop` is set -
    same scheduling shape as `_job_loop` (a failed round is logged and
    retried next interval, never allowed to crash this loop), just without
    `_run_singleton`'s advisory lock (module docstring).
    """
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            await _refresh_metrics(pool, kinds=kinds, embedding_lag_enabled=embedding_lag_enabled)
        except Exception:
            _logger.exception("metrics refresh failed")


async def _cleanup_job(pool: asyncpg.Pool, *, rate_limit_window_floor_seconds: float) -> None:
    """`auth.store.cleanup` plus `PostgresSharedState.sweep_expired_windows` - the
    exact sweep `http.py`'s own `_run_cleanup_iteration` used to run from every
    `api` replica (#106), now this worker's job for the `"postgres"` backend
    instead (module docstring: run unconditionally, not gated on whether either
    half currently has anything to do).
    """
    stats = await store.cleanup(pool)
    _logger.info(
        "oauth cleanup: pending=%d codes=%d tokens=%d clients=%d",
        stats.pending,
        stats.codes,
        stats.tokens,
        stats.clients,
    )
    swept = await PostgresSharedState(pool).sweep_expired_windows(
        older_than_seconds=rate_limit_window_floor_seconds
    )
    _logger.info("rate limit sweep: removed %d expired rate-limit window(s)", swept)


#: `Job.name` for the Entra deprovisioning delta sync below (#223, ADR-0006 §6) -
#: a module-level constant (not inlined at the one `build_jobs` call site) so a
#: test can derive the same `_lock_key` without hardcoding the string twice.
_ENTRA_DELTA_SYNC_JOB_NAME = "entra_delta_sync"


async def _entra_delta_cursor_link(pool: asyncpg.Pool) -> str | None:
    """The stored `@odata.deltaLink` from the previous successful round
    (`entra_delta_cursor`, migration 0014), or `None` before any round has ever
    completed - exactly the meaning `auth.graph.GraphClient.users_delta`'s own
    `delta_link` parameter already gives a bare `None` (a full sync)."""
    delta_link: str | None = await pool.fetchval(
        "select delta_link from entra_delta_cursor where id"
    )
    return delta_link


async def _touch_entra_delta_run(pool: asyncpg.Pool) -> None:
    """Stamp `entra_delta_cursor.last_run_at` to now, creating the singleton row
    with `delta_link = null` if this is the very first attempt - called at the
    start of every `_entra_delta_sync_job` run, whether or not it goes on to
    succeed (`last_run_at` vs. `last_success_at`, the migration's own docstring)."""
    await pool.execute(
        """
        insert into entra_delta_cursor (id, last_run_at) values (true, now())
        on conflict (id) do update set last_run_at = excluded.last_run_at
        """
    )


async def _save_entra_delta_cursor(pool: asyncpg.Pool, delta_link: str) -> None:
    """Persist `delta_link` plus `last_success_at` - called only once every page of
    one round was already applied (module docstring: "cursor advanced only after
    all pages were applied"), never from inside the paging itself."""
    await pool.execute(
        """
        insert into entra_delta_cursor (id, delta_link, last_run_at, last_success_at)
        values (true, $1, now(), now())
        on conflict (id) do update
            set delta_link = excluded.delta_link,
                last_run_at = excluded.last_run_at,
                last_success_at = excluded.last_success_at
        """,
        delta_link,
    )


async def _entra_delta_sync_job(pool: asyncpg.Pool, graph: GraphClient) -> None:
    """One Graph `users/delta` round, applied to `users` (#223, ADR-0006 §6).

    `auth.graph.GraphClient.users_delta` already follows every `@odata.nextLink`
    page of the round itself before returning, so every change below is applied
    from one complete, in-memory `UsersDeltaResult` - `_save_entra_delta_cursor`
    only ever runs after every one of them went through, never mid-round. Any
    `GraphError` (including a `GraphDeltaExpired` from the *second* attempt
    below) propagates straight out of this function to `_job_loop`'s own
    try/except, which logs it and retries next interval - `entra_delta_cursor`
    is left exactly where `_touch_entra_delta_run` put it, i.e. `delta_link`
    unchanged, so that retry re-fetches the identical round rather than one
    that silently skipped ahead.

    A `GraphDeltaExpired` from the *first* attempt (the stored `delta_link` is
    older than Entra's 7-day retention, or was reset upstream) is caught once:
    this round restarts immediately with a full sync (`users_delta(None)`) -
    `docs/research/entra-contract.md` §6's own "the application must restart
    with a full sync".

    An oid this server has never seen (`auth.users.get_user` returns `None`) is
    skipped outright - #223's own Implementation checklist: "unknown oids
    ignored", the sync only ever narrows what is already known, never imports
    the tenant.
    """
    await _touch_entra_delta_run(pool)
    delta_link = await _entra_delta_cursor_link(pool)
    try:
        result = await graph.users_delta(delta_link)
    except GraphDeltaExpired:
        _logger.warning(
            "entra delta sync: stored delta link expired or was reset, restarting with a full sync"
        )
        result = await graph.users_delta(None)

    for change in result.changes:
        user = await get_user(pool, change.oid)
        if user is None:
            continue
        if change.enabled:
            await enable_user(pool, change.oid, reason="entra delta sync: re-enabled in Graph")
        else:
            await disable_user(
                pool, change.oid, reason="entra delta sync: disabled or removed in Graph"
            )

    await _save_entra_delta_cursor(pool, result.delta_link)


#: `Job.name` for the retention sweep below (#240, ADR-0008 "Deprovisioned
#: users") - a module-level constant, same reason `_ENTRA_DELTA_SYNC_JOB_NAME`
#: above has one: a test derives `_lock_key` from it without hardcoding the
#: string twice.
_RETENTION_JOB_NAME = "retention"

#: `erasure_log`/`audit_log` actor for every erasure this job performs - never
#: a real `oid` (CLAUDE.md "audit log for every write"): this is the system,
#: not the user, choosing to erase, on a schedule the user has no part in.
_RETENTION_ACTOR = "system:retention"


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _users_disabled_before(pool: asyncpg.Pool, cutoff: datetime) -> list[str]:
    """Every `users.oid` still disabled, with `disabled_at` at or before `cutoff`.

    A user `auth.users.enable_user` re-enabled in the meantime has
    `disabled_at = null` again (that function's own docstring) and is
    therefore never selected here - the same "disabled state read live, never
    cached" rule `auth.users.get_user`'s own docstring gives for
    `mm_readable_ns()`/`mm_writable_ns()`.
    """
    rows = await pool.fetch(
        "select oid from users where disabled_at is not null and disabled_at <= $1", cutoff
    )
    return [row["oid"] for row in rows]


async def _retention_job(
    pool: asyncpg.Pool, *, retention_days: int, clock: Callable[[], datetime] = _utc_now
) -> None:
    """Erase the personal memory of every user disabled at least `retention_days`
    ago (#240, ADR-0008 "Deprovisioned users": "the personal namespace is
    frozen. It is hard-deleted after `PERSONAL_RETENTION_DAYS`").

    `clock` stands in for `datetime.now(UTC)` the same way `storage.postgres.
    PostgresBackend.__init__`'s own `clock` parameter does - a test freezes
    it to place a disabled user on either side of the retention cutoff
    without ever sleeping `retention_days` days for real.

    Each erasure goes through `storage.postgres.PostgresBackend.erase`
    (`storage/erasure.py`'s own `erase_user`, ADR-0007 §3 addendum) - the one
    primitive every other erasure caller already uses, so `erasure_log` and
    the configured `AUDIT_EXPORT`/SIEM export happen exactly as they would
    for an admin-triggered erasure (#231), with `_RETENTION_ACTOR` the only
    difference from one. Idempotent: an oid a previous round (or another
    worker replica, had it won this round's advisory lock instead) already
    erased no longer has a `users` row at all, so it is simply not selected
    again - two replicas racing for `_lock_key(_RETENTION_JOB_NAME)` can
    therefore never erase the same user twice, the same guarantee
    `_run_singleton` already gives every other job here.
    """
    cutoff = clock() - timedelta(days=retention_days)
    oids = await _users_disabled_before(pool, cutoff)
    if not oids:
        return
    backend = PostgresBackend(pool)
    reason = f"retention: disabled at least {retention_days} day(s) ago (PERSONAL_RETENTION_DAYS)"
    for oid in oids:
        await backend.erase("user", oid, actor=_RETENTION_ACTOR, reason=reason)


def build_jobs(
    config: ServerConfig,
    *,
    worker_config: WorkerConfig | None = None,
    graph_client: GraphClient | None = None,
) -> list[Job]:
    """This worker's job registry: `_cleanup_job` and `_retention_job` (#240) always,
    plus `_entra_delta_sync_job` (#223) when `graph_client` is given - `cli.py`'s
    `_serve_worker` is the one production caller that passes `auth.graph.
    GraphClient.from_env(os.environ)`, `None` for a deployment that never
    configured Entra (`GraphClient.from_env`'s own docstring); every existing
    test of this function omits both `graph_client` and `worker_config` and
    gets exactly the two jobs it always has.

    `config` is read only for its `RATE_LIMIT_*`/`mcp_path`-independent fields
    (`config.rate_limit_sweep_floor_seconds`, shared with `http.py`'s own
    cleanup sweep rather than duplicated here) - `ServerConfig.from_env(os.
    environ)` builds one without requiring `PUBLIC_URL` or anything else
    HTTP-transport-specific to be set, since `resource_url()` (the one method
    that does require it) is never called here. `worker_config` supplies the
    interval for the Entra job (`WorkerConfig.entra_delta_sync_seconds`,
    default 300s) and the retention job's own `personal_retention_days`/
    `retention_sweep_seconds` (defaults 30 days / once a day) - defaulted to a
    bare `WorkerConfig()`'s own values when omitted, same defaults `cli.py`'s
    own `WorkerConfig.from_env` would give.
    """
    floor_seconds = rate_limit_sweep_floor_seconds(config)

    async def run_cleanup(pool: asyncpg.Pool) -> None:
        await _cleanup_job(pool, rate_limit_window_floor_seconds=floor_seconds)

    jobs_list = [Job(name="cleanup", interval_seconds=_CLEANUP_INTERVAL_SECONDS, run=run_cleanup)]

    retention_days = (
        worker_config.personal_retention_days
        if worker_config is not None
        else WorkerConfig().personal_retention_days
    )
    retention_interval_seconds = (
        worker_config.retention_sweep_seconds
        if worker_config is not None
        else WorkerConfig().retention_sweep_seconds
    )

    async def run_retention(pool: asyncpg.Pool) -> None:
        await _retention_job(pool, retention_days=retention_days)

    jobs_list.append(
        Job(
            name=_RETENTION_JOB_NAME,
            interval_seconds=retention_interval_seconds,
            run=run_retention,
        )
    )

    if graph_client is not None:
        interval_seconds = (
            worker_config.entra_delta_sync_seconds
            if worker_config is not None
            else WorkerConfig().entra_delta_sync_seconds
        )

        async def run_entra_delta_sync(pool: asyncpg.Pool) -> None:
            await _entra_delta_sync_job(pool, graph_client)

        jobs_list.append(
            Job(
                name=_ENTRA_DELTA_SYNC_JOB_NAME,
                interval_seconds=interval_seconds,
                run=run_entra_delta_sync,
            )
        )

    return jobs_list


# --- the `jobs` outbox consumer (#218) --------------------------------------
#
# A second, unrelated kind of loop this module runs: unlike `Job`/`_job_loop`
# above (a periodic singleton, won by exactly one replica's advisory lock,
# nothing ever waits on it), `consume_jobs` below claims rows several worker
# replicas may run *concurrently* - `jobs.claim`'s own `FOR UPDATE SKIP
# LOCKED` is what keeps two replicas from ever claiming the same row, not an
# advisory lock. No job kind is registered here yet (`handlers` is empty in
# production today) - the embedding queue is #219's own follow-up; this is
# only the consumer shape `jobs.claim`/`complete`/`fail`/`fail_or_retry` plug
# into, plus the unknown-kind-fails-outright case any future kind not yet
# known to a given worker build would otherwise hit.

#: `handlers`' value type: runs a claimed job's own `payload` against `pool`
#: (as the owner, same as every other call this module makes - `jobs.py`'s
#: own module docstring) and either returns (the job is `complete`d) or
#: raises (`fail_or_retry`, with the raised message as `last_error`).
JobHandler = Callable[[asyncpg.Pool, Mapping[str, object]], Awaitable[None]]

#: `consume_jobs`'s own default batch size - `create_worker_app`'s wiring
#: and this module's tests share the one constant rather than each picking
#: their own number.
_DEFAULT_JOBS_BATCH_SIZE = 10

#: `create_worker_app`'s own fallback for `jobs_poll_seconds` if a caller
#: does not read `WorkerConfig.jobs_poll_seconds` itself - the same default
#: that config carries (`config.py`'s own `_DEFAULT_JOBS_POLL_SECONDS`), not
#: imported from there to avoid a needless cross-module constant coupling
#: for one float both modules are free to default independently.
_DEFAULT_JOBS_POLL_SECONDS = 5.0


def build_job_handlers(indexer: Indexer) -> dict[str, JobHandler]:
    """This worker's `jobs`-outbox handler registry: `"embed_note"` (#219) today.

    Mirrors `build_jobs` above for the other loop shape this module runs -
    `indexer` is the one `index.indexer.Indexer` `cli.py`'s `_serve_worker`
    builds over `index.indexer.VaultNotesSource()` (the `"postgres"`
    backend's `vault_notes`, same source `app.py`'s own `"postgres"` branch
    indexes through on the request path; this worker never touches a vault
    working copy at all). The handler itself does no more than decode the
    job's `payload` (`index.indexer.Indexer.index_on_connection`'s own
    `{"note_id": ..., "version": ...}` shape) and call
    `indexer.embed_note_job` - an `EmbeddingError` it raises propagates
    straight through to `_dispatch_job`, which retries the job with backoff
    rather than swallowing it. `create_worker_app` derives `consume_jobs`'s
    own `kinds` from this registry's keys, so registering a handler here is
    the only step a future job kind needs on this side - no separate kind
    list to keep in sync.
    """

    async def embed_note(_pool: asyncpg.Pool, payload: Mapping[str, object]) -> None:
        note_id = payload.get("note_id")
        job_version = payload.get("version")
        if not isinstance(note_id, str) or not isinstance(job_version, str):
            raise ValueError(f"malformed 'embed_note' payload: {payload!r}")
        await indexer.embed_note_job(note_id, job_version)

    return {"embed_note": embed_note}


async def enqueue_pending_embeddings(indexer: Indexer) -> int:
    """This worker's own startup catch-up (#219, module docstring): enqueue one
    `"embed_note"` job for every note with a chunk still missing an embedding
    or stamped with a model other than the provider's current one.

    `index.indexer.Indexer.enqueue_stale_embeddings` does the actual query
    and enqueueing; `cli.py`'s `_serve_worker` calls this once, before
    `consume_jobs` starts claiming anything, so a chunk left stale by a job
    lost before this worker ever ran, a provider outage, or an
    `EMBEDDING_MODEL` change all converge without needing a manual
    `memory-manager reindex`. A no-op without a configured provider. Returns
    how many notes were enqueued.
    """
    return await indexer.enqueue_stale_embeddings()


async def _dispatch_job(
    pool: asyncpg.Pool,
    handlers: Mapping[str, JobHandler],
    job: jobs.ClaimedJob,
    *,
    max_attempts: int,
) -> None:
    """Run `job` against `handlers`, or `jobs.fail` it outright if `handlers` has
    nothing registered for `job.kind` - unlike a handler's own failure (caught
    below, retried with backoff through `jobs.fail_or_retry`), an unknown kind
    can never succeed on a later attempt, so retrying it would only delay the
    same outcome.

    The whole call runs inside `observability.tracing.job_span` (#263
    WP-31): a CONSUMER span continuing `job.traceparent` (the enqueuing
    request's own trace, `index/indexer.py`'s `current_traceparent()` call)
    when it is set, or starting a fresh trace otherwise - a handler's own DB
    statements (`db_span`, where they use it) nest under this span the same
    way they nest under `TracingMiddleware`'s SERVER span on the request path.
    """
    with job_span(job.kind, job_id=job.id, attempt=job.attempts, traceparent=job.traceparent):
        handler = handlers.get(job.kind)
        if handler is None:
            _logger.error("job %s: no handler registered for kind %r, failing", job.id, job.kind)
            await jobs.fail(pool, job.id, error=f"unknown job kind {job.kind!r}")
            return
        try:
            await handler(pool, job.payload)
        except Exception as exc:
            _logger.exception("job %s (kind=%r) failed", job.id, job.kind)
            await jobs.fail_or_retry(pool, job.id, error=str(exc), max_attempts=max_attempts)
        else:
            await jobs.complete(pool, job.id)


async def consume_jobs(
    pool: asyncpg.Pool,
    listen_conn: asyncpg.Connection,
    handlers: Mapping[str, JobHandler],
    *,
    kinds: Sequence[str],
    stop: asyncio.Event,
    poll_seconds: float,
    batch_size: int = _DEFAULT_JOBS_BATCH_SIZE,
    max_attempts: int = jobs.DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Claim and dispatch `kinds` jobs until `stop` is set.

    `listen_conn` must be a connection dedicated to this call for as long as
    it runs, never a pool connection (docs/research/enterprise.md §"Pool
    sizing": "LISTEN does not work through PgBouncer transaction mode ->
    dedicated direct connection per worker") - this function registers a
    listener on it (`jobs.NOTIFY_CHANNEL`) and removes it again on return,
    but never acquires or releases `listen_conn` itself; the caller owns its
    lifetime.

    Each round: claim up to `batch_size` jobs and dispatch every one of them
    in turn (a single worker runs its own batch sequentially, not
    concurrently - `jobs.claim`'s `SKIP LOCKED` is what lets *several worker
    replicas* overlap, not what this one replica does with its own claimed
    batch). If the batch was non-empty, loop straight back to `claim` again
    without waiting - more may already be waiting. Once a `claim` comes back
    empty, wait for either `stop`, a `NOTIFY` on `jobs.NOTIFY_CHANNEL`, or
    `poll_seconds`, whichever comes first - the `NOTIFY` is only ever a
    wake-up hint (ADR-0007 §4): a notification lost between this round's
    empty `claim` and the listener being (re)armed is still caught by the
    next `poll_seconds` timeout regardless.
    """
    wake = asyncio.Event()

    def _on_notify(
        _conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        _pid: int,
        _channel: str,
        _payload: object,
    ) -> None:
        wake.set()

    await listen_conn.add_listener(jobs.NOTIFY_CHANNEL, _on_notify)
    try:
        while not stop.is_set():
            claimed = await jobs.claim(pool, kinds, limit=batch_size)
            for job in claimed:
                await _dispatch_job(pool, handlers, job, max_attempts=max_attempts)
            if claimed:
                continue
            wake.clear()
            stop_wait = asyncio.ensure_future(stop.wait())
            wake_wait = asyncio.ensure_future(wake.wait())
            try:
                await asyncio.wait(
                    {stop_wait, wake_wait},
                    timeout=poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (stop_wait, wake_wait):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(stop_wait, wake_wait, return_exceptions=True)
    finally:
        await listen_conn.remove_listener(jobs.NOTIFY_CHANNEL, _on_notify)


def create_worker_app(
    pool: asyncpg.Pool,
    jobs: Sequence[Job],
    *,
    shutdown_grace_seconds: int,
    jobs_listen_conn: asyncpg.Connection | None = None,
    job_handlers: Mapping[str, JobHandler] | None = None,
    jobs_poll_seconds: float = _DEFAULT_JOBS_POLL_SECONDS,
    jobs_batch_size: int = _DEFAULT_JOBS_BATCH_SIZE,
    metrics_refresh_seconds: float = _METRICS_REFRESH_INTERVAL_SECONDS,
) -> Starlette:
    """Build the worker's own minimal Starlette app: `/healthz`, `/readyz`,
    `/metrics`, plus a `lifespan` that runs one `_job_loop` per `jobs` entry,
    and - if `jobs_listen_conn` is given - one `consume_jobs` loop for the
    `jobs` outbox (#218), for as long as the app is up.

    `jobs_listen_conn` is `None` in every existing test of this function and
    for any caller that has no dedicated `LISTEN` connection to hand it
    (`consume_jobs`'s own docstring: never a pool connection) - the outbox
    loop is then simply not started, exactly as if this parameter did not
    exist. `cli.py`'s `_serve_worker` is the one production caller that
    passes one; `job_handlers` defaults to an empty registry if omitted
    (every existing test of this function bar the `"embed_note"` ones).
    `consume_jobs`'s own `kinds` is derived from `job_handlers`' keys -
    registering a handler is the only step adding a job kind needs on this
    side, no separate list to keep in sync.

    `cli.py`'s `_serve_worker` serves the result with `http.py`'s own
    `GracefulShutdownServer` (`handle_exit` flips `app.state.draining = True`
    on `SIGTERM`/`SIGINT`, exactly as it does for the `api` process) - this
    function only has to set that flag's initial value and react to it in
    `_readyz`, the same split `http.py`'s `create_app` uses.

    On shutdown, `lifespan` sets `stop` (no job loop schedules another run,
    and `consume_jobs` claims nothing further, after this) and waits up to
    `shutdown_grace_seconds` for every loop to return on its own - a loop
    whose job is still running when `stop` was set is not interrupted
    mid-run, only left to finish; past the deadline, any loop still not done
    is cancelled outright so this app's own shutdown (and therefore the
    process, `cli.py`'s `_serve_worker`) is never blocked forever on one job
    that never finishes. `jobs_listen_conn` itself is never closed here - it
    was never opened here either (module docstring of `consume_jobs`); the
    caller that opened it closes it, same as `pool`.

    Also always starts `_metrics_refresh_loop` (#261, module docstring) -
    unlike every task above, its own task is never added to `app.state.
    tasks`: `_readyz`'s `jobs_running` check must keep meaning exactly what
    it already does (a registered `Job` or `jobs`-outbox handler actually
    running), not "this app's lifespan has not yet torn every task down" -
    `tests/worker/test_singleton.py`'s own `test_readyz_is_not_ready_without_
    any_registered_job` pins a worker with neither as not ready, and this
    always-on loop must not change that. Still started and joined on
    shutdown exactly like the others, just outside that one check.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        stop = asyncio.Event()
        handlers = job_handlers if job_handlers is not None else {}
        tasks = [asyncio.create_task(_job_loop(pool, job, stop)) for job in jobs]
        if jobs_listen_conn is not None:
            tasks.append(
                asyncio.create_task(
                    consume_jobs(
                        pool,
                        jobs_listen_conn,
                        handlers,
                        kinds=tuple(handlers),
                        stop=stop,
                        poll_seconds=jobs_poll_seconds,
                        batch_size=jobs_batch_size,
                    )
                )
            )
        app.state.tasks = tasks

        metrics_task = asyncio.create_task(
            _metrics_refresh_loop(
                pool,
                stop,
                interval_seconds=metrics_refresh_seconds,
                kinds=tuple(handlers),
                embedding_lag_enabled=_embedding_provider_configured(),
            )
        )
        try:
            yield
        finally:
            stop.set()
            all_tasks = [*tasks, metrics_task]
            if all_tasks:
                _done, pending = await asyncio.wait(all_tasks, timeout=shutdown_grace_seconds)
                for task in pending:
                    task.cancel()
                for task in pending:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    routes = [
        Route(HEALTH_PATH, endpoint=_healthz, methods=["GET"]),
        Route(READY_PATH, endpoint=_readyz, methods=["GET"]),
        Route(METRICS_PATH, endpoint=metrics_endpoint, methods=["GET"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.pool = pool
    # Flipped to `True` by `http.GracefulShutdownServer.handle_exit` on
    # `SIGTERM`/`SIGINT` - `_readyz` checks this before anything else.
    app.state.draining = False
    return app


async def _healthz(_request: Request) -> Response:
    """ADR-0002 §13: a modified deployment's `/healthz` must point back at its source -
    the same body `http.py`'s own `_healthz` returns, this process is simply a
    different one of the same build."""
    return JSONResponse(
        {"status": "ok", "version": __version__, "commit": __commit__, "source": _SOURCE_URL}
    )


async def _readyz(request: Request) -> Response:
    """503 when draining (ADR-0009 §5), the database is unreachable, or a job loop's
    task has already finished on its own (module docstring: that can only mean
    the loop itself crashed, not that a job failed - every job run is already
    guarded inside `_job_loop`).
    """
    if request.app.state.draining:
        return JSONResponse({"ready": False, "draining": True}, status_code=503)

    pool: asyncpg.Pool = request.app.state.pool
    database_ready = True
    try:
        await pool.fetchval("select 1")
    except Exception:
        database_ready = False

    tasks: list[asyncio.Task[None]] = request.app.state.tasks
    jobs_running = bool(tasks) and all(not task.done() for task in tasks)

    ready = database_ready and jobs_running
    body = {
        "ready": ready,
        "database": database_ready,
        "jobs_running": jobs_running,
        "draining": False,
    }
    return JSONResponse(body, status_code=200 if ready else 503)
