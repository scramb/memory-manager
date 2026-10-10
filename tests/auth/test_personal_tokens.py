# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for personal tokens (ADR-0012, #134).

Three layers, bottom to top, the same shape `tests/auth/test_static_tokens.py`/
`test_static_tokens_enterprise.py` already use:

- `memory_manager.auth.tokens.create_token`/`revoke_token` with `kind=KIND_PERSONAL`
  (and the `kind=KIND_SERVICE` default, unchanged) against a real Postgres.
- `memory_manager.auth.owner_rights.intersect_namespaces`, pure set arithmetic.
- `memory_manager.auth.verifier.StaticTokenVerifier` with an
  `OwnerRightsResolver` wired in, for both `"git"` (`LOGIN_NAMESPACE_MAP`-style
  namespace resolution, built here by hand rather than through `app.open_services`)
  and `"postgres"` (`users`/`user_groups`, seeded directly - `_add_user` is the
  same pattern `test_static_tokens_enterprise.py` already uses, since only a real
  Entra login writes that row otherwise).
- `memory-manager token create|list|revoke` (`cli.py`), through `cli.main`
  directly, for the one thing that only exists at that layer: the `"git"`-mode
  creation-time namespace check.

`pool` migrates with the plain, backend-agnostic `migrate(conn)` call: migration
`0025_token_kind.sql` lives in the shared `migrations/` directory, not
`migrations/postgres/`, and `users`/`user_groups` (`0005_rls.sql`) are shared
too - so this one fixture is enough to exercise both backends' behaviour, the
same way `test_static_tokens_enterprise.py`'s own `pool` fixture already does.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio

from memory_manager.auth.owner_rights import OwnerRightsResolver, intersect_namespaces
from memory_manager.auth.tokens import (
    ALL_NAMESPACES,
    DEFAULT_PERSONAL_MAX_EXPIRES_DAYS,
    KIND_PERSONAL,
    KIND_SERVICE,
    create_token,
    revoke_token,
    verify,
)
from memory_manager.auth.verifier import StaticTokenVerifier
from memory_manager.cli import main
from memory_manager.db.migrate import migrate
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE

_OWNER = "oid-alice"


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(conn)
    finally:
        await conn.close()

    created_pool = await asyncpg.create_pool(test_database_url)
    try:
        yield created_pool
    finally:
        await created_pool.close()


async def _add_user(pool: asyncpg.Pool, oid: str = _OWNER, *, disabled: bool = False) -> None:
    await pool.execute(
        "insert into users (oid, tid, display_name, disabled_at) values ($1, 'tenant-1', "
        "'Alice', $2)",
        oid,
        datetime.now(UTC) if disabled else None,
    )


async def _add_group(pool: asyncpg.Pool, oid: str, group_id: str) -> None:
    await pool.execute("insert into user_groups (oid, group_id) values ($1, $2)", oid, group_id)


def _future(days: int = 1) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


# --- `create_token(..., kind=KIND_PERSONAL)` --------------------------------


async def test_create_token_personal_requires_an_owner(pool: asyncpg.Pool) -> None:
    with pytest.raises(ValueError, match="owner"):
        await create_token(
            pool,
            "alice-cli",
            scopes=[READ_SCOPE],
            namespaces=[ALL_NAMESPACES],
            kind=KIND_PERSONAL,
            expires_at=_future(),
        )


async def test_create_token_personal_requires_an_expiry(pool: asyncpg.Pool) -> None:
    with pytest.raises(ValueError, match="expiry"):
        await create_token(
            pool,
            "alice-cli",
            scopes=[READ_SCOPE],
            namespaces=[ALL_NAMESPACES],
            kind=KIND_PERSONAL,
            owner_oid=_OWNER,
            roles=["Memory.User"],
        )


