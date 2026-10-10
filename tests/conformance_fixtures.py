# SPDX-License-Identifier: AGPL-3.0-only
"""Shared fixtures for the per-profile conformance suite (#136, ADR-0010).

`all_combos()` is the matrix `tests/conformance/profiles/test_profile_conformance.py`
parametrises over: every registered profile (`compat.profiles.profile_names()`) times
every transport/backend pair that actually exists - `stdio`+`git`, `http`+`git`,
`http`+`postgres` (`stdio`+`postgres` does not exist: `cli.py`'s `serve --stdio` refuses
`STORAGE_BACKEND=postgres` outright, ADR-0008 addendum). `open_combination` starts a real
`memory-manager` subprocess for one `Combo`, seeded and ready, and yields a
`ConformanceSession` that holds one already-connected `mcp.Client` per identity the
combination actually has: `"primary"` always; `"read_only"`/`"restricted"` (`"git"`) or
`"read_only"`/`"foreign"` (`"postgres"`) only where a bearer token exists at all - stdio
has none, so a scenario that needs one skips that combination instead of failing
(`ConformanceSession.has_identity`).

Both backends always carry `DATABASE_URL` (ADR-0010's own design decision for this
suite): `"git"` gains bearer-token auth over HTTP (#34) and a derived Postgres index for
`memory_search`'s fulltext mode the same way `"postgres"` always has one - the scan-search
mode (`"git"` without a database) is deliberately out of scope here, already covered by
`tests/conformance/test_http.py`. `EMBEDDING_PROVIDER=none` is set explicitly on every
combination's env, not left to default: `run_http_server` merges the ambient
`os.environ` on top of what is given here, so an operator's shell setting it to something
else would otherwise leak into a supposedly fulltext-only run.

The seeding helpers below (`seed_postgres_notes`, `seed_personal_namespace`,
`create_app_role`, `drop_app_role`) are `tests/conformance/test_http.py`'s own
`_seed_postgres_note`/`_seed_personal_namespace`/`_create_app_role`/`_drop_app_role`,
moved here unchanged in behaviour and generalised to take their path/content and
oid/alias as parameters instead of one hardcoded pair - that module's own fixtures call
them exactly as before. `counter_value`/`tool_calls_total` are that module's
`_counter_value`/`_tool_calls_total`, moved the same way (`tool_calls_total` gained a
`tool` keyword, defaulted to `"memory_index"` - every existing caller only ever checked
that one tool, so this is additive, not a behaviour change).
"""

from __future__ import annotations

import re
import secrets
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import asyncpg
import httpx
import httpx2
from git_fixtures import seed_notes
from http_fixtures import Server as HttpServer
from http_fixtures import run_http_server
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, ListToolsResult

from memory_manager.auth.tokens import ALL_NAMESPACES, MEMORY_ROLES, create_token
from memory_manager.compat.profiles import profile_names
from memory_manager.db.migrate import migrate
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

__all__ = [
    "ARCHIVE_PATH",
    "EDIT_PATH",
    "FOREIGN_PATH_GIT",
    "FOREIGN_PATH_POSTGRES",
    "MISSING_SCOPE_PATH",
    "RESTRICTED_NAMESPACE",
    "SEED_BODY",
    "SEED_PATH",
    "STALE_PATH",
    "SUPERSEDE_NEW_PATH",
    "SUPERSEDE_OLD_PATH",
    "WRITE_PATH",
    "Combo",
    "ConformanceSession",
    "all_combos",
    "counter_value",
    "create_app_role",
    "drop_app_role",
    "open_combination",
    "seed_personal_namespace",
    "seed_postgres_notes",
    "tool_calls_total",
]

_STDIO_CLI_ARGS = ("-m", "memory_manager.cli", "serve", "--stdio")

# DATABASE_URL always turns bearer-token auth on for `/mcp` (#34, ADR-0007 §2) once
# set - unrelated to what any scenario actually checks, but required for the
# subprocess to start at all (#35, ADR-0004).
_PUBLIC_URL = "https://mm.example.test"

