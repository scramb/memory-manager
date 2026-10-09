# SPDX-License-Identifier: AGPL-3.0-only
"""Replays `erasure_log` after a backup restore or PITR (#233, ADR-0007 §3 addendum
2026-10-08: "After a backup restore or PITR, `erasure_log` is replayed before the
API becomes ready").

A restore rolls `erasure_log` itself back to the backup's point in time too, and
can bring back content a later erasure (`storage.erasure`, #231) had already
removed - the backup predates it. The off-database copy that survives a restore
is the `erasure` audit record every `erase_note`/`erase_namespace`/`erase_user`
call already writes (owner decision 2026-10-08: exported like every other audit
record, `observability/audit_export.py`, #245/27d); an operator extracts those
records since the backup point from the SIEM into a JSONL file and points
`ERASURE_LOG_REPLAY_FILE` (`config.py`) at it. `http.py`'s `lifespan` is the one
caller: `parse_replay_file` runs synchronously at startup, before the server ever
starts serving, so a malformed file refuses startup outright (ADR-0007 §3
addendum's own wording) rather than leaving `/readyz` 503 forever with no visible
cause; `replay` then runs in the background, gating `/readyz` until it finishes
(see `http.py`'s own module for the readiness wiring).

Idempotent per record, not merely per file: a target already gone (the restore
never actually reintroduced it, or a previous, interrupted replay already handled
it) is a no-op, exactly `storage.erasure`'s own "erasing absent IDs is a no-op"
contract - checked by this module before calling into it, since only `erase_note`
itself raises `NotFound` for a missing target; `erase_namespace`/`erase_user`
happily report zero counts for one instead, which would otherwise insert a
second, empty `erasure_log`/`audit_log` row every time this runs. `replay` holds
one blocking Postgres advisory lock (`pg_advisory_xact_lock`, `db/migrate.py`'s
own technique, unlike `http.py`'s periodic cleanup sweep's non-blocking
`pg_try_advisory_xact_lock`) for its entire run: a second replica starting up
concurrently waits for the first to finish replaying rather than either racing it
or - `_run_singleton`'s non-blocking pattern - skipping its own replay and going
ready before the first replica's work is actually done. Each record replays in
its own nested transaction (savepoint) under that lock, the same shape `migrate()`
applies each migration file in: a record that fails partway rolls the whole run
back together with the lock-holding transaction, so a retry (this process
restarting, or the next replica up) starts clean rather than half-applied.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from memory_manager.storage.base import ErasureTargetKind, NotFound
from memory_manager.storage.erasure import erase_namespace, erase_note, erase_user

__all__ = ["ErasureReplayFormatError", "ReplayRecord", "ReplayStats", "parse_replay_file", "replay"]

#: Fixed advisory lock key for erasure-log replay (#233) - distinct from
#: `db/migrate.py`'s own `_LOCK_KEY` and `http.py`'s `_CLEANUP_LOCK_KEY`, so
#: replay never blocks on, or is blocked by, either of those for an unrelated
#: reason.
_LOCK_KEY = zlib.crc32(b"memory_manager:erasure_replay")

#: `storage.erasure.erase_note`/`erase_namespace`/`erase_user`'s `reason`
#: argument for every record this module replays - distinct from whatever the
#: original erasure's own `reason` was (not carried in the exported record at
#: all, `audit-export.md`'s "detail carries only the IDs"), so the new
#: `erasure_log`/`audit_log` row this (re-)erasure writes is identifiable as a
#: replay, not mistaken for a second, independent request.
_REPLAY_REASON = "backup-restore replay (#233)"

#: The one `op` value `parse_replay_file` accepts - every other exported audit
#: record (`write`, `edit`, ...) is irrelevant to replay and a sign the
#: operator extracted more than the `erasure`-only feed `docs/guides/
#: audit-export.md` documents.
_EXPECTED_OP = "erasure"

_TARGET_KINDS: frozenset[ErasureTargetKind] = frozenset({"note", "namespace", "user"})

# A connection acquired from a pool - `storage.erasure`'s own `_Connectable`,
# redefined here rather than imported: that name is private to its module.
_Connectable = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection


class ErasureReplayFormatError(ValueError):
    """`ERASURE_LOG_REPLAY_FILE` is not valid JSONL of exported `erasure` audit
    records - message names the file and the offending 1-based line number, so an
    operator can find and fix (or drop) that one line without guessing."""


@dataclass(frozen=True)
class ReplayRecord:
    """One exported `erasure` audit record, parsed (#233).

    `actor` is replayed verbatim as the new `erase_*` call's own `actor` -
    whoever originally requested the erasure stays the attributed actor on the
    row replay (re-)writes. `target_ids` is always exactly one id today (the
    same "never more than one" contract `storage.base.ErasureResult.target_ids`
    documents), but kept as a tuple rather than a bare `str` to mirror that
    type exactly, in case a future `erasure_log` row ever carries more.
    """

    actor: str
    target_kind: ErasureTargetKind
    target_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReplayStats:
    """What one `replay()` call did: how many of `records` actually needed a
    (re-)erasure (`applied`) versus were already gone (`skipped`, `storage.
    erasure`'s own idempotent-no-op contract) - `total` is `applied + skipped`,
    kept as its own field only so a caller never has to add the two back up."""

    applied: int
    skipped: int
    total: int


def parse_replay_file(path: Path) -> list[ReplayRecord]:
    """Parse `path` (`ERASURE_LOG_REPLAY_FILE`) into `ReplayRecord`s, in file order.

    Raises `ErasureReplayFormatError` naming `path` and the offending 1-based
    line number on the first line that is not valid JSON, is missing `op`/
    `actor`/`detail.target_kind`/`detail.target_ids`, or names an `op` other
    than `"erasure"` - called eagerly, synchronously, before `http.py`'s
    `lifespan` ever starts the background replay task, so a malformed file
    refuses startup outright (module docstring) rather than failing silently
    once replay actually runs. Blank lines are skipped (a log shipper's
    trailing newline, same leniency `jsonlines`-style tooling usually gives).
    """
    records: list[ReplayRecord] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        records.append(_parse_line(path, line_no, line))
    return records


def _parse_line(path: Path, line_no: int, line: str) -> ReplayRecord:
    def fail(reason: str) -> ErasureReplayFormatError:
        return ErasureReplayFormatError(f"{path}:{line_no}: {reason}")

    try:
        payload: Any = json.loads(line)
    except json.JSONDecodeError as exc:
        raise fail(f"not valid JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise fail(f"expected a JSON object, got {type(payload).__name__}")

    op = payload.get("op")
    if op != _EXPECTED_OP:
        raise fail(f"expected op={_EXPECTED_OP!r}, got {op!r}")

    actor = payload.get("actor")
    if not isinstance(actor, str) or not actor:
        raise fail("missing or empty 'actor'")

    detail = payload.get("detail")
    if not isinstance(detail, dict):
        raise fail("missing or non-object 'detail'")

    target_kind = detail.get("target_kind")
    if target_kind not in _TARGET_KINDS:
        raise fail(
            f"detail.target_kind must be one of {sorted(_TARGET_KINDS)}, got {target_kind!r}"
        )

    target_ids = detail.get("target_ids")
    if (
        not isinstance(target_ids, list)
        or not target_ids
        or not all(isinstance(target_id, str) for target_id in target_ids)
    ):
        raise fail("detail.target_ids must be a non-empty list of strings")

    return ReplayRecord(actor=actor, target_kind=target_kind, target_ids=tuple(target_ids))


async def replay(pool: asyncpg.Pool, records: list[ReplayRecord]) -> ReplayStats:
    """Idempotently (re-)erase every target `records` names, under one blocking
    advisory lock for the whole run (module docstring).

    Safe to call with an empty `records` (nothing to do, `ReplayStats(0, 0, 0)`)
    and safe to call twice in a row, from the same or a different replica: the
    second call finds every target already gone and reports `applied=0`.
    """
    applied = 0
    skipped = 0
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("select pg_advisory_xact_lock($1)", _LOCK_KEY)
        for record in records:
            async with conn.transaction():
                if await _replay_one(conn, record):
                    applied += 1
                else:
                    skipped += 1
    return ReplayStats(applied=applied, skipped=skipped, total=len(records))


async def _replay_one(conn: _Connectable, record: ReplayRecord) -> bool:
    """(Re-)erase `record`'s one target; `True` if it still existed and was
    erased now, `False` if it was already gone (a no-op, module docstring)."""
    target_id = record.target_ids[0]

    if record.target_kind == "note":
        try:
            await erase_note(conn, target_id, actor=record.actor, reason=_REPLAY_REASON)
        except NotFound:
            return False
        return True

    if record.target_kind == "namespace":
        exists = await conn.fetchval("select 1 from namespaces where alias = $1", target_id)
        if exists is None:
            return False
        await erase_namespace(conn, target_id, actor=record.actor, reason=_REPLAY_REASON)
        return True

    # "user": gone only once both its identity row and its personal namespace
    # (if it ever had one, #101) are gone - either still present means there is
    # still something for `erase_user` to do.
    namespace_exists = await conn.fetchval(
        "select 1 from namespaces where kind = 'user' and external_key = $1", target_id
    )
    user_exists = await conn.fetchval("select 1 from users where oid = $1", target_id)
    if namespace_exists is None and user_exists is None:
        return False
    await erase_user(conn, target_id, actor=record.actor, reason=_REPLAY_REASON)
    return True
