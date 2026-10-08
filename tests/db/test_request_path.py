# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the request path's role switch (ADR-0008 addendum, #116).

Wires `db.rls.request_connection`/`check_app_role` into
`storage.postgres.PostgresBackend` and proves the structural guarantee the
addendum asks for: once `app_role` is configured, a request-serving
transaction can never touch a `FORCE ROW LEVEL SECURITY` content table on a
connection that has not switched to the app role and the caller's own
identity.

`request_path_db` mirrors `tests/db/test_rls.py:61-227`'s non-superuser
owner shape (`mm`, the connection `MM_TEST_DATABASE_URL` names, is a
superuser locally - `grant_app_role`/`check_app_role`'s own checks are only
meaningful against a non-superuser owner, and `pg_has_role` always answers
`true` for a superuser regardless of any actual grant). `TestSuperuserOwner`
below is the deliberate exception: it uses the plain `test_database_url`
fixture (owned by `mm`) on purpose, to prove the addendum's own claim that
even a superuser *session* only sees permitted rows once it has switched
role within a transaction - `mm`'s superuser bit does not leak through the
switch.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio
from mcp.server.auth.provider import AccessToken
from storage.contract import note_bytes

from memory_manager.db import rls
from memory_manager.db.migrate import migrate
from memory_manager.index.indexer import Indexer, VaultNotesSource
from memory_manager.search import fulltext_search
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.ulid import new_ulid

_MEMORY_USER = "Memory.User"


@dataclass(frozen=True)
class RequestPathDb:
    owner_url: str
    app_role: str
    pool: asyncpg.Pool


@asynccontextmanager
async def _request_path_db(
    admin_database_url: str, *, pre_grant: bool
) -> AsyncIterator[RequestPathDb]:
    """A non-superuser owner, migrated, with one app role created.

    `pre_grant=True` is `request_path_db` below's own shape: the app role
    already holds `grant_app_role`'s grant (#116), the way a replica that
    already passed startup sees it. `pre_grant=False` is `ungranted_app_role_db`'s
    shape instead - the role exists and the owner is a member of it (what
    `check_app_role` needs to pass), but nothing has granted it yet - the
    state several replicas starting at once race over (#123).
    """
    db_name = f"mm_test_reqpath_{secrets.token_hex(8)}"
    owner_role = f"mm_test_owner_{secrets.token_hex(8)}"
    owner_password = secrets.token_urlsafe(16)
    app_role = f"mm_test_app_{secrets.token_hex(8)}"

    admin_conn = await asyncpg.connect(admin_database_url)
    owner_conn: asyncpg.Connection | None = None
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(
            f"create role \"{owner_role}\" login password '{owner_password}' nosuperuser"
        )
        await admin_conn.execute(f'create database "{db_name}" owner "{owner_role}"')
        await admin_conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
        await admin_conn.execute(f'grant "{app_role}" to "{owner_role}"')

        parsed = urlsplit(admin_database_url)
        base, _, _ = admin_database_url.rpartition("/")
        bootstrap_conn = await asyncpg.connect(f"{base}/{db_name}")
        try:
            await bootstrap_conn.execute("create extension if not exists vector")
        finally:
            await bootstrap_conn.close()

        owner_url = urlunsplit(
            (
                parsed.scheme,
                f"{owner_role}:{owner_password}@{parsed.hostname}:{parsed.port}",
                f"/{db_name}",
                "",
                "",
            )
        )
        owner_conn = await asyncpg.connect(owner_url)
        await migrate(owner_conn)
        if pre_grant:
            await rls.grant_app_role(owner_conn, app_role)

        pool = await asyncpg.create_pool(owner_url)
        yield RequestPathDb(owner_url=owner_url, app_role=app_role, pool=pool)
    finally:
        if pool is not None:
            await pool.close()
        if owner_conn is not None:
            await owner_conn.close()
        await admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'drop database if exists "{db_name}"')
        await admin_conn.execute(f'drop role if exists "{app_role}"')
        await admin_conn.execute(f'drop role if exists "{owner_role}"')
        await admin_conn.close()