async def test_create_token_personal_rejects_an_expiry_above_the_default_maximum(
    pool: asyncpg.Pool,
) -> None:
    too_far = _future(DEFAULT_PERSONAL_MAX_EXPIRES_DAYS + 1)
    with pytest.raises(ValueError, match="at most"):
        await create_token(
            pool,
            "alice-cli",
            scopes=[READ_SCOPE],
            namespaces=[ALL_NAMESPACES],
            kind=KIND_PERSONAL,
            owner_oid=_OWNER,
            roles=["Memory.User"],
            expires_at=too_far,
        )


async def test_create_token_personal_honours_a_smaller_personal_max_days(
    pool: asyncpg.Pool,
) -> None:
    with pytest.raises(ValueError, match="at most 5"):
        await create_token(
            pool,
            "alice-cli",
            scopes=[READ_SCOPE],
            namespaces=[ALL_NAMESPACES],
            kind=KIND_PERSONAL,
            owner_oid=_OWNER,
            roles=["Memory.User"],
            expires_at=_future(10),
            personal_max_days=5,
        )


async def test_create_token_rejects_an_unknown_kind(pool: asyncpg.Pool) -> None:
    with pytest.raises(ValueError, match="kind"):
        await create_token(
            pool, "x", scopes=[READ_SCOPE], namespaces=[ALL_NAMESPACES], kind="bogus"
        )


async def test_create_token_without_kind_is_service_and_claims_are_unchanged(
    pool: asyncpg.Pool,
) -> None:
    plaintext, info = await create_token(
        pool, "claude-code", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=["personal", "work"]
    )

    assert info.kind == KIND_SERVICE
    assert info.created_by is None
    assert info.description is None

    # No resolver at all - must not matter for a `kind=KIND_SERVICE` token.
    verifier = StaticTokenVerifier(pool)
    access_token = await verifier.verify_token(plaintext)

    assert access_token is not None
    assert access_token.claims == {"namespaces": ["personal", "work"]}


async def test_create_token_personal_stores_description(pool: asyncpg.Pool) -> None:
    _plaintext, info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
        description="laptop CLI",
    )

    assert info.description == "laptop CLI"


# --- audit (create/revoke) --------------------------------------------------


async def test_create_token_writes_an_audit_entry(pool: asyncpg.Pool) -> None:
    await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
        created_by="cli",
    )

    row = await pool.fetchrow(
        "select actor, client, op, detail from audit_log where op = $1", "token_create"
    )
    assert row is not None
    assert row["actor"] == "cli"
    assert row["client"] == "auth"
    detail = json.loads(row["detail"])
    assert detail == {"name": "alice-cli", "kind": "personal"}


async def test_create_token_audit_actor_falls_back_to_the_token_name(pool: asyncpg.Pool) -> None:
    await create_token(pool, "ci", scopes=[READ_SCOPE], namespaces=[ALL_NAMESPACES])

    row = await pool.fetchrow("select actor from audit_log where op = 'token_create'")
    assert row is not None
    assert row["actor"] == "ci"


async def test_revoke_token_writes_an_audit_entry_with_the_given_actor(pool: asyncpg.Pool) -> None:
    _plaintext, info = await create_token(
        pool, "ci", scopes=[READ_SCOPE], namespaces=[ALL_NAMESPACES]
    )

    assert await revoke_token(pool, info.name, actor="alice") is True

    row = await pool.fetchrow("select actor, detail from audit_log where op = 'token_revoke'")
    assert row is not None
    assert row["actor"] == "alice"
    assert json.loads(row["detail"]) == {"name": "ci"}


async def test_revoke_token_writes_no_audit_entry_when_nothing_was_revoked(
    pool: asyncpg.Pool,
) -> None:
    assert await revoke_token(pool, "no-such-token", actor="alice") is False

    count = await pool.fetchval("select count(*) from audit_log where op = 'token_revoke'")
    assert count == 0


# --- `owner_rights.intersect_namespaces` ------------------------------------


def test_intersect_namespaces_both_wildcard_returns_wildcard() -> None:
    assert intersect_namespaces([ALL_NAMESPACES], [ALL_NAMESPACES]) == (ALL_NAMESPACES,)


