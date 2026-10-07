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
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

__all__ = ["AuditWriter"]

_logger = logging.getLogger(__name__)

_INSERT = """
insert into audit_log (actor, client, op, path, commit_sha, outcome, detail)
values ($1, $2, $3, $4, $5, $6, $7::jsonb)
"""


class AuditWriter:
    """Inserts one row into `audit_log` per `record()` call, against `pool`."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

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
        `"ok"`/`"conflict"`/`"rejected"`/`"failed"`.
        """
        try:
            await self._pool.execute(
                _INSERT,
                actor,
                client,
                op,
                path,
                commit_sha,
                outcome,
                json.dumps(detail if detail is not None else {}),
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