@pytest_asyncio.fixture
async def request_path_db(admin_database_url: str) -> AsyncIterator[RequestPathDb]:
    """A non-superuser owner, migrated, with one app role granted (#116)."""
    async with _request_path_db(admin_database_url, pre_grant=True) as db:
        yield db


@pytest_asyncio.fixture
async def ungranted_app_role_db(admin_database_url: str) -> AsyncIterator[RequestPathDb]:
    """Same shape as `request_path_db`, but nothing has granted the app role
    yet - the state several replicas starting at once race `grant_app_role`
    over (#123)."""
    async with _request_path_db(admin_database_url, pre_grant=False) as db:
        yield db


async def _seed_personal_namespace(owner_url: str, *, oid: str, alias: str) -> None:
    conn = await asyncpg.connect(owner_url)
    try:
        await conn.execute(
            "insert into users (oid, tid, display_name) values ($1, 'tenant-test', $1)", oid
        )
        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('user', $1, $2)",
            oid,
            alias,
        )
    finally:
        await conn.close()


def _as_principal(monkeypatch: pytest.MonkeyPatch, *, oid: str, roles: list[str]) -> None:
    """Make `db.rls.current_principal()` resolve to `oid`/`roles` for this test."""
    token = AccessToken(
        token="mm_x",  # noqa: S106 - a fake test token, not a credential
        client_id="static:test",
        scopes=[],
        claims={"oid": oid, "roles": roles},
    )
    monkeypatch.setattr(rls, "get_access_token", lambda: token)


def _no_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rls, "get_access_token", lambda: None)


class _ExplodingPool:
    """An `asyncpg.Pool` stand-in that fails the test if `acquire` is ever called.

    Used to prove a `NoPrincipal` refusal happens *before* any connection is
    acquired at all - the structural guarantee #116 asks for (`db.rls.
    request_connection`'s own docstring).
    """

    def acquire(self) -> object:
        raise AssertionError(
            "content access must never call pool.acquire() directly in request mode"
        )


