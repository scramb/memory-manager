# SPDX-License-Identifier: AGPL-3.0-only
"""`quotas.QuotaChecker` (#242), run against all three `SharedState` implementations
(`InMemorySharedState`, `PostgresSharedState`, `ValkeySharedState`) the same way
`tests/auth/test_shared_state.py` already does for `auth.ratelimit.RateLimiter`'s
own contract - `shared_state` below is that module's own fixture, copied rather than
imported for the same "no unambiguous cross-package `from conftest import ...`"
reason `tests/quotas/conftest.py`'s docstring gives for its own `pool`.

`QuotaChecker.check_write` reads `user`/`token` identity off the current request's
access token/principal the same contextvar-based way `mcp/authz.py`/`db.rls` do - so
every test that needs one monkeypatches `quotas.get_access_token` and `rls.
get_access_token` together (`_identity` below), the same technique `tests/mcp/
test_permission_matrix.py`/`tests/db/test_request_path.py` already use for
`db.rls.get_access_token` alone.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from typing import cast

import asyncpg
import pytest
import pytest_asyncio
from mcp.server.auth.provider import AccessToken
from redis.asyncio import Redis

from memory_manager import quotas as quotas_module
from memory_manager.audit import AuditWriter
from memory_manager.auth.shared_state import (
    InMemorySharedState,
    PostgresSharedState,
    SharedState,
    ValkeySharedState,
    build_valkey_shared_state,
)
from memory_manager.auth.store import ClientSecretCipher
from memory_manager.db import rls
from memory_manager.quotas import QuotaChecker, QuotaExceeded
from memory_manager.storage import Op

#: A Fernet key generated once for this test module only, same as
#: `tests/auth/test_shared_state.py`'s own - not a credential protecting
#: anything real, just what `PostgresSharedState`'s/`ValkeySharedState`'s
#: `cipher` constructor argument requires (unused by `QuotaChecker` itself,
#: which only ever calls `window_hit`).
_CIPHER_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="


def _postgres_state(pool: asyncpg.Pool) -> PostgresSharedState:
    return PostgresSharedState(pool, cipher=ClientSecretCipher(_CIPHER_KEY))


def _unique_valkey_prefixes() -> tuple[str, str]:
    unique = f"test:{secrets.token_hex(8)}:"
    return f"{unique}window:", f"{unique}pending:"


def _valkey_state(url: str) -> ValkeySharedState:
    window_prefix, pending_prefix = _unique_valkey_prefixes()
    return build_valkey_shared_state(
        url,
        cipher=ClientSecretCipher(_CIPHER_KEY),
        window_prefix=window_prefix,
        pending_prefix=pending_prefix,
    )


@pytest.fixture
def _postgres_shared_state(pool: asyncpg.Pool) -> SharedState:
    return _postgres_state(pool)


@pytest_asyncio.fixture
async def _valkey_shared_state(valkey_url: str) -> AsyncIterator[SharedState]:
    state = _valkey_state(valkey_url)
    try:
        yield state
    finally:
        await state.aclose()


@pytest.fixture(params=["in_memory", "postgres", "valkey"])
def shared_state(request: pytest.FixtureRequest) -> SharedState:
    """Same shape as `tests/auth/test_shared_state.py`'s own fixture of this name -
    see that module's docstring for why this has to stay a plain (non-async)
    fixture resolving the other two through `request.getfixturevalue`."""
    if request.param == "in_memory":
        return InMemorySharedState()
    return cast(SharedState, request.getfixturevalue(f"_{request.param}_shared_state"))


def _token(*, raw: str, oid: str | None) -> AccessToken:
    claims = {"oid": oid} if oid is not None else None
    return AccessToken(token=raw, client_id="static:test", scopes=[], claims=claims)


async def _write(
    checker: QuotaChecker,
    namespace: str,
    *,
    op: Op = "write",
    path: str | None = None,
    actor: str = "tester",
    client: str = "tester-client",
) -> None:
    """`QuotaChecker.check_write` with the `op`/`path`/`actor`/`client` audit-only
    arguments defaulted - every test in this module cares about the quota
    behaviour, not about those four, unless it says otherwise."""
    await checker.check_write(
        op=op,
        path=path if path is not None else f"{namespace}/note.md",
        namespace=namespace,
        actor=actor,
        client=client,
    )


class _Identity:
    """What `monkeypatch`-ing both `quotas.get_access_token` and `rls.get_access_token`
    to the same current token gives a test: `QuotaChecker.check_write` and
    `db.rls.current_principal` (which `check_write` calls internally for the `user`
    scope) read identity off two separate module-level imports of the same SDK
    function - both have to be patched together for a test to see one consistent
    identity, the way a real per-request contextvar would."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def set(self, token: AccessToken | None) -> None:
        self._monkeypatch.setattr(quotas_module, "get_access_token", lambda: token)
        self._monkeypatch.setattr(rls, "get_access_token", lambda: token)


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> _Identity:
    helper = _Identity(monkeypatch)
    helper.set(None)  # the stdio-shaped default: no token, no principal
    return helper