_SEED_NOW = datetime(2025, 6, 1, tzinfo=UTC)

#: The read-only table-of-contents note every combination seeds before the server
#: starts - used by the `index`/`search`/`read` scenarios. Every write scenario uses
#: its own path below instead, and never touches this one.
SEED_PATH = "me/fact/conformance-seed.md"
SEED_BODY = "Blue.\n"

#: One dedicated path per write/error scenario (`tests/conformance_scenarios.py`),
#: so no two scenarios - and no scenario and the seed note above - ever race over the
#: same note.
WRITE_PATH = "me/fact/conformance-write.md"
EDIT_PATH = "me/fact/conformance-edit.md"
SUPERSEDE_OLD_PATH = "me/fact/conformance-supersede.md"
SUPERSEDE_NEW_PATH = "me/fact/conformance-supersede-new.md"
ARCHIVE_PATH = "me/fact/conformance-archive.md"
STALE_PATH = "me/fact/conformance-error-stale-version.md"
MISSING_SCOPE_PATH = "me/fact/conformance-error-missing-scope.md"

#: `error-foreign-namespace`'s own path, one per backend (ADR-0010: a golden is per
#: scenario x backend): the `"postgres"` case needs a real, non-`"me"` alias so a
#: second principal can address it by its literal name (`namespaces.ME_ALIAS` always
#: means the *caller's own* personal namespace, never a registered alias that happens
#: to be the literal string `"me"`); the `"git"` case has no namespace registry at
#: all, so it reuses the primary identity's own `"me"` and relies on a namespace-
#: restricted token instead (`RESTRICTED_NAMESPACE` below).
FOREIGN_PATH_POSTGRES = "ns-a/fact/conformance-error-foreign-namespace.md"
FOREIGN_PATH_GIT = "me/fact/conformance-error-foreign-namespace.md"

#: The namespace the `"restricted"` git identity's token is limited to - deliberately
#: not the one `FOREIGN_PATH_GIT`/`WRITE_PATH`/... live in, so a write there is always
#: outside what the token allows.
RESTRICTED_NAMESPACE = "ns-x"

_OID_PRIMARY = "oid-conformance-primary"
_PRIMARY_ALIAS = "me"
_OID_FOREIGN_OWNER = "oid-conformance-a"
_FOREIGN_OWNER_ALIAS = "ns-a"
_OID_FOREIGN_VISITOR = "oid-conformance-b"

_MEMORY_USER = MEMORY_ROLES[0]


def _note(*, title: str, description: str, body: str, tags: tuple[str, ...] = ()) -> bytes:
    note = Note(
        id=new_ulid(_SEED_NOW),
        title=title,
        description=description,
        type="fact",
        created=_SEED_NOW,
        updated=_SEED_NOW,
        body=body,
        tags=tags,
    )
    return serialize(note)


# --- Seeding helpers, moved from tests/conformance/test_http.py (unchanged in
# behaviour; generalised to take their path/content and oid/alias as arguments) -----


async def seed_postgres_notes(database_url: str, items: Mapping[str, bytes]) -> None:
    """Migrate `database_url`, then write every `path -> content` in `items` directly
    through `PostgresBackend` - the `"postgres"` backend's counterpart to
    `git_fixtures.seed_notes`'s single commit onto a bare remote.

    No `app_role` (ADR-0008 addendum, #116): connects, and writes, as the migrating
    owner - the one identity every content table's owner-only policy always lets
    through regardless of namespace, exactly like Git-mode indexing or
    `reindex --full` would.
    """
    migration_conn = await asyncpg.connect(database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    pool = await asyncpg.create_pool(database_url)
    try:
        backend = PostgresBackend(pool)
        for path, content in items.items():
            await backend.write(path, content, if_version="new", client="conformance-seed")
    finally:
        await pool.close()


async def seed_personal_namespace(database_url: str, *, oid: str, alias: str) -> None:
    """Seed `namespaces`/`users` rows so `oid`'s own namespace `alias` is
    readable/writable under RLS (`mm_readable_ns`/`mm_writable_ns`,
    `migrations/0005_rls.sql`) - connects as the test database's owner, which carries
    no RLS on these two membership tables at all.
    """
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into users (oid, tid, display_name) values ($1, 'tenant-conformance', $1)",
            oid,
        )
        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('user', $1, $2)",
            oid,
            alias,
        )
    finally:
        await conn.close()


