# SPDX-License-Identifier: AGPL-3.0-only
"""The `jobs` outbox: a write transaction enqueues work here, exactly one worker
claims and finishes each row (#218, ADR-0007 §2/§4).

`enqueue` is called on the caller's own connection, inside the caller's own
transaction (the write that produced the job and the job row commit or roll
back together - "enqueue rolled back with its transaction never runs" is
this module's own `test_queue.py`'s first test) - never on a pool directly.
Its `pg_notify` runs on that same connection: Postgres only ever delivers a
transaction's `NOTIFY`s once that transaction commits, never on rollback, so
the wake-up hint and the row it is about always agree.

`claim`/`complete`/`fail`/`fail_or_retry` run as the owner against a pool,
never through `db.rls.request_connection` - the worker is a system identity
like `reindex`/`import` (`worker.py`'s own module docstring), and the app
role holds `INSERT` on `jobs` only (`db/rls.py`'s `grant_app_role`): a
request transaction enqueues, nothing else ever reads, claims or completes a
row under that role.

`claim` is the one `FOR UPDATE SKIP LOCKED` query ADR-0007 §4 names: several
workers calling it concurrently each walk away with a disjoint set of rows,
never the same one twice, and a worker that loses the race on a given row
simply does not see it (`LIMIT`, not retried). It also reclaims any `running`
row whose `locked_at` is older than `stale_after` - a worker that crashed
mid-job leaves its claim behind, caught by the next `claim` call rather than
stuck `running` forever.

Payloads carry IDs only (M9's erasure acceptance, WP-26: nothing in `jobs`
needs scrubbing beyond the row itself once a note or namespace is erased) -
`enqueue` enforces this with a plain size check (`_MAX_PAYLOAD_BYTES`), not a
column constraint: a few ULIDs/paths/namespaces comfortably fit, an embedded
note body does not, and the check costs nothing to update for a future job
kind's own small shape.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import asyncpg

from memory_manager.vault.ulid import new_ulid

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "NOTIFY_CHANNEL",
    "ClaimedJob",
    "PayloadTooLarge",
    "claim",
    "complete",
    "enqueue",
    "fail",
    "fail_or_retry",
]

_logger = logging.getLogger(__name__)

#: `LISTEN`s on this channel; `enqueue`'s `pg_notify` always names it (never the
#: job's own `kind`) - one channel for every kind keeps a worker's dedicated
#: `LISTEN` connection (`worker.py`, docs/research/enterprise.md §"Pool sizing":
#: "`LISTEN` does not work through PgBouncer transaction mode -> dedicated direct
#: connection per worker") from having to subscribe to a growing list of channels
#: as job kinds are added.
NOTIFY_CHANNEL = "memory_manager_jobs"

#: `fail_or_retry`'s default ceiling: a job that still fails after this many
#: claims is parked `failed` rather than retried forever.
DEFAULT_MAX_ATTEMPTS = 5

#: `enqueue`'s own ceiling (module docstring: "payloads carry IDs only"). A
#: ULID is 26 bytes; a handful of them plus a namespace and a couple of short
#: keys fits in a small fraction of this, an embedded note body does not.
_MAX_PAYLOAD_BYTES = 2048

#: `claim`'s own default for reclaiming a stale `running` row (a crashed
#: worker never got to `complete`/`fail`/`fail_or_retry`) - generous enough
#: that no real job embedding or export this project runs today is still
#: legitimately in flight after this long.
_DEFAULT_STALE_AFTER = timedelta(minutes=5)

# Connections this module accepts: a plain `asyncpg.Connection` (`enqueue`'s
# caller already holds one inside its own transaction) or a pool - `claim`/
# `complete`/`fail`/`fail_or_retry` take the pool itself, since each opens its
# own short-lived statement rather than sharing the caller's transaction
# (`db/rls.py`'s `_Connectable` is the same idea for the request path).
_Connectable = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

_ENQUEUE_SQL = """
insert into jobs (id, kind, payload, run_after, traceparent)
values ($1, $2, $3::jsonb, coalesce($4, now()), $5)
"""

_CLAIM_SQL = """
with candidates as (
    select id
    from jobs
    where kind = any($1::text[])
      and (
        (state = 'pending' and run_after <= now())
        or (state = 'running' and locked_at < now() - $3::interval)
      )
    order by run_after
    limit $2
    for update skip locked
)
update jobs
set state = 'running', attempts = attempts + 1, locked_at = now()
from candidates
where jobs.id = candidates.id
returning jobs.id, jobs.kind, jobs.payload, jobs.attempts, jobs.traceparent
"""


class PayloadTooLarge(ValueError):
    """Raised by `enqueue` for a payload over `_MAX_PAYLOAD_BYTES` - likely note
    content, not the IDs-only shape `jobs` is meant to carry (module docstring).
    """


@dataclass(frozen=True)
class ClaimedJob:
    """One row `claim` handed to this worker; `payload` already decoded from `jsonb`."""

    id: str
    kind: str
    payload: dict[str, object]
    attempts: int
    traceparent: str | None


async def enqueue(
    conn: _Connectable,
    kind: str,
    payload: Mapping[str, object],
    *,
    run_after: datetime | None = None,
    traceparent: str | None = None,
) -> str:
    """Insert one `jobs` row on `conn` and `pg_notify` the same connection.

    Must be called inside the caller's own transaction (module docstring):
    `conn` is never acquired or committed here. Raises `PayloadTooLarge`
    before the `INSERT` runs at all if `payload`'s JSON encoding is over
    `_MAX_PAYLOAD_BYTES`.

    The new row's id is a ULID generated here, not read back with `RETURNING`
    (migration `0011_jobs.sql`'s own comment: `RETURNING` needs `SELECT`
    privilege, which the app role must never hold on this table).
    """
    body = json.dumps(payload, sort_keys=True)
    size = len(body.encode("utf-8"))
    if size > _MAX_PAYLOAD_BYTES:
        raise PayloadTooLarge(
            f"job payload for kind {kind!r} is {size} bytes, over the "
            f"{_MAX_PAYLOAD_BYTES}-byte limit for an IDs-only payload (#218)"
        )
    job_id = new_ulid()
    await conn.execute(_ENQUEUE_SQL, job_id, kind, body, run_after, traceparent)
    await conn.execute("select pg_notify($1, $2)", NOTIFY_CHANNEL, kind)
    return job_id


async def claim(
    pool: asyncpg.Pool,
    kinds: Sequence[str],
    *,
    limit: int = 10,
    stale_after: timedelta = _DEFAULT_STALE_AFTER,
) -> list[ClaimedJob]:
    """Claim up to `limit` claimable rows of `kinds` (module docstring: `FOR UPDATE
    SKIP LOCKED`, plus reclaiming a stale `running` row).

    Returns an empty list for an empty `kinds` - never issues the query, which
    would otherwise match every kind via `kind = any('{}'::text[])`'s `where`
    on `state`/`run_after` alone.
    """
    if not kinds:
        return []
    rows = await pool.fetch(_CLAIM_SQL, list(kinds), limit, stale_after)
    return [
        ClaimedJob(
            id=row["id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            attempts=row["attempts"],
            traceparent=row["traceparent"],
        )
        for row in rows
    ]


async def complete(pool: asyncpg.Pool, job_id: str) -> None:
    """Mark `job_id` `done` - the handler ran without raising."""
    await pool.execute("update jobs set state = 'done', locked_at = null where id = $1", job_id)


async def fail(pool: asyncpg.Pool, job_id: str, *, error: str) -> None:
    """Mark `job_id` `failed` outright, no retry - an unknown job kind
    (`worker.py`'s dispatch) is the one caller today; a handler's own failure
    goes through `fail_or_retry` instead, which still retries up to its own
    ceiling.
    """
    await pool.execute(
        "update jobs set state = 'failed', last_error = $2, locked_at = null where id = $1",
        job_id,
        error,
    )


def _retry_delay_seconds(attempts: int) -> float:
    """Exponential backoff, capped at 5 minutes: `2 ** attempts` seconds."""
    return min(2.0**attempts, 300.0)


async def fail_or_retry(
    pool: asyncpg.Pool,
    job_id: str,
    *,
    error: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    """A handler raised for `job_id`: retry with backoff, or `failed` once
    `attempts` (already incremented by the `claim` that handed this job out)
    reaches `max_attempts`.

    A no-op if `job_id` no longer exists (nothing to update) - defensive only,
    nothing in this codebase deletes a `jobs` row outside erasure.
    """
    row = await pool.fetchrow("select attempts from jobs where id = $1", job_id)
    if row is None:
        return
    attempts: int = row["attempts"]
    if attempts >= max_attempts:
        await fail(pool, job_id, error=error)
        return
    delay = timedelta(seconds=_retry_delay_seconds(attempts))
    await pool.execute(
        "update jobs set state = 'pending', last_error = $2, locked_at = null, "
        "run_after = now() + $3::interval where id = $1",
        job_id,
        error,
        delay,
    )