class TestWriteReadListAndSearchRunUnderTheAppRole:
    async def test_write_and_its_index_hook_run_as_the_app_role(
        self, request_path_db: RequestPathDb, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        oid = "oid-writer"
        await _seed_personal_namespace(request_path_db.owner_url, oid=oid, alias="personal")
        _as_principal(monkeypatch, oid=oid, roles=[_MEMORY_USER])

        observed_current_user: list[str] = []

        async def hook(conn: object, path: str, content: bytes) -> None:
            observed_current_user.append(await conn.fetchval("select current_user"))  # type: ignore[attr-defined]

        backend = PostgresBackend(
            request_path_db.pool, app_role=request_path_db.app_role, index_hook=hook
        )

        result = await backend.write(
            "personal/fact/one.md",
            note_bytes(id=new_ulid(datetime.now(UTC)), title="One"),
            if_version="new",
            client="test",
        )

        assert result.version
        assert observed_current_user == [request_path_db.app_role]

    async def test_read_and_list_see_only_the_principals_readable_namespaces(
        self, request_path_db: RequestPathDb, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seeded as the plain owner (no app_role): bypasses RLS entirely, the
        # same way Git-mode indexing or `reindex --full` would.
        owner_backend = PostgresBackend(request_path_db.pool)
        await owner_backend.write(
            "personal/fact/mine.md",
            note_bytes(id=new_ulid(datetime.now(UTC)), title="Mine"),
            if_version="new",
            client="seed",
        )
        await owner_backend.write(
            "other/fact/theirs.md",
            note_bytes(id=new_ulid(datetime.now(UTC)), title="Theirs"),
            if_version="new",
            client="seed",
        )

        oid = "oid-reader"
        await _seed_personal_namespace(request_path_db.owner_url, oid=oid, alias="personal")
        _as_principal(monkeypatch, oid=oid, roles=[_MEMORY_USER])

        backend = PostgresBackend(request_path_db.pool, app_role=request_path_db.app_role)

        mine = await backend.read("personal/fact/mine.md")
        assert mine is not None

        # Exists (seeded above, as the owner) but not in this principal's
        # readable set - a plain owner connection would see it; this one must
        # not, proving the connection really switched role, not bypassed it.
        theirs = await backend.read("other/fact/theirs.md")
        assert theirs is None

        listed = {entry.path for entry in await backend.list()}
        assert listed == {"personal/fact/mine.md"}

    async def test_search_over_a_request_connection_is_restricted_to_readable_namespaces(
        self, request_path_db: RequestPathDb, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An `Indexer` wired as `index_hook`, like `app.py` wires one, so the
        # seeded writes below actually populate `notes`/`chunks` - without
        # one, `PostgresBackend.write` only ever touches `vault_notes`.
        indexer = Indexer(request_path_db.pool, VaultNotesSource())
        try:
            owner_backend = PostgresBackend(
                request_path_db.pool, index_hook=indexer.index_on_connection
            )
            await owner_backend.write(
                "personal/fact/aardvark.md",
                note_bytes(
                    id=new_ulid(datetime.now(UTC)),
                    title="Aardvark",
                    body="A note about an aardvark.\n",
                ),
                if_version="new",
                client="seed",
            )
            await owner_backend.write(
                "other/fact/aardvark-two.md",
                note_bytes(
                    id=new_ulid(datetime.now(UTC)),
                    title="Aardvark Two",
                    body="Another note about an aardvark.\n",
                ),
                if_version="new",
                client="seed",
            )
        finally:
            await indexer.aclose()

        oid = "oid-searcher"
        await _seed_personal_namespace(request_path_db.owner_url, oid=oid, alias="personal")
        _as_principal(monkeypatch, oid=oid, roles=[_MEMORY_USER])

        async with rls.request_connection(
            request_path_db.pool, role=request_path_db.app_role
        ) as conn:
            hits = await fulltext_search(conn, "aardvark")

        assert {hit.path for hit in hits} == {"personal/fact/aardvark.md"}


class TestNoPrincipalFailsClosed:
    async def test_request_connection_raises_before_acquiring_a_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_principal(monkeypatch)
        with pytest.raises(rls.NoPrincipal):
            async with rls.request_connection(
                cast(asyncpg.Pool, _ExplodingPool()), role="irrelevant"
            ):
                pytest.fail("must never yield a connection without a principal")  # pragma: no cover

    async def test_postgresbackend_read_raises_before_touching_the_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_principal(monkeypatch)
        backend = PostgresBackend(cast(asyncpg.Pool, _ExplodingPool()), app_role="irrelevant")
        with pytest.raises(rls.NoPrincipal):
            await backend.read("personal/fact/x.md")

    async def test_postgresbackend_write_raises_before_touching_the_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_principal(monkeypatch)
        backend = PostgresBackend(cast(asyncpg.Pool, _ExplodingPool()), app_role="irrelevant")
        with pytest.raises(rls.NoPrincipal):
            await backend.write(
                "personal/fact/x.md",
                note_bytes(id=new_ulid(datetime.now(UTC))),
                if_version="new",
                client="test",
            )

    async def test_a_legacy_token_with_no_oid_claim_resolves_to_no_principal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = AccessToken(
            token="mm_legacy",  # noqa: S106 - a fake test token, not a credential
            client_id="static:legacy",
            scopes=[],
            claims={"namespaces": ["*"]},
        )
        monkeypatch.setattr(rls, "get_access_token", lambda: token)

        assert rls.current_principal() is None


class TestCheckAppRoleStartupGate:
    async def test_refuses_a_role_that_does_not_exist(self, conn: asyncpg.Connection) -> None:
        with pytest.raises(rls.RoleRefused, match="does not exist"):
            await rls.check_app_role(conn, "no-such-role")

    async def test_refuses_a_superuser_role(
        self, admin_database_url: str, conn: asyncpg.Connection
    ) -> None:
        role = f"mm_test_super_{secrets.token_hex(8)}"
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(f'create role "{role}" superuser nologin')
            with pytest.raises(rls.RoleRefused, match="superuser"):
                await rls.check_app_role(conn, role)
        finally:
            await admin_conn.execute(f'drop role if exists "{role}"')
            await admin_conn.close()

    async def test_refuses_a_bypassrls_role(
        self, admin_database_url: str, conn: asyncpg.Connection
    ) -> None:
        role = f"mm_test_bypass_{secrets.token_hex(8)}"
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(f'create role "{role}" bypassrls nologin nosuperuser')
            with pytest.raises(rls.RoleRefused, match="BYPASSRLS"):
                await rls.check_app_role(conn, role)
        finally:
            await admin_conn.execute(f'drop role if exists "{role}"')
            await admin_conn.close()

    async def test_refuses_the_owner_itself(self, request_path_db: RequestPathDb) -> None:
        # A non-superuser owner (`request_path_db`, not the `conn` fixture's
        # superuser `mm`): the owner here is neither superuser nor BYPASSRLS,
        # so `check_app_role` actually reaches the "is this the owner itself"
        # check instead of refusing on the superuser check first.
        owner_conn = await asyncpg.connect(request_path_db.owner_url)
        try:
            owner = await owner_conn.fetchval("select current_user")
            with pytest.raises(rls.RoleRefused, match="owner"):
                await rls.check_app_role(owner_conn, owner)
        finally:
            await owner_conn.close()

    async def test_refuses_a_role_the_non_superuser_owner_is_not_a_member_of(
        self, request_path_db: RequestPathDb, admin_database_url: str
    ) -> None:
        ungranted_role = f"mm_test_ungranted_{secrets.token_hex(8)}"
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(
                f'create role "{ungranted_role}" nologin nosuperuser nobypassrls'
            )
            owner_conn = await asyncpg.connect(request_path_db.owner_url)
            try:
                with pytest.raises(rls.RoleRefused, match="not a member"):
                    await rls.check_app_role(owner_conn, ungranted_role)
            finally:
                await owner_conn.close()
        finally:
            await admin_conn.execute(f'drop role if exists "{ungranted_role}"')
            await admin_conn.close()

    async def test_accepts_a_role_the_non_superuser_owner_is_a_member_of(
        self, request_path_db: RequestPathDb
    ) -> None:
        owner_conn = await asyncpg.connect(request_path_db.owner_url)
        try:
            await rls.check_app_role(owner_conn, request_path_db.app_role)
        finally:
            await owner_conn.close()


class TestSuperuserOwner:
    async def test_a_superuser_session_still_sees_only_permitted_rows_after_switching_role(
        self, test_database_url: str, admin_database_url: str
    ) -> None:
        """ADR-0008 addendum: the owner's own superuser bit never leaks through
        the role switch - once `request_identity` has switched `current_user`
        to the (non-superuser) app role, RLS applies for real, even though the
        session (`mm`) started out a superuser.
        """
        app_role = f"mm_test_app_{secrets.token_hex(8)}"
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
        finally:
            await admin_conn.close()

        try:
            migration_conn = await asyncpg.connect(test_database_url)
            try:
                await migrate(migration_conn)
                await rls.grant_app_role(migration_conn, app_role)
                await _seed_personal_namespace(test_database_url, oid="oid-super", alias="personal")

                seed_pool = await asyncpg.create_pool(test_database_url, min_size=1, max_size=1)
                try:
                    # An `Indexer` wired as `index_hook`, like `app.py` wires
                    # one, so the seeded writes below actually populate
                    # `notes` - the table this test reads from below.
                    seed_indexer = Indexer(seed_pool, VaultNotesSource())
                    try:
                        owner_backend = PostgresBackend(
                            seed_pool, index_hook=seed_indexer.index_on_connection
                        )
                        await owner_backend.write(
                            "personal/fact/mine.md",
                            note_bytes(id=new_ulid(datetime.now(UTC)), title="Mine"),
                            if_version="new",
                            client="seed",
                        )
                        await owner_backend.write(
                            "other/fact/theirs.md",
                            note_bytes(id=new_ulid(datetime.now(UTC)), title="Theirs"),
                            if_version="new",
                            client="seed",
                        )
                    finally:
                        await seed_indexer.aclose()
                finally:
                    await seed_pool.close()

                async with (
                    migration_conn.transaction(),
                    rls.request_identity(
                        migration_conn, role=app_role, oid="oid-super", roles=[_MEMORY_USER]
                    ),
                ):
                    current_user = await migration_conn.fetchval("select current_user")
                    readable = {
                        row["path"] for row in await migration_conn.fetch("select path from notes")
                    }
            finally:
                # `drop owned by` against `test_database_url` first: `grant_app_role`
                # above left `app_role` with grants on this database's own tables,
                # which a cluster-wide `DROP ROLE` would otherwise refuse as
                # "dependent objects" while the database still exists (it is only
                # dropped later, by `test_database_url`'s own fixture teardown).
                await migration_conn.execute(f'drop owned by "{app_role}"')
                await migration_conn.close()
        finally:
            admin_conn = await asyncpg.connect(admin_database_url)
            try:
                await admin_conn.execute(f'drop role if exists "{app_role}"')
            finally:
                await admin_conn.close()

        assert current_user == app_role
        assert readable == {"personal/fact/mine.md"}


class TestHnswIndexIsCreatedAsTheOwner:
    async def test_ensure_vector_index_runs_on_the_owner_pool_not_the_app_role(
        self, request_path_db: RequestPathDb
    ) -> None:
        """The `Indexer` behind `index_commit_hook`/background embeddings never
        takes an `app_role` at all (`app.py`'s own wiring) - it always runs on
        the plain owner pool, so the HNSW partial index it creates is owned by
        the owner, never the app role, regardless of what `PostgresBackend`
        itself was given.
        """
        indexer = Indexer(request_path_db.pool, VaultNotesSource())
        try:
            await indexer.ensure_vector_index("test-model", 3)
        finally:
            await indexer.aclose()

        owner_conn = await asyncpg.connect(request_path_db.owner_url)
        try:
            owner_name = await owner_conn.fetchval("select current_user")
            rows = await owner_conn.fetch(
                "select pg_get_userbyid(c.relowner) as owner "
                "from pg_class c where c.relname like 'chunks_hnsw_%'"
            )
        finally:
            await owner_conn.close()

        assert len(rows) == 1
        assert rows[0]["owner"] == owner_name
        assert rows[0]["owner"] != request_path_db.app_role


class TestConcurrentReplicaStartupNeverFailsOnAppRoleGrants:
    async def test_several_concurrent_grant_app_role_calls_on_a_fresh_role_all_succeed(
        self, ungranted_app_role_db: RequestPathDb
    ) -> None:
        """Several API replicas starting at the same time each run `migrate()`
        then `grant_app_role` on their own connection (`app.py`'s
        `_open_backend`, ADR-0009). Without something serializing the
        `GRANT`s, Postgres can answer concurrent `GRANT`s on the same object
        with "tuple concurrently updated" (#123) - this must never surface
        to any of them, however many start at once.

        One round is not guaranteed to hit the race (it is a timing window
        in Postgres' own catalog update, not a deterministic conflict), so
        this repeats a fresh round of concurrent connections several times;
        a single raised exception across all rounds fails the test.
        """
        owner_url = ungranted_app_role_db.owner_url
        app_role = ungranted_app_role_db.app_role

        for _ in range(20):
            connections = [await asyncpg.connect(owner_url) for _ in range(8)]
            try:
                await asyncio.gather(
                    *(rls.grant_app_role(connection, app_role) for connection in connections)
                )
            finally:
                await asyncio.gather(*(connection.close() for connection in connections))
