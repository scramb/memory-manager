# SPDX-License-Identifier: AGPL-3.0-only
"""Write-rate quotas per user, namespace and token, held across replicas (#242,
ADR-0009 §2).

CLAUDE.md's `RATE_LIMIT_*` pair already caps how fast any one key may call a
write tool at all (`auth.ratelimit.RateLimiter`, `http.py`'s `write_limiter`);
`QuotaChecker` is a second, independent budget on top, counted per scope
rather than per request-layer key, and - unlike a rate limit - off by default.
Three scopes, each its own fixed window (`auth.shared_state.SharedState.
window_hit`, the same shared-state contract `RateLimiter` uses, #103/#104):

- `user`: keyed by the calling request's `db.rls.Principal.oid`. Only ever
  applies once a principal exists, i.e. `"postgres"` mode (ADR-0008 addendum,
  #116) - a static token or a stdio session carries no `oid` claim at all
  (`db.rls.current_principal`'s own docstring), so there is no stable user
  identity to key on in `"git"` mode; the check is skipped for that call, not
  enforced against some fallback identity.
- `namespace`: keyed by the namespace this write actually targets - whatever
  `mcp/server.py` already resolved it to by the time it calls `check_write`
  (the real personal-namespace alias once `mcp/namespaces.py` has rewritten
  `me` for a `"postgres"`-mode caller, the literal path segment in `"git"`
  mode, which has no such rewrite to begin with). This scope needs nothing
  from this module beyond the string it is handed.
- `token`: keyed by a SHA-256 hash of the calling request's raw bearer token
  (CLAUDE.md: "token hashes only" - never the token itself, same as `http.py`'s
  `_request_key`). Works on both storage backends and for an OAuth or a
  static token alike; skipped only for a request with no access token at all
  (stdio, or unauthenticated HTTP loopback mode).

Each scope has two independent fixed windows - per-minute and per-day
(`ServerConfig`'s `quota_*_per_minute`/`quota_*_per_day`, #242) - checked
separately; exceeding either one raises `QuotaExceeded` (a `ToolError`, same
contract `mcp/authz.py`'s `require_scope`/`require_writable_namespace` already
use). A limit of `0` means "off": `check_write` never even calls `window_hit`
for that scope/window, so a deployment that sets none of the six
`QUOTA_WRITES_*` variables pays no extra shared-state round trip at all.

`check_write` fails *open* on a `SharedState` backend error, logged as a
warning - the same reasoning `http.py`'s `_allow_or_fail_open` gives: a
transient Postgres/Valkey outage must not itself turn into "every write is
rejected" on top of whatever else that outage already breaks. Every check,
allowed or rejected, is counted in `observability.metrics.record_quota_hit`.

A rejection is audited the same way a write that failed its own version or
secret-scan check already is (`audit.AuditWriter.record`, `app.py`'s
`_audit_write_hook`/`_audit_outcome`): one `audit_log` row, `outcome=
"rejected_quota"`, `op`/`path`/`actor`/`client` exactly as the write itself
would have recorded them had it gone through, `detail` carrying only
`scope`/`window`/`limit`/`retry_after` - never the quota key (a user's `oid`,
a namespace string or a token hash) and never note content, matching
`AuditWriter`'s own "`detail` is the one place a note's content could leak"
rule. `audit` is `None` for `"git"` mode without a database configured (no
`audit_log` table exists there at all) - a rejection then is logged at
WARNING only, same as the backend-unavailable fail-open path above.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError

from memory_manager.audit import AuditWriter
from memory_manager.auth.shared_state import SharedState
from memory_manager.db.rls import current_principal
from memory_manager.observability.metrics import record_quota_hit
from memory_manager.storage import Op

__all__ = ["QuotaChecker", "QuotaExceeded", "QuotaScope"]

_logger = logging.getLogger(__name__)

_MINUTE_SECONDS = 60.0
_DAY_SECONDS = 24 * 60 * 60.0

QuotaScope = Literal["user", "namespace", "token"]


class QuotaExceeded(ToolError):
    """Raised by `QuotaChecker.check_write` once `scope`'s window is over `limit`.

    Carries `scope`, `limit` and `retry_after` as attributes (for a caller
    that wants them structured, e.g. a future HTTP-level mapping to a 429)
    on top of the `ToolError` message every write tool already surfaces to
    the client unchanged, the same way `require_scope`/`require_writable_namespace`
    raise a plain `ToolError` today.
    """

    def __init__(self, *, scope: QuotaScope, limit: float, retry_after: float) -> None:
        self.scope: QuotaScope = scope
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(
            f"write quota exceeded for scope={scope!r}: limit={limit:g} writes per window, "
            f"retry after {retry_after:.1f}s"
        )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class QuotaChecker:
    """Enforces the `user`/`namespace`/`token` write quotas on `state`.

    `state` is the same `auth.shared_state.SharedState` (or `http.py`'s
    `_SharedStateHandle` forwarding to one) every `auth.ratelimit.RateLimiter`
    in this process already shares - counters live in their own key space
    (`quota:<scope>:<minute|day>:<key>`), so a quota window can never collide
    with a `RateLimiter`'s own `<name>:<key>` one even if the two happen to
    share a raw key value.

    `*_per_minute`/`*_per_day` are each `0` (off) by default - see the module
    docstring for the exact semantics of each scope and window. `audit` is
    the sink a rejection is recorded to (module docstring) - `None` to only
    log it, never raise for lack of one.
    """

    def __init__(
        self,
        *,
        state: SharedState,
        audit: AuditWriter | None = None,
        user_per_minute: float = 0.0,
        user_per_day: float = 0.0,
        namespace_per_minute: float = 0.0,
        namespace_per_day: float = 0.0,
        token_per_minute: float = 0.0,
        token_per_day: float = 0.0,
    ) -> None:
        self._state = state
        self._audit = audit
        self._limits: dict[QuotaScope, tuple[float, float]] = {
            "user": (user_per_minute, user_per_day),
            "namespace": (namespace_per_minute, namespace_per_day),
            "token": (token_per_minute, token_per_day),
        }

    async def check_write(
        self, *, op: Op, path: str, namespace: str | None, actor: str, client: str
    ) -> None:
        """Raise `QuotaExceeded` if the current write is over any applicable quota.

        Checks `user`, `namespace` then `token`, in that order, each
        per-minute before per-day - the first exceeded window raises
        immediately, so a call never pays for a window it was already going
        to fail on. `namespace` is the caller's own, already-resolved
        namespace string, verbatim - `None` (a `path` that failed to parse)
        skips that scope's check, the same as `user`/`token` do when the
        current request carries neither a principal nor an access token.

        `op`/`path`/`actor`/`client` are never used to key a window - only to
        audit a rejection exactly as the write tool itself would have, had it
        reached `Services.storage` (module docstring).
        """
        principal = current_principal()
        token = get_access_token()
        keys: tuple[tuple[QuotaScope, str | None], ...] = (
            ("user", principal.oid if principal is not None else None),
            ("namespace", namespace),
            ("token", _hash_token(token.token) if token is not None else None),
        )
        for scope, key in keys:
            if key is None:
                continue
            per_minute, per_day = self._limits[scope]
            if per_minute > 0:
                await self._enforce(
                    scope, "minute", key, per_minute, _MINUTE_SECONDS, op, path, actor, client
                )
            if per_day > 0:
                await self._enforce(
                    scope, "day", key, per_day, _DAY_SECONDS, op, path, actor, client
                )

    async def _enforce(
        self,
        scope: QuotaScope,
        window_name: str,
        key: str,
        limit: float,
        window_seconds: float,
        op: Op,
        path: str,
        actor: str,
        client: str,
    ) -> None:
        state_key = f"quota:{scope}:{window_name}:{key}"
        try:
            count, remaining = await self._state.window_hit(
                state_key, window_seconds=window_seconds
            )
        except Exception:
            _logger.warning(
                "quota backend unavailable; failing open for scope=%s window=%s",
                scope,
                window_name,
                exc_info=True,
            )
            return
        if count <= limit:
            record_quota_hit(scope=scope, outcome="allowed")
            return
        record_quota_hit(scope=scope, outcome="rejected")
        _logger.warning(
            "write quota exceeded: scope=%s window=%s limit=%g retry_after=%.1fs",
            scope,
            window_name,
            limit,
            remaining,
        )
        await self._audit_rejection(
            scope=scope,
            window_name=window_name,
            limit=limit,
            retry_after=remaining,
            op=op,
            path=path,
            actor=actor,
            client=client,
        )
        raise QuotaExceeded(scope=scope, limit=limit, retry_after=remaining)

    async def _audit_rejection(
        self,
        *,
        scope: QuotaScope,
        window_name: str,
        limit: float,
        retry_after: float,
        op: Op,
        path: str,
        actor: str,
        client: str,
    ) -> None:
        """One `audit_log` row for a rejected write, if `self._audit` is configured.

        `detail` never carries the quota key that was actually over budget
        (a user's `oid`, the namespace string or a token hash) - only the
        scope name, the window, the limit and the retry-after, same as the
        WARNING log line right above this call already reports.
        """
        if self._audit is None:
            return
        await self._audit.record(
            actor=actor,
            client=client,
            op=op,
            path=path,
            commit_sha=None,
            outcome="rejected_quota",
            detail={
                "scope": scope,
                "window": window_name,
                "limit": limit,
                "retry_after": round(retry_after, 1),
            },
        )