def test_intersect_namespaces_one_side_wildcard_returns_the_other_sides_namespaces() -> None:
    assert intersect_namespaces([ALL_NAMESPACES], ["personal"]) == ("personal",)
    assert intersect_namespaces(["personal", "work"], [ALL_NAMESPACES]) == ("personal", "work")


def test_intersect_namespaces_disjoint_returns_none() -> None:
    assert intersect_namespaces(["personal"], ["work"]) is None


def test_intersect_namespaces_one_side_empty_and_not_wildcard_returns_none() -> None:
    assert intersect_namespaces(["personal"], []) is None
    assert intersect_namespaces([ALL_NAMESPACES], []) is None


# --- `StaticTokenVerifier` + `OwnerRightsResolver`, `"git"` -----------------


async def test_verifier_narrows_a_personal_tokens_namespaces_to_the_owners_current_ones_git(
    pool: asyncpg.Pool,
) -> None:
    plaintext, _info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE, WRITE_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
    )
    resolver = OwnerRightsResolver(
        backend="git", namespace_map={_OWNER: ["personal"]}, default_namespaces=[ALL_NAMESPACES]
    )
    verifier = StaticTokenVerifier(pool, resolver=resolver)

    access_token = await verifier.verify_token(plaintext)

    assert access_token is not None
    assert access_token.claims is not None
    assert access_token.claims["namespaces"] == ["personal"]
    assert "groups" not in access_token.claims


async def test_verifier_rejects_a_personal_token_disjoint_from_the_owners_namespaces_git(
    pool: asyncpg.Pool,
) -> None:
    plaintext, _info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=["work"],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
    )
    resolver = OwnerRightsResolver(
        backend="git", namespace_map={_OWNER: ["personal"]}, default_namespaces=[ALL_NAMESPACES]
    )
    verifier = StaticTokenVerifier(pool, resolver=resolver)

    assert await verifier.verify_token(plaintext) is None


async def test_verifier_rejects_a_personal_token_when_the_owner_has_no_namespaces_at_all_git(
    pool: asyncpg.Pool,
) -> None:
    plaintext, _info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
    )
    resolver = OwnerRightsResolver(
        backend="git", namespace_map={_OWNER: []}, default_namespaces=[ALL_NAMESPACES]
    )
    verifier = StaticTokenVerifier(pool, resolver=resolver)

    assert await verifier.verify_token(plaintext) is None


async def test_verifier_rejects_a_personal_token_when_no_resolver_is_configured(
    pool: asyncpg.Pool,
) -> None:
    plaintext, _info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
    )
    verifier = StaticTokenVerifier(pool)  # no resolver at all - fails closed, not unrestricted

    assert await verifier.verify_token(plaintext) is None


# --- `StaticTokenVerifier` + `OwnerRightsResolver`, `"postgres"` ------------


async def test_verifier_rejects_a_personal_token_whose_owner_has_no_users_row_postgres(
    pool: asyncpg.Pool,
) -> None:
    plaintext, _info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
    )
    resolver = OwnerRightsResolver(backend="postgres", pool=pool)
    verifier = StaticTokenVerifier(pool, resolver=resolver)

    assert await verifier.verify_token(plaintext) is None


async def test_verifier_rejects_a_personal_token_whose_owner_is_disabled_postgres(
    pool: asyncpg.Pool,
) -> None:
    await _add_user(pool, disabled=True)
    plaintext, _info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
    )
    resolver = OwnerRightsResolver(backend="postgres", pool=pool)
    verifier = StaticTokenVerifier(pool, resolver=resolver)

    assert await verifier.verify_token(plaintext) is None


async def test_verifier_sets_live_groups_claim_for_a_personal_token_postgres(
    pool: asyncpg.Pool,
) -> None:
    await _add_user(pool)
    plaintext, _info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
    )
    # Added after the token already exists - proves the group is read live on
    # every verification, never baked into the token at creation (ADR-0009 §3).
    await _add_group(pool, _OWNER, "grp-eng")
    resolver = OwnerRightsResolver(backend="postgres", pool=pool)
    verifier = StaticTokenVerifier(pool, resolver=resolver)

    access_token = await verifier.verify_token(plaintext)

    assert access_token is not None
    assert access_token.claims is not None
    assert access_token.claims["groups"] == ["grp-eng"]
    assert access_token.claims["namespaces"] == [ALL_NAMESPACES]


