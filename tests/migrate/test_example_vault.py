# SPDX-License-Identifier: AGPL-3.0-only
"""The M9 acceptance test (#248, ADR-0007 §6): `examples/vault` migrated with
`migrate git-to-postgres` yields the same `version` for every note when read
back through the MCP server.

`examples/vault` (`personal`/`work`/`shared` plus `_archive/`) is committed
into a throwaway bare remote in one commit (`seed_notes`) and cloned - the
source tree itself is never touched, only read. The CLI then runs as a real
subprocess (`sys.executable -m memory_manager.cli migrate git-to-postgres
--map ...`), the same way a human operator would invoke it, against a fresh
database created from `MM_TEST_DATABASE_URL` - a subprocess sidesteps the
nested-event-loop trap `tests/migrate/test_import.py`'s own `cli_database_url`
fixture works around, so the ordinary async `test_database_url` fixture can
be used directly here.

Once imported, a Postgres-mode `memory-manager serve --http` is started
(`tests/http_fixtures.py`) with a static bearer token owned by `oid-example`
(the same oid `--map personal=user:oid-example` imported the `personal`
namespace under) - `memory_read` is then called, through the real HTTP/MCP
transport, for every note the vault held, under the path the caller sees it
at (`me/...` for the personal notes, `work/...`/`org/...` for the other two,
`_archive/...` for the four archived ones), and each returned `version` is
compared against `vault.note.version` of the exact bytes `git` checked out -
the migration's own "byte-identical to what is on disk" guarantee, this time
proven end to end through the server a client actually talks to, not just
against the database directly.

Archived notes and revision counts (one commit, so exactly one revision per
note) are checked directly against Postgres instead - `tests/migrate/
test_import.py` already exercises revision history in depth with a
multi-commit vault; this module's own job is proving *this* vault's shape,
not re-proving the general mechanism.

`work`'s `project` namespace needs a `project_members` row before `oid-example`
can read it at all (ADR-0008's membership-gated project namespace) - there is
no CLI for that yet, so this test inserts it directly, the same way an
operator currently has to.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from git_fixtures import seed_notes
from http_fixtures import Server, run_http_server

from memory_manager.auth.tokens import ALL_NAMESPACES, MEMORY_ROLES, create_token
from memory_manager.db import rls
from memory_manager.mcp.authz import READ_SCOPE
from memory_manager.vault.git import Git
from memory_manager.vault.note import version as note_version
from memory_manager.vault.paths import iter_md_files, parse_note_path

_EXAMPLES_VAULT = Path(__file__).resolve().parents[2] / "examples" / "vault"

_OID = "oid-example"
_OWNER_ROLE = MEMORY_ROLES[0]  # "Memory.User"
_PUBLIC_URL = "https://mm-example-vault.example.test"

#: Every top-level Git namespace `examples/vault` holds, to the alias a
#: caller sees it under once imported (`ME_ALIAS` for the personal one,
#: `mcp/namespaces.py`'s own translation - the other two keep the plain
#: stored alias, `work`'s default and `org`'s fixed one).
_DISPLAY_ALIAS = {"personal": "me", "work": "work", "shared": "org"}

_MAP_ARGS = [
    "--map",
    "personal=user:oid-example",
    "--map",
    "work=project:work",
    "--map",
    "shared=org:org",
]

_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _collect_example_notes() -> dict[str, bytes]:
    """Every `.md` file under `examples/vault`, keyed by its vault-relative path.

    Read-only: `examples/vault` must never be modified (CLAUDE.md).
    """
    return {
        file_path.relative_to(_EXAMPLES_VAULT).as_posix(): file_path.read_bytes()
        for file_path in iter_md_files(_EXAMPLES_VAULT)
    }


def _display_path(rel: str) -> str:
    """`rel` (a Git-relative path, archive included) as the path a client sees
    it at once imported - `mcp/namespaces.py`'s `rewrite_path_to_display`,
    computed by hand here since there is no `Resolution` without a live
    request.
    """
    note_path = parse_note_path(rel, allow_archive=True)
    alias = _DISPLAY_ALIAS[note_path.namespace]
    return replace(note_path, namespace=alias).relative


def _chunked(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class _ToolCallOutcome:
    def __init__(self, status_code: int, result: dict[str, Any] | None) -> None:
        self.status_code = status_code
        self.result = result


async def _call_tool(
    client: httpx.AsyncClient, server: Server, token: str, tool: str, arguments: dict[str, object]
) -> _ToolCallOutcome:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {token}"}
    response = await client.post(server.mcp_url, json=body, headers=headers)
    if response.status_code != 200:
        return _ToolCallOutcome(status_code=response.status_code, result=None)
    payload = response.json()
    assert "error" not in payload, payload
    result = payload["result"]
    assert isinstance(result, dict)
    return _ToolCallOutcome(status_code=200, result=result)


async def test_example_vault_migrates_with_identical_versions(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    notes = _collect_example_notes()
    assert notes, "examples/vault yielded no notes - check the fixture path"

    seed_notes(bare_remote, notes)
    vault_dir = tmp_path / "vault"
    Git(cwd=tmp_path).run("clone", "--origin", "origin", str(bare_remote), str(vault_dir))

    # --- run the CLI as a real subprocess, as an operator would --------------

    env = {**os.environ, "DATABASE_URL": test_database_url}
    result = subprocess.run(  # noqa: S603 - fixed executable, argument list, no shell
        [
            sys.executable,
            "-m",
            "memory_manager.cli",
            "migrate",
            "git-to-postgres",
            "--vault",
            str(vault_dir),
            *_MAP_ARGS,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "3 namespace(s) imported, 0 refused" in result.stdout

    # --- archived notes and revision counts, straight against Postgres ------

    conn = await asyncpg.connect(test_database_url)
    try:
        total_notes = await conn.fetchval("select count(*) from vault_notes")
        total_revisions = await conn.fetchval("select count(*) from vault_revisions")
        assert total_notes == len(notes)
        # One seed commit touched every file exactly once.
        assert total_revisions == len(notes)

        archived_in_vault = sum(1 for rel in notes if rel.startswith("_archive/"))
        archived_in_db = await conn.fetchval(
            "select count(*) from vault_notes where path like '_archive/%'"
        )
        assert archived_in_db == archived_in_vault > 0

        work_namespace_id = await conn.fetchval(
            "select id from namespaces where kind = 'project' and alias = 'work'"
        )
        assert work_namespace_id is not None
        await conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', $2, 'owner')",
            work_namespace_id,
            _OID,
        )
    finally:
        await conn.close()

    # --- grant a fresh app role the request path needs (ADR-0008 addendum) --

    owner_conn = await asyncpg.connect(test_database_url)
    app_role = f"mm_test_example_vault_{os.urandom(8).hex()}"
    try:
        await owner_conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
        await rls.grant_app_role(owner_conn, app_role)

        pool = await asyncpg.create_pool(test_database_url)
        try:
            token, _info = await create_token(
                pool,
                "example-vault-check",
                scopes=[READ_SCOPE],
                namespaces=[ALL_NAMESPACES],
                owner_oid=_OID,
                roles=[_OWNER_ROLE],
            )
        finally:
            await pool.close()

        server_env = {
            "STORAGE_BACKEND": "postgres",
            "DATABASE_URL": test_database_url,
            "DATABASE_APP_ROLE": app_role,
            "PUBLIC_URL": _PUBLIC_URL,
        }

        # --- memory_read, through the real HTTP/MCP transport, for every note

        expected_version: dict[str, str] = {}
        for rel in notes:
            expected_version[_display_path(rel)] = note_version((vault_dir / rel).read_bytes())
        display_items = list(expected_version)

        async with (
            run_http_server(server_env) as server,
            httpx.AsyncClient(timeout=10.0) as client,
        ):
            for chunk in _chunked(display_items, 20):
                outcome = await _call_tool(client, server, token, "memory_read", {"items": chunk})
                assert outcome.result is not None and outcome.result["isError"] is False
                items = outcome.result["structuredContent"]["result"]
                assert len(items) == len(chunk)
                for item in items:
                    assert "error" not in item, item
                    display_path = item["path"]
                    assert item["version"] == expected_version[display_path], display_path
    finally:
        # `grant_app_role` granted this role privileges in the current
        # (throwaway) database; `drop owned by` revokes them so `drop role`
        # below does not fail with `DependentObjectsStillExistError` - the
        # database itself is dropped later, by `test_database_url`'s own
        # teardown, after this role is already gone.
        await owner_conn.execute(f'drop owned by "{app_role}"')
        await owner_conn.execute(f'drop role if exists "{app_role}"')
        await owner_conn.close()
