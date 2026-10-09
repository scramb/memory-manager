# SPDX-License-Identifier: AGPL-3.0-only
"""Writes one `audit_log` row for every vault write, success or rejection (#39).

CLAUDE.md: "audit log for every write" is one of the non-negotiable security
rules. `audit_log` already exists (migration `0001_index_schema.sql`) as
operational data that lives only in Postgres, never derived from the vault
(CLAUDE.md: "except operational data (tokens, audit log, OAuth state)").

`AuditWriter.record` is called by `memory_manager.app`'s audit hook, wired
onto `memory_manager.queue.WriteQueue` for every processed write - `ok` and
every rejection kind alike (`memory_manager.queue.WriteError` subclasses),
never only successes. `detail` is the one place a caller could accidentally
leak a note's content, a token or a secret into the audit log: every call
site in this codebase builds it from op-level metadata only (a version, an
error class name, a conflict file path) - never from `WriteRequest.content`/
`old_str`/`new_str` or a raised error's `current_content`. Nothing in this
module itself enforces that (there is no note content in scope here to
filter out); it is a rule for every caller, not a check this class could
make on their behalf.

A failed audit write is logged loudly but never raised: the vault write it
describes has already happened (or already failed) by the time `record` is
called, and losing the audit trail must not also lose - or retroactively
undo - that outcome.

Every record that reaches the DB also reaches `AuditExporter.export`
(#245, `observability/audit_export.py`) - to stdout, an OTLP logs endpoint,
both, or neither (`AUDIT_EXPORT`, off by default), for a SIEM. Exported
unconditionally after the DB insert attempt, success or failure alike: a
database outage must not also silence the SIEM copy, and a SIEM outage
(`AuditExporter.export` never raises) must never fail or undo the DB row.
`request_id` is read fresh for every record from `observability.logging.
current_request_id` - `None` outside an HTTP request (stdio mode, the poll
loop's own sync) - never threaded through by a caller.

`DETAIL_ALLOWLIST` is this module's own account of every key any caller in
this codebase ever puts into `detail` today (`app.py`'s `_audit_outcome`,
`quotas.py`, `migrate_git.py`, `exporter.py`) *minus* the two that are
shaped like a path rather than a scalar fact (`WriteConflict`'s
`conflict_path`, and `VersionConflict`'s/`EditMismatch`'s own never-stored
`current_content`) - dropped defensively even though no caller actually
writes either key into a Postgres-mode `audit_log` row today.
`storage.erasure` (#231, ADR-0007 §3 addendum: "audit rows keep only
metadata") is its one consumer so far, filtering a redacted row's `detail`
down to this set; nothing in `AuditWriter`/`record` itself reads or
enforces it - every call site above is still the one place that rule is
actually kept.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import asyncpg

from memory_manager.observability.audit_export import AuditExporter
from memory_manager.observability.logging import current_request_id

__all__ = ["DETAIL_ALLOWLIST", "AuditWriter"]

_logger = logging.getLogger(__name__)

#: See the module docstring's last paragraph.
DETAIL_ALLOWLIST = (
    "version",
    "current_version",
    "error",
    "category",
    "scope",
    "window",
    "limit",
    "retry_after",
    "resource",
    "namespace_kind",
    "predicted",
    "reason",
    "revisions",
    "namespace",
)

_INSERT = """
insert into audit_log (actor, client, op, path, commit_sha, outcome, detail)
values ($1, $2, $3, $4, $5, $6, $7::jsonb)
"""


class AuditWriter:
    """Inserts one row into `audit_log` per `record()` call, against `pool`.

    `exporter` defaults to `AuditExporter.from_env(os.environ)` - built once,
    here, so a misconfigured `AUDIT_EXPORT` (`AuditConfigError`, e.g. `otlp`
    without the `otel` extra) surfaces at startup, when this is constructed
    (`memory_manager.app.open_services`), not only once the first write is
    audited. Given explicitly only by `tests/test_audit_export.py`.
    """

    def __init__(self, pool: asyncpg.Pool, *, exporter: AuditExporter | None = None) -> None:
        self._pool = pool
        self._exporter = (
            exporter if exporter is not None else AuditExporter.from_env(dict(os.environ))
        )

    async def record(
        self,
        *,
        actor: str,
        client: str,
        op: str,
        path: str | None,
        commit_sha: str | None,
        outcome: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Insert one `audit_log` row; logs and swallows a DB failure instead of raising.

        `actor` is the token subject (OAuth `sub`), a static token's name,
        or `"stdio"` for a local session; `client` is the committer label
        (`memory_manager.mcp.server.current_client`); `outcome` is one of
        `"ok"`/`"conflict"`/`"rejected"`/`"failed"`. Exported to every
        configured `AUDIT_EXPORT` target unconditionally afterwards, whether
        the insert above succeeded or not (see module docstring).
        """
        detail_value = detail if detail is not None else {}
        at = datetime.now(UTC)
        try:
            await self._pool.execute(
                _INSERT,
                actor,
                client,
                op,
                path,
                commit_sha,
                outcome,
                json.dumps(detail_value),
            )
        except Exception:
            _logger.exception(
                "audit log write failed: actor=%s client=%s op=%s path=%s outcome=%s",
                actor,
                client,
                op,
                path,
                outcome,
            )

        try:
            self._exporter.export(
                {
                    "at": at.isoformat(timespec="milliseconds"),
                    "actor": actor,
                    "client": client,
                    "op": op,
                    "path": path,
                    "outcome": outcome,
                    "detail": detail_value,
                    "request_id": current_request_id(),
                }
            )
        except Exception:
            # Belt and suspenders: `AuditExporter.export` already swallows a
            # failure on every target it knows about itself, but `exporter`
            # here is a plain protocol - a caller-supplied stand-in (tests)
            # could still raise, and a SIEM outage must never fail or undo
            # the DB row just inserted above, same as a DB failure must not
            # skip this export.
            _logger.exception(
                "audit export failed: actor=%s client=%s op=%s path=%s outcome=%s",
                actor,
                client,
                op,
                path,
                outcome,
            )
