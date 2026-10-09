# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the enterprise static-token rule (ADR-0006 §7, #224).

`create_token(..., enterprise=True)` - the mode `cli.py`'s `token create`
turns on exactly when `STORAGE_BACKEND=postgres` - additionally requires an
owner with at least one role, an expiry within `max_expires_days`, and an
owner that already exists in `users` (so the delta sync can later revoke a
departed owner's token). `enterprise=False` (today's default, every
deployment without enterprise mode) keeps accepting everything it already
did - except the owner/roles pairing `_validate_owner_and_roles` has always
enforced unconditionally, independent of this task.

`enterprise_violations`/`owners_not_in_users` are the pure/batched halves
`cli.py`'s `token list` uses to flag a token that predates enterprise mode
(or was inserted directly) without re-deriving `create_token`'s own checks.

The CLI section at the bottom drives the same wiring through `cli.main`,
the way `tests/auth/test_static_tokens.py` already does for the rest of
`token create|list|revoke`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio

from memory_manager.auth.tokens import (
    DEFAULT_MAX_EXPIRES_DAYS,
    TokenInfo,
    create_token,
    enterprise_violations,
    owners_not_in_users,
)
from memory_manager.cli import main
from memory_manager.db.migrate import migrate
from memory_manager.mcp.authz import READ_SCOPE

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


async def _add_user(pool: asyncpg.Pool, oid: str = _OWNER) -> None:
    await pool.execute(
        "insert into users (oid, tid, display_name) values ($1, 'tenant-1', 'Alice')", oid
    )


# --- `create_token(..., enterprise=True)` -----------------------------------


async def test_enterprise_rejects_a_token_without_an_expiry(pool: asyncpg.Pool) -> None:
    await _add_user(pool)

    with pytest.raises(ValueError, match="expiry"):
        await create_token(
            pool,
            "ci",
            scopes=[READ_SCOPE],
            namespaces=["personal"],
            owner_oid=_OWNER,
            roles=["Memory.User"],
            enterprise=True,
        )


async def test_enterprise_rejects_an_expiry_above_the_maximum(pool: asyncpg.Pool) -> None:
    await _add_user(pool)
    too_far = datetime.now(UTC) + timedelta(days=DEFAULT_MAX_EXPIRES_DAYS + 1)

    with pytest.raises(ValueError, match="at most"):
        await create_token(
            pool,
            "ci",
            scopes=[READ_SCOPE],
            namespaces=["personal"],
            owner_oid=_OWNER,
            roles=["Memory.User"],
            expires_at=too_far,
            enterprise=True,
        )


async def test_enterprise_accepts_an_expiry_exactly_at_the_maximum(pool: asyncpg.Pool) -> None:
    await _add_user(pool)
    at_max = datetime.now(UTC) + timedelta(days=DEFAULT_MAX_EXPIRES_DAYS)

    _plaintext, info = await create_token(
        pool,
        "ci",
        scopes=[READ_SCOPE],
        namespaces=["personal"],
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=at_max,
        enterprise=True,
    )

    assert info.expires_at == at_max


async def test_enterprise_rejects_a_token_without_an_owner(pool: asyncpg.Pool) -> None:
    expires_at = datetime.now(UTC) + timedelta(days=30)

    with pytest.raises(ValueError, match="owner"):
        await create_token(
            pool,
            "ci",
            scopes=[READ_SCOPE],
            namespaces=["personal"],
            expires_at=expires_at,
            enterprise=True,
        )


async def test_enterprise_rejects_an_owner_without_a_role(pool: asyncpg.Pool) -> None:
    await _add_user(pool)
    expires_at = datetime.now(UTC) + timedelta(days=30)

    with pytest.raises(ValueError, match="role"):
        await create_token(
            pool,
            "ci",
            scopes=[READ_SCOPE],
            namespaces=["personal"],
            owner_oid=_OWNER,
            expires_at=expires_at,
            enterprise=True,
        )


async def test_enterprise_rejects_an_owner_not_in_users(pool: asyncpg.Pool) -> None:
    # No `_add_user` here - the owner has never signed in.
    expires_at = datetime.now(UTC) + timedelta(days=30)

    with pytest.raises(ValueError, match="users"):
        await create_token(
            pool,
            "ci",
            scopes=[READ_SCOPE],
            namespaces=["personal"],
            owner_oid=_OWNER,
            roles=["Memory.User"],
            expires_at=expires_at,
            enterprise=True,
        )


async def test_enterprise_accepts_a_token_meeting_every_rule(pool: asyncpg.Pool) -> None:
    await _add_user(pool)
    expires_at = datetime.now(UTC) + timedelta(days=30)

    plaintext, info = await create_token(
        pool,
        "ci",
        scopes=[READ_SCOPE],
        namespaces=["personal"],
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=expires_at,
        enterprise=True,
    )

    assert plaintext.startswith("mm_")
    assert info.owner_oid == _OWNER
    assert info.expires_at == expires_at


# --- Non-enterprise mode keeps today's behaviour -----------------------------


async def test_non_enterprise_accepts_a_token_without_an_expiry(pool: asyncpg.Pool) -> None:
    await _add_user(pool)

    _plaintext, info = await create_token(
        pool,
        "ci",
        scopes=[READ_SCOPE],
        namespaces=["personal"],
        owner_oid=_OWNER,
        roles=["Memory.User"],
        enterprise=False,
    )

    assert info.expires_at is None