# === Each scope limits independently ================================================


class TestScopesAreIndependent:
    async def test_namespace_scope_limits_on_its_own(
        self, shared_state: SharedState, identity: _Identity
    ) -> None:
        checker = QuotaChecker(state=shared_state, namespace_per_minute=2)
        await _write(checker, "me")
        await _write(checker, "me")
        with pytest.raises(QuotaExceeded) as excinfo:
            await _write(checker, "me")
        assert excinfo.value.scope == "namespace"

    async def test_namespace_scope_keys_are_isolated_from_each_other(
        self, shared_state: SharedState, identity: _Identity
    ) -> None:
        checker = QuotaChecker(state=shared_state, namespace_per_minute=1)
        await _write(checker, "ns-a")
        # A different namespace has its own, unrelated window.
        await _write(checker, "ns-b")
        with pytest.raises(QuotaExceeded):
            await _write(checker, "ns-a")

    async def test_user_scope_only_applies_with_a_principal(
        self, shared_state: SharedState, identity: _Identity
    ) -> None:
        checker = QuotaChecker(state=shared_state, user_per_minute=1)
        identity.set(_token(raw="raw-token-no-oid", oid=None))
        # No principal (no `oid` claim) - the user scope is skipped entirely,
        # however many times this call is repeated.
        await _write(checker, "ns-a")
        await _write(checker, "ns-a")
        await _write(checker, "ns-a")

    async def test_user_scope_limits_once_a_principal_exists(
        self, shared_state: SharedState, identity: _Identity
    ) -> None:
        checker = QuotaChecker(state=shared_state, user_per_minute=2)
        identity.set(_token(raw="raw-token-1", oid="user-1"))
        await _write(checker, "ns-a")
        await _write(checker, "ns-b")  # different namespace, same user
        with pytest.raises(QuotaExceeded) as excinfo:
            await _write(checker, "ns-c")
        assert excinfo.value.scope == "user"

    async def test_token_scope_works_without_a_principal(
        self, shared_state: SharedState, identity: _Identity
    ) -> None:
        checker = QuotaChecker(state=shared_state, token_per_minute=2)
        identity.set(_token(raw="raw-token-no-oid", oid=None))
        await _write(checker, "ns-a")
        await _write(checker, "ns-a")
        with pytest.raises(QuotaExceeded) as excinfo:
            await _write(checker, "ns-a")
        assert excinfo.value.scope == "token"

    async def test_per_minute_and_per_day_windows_are_independent(
        self, shared_state: SharedState, identity: _Identity
    ) -> None:
        checker = QuotaChecker(state=shared_state, namespace_per_minute=100, namespace_per_day=1)
        await _write(checker, "ns-a")
        with pytest.raises(QuotaExceeded) as excinfo:
            # Still well under the per-minute limit - the per-day one is what fires.
            await _write(checker, "ns-a")
        assert excinfo.value.scope == "namespace"

    async def test_a_limit_of_zero_means_off(
        self, shared_state: SharedState, identity: _Identity
    ) -> None:
        checker = QuotaChecker(state=shared_state)  # every scope defaults to 0
        for _ in range(5):
            await _write(checker, "ns-a")


