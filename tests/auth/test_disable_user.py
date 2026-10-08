# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for #222: disabling a user revokes every OAuth token family and static
token it owns, in one transaction, with its own audit entry; enabling it back
revokes nothing.

Drives `memory_manager.auth.provider.MemoryManagerOAuthProvider` and
`memory_manager.auth.tokens` directly against a real Postgres (`pool` fixture,
`tests/auth/conftest.py`) - no HTTP transport, the same shape
`tests/auth/test_principal_tokens.py` already uses for Entra-principal OAuth
tokens; `_provider`/`_client`/`_park_pending`/`_code_from_redirect`/
`_issue_tokens` are duplicated from that module rather than imported, since it
re-exports none of its own private helpers (the same reasoning
`tests/auth/test_entra_refresh.py`'s own docstring gives for its own
duplicated `entra_environ`).
"""

from __future__ import annotations

import json
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import asyncpg
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from memory_manager.auth import store, tokens, users
from memory_manager.auth.login import LoginPrincipal
from memory_manager.auth.provider import MemoryManagerOAuthProvider
from memory_manager.auth.verifier import verify_bearer_token

_RESOURCE = "https://mm.example.test/mcp"
_ISSUER = "https://mm.example.test"
_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
_CLIENT_ID = "test-client"
_NAMESPACES = ["personal"]

#: A Fernet key generated once for this module only - not a credential protecting
#: anything real, just what `OAUTH_CLIENT_SECRET_KEY` requires (same as `test_principal_tokens.py`).
_CLIENT_SECRET_KEY = "r8rGp30uQA9cx9egMfZk4ez3xkfaFzL0tCst-kNzrcI="  # noqa: S105


def _provider(pool: asyncpg.Pool) -> MemoryManagerOAuthProvider:
    return MemoryManagerOAuthProvider(
        pool, resource=_RESOURCE, issuer=_ISSUER, client_secret_key=_CLIENT_SECRET_KEY
    )


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=_CLIENT_ID,
        redirect_uris=[AnyUrl(_REDIRECT_URI)],
        token_endpoint_auth_method="none",  # noqa: S106 - an auth method, not a credential
    )


async def _park_pending(pool: asyncpg.Pool) -> str:
    pending_id = secrets.token_urlsafe(16)
    await store.save_pending(
        pool,
        pending_id,
        client_id=_CLIENT_ID,
        redirect_uri=_REDIRECT_URI,
        redirect_uri_provided_explicitly=True,
        code_challenge="challenge",
        state=None,
        resource=_RESOURCE,
        scopes=["memory:read", "memory:write"],
        ttl=timedelta(minutes=10),
    )
    return pending_id


def _code_from_redirect(redirect_url: str) -> str:
    query = parse_qs(urlsplit(redirect_url).query)
    return query["code"][0]


async def _issue_tokens(
    pool: asyncpg.Pool,
    provider: MemoryManagerOAuthProvider,
    *,
    subject: str,
    principal: LoginPrincipal,
) -> tuple[str, str]:
    """Register the test client, park a pending authorization, complete it, and
    exchange the resulting code - a fresh `family_id` every call (`auth.provider`'s
    own `exchange_authorization_code`), exactly what a second, independent login by
    the same `oid` would produce.

    Returns `(access_token, refresh_token)`.
    """
    await provider.register_client(_client())
    pending_id = await _park_pending(pool)
    redirect_url = await provider.complete_authorization(
        pending_id, subject, _NAMESPACES, principal
    )
    assert redirect_url is not None
    code = _code_from_redirect(redirect_url)

    client = _client()
    loaded_code = await provider.load_authorization_code(client, code)
    assert loaded_code is not None
    oauth_token = await provider.exchange_authorization_code(client, loaded_code)
    assert oauth_token.refresh_token is not None
    return oauth_token.access_token, oauth_token.refresh_token


async def _create_static_token(pool: asyncpg.Pool, name: str, *, owner_oid: str) -> str:
    plaintext, _info = await tokens.create_token(
        pool,
        name,
        scopes=["memory:read", "memory:write"],
        namespaces=["*"],
        owner_oid=owner_oid,
        roles=["Memory.User"],
    )
    return plaintext


async def _live_oauth_token_count(pool: asyncpg.Pool, oid: str) -> int:
    count = await pool.fetchval(
        "select count(*) from oauth_tokens where user_oid = $1 and revoked_at is null", oid
    )
    assert isinstance(count, int)
    return count


class TestDisableUser:
    async def test_disable_user_revokes_every_family_and_static_token_but_not_anothers(
        self, pool: asyncpg.Pool
    ) -> None:
        provider = _provider(pool)
        await users.upsert_user(pool, "oid-eve", tid="tenant-1", display_name="Eve")
        await users.upsert_user(pool, "oid-frank", tid="tenant-1", display_name="Frank")

        eve_principal = LoginPrincipal(oid="oid-eve", roles=("Memory.User",))
        eve_access_1, eve_refresh_1 = await _issue_tokens(
            pool, provider, subject="eve-session-1", principal=eve_principal
        )
        eve_access_2, eve_refresh_2 = await _issue_tokens(
            pool, provider, subject="eve-session-2", principal=eve_principal
        )
        frank_access, frank_refresh = await _issue_tokens(
            pool,
            provider,
            subject="frank-session-1",
            principal=LoginPrincipal(oid="oid-frank", roles=("Memory.User",)),
        )

        eve_static = await _create_static_token(pool, "eve-static", owner_oid="oid-eve")
        frank_static = await _create_static_token(pool, "frank-static", owner_oid="oid-frank")

        live_before = await _live_oauth_token_count(pool, "oid-eve")
        assert live_before == 4  # two families, access + refresh each

        counts = await users.disable_user(pool, "oid-eve", reason="test: offboarding")

        assert counts.oauth_tokens == live_before
        assert counts.static_tokens == 1

        for access in (eve_access_1, eve_access_2):
            assert await verify_bearer_token(pool, access, oauth_resource=_RESOURCE) is None
        for refresh in (eve_refresh_1, eve_refresh_2):
            stored = await store.get_token(pool, refresh, "refresh", include_revoked=True)
            assert stored is not None
            assert stored.revoked_at is not None
        assert await tokens.verify(pool, eve_static) is None

        assert await verify_bearer_token(pool, frank_access, oauth_resource=_RESOURCE) is not None
        frank_refresh_stored = await store.get_token(pool, frank_refresh, "refresh")
        assert frank_refresh_stored is not None
        assert await tokens.verify(pool, frank_static) is not None

        eve = await users.get_user(pool, "oid-eve")
        assert eve is not None
        assert eve.disabled_at is not None
        frank = await users.get_user(pool, "oid-frank")
        assert frank is not None
        assert frank.disabled_at is None

        audit_row = await pool.fetchrow(
            "select actor, op, outcome, detail from audit_log where actor = $1 and op = $2",
            "oid-eve",
            "disable_user",
        )
        assert audit_row is not None
        assert audit_row["outcome"] == "ok"
        detail = json.loads(audit_row["detail"])
        assert detail["reason"] == "test: offboarding"
        assert detail["oauth_tokens_revoked"] == live_before
        assert detail["static_tokens_revoked"] == 1

    async def test_second_disable_call_is_a_noop(self, pool: asyncpg.Pool) -> None:
        provider = _provider(pool)
        await users.upsert_user(pool, "oid-gina", tid="tenant-1", display_name="Gina")
        await _issue_tokens(
            pool,
            provider,
            subject="gina-session-1",
            principal=LoginPrincipal(oid="oid-gina", roles=("Memory.User",)),
        )
        await _create_static_token(pool, "gina-static", owner_oid="oid-gina")

        first = await users.disable_user(pool, "oid-gina", reason="first")
        assert first.oauth_tokens > 0
        assert first.static_tokens == 1
        first_disabled_at = await pool.fetchval(
            "select disabled_at from users where oid = $1", "oid-gina"
        )

        second = await users.disable_user(pool, "oid-gina", reason="second")
        assert second.oauth_tokens == 0
        assert second.static_tokens == 0
        second_disabled_at = await pool.fetchval(
            "select disabled_at from users where oid = $1", "oid-gina"
        )
        assert second_disabled_at == first_disabled_at

        audit_rows = await pool.fetch(
            "select detail from audit_log where actor = $1 and op = $2 order by id",
            "oid-gina",
            "disable_user",
        )
        assert len(audit_rows) == 2
        assert json.loads(audit_rows[1]["detail"])["reason"] == "second"

    async def test_enable_user_does_not_revive_old_credentials_but_allows_a_fresh_login(
        self, pool: asyncpg.Pool
    ) -> None:
        provider = _provider(pool)
        await users.upsert_user(pool, "oid-hank", tid="tenant-1", display_name="Hank")
        principal = LoginPrincipal(oid="oid-hank", roles=("Memory.User",))

        old_access, _old_refresh = await _issue_tokens(
            pool, provider, subject="hank-session-1", principal=principal
        )
        old_static = await _create_static_token(pool, "hank-static", owner_oid="oid-hank")

        await users.disable_user(pool, "oid-hank", reason="offboarding")
        await users.enable_user(pool, "oid-hank", reason="back from leave")

        hank = await users.get_user(pool, "oid-hank")
        assert hank is not None
        assert hank.disabled_at is None

        # The old credentials stay revoked - re-enabling never revives them.
        assert await verify_bearer_token(pool, old_access, oauth_resource=_RESOURCE) is None
        assert await tokens.verify(pool, old_static) is None

        # A fresh login/token works again.
        new_access, _new_refresh = await _issue_tokens(
            pool, provider, subject="hank-session-2", principal=principal
        )
        assert await verify_bearer_token(pool, new_access, oauth_resource=_RESOURCE) is not None