# --- expiry/revocation, for a `kind=KIND_PERSONAL` token specifically -------


async def test_verifier_rejects_an_expired_personal_token(pool: asyncpg.Pool) -> None:
    plaintext, _info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    resolver = OwnerRightsResolver(backend="git")
    verifier = StaticTokenVerifier(pool, resolver=resolver)

    assert await verifier.verify_token(plaintext) is None


async def test_verifier_rejects_a_revoked_personal_token(pool: asyncpg.Pool) -> None:
    plaintext, info = await create_token(
        pool,
        "alice-cli",
        scopes=[READ_SCOPE],
        namespaces=[ALL_NAMESPACES],
        kind=KIND_PERSONAL,
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=_future(),
    )
    assert await revoke_token(pool, info.name) is True
    resolver = OwnerRightsResolver(backend="git")
    verifier = StaticTokenVerifier(pool, resolver=resolver)

    assert await verifier.verify_token(plaintext) is None


# --- `verify`'s `last_used_at` throttle (tokens.py:76-80, :333-336) --------


async def test_verify_writes_last_used_at_once_per_minute(pool: asyncpg.Pool) -> None:
    plaintext, info = await create_token(
        pool, "ci", scopes=[READ_SCOPE], namespaces=[ALL_NAMESPACES]
    )

    await verify(pool, plaintext)
    first = await pool.fetchval("select last_used_at from static_tokens where name = $1", info.name)
    await verify(pool, plaintext)
    second = await pool.fetchval(
        "select last_used_at from static_tokens where name = $1", info.name
    )

    assert first is not None
    assert first == second


# --- `memory-manager token create|list|revoke` ------------------------------


def test_cli_token_create_personal_shows_up_in_token_list(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    create_exit = main(
        [
            "token",
            "create",
            "alice-cli",
            "--scope",
            READ_SCOPE,
            "--kind",
            "personal",
            "--owner",
            _OWNER,
            "--role",
            "Memory.User",
            "--expires-days",
            "30",
            "--description",
            "laptop CLI",
        ]
    )
    assert create_exit == 0
    capsys.readouterr()

    assert main(["token", "list"]) == 0
    listed_out = capsys.readouterr().out
    assert "kind=personal" in listed_out
    assert "description=laptop CLI" in listed_out


def test_cli_token_create_personal_requires_an_expiry(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    exit_code = main(
        [
            "token",
            "create",
            "alice-cli",
            "--scope",
            READ_SCOPE,
            "--kind",
            "personal",
            "--owner",
            _OWNER,
            "--role",
            "Memory.User",
        ]
    )
    assert exit_code == 2


def test_cli_token_create_personal_rejects_namespaces_outside_the_owners_map_git(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("LOGIN_NAMESPACE_MAP", json.dumps({_OWNER: ["personal"]}))

    exit_code = main(
        [
            "token",
            "create",
            "alice-cli",
            "--scope",
            READ_SCOPE,
            "--namespace",
            "work",
            "--kind",
            "personal",
            "--owner",
            _OWNER,
            "--role",
            "Memory.User",
            "--expires-days",
            "30",
        ]
    )

    assert exit_code == 2


def test_cli_token_create_personal_narrows_a_wildcard_request_to_the_owners_map_git(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("LOGIN_NAMESPACE_MAP", json.dumps({_OWNER: ["personal"]}))

    exit_code = main(
        [
            "token",
            "create",
            "alice-cli",
            "--scope",
            READ_SCOPE,
            "--kind",
            "personal",
            "--owner",
            _OWNER,
            "--role",
            "Memory.User",
            "--expires-days",
            "30",
        ]
    )

    assert exit_code == 0