async def create_app_role(admin_database_url: str) -> str:
    """A disposable, non-owner, non-superuser role for the RLS request path
    (ADR-0008 addendum, #116). Roles are cluster-wide - created against
    `admin_database_url`, not the per-test database - and never granted here: the
    subprocess's own `open_services` does that at startup (`db.rls.grant_app_role`),
    once `DATABASE_APP_ROLE` names it.
    """
    role = f"mm_test_app_{secrets.token_hex(8)}"
    conn = await asyncpg.connect(admin_database_url)
    try:
        await conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
    finally:
        await conn.close()
    return role


async def drop_app_role(admin_database_url: str, database_url: str, role: str) -> None:
    """Undo `create_app_role`, in the order that avoids `DependentObjectsStillExistError`
    (see `tests/test_app.py`'s identical `app_role` fixture for why)."""
    owned_conn: asyncpg.Connection | None
    try:
        owned_conn = await asyncpg.connect(database_url)
    except asyncpg.PostgresError:
        owned_conn = None
    if owned_conn is not None:
        try:
            await owned_conn.execute(f'drop owned by "{role}"')
        finally:
            await owned_conn.close()
    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(f'drop role if exists "{role}"')
    finally:
        await admin_conn.close()


_METRIC_LINE_RE = re.compile(r"^(?P<name>\w+)\{(?P<labels>[^}]*)\}\s+(?P<value>\S+)$")
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def counter_value(text: str, name: str, **labels: str) -> float:
    """The sample value of `name{labels...}` in a Prometheus text-exposition `text`,
    or `0.0` if that exact label set never appeared - the same "absent means zero"
    reading a fresh `Counter` already has before its first `.inc()`.
    """
    for line in text.splitlines():
        match = _METRIC_LINE_RE.match(line)
        if match is None or match["name"] != name:
            continue
        if dict(_LABEL_RE.findall(match["labels"])) == labels:
            return float(match["value"])
    return 0.0


async def tool_calls_total(
    server: HttpServer, *, profile: str, tool: str = "memory_index"
) -> float:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{server.base_url}/metrics")
    return counter_value(
        response.text, "mm_tool_calls_total", tool=tool, outcome="ok", profile=profile
    )


# --- The combination matrix ---------------------------------------------------------

_TRANSPORT_BACKENDS: tuple[tuple[Literal["stdio", "http"], Literal["git", "postgres"]], ...] = (
    ("stdio", "git"),
    ("http", "git"),
    ("http", "postgres"),
)


@dataclass(frozen=True)
class Combo:
    """One cell of the conformance matrix: a profile, run over one transport/backend."""

    profile: str
    transport: Literal["stdio", "http"]
    backend: Literal["git", "postgres"]

    @property
    def id(self) -> str:
        """A `pytest.mark.parametrize` id, e.g. `"claude-ai-http-postgres"`."""
        return f"{self.profile}-{self.transport}-{self.backend}"


def all_combos() -> list[Combo]:
    """Every registered profile times every transport/backend pair that exists."""
    return [
        Combo(profile=profile, transport=transport, backend=backend)
        for profile in profile_names()
        for transport, backend in _TRANSPORT_BACKENDS
    ]