async def test_non_enterprise_accepts_an_expiry_above_the_enterprise_maximum(
    pool: asyncpg.Pool,
) -> None:
    await _add_user(pool)
    too_far = datetime.now(UTC) + timedelta(days=DEFAULT_MAX_EXPIRES_DAYS + 1)

    _plaintext, info = await create_token(
        pool,
        "ci",
        scopes=[READ_SCOPE],
        namespaces=["personal"],
        owner_oid=_OWNER,
        roles=["Memory.User"],
        expires_at=too_far,
        enterprise=False,
    )

    assert info.expires_at == too_far


async def test_non_enterprise_accepts_a_token_without_an_owner(pool: asyncpg.Pool) -> None:
    _plaintext, info = await create_token(
        pool, "ci", scopes=[READ_SCOPE], namespaces=["personal"], enterprise=False
    )

    assert info.owner_oid is None


async def test_non_enterprise_still_rejects_an_owner_without_a_role(pool: asyncpg.Pool) -> None:
    # Pre-existing behaviour (`_validate_owner_and_roles`), unrelated to #224 -
    # enterprise mode never makes this one any stricter than it already was.
    with pytest.raises(ValueError, match="role"):
        await create_token(
            pool,
            "ci",
            scopes=[READ_SCOPE],
            namespaces=["personal"],
            owner_oid=_OWNER,
            enterprise=False,
        )


async def test_non_enterprise_accepts_an_owner_not_in_users(pool: asyncpg.Pool) -> None:
    # No `_add_user` - outside enterprise mode, the owner need not exist yet.
    _plaintext, info = await create_token(
        pool,
        "ci",
        scopes=[READ_SCOPE],
        namespaces=["personal"],
        owner_oid=_OWNER,
        roles=["Memory.User"],
        enterprise=False,
    )

    assert info.owner_oid == _OWNER


# --- `enterprise_violations`/`owners_not_in_users` ---------------------------


def _info(
    *, owner_oid: str | None, roles: tuple[str, ...], expires_at: datetime | None
) -> TokenInfo:
    now = datetime.now(UTC)
    return TokenInfo(
        name="legacy",
        scopes=(READ_SCOPE,),
        namespaces=("personal",),
        owner_oid=owner_oid,
        roles=roles,
        created_at=now,
        expires_at=expires_at,
        revoked_at=None,
        last_used_at=None,
    )


def test_enterprise_violations_is_empty_for_a_compliant_token() -> None:
    now = datetime.now(UTC)
    info = _info(owner_oid=_OWNER, roles=("Memory.User",), expires_at=now + timedelta(days=30))

    assert enterprise_violations(info, max_expires_days=DEFAULT_MAX_EXPIRES_DAYS) == ()


def test_enterprise_violations_flags_a_legacy_token_without_an_owner() -> None:
    info = _info(owner_oid=None, roles=(), expires_at=None)

    violations = enterprise_violations(info, max_expires_days=DEFAULT_MAX_EXPIRES_DAYS)

    assert "no-owner" in violations
    assert "no-expiry" in violations


def test_enterprise_violations_flags_an_expiry_set_too_far_past_creation() -> None:
    now = datetime.now(UTC)
    info = _info(
        owner_oid=_OWNER,
        roles=("Memory.User",),
        expires_at=now + timedelta(days=DEFAULT_MAX_EXPIRES_DAYS + 10),
    )

    violations = enterprise_violations(info, max_expires_days=DEFAULT_MAX_EXPIRES_DAYS)

    assert violations == ("expiry-too-far",)


async def test_owners_not_in_users_returns_only_the_unknown_ones(pool: asyncpg.Pool) -> None:
    await _add_user(pool, "oid-known")

    unknown = await owners_not_in_users(pool, ["oid-known", "oid-unknown"])

    assert unknown == frozenset({"oid-unknown"})


async def test_owners_not_in_users_with_no_owners_skips_the_query(pool: asyncpg.Pool) -> None:
    assert await owners_not_in_users(pool, []) == frozenset()


# --- `memory-manager token create|list` with STORAGE_BACKEND=postgres -------


def test_cli_token_create_in_postgres_mode_requires_an_owner_and_expiry(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")

    exit_code = main(["token", "create", "ci", "--scope", READ_SCOPE])

    assert exit_code == 2


def test_cli_token_create_in_postgres_mode_succeeds_for_a_known_owner(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")

    async def _seed_owner() -> None:
        conn = await asyncpg.connect(test_database_url)
        try:
            await migrate(conn)
            await conn.execute(
                "insert into users (oid, tid, display_name) values ($1, 'tenant-1', 'Alice')",
                _OWNER,
            )
        finally:
            await conn.close()

    asyncio.run(_seed_owner())

    exit_code = main(
        [
            "token",
            "create",
            "ci",
            "--scope",
            READ_SCOPE,
            "--owner",
            _OWNER,
            "--role",
            "Memory.User",
            "--expires-days",
            "30",
        ]
    )
    created_out = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert created_out.startswith("mm_")


def test_cli_token_list_flags_a_legacy_token_in_postgres_mode(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    # Created in git mode (today's fully optional owner/expiry) ...
    assert main(["token", "create", "legacy", "--scope", READ_SCOPE]) == 0
    capsys.readouterr()

    # ... then listed in postgres/enterprise mode, where it is a violator.
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    assert main(["token", "list"]) == 0
    listed_out = capsys.readouterr().out

    assert "legacy" in listed_out
    assert "enterprise_violations=no-owner,no-expiry" in listed_out