# === Held across replicas ============================================================


class TestHeldAcrossReplicas:
    async def test_two_app_instances_share_one_counter(
        self, shared_state: SharedState, identity: _Identity
    ) -> None:
        """Two `QuotaChecker`s built over the same `shared_state` - standing in for
        two replicas sharing one Postgres/Valkey backend - see each other's hits."""
        replica_a = QuotaChecker(state=shared_state, namespace_per_minute=2)
        replica_b = QuotaChecker(state=shared_state, namespace_per_minute=2)
        await _write(replica_a, "ns-a")
        await _write(replica_b, "ns-a")
        with pytest.raises(QuotaExceeded):
            await _write(replica_a, "ns-a")

    async def test_token_quota_applies_across_users(
        self, shared_state: SharedState, identity: _Identity
    ) -> None:
        """One token, two different callers (e.g. an agent token used on behalf of
        more than one owner, ADR-0013) - the token scope counts both against the
        same window, regardless of whose `oid` each individual call carries."""
        checker = QuotaChecker(state=shared_state, token_per_minute=2)
        identity.set(_token(raw="shared-raw-token", oid="user-1"))
        await _write(checker, "ns-a")
        identity.set(_token(raw="shared-raw-token", oid="user-2"))
        await _write(checker, "ns-b")
        with pytest.raises(QuotaExceeded) as excinfo:
            await _write(checker, "ns-c")
        assert excinfo.value.scope == "token"


# === Error shape stable ===============================================================


class TestErrorShape:
    async def test_quota_exceeded_carries_scope_limit_and_retry_after(self) -> None:
        checker = QuotaChecker(state=InMemorySharedState(), namespace_per_minute=1)
        await _write(checker, "ns-a")
        with pytest.raises(QuotaExceeded) as excinfo:
            await _write(checker, "ns-a")
        exc = excinfo.value
        assert exc.scope == "namespace"
        assert exc.limit == 1
        assert 0 < exc.retry_after <= 60.0
        assert str(exc) == (
            f"write quota exceeded for scope='namespace': limit=1 writes per window, "
            f"retry after {exc.retry_after:.1f}s"
        )

    async def test_quota_exceeded_is_a_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        checker = QuotaChecker(state=InMemorySharedState(), token_per_day=1)
        token = _token(raw="raw-token", oid=None)
        monkeypatch.setattr(quotas_module, "get_access_token", lambda: token)

        await _write(checker, "ns-a")
        with pytest.raises(ToolError):
            await _write(checker, "ns-a")


# === Rejections are audited (#242 correction) ========================================