@dataclass
class ConformanceSession:
    """One running combination: an already-connected `mcp.Client` per identity it has.

    `identities` only ever carries the identities this particular combination can
    actually produce - stdio carries no bearer token at all, so it is always just
    `{"primary": ...}`; a scenario that needs `"read_only"`/`"foreign"`/`"restricted"`
    calls `has_identity` first and skips itself (`conformance_scenarios.ScenarioSkipped`)
    rather than failing when it is missing.

    `stdio_params`/`primary_url`/`primary_headers` are deliberately *not* the already-
    opened `identities["primary"]` connection: the delivery check (ADR-0010, both
    protocol revisions) needs two fresh connections of its own, one per revision, which
    an already-handshaken `mode="legacy"` `Client` cannot become. `stdio_params` is set
    exactly for `combo.transport == "stdio"`; `primary_url`/`primary_headers` exactly for
    `"http"` - a caller matches on whichever is not `None`.
    """

    combo: Combo
    identities: dict[str, Client]
    metrics_url: str | None
    stdio_params: StdioServerParameters | None = None
    primary_url: str | None = None
    primary_headers: Mapping[str, str] = field(default_factory=dict)

    def has_identity(self, name: str) -> bool:
        return name in self.identities

    async def call_tool(
        self, identity: str, tool: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        return await self.identities[identity].call_tool(tool, arguments)

    async def list_tools(self, identity: str = "primary") -> ListToolsResult:
        return await self.identities[identity].list_tools()


@dataclass(frozen=True)
class _TokenSpec:
    """What `_open_http_identities` creates one bearer token from."""

    scopes: tuple[str, ...]
    namespaces: tuple[str, ...] = (ALL_NAMESPACES,)
    owner_oid: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)


async def _open_http_identities(
    stack: AsyncExitStack,
    *,
    server: HttpServer,
    profile: str,
    database_url: str,
    specs: Mapping[str, _TokenSpec],
) -> tuple[dict[str, Client], dict[str, str], str]:
    """One bearer token per `specs` entry, each opened as a `mode="legacy"` `mcp.Client`
    against `server`, with `?profile=profile` on the URL (ADR-0010: HTTP always
    selects a profile explicitly, including `"default"`) - entered onto `stack` so the
    caller's own `AsyncExitStack` closes every one of them on teardown.

    Returns the opened clients, the plaintext tokens they were opened with (the
    delivery check's own fresh connections need `"primary"`'s again, `ConformanceSession.
    primary_headers`) and the profile-qualified URL every one of them used.
    """
    pool = await asyncpg.create_pool(database_url)
    try:
        tokens: dict[str, str] = {}
        for name, spec in specs.items():
            plaintext, _info = await create_token(
                pool,
                f"conformance-{name}-{secrets.token_hex(4)}",
                scopes=list(spec.scopes),
                namespaces=list(spec.namespaces),
                owner_oid=spec.owner_oid,
                roles=list(spec.roles),
            )
            tokens[name] = plaintext
    finally:
        await pool.close()

    url = f"{server.mcp_url}?profile={profile}"
    identities: dict[str, Client] = {}
    for name, token in tokens.items():
        transport = streamable_http_client(
            url, http_client=httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        )
        identities[name] = await stack.enter_async_context(Client(transport, mode="legacy"))
    return identities, tokens, url


@asynccontextmanager
async def _open_git_combination(
    combo: Combo, *, tmp_path: Path, bare_remote: Path, test_database_url: str
) -> AsyncIterator[ConformanceSession]:
    seed_notes(
        bare_remote,
        {
            SEED_PATH: _note(
                title="Conformance seed",
                description="Seeded for the conformance suite.",
                body=SEED_BODY,
                tags=("color",),
            )
        },
    )

    env = {
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(tmp_path / "conformance-vault"),
        "STORAGE_BACKEND": "git",
        "DATABASE_URL": test_database_url,
        "EMBEDDING_PROVIDER": "none",
    }

    async with AsyncExitStack() as stack:
        if combo.transport == "stdio":
            params = StdioServerParameters(
                command=sys.executable,
                args=[*_STDIO_CLI_ARGS, "--profile", combo.profile],
                env=env,
            )
            client = await stack.enter_async_context(Client(params, mode="legacy"))
            yield ConformanceSession(
                combo=combo,
                identities={"primary": client},
                metrics_url=None,
                stdio_params=params,
            )
            return

        server = await stack.enter_async_context(
            run_http_server({**env, "PUBLIC_URL": _PUBLIC_URL})
        )
        specs = {
            "primary": _TokenSpec(scopes=(READ_SCOPE, WRITE_SCOPE)),
            "read_only": _TokenSpec(scopes=(READ_SCOPE,)),
            "restricted": _TokenSpec(
                scopes=(READ_SCOPE, WRITE_SCOPE), namespaces=(RESTRICTED_NAMESPACE,)
            ),
        }
        identities, tokens, url = await _open_http_identities(
            stack,
            server=server,
            profile=combo.profile,
            database_url=test_database_url,
            specs=specs,
        )
        yield ConformanceSession(
            combo=combo,
            identities=identities,
            metrics_url=f"{server.base_url}/metrics",
            primary_url=url,
            primary_headers={"Authorization": f"Bearer {tokens['primary']}"},
        )


