# SPDX-License-Identifier: AGPL-3.0-only
"""Erasure: hard-deletes a note, a namespace or a user (ADR-0007 §3 + addendum
2026-10-08, #231).

A separate, non-MCP, admin-only primitive - never reachable from a write
(CLAUDE.md: "no MCP tool hard-deletes notes"; "erasure exists only with the
postgres backend, outside MCP"). `PostgresBackend.erase` (`storage/
postgres.py`) is the one caller today; the actual admin/self-service entry
points that invoke it are future work (issue #231's own "Callers: 25f, 26d,
26h").

Every `erase_note`/`erase_namespace`/`erase_user` call below runs in exactly
one transaction, on a connection the caller already owns (never acquired or
committed here): a failure partway through rolls back every delete, the
`erasure_log` row and the `audit_log` row together - `erasure_log` never
records a partial erasure. The connection is expected to already be the
owner role (ADR-0008 addendum #100, "system identity") - never one
`db.rls.request_connection` switched to the app role: the app role holds no
grant at all on `users`, `static_tokens`, `oauth_auth_codes`, `oauth_tokens`
or `erasure_log` (`db/rls.py`'s `grant_app_role` never mentions any of
them), so running this under that role fails outright on the first
statement that touches one of those tables with `asyncpg.
InsufficientPrivilegeError`, rather than silently doing less than it claims.

`jobs` (#218/WP-23): payloads carry `{"note_id": ..., "version": ...}` only
(`jobs.py`'s own module docstring, "nothing here needs scrubbing beyond the
row itself") - deleted here by matching `payload->>'note_id'`, never
re-interpreted as anything else.

`audit_log` (CLAUDE.md "audited with metadata only"; ADR-0007 §3 addendum:
"the path is redacted as well, because a slug can carry personal data"):
every row whose `path` falls under an erased note/namespace is redacted in
place, never deleted outright - the row (actor, outcome, timestamp) is
still useful for an incident review, just without the personal data a path
or a stray `detail` key could carry. `detail` is filtered down to
`audit.DETAIL_ALLOWLIST` - that module's own account of every key any
`AuditWriter.record` caller in this codebase actually puts into `detail`,
documented there since it is a fact about every one of those call sites,
not just about this one's consumption of it.

`namespaces` (ADR-0007 §3 addendum 2026-10-08: "the personal namespace ...
and the user's identity rows, are hard-deleted"): `erase_user` removes its
personal namespace's own registry row (`kind = 'user'`), and `erase_namespace`
the row for whichever alias it was called with - both only once that
namespace's content is already gone, since nothing above keys off
`namespaces.id` (every content table carries the alias as plain text, not a
foreign key to it). `project_members`/`namespace_settings`/
`break_glass_grants` cascade from that row (`0005_rls.sql`'s own `on delete
cascade`), so a group/project namespace's membership and write-policy rows
disappear with it too - correct for "erase this namespace", wrong only if
this were erasing a user's *shared* membership elsewhere, which it never
does (a user target only ever resolves their own personal namespace's row).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import asyncpg

from memory_manager.audit import DETAIL_ALLOWLIST
from memory_manager.storage.base import ErasureResult, ErasureTargetKind, NotFound

__all__ = ["erase_namespace", "erase_note", "erase_user"]

# A connection acquired from a pool (already switched to the owner role, or a
# plain pool connection with no role switch at all - `storage.postgres.
# PostgresBackend.erase`'s own `self._pool.acquire()`, never `_content_
# connection`/`db.rls.request_connection`) or a bare `asyncpg.Connection`
# (every test in this package) - same idea as `storage.postgres`'s own
# `_Connectable`.
_Connectable = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

#: `audit_log.path` for a redacted row - distinct from `NULL` (which already
#: means "no particular path", e.g. a namespace-level `migrate_git.py`
#: rejection): this value says a path *was* recorded here once, and has
#: deliberately been removed.
_REDACTED_PATH = "[erased]"

_SELECT_PERSONAL_ALIAS = """
select alias from namespaces where kind = 'user' and external_key = $1
"""

_DELETE_NAMESPACE_REGISTRY_ROW = """
delete from namespaces where alias = $1 returning id
"""

_REDACT_AUDIT_FOR_PATHS = """
update audit_log a
set path = $2,
    detail = coalesce(
        (select jsonb_object_agg(e.key, e.value)
         from jsonb_each(a.detail) e
         where e.key = any($3::text[])),
        '{}'::jsonb
    )
