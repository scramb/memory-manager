# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for Entra-principal OAuth codes and tokens (ADR-0006, #213).

Drives `memory_manager.auth.provider.MemoryManagerOAuthProvider` directly - no HTTP
transport, no DCR - against a real Postgres (`pool` fixture, `tests/auth/conftest.py`):
`complete_authorization` is given a `LoginPrincipal` the way an `entra`-mode login
completer (#215, not built yet) would, and `verify_bearer_token` is what `mcp/
server.py`'s `BearerAuthBackend` ultimately calls for every request. `password`/`oidc`
mode is covered too, to prove `principal=None` still behaves exactly as before #213.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import asyncpg
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from memory_manager.auth import store, users
from memory_manager.auth.login import LoginPrincipal
from memory_manager.auth.provider import MemoryManagerOAuthProvider
from memory_manager.auth.verifier import verify_bearer_token

_RESOURCE = "https://mm.example.test/mcp"
_ISSUER = "https://mm.example.test"
_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
_CLIENT_ID = "test-client"
_NAMESPACES = ["personal"]

#: A Fernet key generated once for this module only - not a credential protecting
#: anything real, just what `OAUTH_CLIENT_SECRET_KEY` requires (same as `test_oauth_flow.py`).
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
    subject: str = "alice",
    principal: LoginPrincipal | None = None,
) -> tuple[str, str]:
    """Register the test client, park a pending authorization, complete it, and
    exchange the resulting code.

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


class TestEntraPrincipalClaims:
    """Claims after code exchange and after refresh (issue #213's own checklist)."""

    async def test_code_exchange_yields_oid_roles_and_groups(self, pool: asyncpg.Pool) -> None:
        provider = _provider(pool)
        await users.upsert_user(pool, "oid-alice", tid="tenant-1", display_name="Alice")
        await users.replace_groups(pool, "oid-alice", ["group-eng"])

        access_token, _ = await _issue_tokens(
            pool,
            provider,
            principal=LoginPrincipal(oid="oid-alice", roles=("Memory.User",)),
        )

        verified = await verify_bearer_token(pool, access_token, oauth_resource=_RESOURCE)
        assert verified is not None
        assert verified.claims == {
            "namespaces": _NAMESPACES,
            "client_label": "claude-ai",
            "oid": "oid-alice",
            "roles": ["Memory.User"],
            "groups": ["group-eng"],
        }

    async def test_refresh_keeps_oid_roles_and_carries_family_started_at(
        self, pool: asyncpg.Pool
    ) -> None:
        provider = _provider(pool)
        await users.upsert_user(pool, "oid-bob", tid="tenant-1", display_name="Bob")

        access_token, refresh_token = await _issue_tokens(
            pool, provider, principal=LoginPrincipal(oid="oid-bob", roles=("Memory.Curator",))
        )
        first_stored = await store.get_token(pool, access_token, "access")
        assert first_stored is not None
        assert first_stored.family_started_at is not None

        client = _client()
        loaded_refresh = await provider.load_refresh_token(client, refresh_token)
        assert loaded_refresh is not None
        rotated = await provider.exchange_refresh_token(
            client, loaded_refresh, ["memory:read", "memory:write"]
        )

        verified = await verify_bearer_token(pool, rotated.access_token, oauth_resource=_RESOURCE)
        assert verified is not None
        assert verified.claims is not None
        assert verified.claims["oid"] == "oid-bob"
        assert verified.claims["roles"] == ["Memory.Curator"]

        rotated_stored = await store.get_token(pool, rotated.access_token, "access")
        assert rotated_stored is not None
        assert rotated_stored.family_started_at == first_stored.family_started_at

    async def test_group_change_is_visible_on_the_next_verify_without_reissuing(
        self, pool: asyncpg.Pool
    ) -> None:
        provider = _provider(pool)
        await users.upsert_user(pool, "oid-carol", tid="tenant-1", display_name="Carol")
        await users.replace_groups(pool, "oid-carol", ["group-eng"])

        access_token, _ = await _issue_tokens(
            pool, provider, principal=LoginPrincipal(oid="oid-carol", roles=("Memory.User",))
        )

        before = await verify_bearer_token(pool, access_token, oauth_resource=_RESOURCE)
        assert before is not None
        assert before.claims is not None
        assert before.claims["groups"] == ["group-eng"]

        await users.replace_groups(pool, "oid-carol", ["group-sales", "group-ops"])

        after = await verify_bearer_token(pool, access_token, oauth_resource=_RESOURCE)
        assert after is not None
        assert after.claims is not None
        assert sorted(after.claims["groups"]) == ["group-ops", "group-sales"]

    async def test_disabled_user_verifies_to_none(self, pool: asyncpg.Pool) -> None:
        provider = _provider(pool)
        await users.upsert_user(pool, "oid-dave", tid="tenant-1", display_name="Dave")

        access_token, _ = await _issue_tokens(
            pool, provider, principal=LoginPrincipal(oid="oid-dave", roles=("Memory.User",))
        )
        assert await verify_bearer_token(pool, access_token, oauth_resource=_RESOURCE) is not None

        await pool.execute("update users set disabled_at = now() where oid = $1", "oid-dave")

        assert await verify_bearer_token(pool, access_token, oauth_resource=_RESOURCE) is None

    async def test_password_and_oidc_tokens_carry_no_principal_unchanged(
        self, pool: asyncpg.Pool
    ) -> None:
        provider = _provider(pool)

        access_token, refresh_token = await _issue_tokens(pool, provider, principal=None)

        verified = await verify_bearer_token(pool, access_token, oauth_resource=_RESOURCE)
        assert verified is not None
        assert verified.claims == {"namespaces": _NAMESPACES, "client_label": "claude-ai"}

        client = _client()
        loaded_refresh = await provider.load_refresh_token(client, refresh_token)
        assert loaded_refresh is not None
        rotated = await provider.exchange_refresh_token(
            client, loaded_refresh, ["memory:read", "memory:write"]
        )
        rotated_verified = await verify_bearer_token(
            pool, rotated.access_token, oauth_resource=_RESOURCE
        )
        assert rotated_verified is not None
        assert rotated_verified.claims == {"namespaces": _NAMESPACES, "client_label": "claude-ai"}