@asynccontextmanager
async def _open_postgres_combination(
    combo: Combo, *, admin_database_url: str, test_database_url: str
) -> AsyncIterator[ConformanceSession]:
    assert combo.transport == "http", "stdio+postgres does not exist (cli.py refuses it)"

    await seed_postgres_notes(
        test_database_url,
        {
            SEED_PATH: _note(
                title="Conformance seed",
                description="Seeded for the conformance suite.",
                body=SEED_BODY,
                tags=("color",),
            ),
            FOREIGN_PATH_POSTGRES: _note(
                title="Foreign note",
                description="Owned by a different principal (error-foreign-namespace).",
                body="Not yours.\n",
            ),
        },
    )
    await seed_personal_namespace(test_database_url, oid=_OID_PRIMARY, alias=_PRIMARY_ALIAS)
    await seed_personal_namespace(
        test_database_url, oid=_OID_FOREIGN_OWNER, alias=_FOREIGN_OWNER_ALIAS
    )

    role = await create_app_role(admin_database_url)
    try:
        env = {
            "STORAGE_BACKEND": "postgres",
            "DATABASE_URL": test_database_url,
            "DATABASE_APP_ROLE": role,
            "PUBLIC_URL": _PUBLIC_URL,
            "EMBEDDING_PROVIDER": "none",
        }
        async with AsyncExitStack() as stack:
            server = await stack.enter_async_context(run_http_server(env))
            specs = {
                "primary": _TokenSpec(
                    scopes=(READ_SCOPE, WRITE_SCOPE),
                    owner_oid=_OID_PRIMARY,
                    roles=(_MEMORY_USER,),
                ),
                "read_only": _TokenSpec(
                    scopes=(READ_SCOPE,), owner_oid=_OID_PRIMARY, roles=(_MEMORY_USER,)
                ),
                "foreign": _TokenSpec(
                    scopes=(READ_SCOPE, WRITE_SCOPE),
                    owner_oid=_OID_FOREIGN_VISITOR,
                    roles=(_MEMORY_USER,),
                ),
            }
            identities, tokens, url = await _open_http_identities(
                stack,
                server=server,
                profile=combo.profile,
                database_url=test_database_url,
                specs=specs,
            )
            yield ConformanceSession(
                combo=combo,
                identities=identities,
                metrics_url=f"{server.base_url}/metrics",
                primary_url=url,
                primary_headers={"Authorization": f"Bearer {tokens['primary']}"},
            )
    finally:
        await drop_app_role(admin_database_url, test_database_url, role)


@asynccontextmanager
async def open_combination(
    combo: Combo,
    *,
    tmp_path: Path,
    bare_remote: Path,
    test_database_url: str,
    admin_database_url: str,
) -> AsyncIterator[ConformanceSession]:
    """Seed, start and connect to `combo`, tearing everything down again on exit."""
    if combo.backend == "git":
        async with _open_git_combination(
            combo, tmp_path=tmp_path, bare_remote=bare_remote, test_database_url=test_database_url
        ) as session:
            yield session
    else:
        async with _open_postgres_combination(
            combo, admin_database_url=admin_database_url, test_database_url=test_database_url
        ) as session:
            yield session