where a.path = any($1::text[])
returning a.id
"""

_REDACT_AUDIT_FOR_NAMESPACE = """
update audit_log a
set path = $2,
    detail = coalesce(
        (select jsonb_object_agg(e.key, e.value)
         from jsonb_each(a.detail) e
         where e.key = any($3::text[])),
        '{}'::jsonb
    )
where a.path like $1 || '/%' or a.path like '_archive/' || $1 || '/%'
returning a.id
"""

_INSERT_ERASURE_LOG = """
insert into erasure_log (actor, reason, target_kind, target_ids, row_counts)
values ($1, $2, $3, $4, $5::jsonb)
returning id
"""

_INSERT_AUDIT_ERASURE = """
insert into audit_log (actor, client, op, path, commit_sha, outcome, detail)
values ($1, 'erasure', 'erasure', null, null, 'ok', $2::jsonb)
"""


@dataclass(frozen=True)
class _NamespaceContentCounts:
    """Row counts of what `_erase_namespace_content` removed, keyed exactly
    like `ErasureResult.row_counts`/`erasure_log.row_counts`."""

    vault_notes: int
    vault_revisions: int
    notes: int
    chunks: int
    links: int
    jobs: int

    def as_dict(self) -> dict[str, int]:
        return {
            "vault_notes": self.vault_notes,
            "vault_revisions": self.vault_revisions,
            "notes": self.notes,
            "chunks": self.chunks,
            "links": self.links,
            "jobs": self.jobs,
        }


async def _erase_namespace_content(conn: _Connectable, namespace: str) -> _NamespaceContentCounts:
    """Hard-delete every note `namespace` holds, archived included, plus its
    jobs - the shared core of `erase_namespace` and `erase_user`.

    Counts are taken *before* each cascading delete (`vault_notes` ->
    `vault_revisions`, `notes` -> `chunks`/`links`): once the parent row is
    gone, Postgres has already removed the children too, with nothing left
    to count. `vault_notes`/`notes` ids are gathered separately (they
    should always coincide one-for-one, but nothing here assumes it) so
    `jobs` is matched against the union of both.
    """
    vault_note_ids = {
        row["id"]
        for row in await conn.fetch("select id from vault_notes where namespace = $1", namespace)
    }
    note_ids = {
        row["id"]
        for row in await conn.fetch("select id from notes where namespace = $1", namespace)
    }
    all_ids = vault_note_ids | note_ids

    vault_revisions_count = (
        await conn.fetchval(
            "select count(*) from vault_revisions where note_id = any($1::text[])",
            list(vault_note_ids),
        )
        if vault_note_ids
        else 0
    )
    chunks_count = (
        await conn.fetchval(
            "select count(*) from chunks where note_id = any($1::text[])", list(note_ids)
        )
        if note_ids
        else 0
    )
    links_count = (
        await conn.fetchval(
            "select count(*) from links where source_id = any($1::text[])", list(note_ids)
        )
        if note_ids
        else 0
    )
    jobs_rows = (
        await conn.fetch(
            "delete from jobs where payload->>'note_id' = any($1::text[]) returning id",
            list(all_ids),
        )
        if all_ids
        else []
    )

    vault_notes_rows = await conn.fetch(
        "delete from vault_notes where namespace = $1 returning id", namespace
    )
    notes_rows = await conn.fetch("delete from notes where namespace = $1 returning id", namespace)

    return _NamespaceContentCounts(
        vault_notes=len(vault_notes_rows),
        vault_revisions=int(vault_revisions_count or 0),
        notes=len(notes_rows),
        chunks=int(chunks_count or 0),
        links=int(links_count or 0),
        jobs=len(jobs_rows),
    )


async def _redact_audit_for_paths(conn: _Connectable, paths: list[str]) -> int:
    if not paths:
        return 0
    rows = await conn.fetch(_REDACT_AUDIT_FOR_PATHS, paths, _REDACTED_PATH, list(DETAIL_ALLOWLIST))
    return len(rows)


async def _redact_audit_for_namespace(conn: _Connectable, namespace: str) -> int:
    rows = await conn.fetch(
        _REDACT_AUDIT_FOR_NAMESPACE, namespace, _REDACTED_PATH, list(DETAIL_ALLOWLIST)
    )
    return len(rows)


async def _delete_namespace_registry_row(conn: _Connectable, alias: str) -> int:
    """Delete `namespaces`' own row for `alias` (module docstring's last
    paragraph) - cascades `project_members`/`namespace_settings`/
    `break_glass_grants` for it, if any."""
    rows = await conn.fetch(_DELETE_NAMESPACE_REGISTRY_ROW, alias)
    return len(rows)


async def _write_erasure_log(
    conn: _Connectable,
    *,
    actor: str,
    reason: str,
    target_kind: ErasureTargetKind,
    target_ids: list[str],
    row_counts: dict[str, int],
) -> int:
    log_id = await conn.fetchval(
        _INSERT_ERASURE_LOG,
        actor,
        reason,
        target_kind,
        target_ids,
        json.dumps(row_counts),
    )
    await conn.execute(
        _INSERT_AUDIT_ERASURE,
        actor,
        json.dumps(
            {
                "erasure_log_id": log_id,
                "target_kind": target_kind,
                "target_ids": target_ids,
                "row_counts": row_counts,
            }
        ),
    )
    return int(log_id)


async def erase_note(conn: _Connectable, note_id: str, *, actor: str, reason: str) -> ErasureResult:
    """Hard-delete one note everywhere it lives, in one transaction.

    Raises `NotFound` if `note_id` exists in neither `vault_notes` nor
    `notes` - nothing is written in that case, not even `erasure_log`.
    `audit_log` is redacted by exact path match against every path
    `vault_revisions` ever recorded for this note (including an archived
    move), not by namespace - a single note's erasure must never touch
    another note's audit trail.
    """
    async with conn.transaction():
        revision_paths = [
            row["path"]
            for row in await conn.fetch(
                "select distinct path from vault_revisions where note_id = $1", note_id
            )
        ]

        chunks_count = await conn.fetchval(
            "select count(*) from chunks where note_id = $1", note_id
        )
        links_count = await conn.fetchval(
            "select count(*) from links where source_id = $1", note_id
        )
        jobs_rows = await conn.fetch(
            "delete from jobs where payload->>'note_id' = $1 returning id", note_id
        )
        vault_notes_rows = await conn.fetch(
            "delete from vault_notes where id = $1 returning id", note_id
        )
        notes_rows = await conn.fetch("delete from notes where id = $1 returning id", note_id)

        if not vault_notes_rows and not notes_rows:
            raise NotFound(note_id)

        audit_redacted = await _redact_audit_for_paths(conn, revision_paths)

        row_counts = {
            "vault_notes": len(vault_notes_rows),
            "vault_revisions": len(revision_paths),
            "notes": len(notes_rows),
            "chunks": int(chunks_count or 0),
            "links": int(links_count or 0),
            "jobs": len(jobs_rows),
            "audit_log": audit_redacted,
        }
        log_id = await _write_erasure_log(
            conn,
            actor=actor,
            reason=reason,
            target_kind="note",
            target_ids=[note_id],
            row_counts=row_counts,
        )

    return ErasureResult(
        target_kind="note",
        target_ids=(note_id,),
        row_counts=row_counts,
        erasure_log_id=log_id,
    )


async def erase_namespace(
    conn: _Connectable, namespace: str, *, actor: str, reason: str
) -> ErasureResult:
    """Hard-delete every note in `namespace`, archived included, plus the
    `namespaces` registry row itself, in one transaction.

    `namespace` is erased in full (ADR-0007 §3's "the note or namespace,
    all revisions, chunks and jobs"; its own registry row too, module
    docstring's last paragraph) - content first, the row itself last, since
    nothing in between keys off `namespaces.id`.
    """
    async with conn.transaction():
        counts = await _erase_namespace_content(conn, namespace)
        audit_redacted = await _redact_audit_for_namespace(conn, namespace)
        namespaces_deleted = await _delete_namespace_registry_row(conn, namespace)

        row_counts = counts.as_dict()
        row_counts["audit_log"] = audit_redacted
        row_counts["namespaces"] = namespaces_deleted

        log_id = await _write_erasure_log(
            conn,
            actor=actor,
            reason=reason,
            target_kind="namespace",
            target_ids=[namespace],
            row_counts=row_counts,
        )

    return ErasureResult(
        target_kind="namespace",
        target_ids=(namespace,),
        row_counts=row_counts,
        erasure_log_id=log_id,
    )


async def erase_user(conn: _Connectable, oid: str, *, actor: str, reason: str) -> ErasureResult:
    """Hard-delete `oid`'s personal namespace and identity, in one transaction.

    Owner decision 2026-10-08 (ADR-0007 §3 addendum): "the personal
    namespace with every note, revision, chunk and job, and the user's
    identity rows, are hard-deleted" - its own `namespaces` registry row
    (`kind = 'user', external_key = oid`) included, not only its content:
    `external_key` is the raw `oid`, a persistent identifier that must not
    survive the erasure it names. Notes `oid` wrote in a *shared* namespace
    stay - they belong to the group, project or org, not to this one user -
    but every `vault_revisions` row `oid` authored there has its
    `author`/`author_oid` set to the literal `'erased'` (an `'erased'`
    author counts as foreign for curate, ADR-0008 addendum "curate is
    author-based"). A user with no personal namespace yet
    (`mm_ensure_personal_ns()` lazily creates one on first write, #101) is
    not an error - every content/namespace count is simply zero.
    """
    async with conn.transaction():
        alias = await conn.fetchval(_SELECT_PERSONAL_ALIAS, oid)

        if alias is not None:
            counts = await _erase_namespace_content(conn, alias)
            audit_redacted = await _redact_audit_for_namespace(conn, alias)
            namespaces_deleted = await _delete_namespace_registry_row(conn, alias)
        else:
            counts = _NamespaceContentCounts(0, 0, 0, 0, 0, 0)
            audit_redacted = 0
            namespaces_deleted = 0

        pseudonymized_rows = await conn.fetch(
            "update vault_revisions set author = 'erased', author_oid = 'erased' "
            "where author_oid = $1 returning note_id",
            oid,
        )
        oauth_codes_rows = await conn.fetch(
            "delete from oauth_auth_codes where user_oid = $1 returning code_hash", oid
        )
        oauth_tokens_rows = await conn.fetch(
            "delete from oauth_tokens where user_oid = $1 returning token_hash", oid
        )
        static_tokens_rows = await conn.fetch(
            "delete from static_tokens where owner_oid = $1 returning id", oid
        )
        user_groups_rows = await conn.fetch(
            "delete from user_groups where oid = $1 returning group_id", oid
        )
        users_rows = await conn.fetch("delete from users where oid = $1 returning oid", oid)

        row_counts = counts.as_dict()
        row_counts["audit_log"] = audit_redacted
        row_counts["namespaces"] = namespaces_deleted
        row_counts["vault_revisions_pseudonymized"] = len(pseudonymized_rows)
        row_counts["oauth_auth_codes"] = len(oauth_codes_rows)
        row_counts["oauth_tokens"] = len(oauth_tokens_rows)
        row_counts["static_tokens"] = len(static_tokens_rows)
        row_counts["user_groups"] = len(user_groups_rows)
        row_counts["users"] = len(users_rows)

        log_id = await _write_erasure_log(
            conn,
            actor=actor,
            reason=reason,
            target_kind="user",
            target_ids=[oid],
            row_counts=row_counts,
        )

    return ErasureResult(
        target_kind="user",
        target_ids=(oid,),
        row_counts=row_counts,
        erasure_log_id=log_id,
    )