class TestRejectionIsAudited:
    async def test_rejected_write_gets_one_audit_log_row_with_no_content_or_key(
        self, pool: asyncpg.Pool
    ) -> None:
        """A quota rejection is audited exactly like a write that failed its own
        version/secret-scan check (`app.py`'s `_audit_write_hook`): one `audit_log`
        row, `op`/`path`/`actor`/`client` as the write itself would have recorded,
        `outcome="rejected_quota"`, `detail` carrying only scope/window/limit/
        retry_after - never the quota key (here: the namespace `"secret-ns"`, which
        must not appear anywhere in `detail`) and never note content."""
        audit = AuditWriter(pool)
        checker = QuotaChecker(state=InMemorySharedState(), audit=audit, namespace_per_minute=1)

        await _write(
            checker,
            "secret-ns",
            op="edit",
            path="secret-ns/fact/x.md",
            actor="alice",
            client="claude-code",
        )
        with pytest.raises(QuotaExceeded):
            await _write(
                checker,
                "secret-ns",
                op="edit",
                path="secret-ns/fact/x.md",
                actor="alice",
                client="claude-code",
            )

        rows = await pool.fetch(
            "select actor, client, op, path, commit_sha, outcome, detail from audit_log "
            "where outcome = 'rejected_quota'"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["actor"] == "alice"
        assert row["client"] == "claude-code"
        assert row["op"] == "edit"
        assert row["path"] == "secret-ns/fact/x.md"
        assert row["commit_sha"] is None

        detail = json.loads(row["detail"])
        assert detail == {
            "scope": "namespace",
            "window": "minute",
            "limit": 1,
            "retry_after": detail["retry_after"],
        }
        assert 0 < detail["retry_after"] <= 60.0
        # No internal quota key (the namespace string itself, a user oid or a
        # token hash) and no note content anywhere in the recorded detail.
        assert "secret-ns" not in json.dumps(detail)

    async def test_allowed_write_gets_no_audit_log_row(self, pool: asyncpg.Pool) -> None:
        audit = AuditWriter(pool)
        checker = QuotaChecker(state=InMemorySharedState(), audit=audit, namespace_per_minute=10)

        await _write(checker, "ns-a")

        rows = await pool.fetch("select 1 from audit_log")
        assert rows == []

    async def test_without_an_audit_writer_the_rejection_is_only_logged(self) -> None:
        """`audit=None` (`"git"` mode without a database, #242) - no `audit_log`
        table to write to; `check_write` must still raise `QuotaExceeded` cleanly,
        nothing more to assert here than that this never crashes."""
        checker = QuotaChecker(state=InMemorySharedState(), namespace_per_minute=1)
        await _write(checker, "ns-a")
        with pytest.raises(QuotaExceeded):
            await _write(checker, "ns-a")


# === Fails open on a SharedState backend error =======================================


class _RaisingSharedState:
    """A `SharedState` whose `window_hit` always raises - `QuotaChecker.check_write`'s
    own fail-open contract (module docstring), not part of the shared `SharedState`
    contract suite itself."""

    async def window_hit(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        raise RuntimeError("backend unavailable")

    async def window_peek(self, key: str, *, window_seconds: float) -> tuple[int, float]:
        raise AssertionError("not used by QuotaChecker")  # pragma: no cover

    async def put_pending(self, key: str, payload: str, *, ttl_seconds: float) -> None:
        raise AssertionError("not used by QuotaChecker")  # pragma: no cover

    async def take_pending(self, key: str) -> str | None:
        raise AssertionError("not used by QuotaChecker")  # pragma: no cover


class TestFailsOpen:
    async def test_a_backend_error_allows_the_write_instead_of_rejecting_it(self) -> None:
        checker = QuotaChecker(
            state=cast(SharedState, _RaisingSharedState()), namespace_per_minute=1
        )
        await _write(checker, "ns-a")  # does not raise


# === ValkeySharedState extra: redis not installed, mirrored from auth's own suite ====


class TestValkeySharedStateSecrecyUnaffected:
    async def test_quota_windows_use_the_plain_window_key_like_rate_limits_do(
        self, valkey_url: str
    ) -> None:
        """Quota windows carry no secret (same as a `RateLimiter` window, unlike
        pending login state) - plain keys, visible in Valkey, same as `rate_limits.key`
        (`auth.shared_state`'s own module docstring)."""
        window_prefix, pending_prefix = _unique_valkey_prefixes()
        state = build_valkey_shared_state(
            valkey_url, window_prefix=window_prefix, pending_prefix=pending_prefix
        )
        try:
            checker = QuotaChecker(state=state, namespace_per_minute=10)
            await _write(checker, "a-very-recognizable-namespace")

            client = Redis.from_url(valkey_url, decode_responses=True)
            try:
                keys = [found async for found in client.scan_iter(match=f"{window_prefix}*")]
                assert len(keys) == 1
                assert "a-very-recognizable-namespace" in keys[0]
            finally:
                await client.aclose()
        finally:
            await state.aclose()
