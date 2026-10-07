# SPDX-License-Identifier: AGPL-3.0-only
"""The single-admin-password `Authenticator` (ADR-0004 L1, #37).

`PasswordAuthenticator` is the quickstart login method: one password,
hashed with argon2id (`argon2-cffi`, accepted in ADR-0004's addendum and
`docs/adr/0004-auth-model.md`'s guardrail check - the one dependency this
task adds), checked with `PasswordHasher.verify`, which runs in constant
time with respect to the *candidate* password (argon2's own guarantee, not
something this module has to engineer itself) and never needs the plaintext
hash recomputed by anyone but the operator running `memory-manager
hash-password` once, at setup time. The subject is always `ADMIN_SUBJECT`
("admin") - there is exactly one account in this mode.

Brute-force protection (ADR-0004: "brute-force protection needed as in
bring") is two independent in-process, single-replica sliding windows
(`_FailureWindow`, same ADR-0004 caveat the OAuth rate limiting already
carries: "in-process, same as bring"): one per client IP, one global for the
one subject this mode ever has - a failure anywhere counts against both, so
an attacker spreading guesses across source IPs is still capped by the
global window, and a legitimate user behind a shared/rotating IP is still
capped by the global window too, not just their own. Either window at
`_MAX_FAILURES` blocks the *next* attempt outright, even a correct password -
"the 6th attempt is blocked even if it is right" is the point, not an
accident: a correct guess during an active brute-force run must not reset
the clock for the attacker still trying.

Per-IP windows live in `_window_for`'s `by_ip`, bounded to `_MAX_TRACKED_IPS`
entries with the same LRU eviction `auth.ratelimit.RateLimiter` uses for the
same reason: `POST /login` carries no rate limit of its own ahead of this
(`http.py`'s `_LimitsMiddleware` only meters `mcp_path`/`webhook_path`/the
OAuth AS endpoints, not `LOGIN_PATH`), so an unbounded `dict` keyed by
client IP would let an attacker cycling through source addresses grow this
process's memory without ever triggering a single block.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from memory_manager.auth.login import (
    BoundCompleter,
    PendingAuthorization,
    parse_namespace_map,
    parse_namespaces,
    resolve_namespaces,
)
from memory_manager.auth.templates import html_response, login_error_page, login_password_page
from memory_manager.config import ServerConfigError

__all__ = ["PasswordAuthenticator", "hash_password"]

_PASSWORD_FIELD = "password"  # noqa: S105 - a form field name, not a credential

#: Failures allowed within `_WINDOW_SECONDS` before the *next* attempt is blocked
#: outright (ADR-0004's brute-force protection: "5 failures / 10 min").
_MAX_FAILURES = 5
_WINDOW_SECONDS = 10 * 60.0

#: Bound on `_by_ip`'s size - same default and eviction policy as
#: `auth.ratelimit.RateLimiter`'s `max_keys` (see the module docstring).
_MAX_TRACKED_IPS = 10_000

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """An argon2id PHC string for `password` - what `ADMIN_PASSWORD_HASH` must hold.

    Used by both `memory-manager hash-password` (the operator-facing CLI) and
    `tests/auth/test_login.py` (to build a known-good `ADMIN_PASSWORD_HASH` for a test).
    """
    return _hasher.hash(password)


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False
    return True


@dataclass
class _FailureWindow:
    """A plain list of failure timestamps (`time.monotonic()`), trimmed to the last
    `_WINDOW_SECONDS` on every check - good enough for "in-process, single replica"
    (ADR-0004); nothing here survives a restart or is shared across processes."""

    _failures: list[float] = field(default_factory=list)

    def record(self, now: float) -> None:
        self._failures.append(now)

    def blocked(self, now: float) -> bool:
        self._failures[:] = [moment for moment in self._failures if now - moment < _WINDOW_SECONDS]
        return len(self._failures) >= _MAX_FAILURES


def _window_for(
    by_ip: OrderedDict[str, _FailureWindow], client_ip: str, *, max_tracked: int = _MAX_TRACKED_IPS
) -> _FailureWindow:
    """`by_ip[client_ip]`, creating it on first use - bounded to `max_tracked` entries,
    least-recently-used evicted first, the same policy `auth.ratelimit.RateLimiter`
    uses and for the same reason (see this module's docstring)."""
    window = by_ip.get(client_ip)
    if window is None:
        window = _FailureWindow()
        by_ip[client_ip] = window
        if len(by_ip) > max_tracked:
            by_ip.popitem(last=False)
    else:
        by_ip.move_to_end(client_ip)
    return window


class PasswordAuthenticator:
    """`LOGIN_MODE=password`: one admin password, subject always `ADMIN_SUBJECT`."""

    ADMIN_SUBJECT = "admin"

    def __init__(self, *, password_hash: str, namespaces: list[str]) -> None:
        self._password_hash = password_hash
        self._namespaces = namespaces
        self._by_ip: OrderedDict[str, _FailureWindow] = OrderedDict()
        self._global = _FailureWindow()

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> PasswordAuthenticator:
        """Build a `PasswordAuthenticator` from `ADMIN_PASSWORD_HASH`/`LOGIN_NAMESPACES`/
        `LOGIN_NAMESPACE_MAP`.

        Raises `ServerConfigError` if `ADMIN_PASSWORD_HASH` is unset - `LOGIN_MODE=password`
        with no hash configured must refuse to start, not fall back to "no password works".
        """
        password_hash = environ.get("ADMIN_PASSWORD_HASH")
        if not password_hash:
            raise ServerConfigError(
                "ADMIN_PASSWORD_HASH is required when LOGIN_MODE=password - generate one "
                "with 'memory-manager hash-password' (reads the password from stdin)"
            )
        namespace_map = parse_namespace_map(environ.get("LOGIN_NAMESPACE_MAP"))
        default_namespaces = parse_namespaces(environ.get("LOGIN_NAMESPACES"))
        namespaces = resolve_namespaces(
            [cls.ADMIN_SUBJECT], namespace_map=namespace_map, default=default_namespaces
        )
        return cls(password_hash=password_hash, namespaces=namespaces)

    async def handle(
        self, request: Request, pending: PendingAuthorization, complete: BoundCompleter
    ) -> Response:
        if request.method == "GET":
            return html_response(
                login_password_page(
                    pending_id=pending.id,
                    client_name=pending.client_name,
                    redirect_uri=pending.redirect_uri,
                )
            )

        form = await request.form()
        password = str(form.get(_PASSWORD_FIELD, ""))
        client_ip = request.client.host if request.client is not None else "unknown"
        now = time.monotonic()
        window = _window_for(self._by_ip, client_ip)

        if window.blocked(now) or self._global.blocked(now):
            return html_response(
                login_password_page(
                    pending_id=pending.id,
                    client_name=pending.client_name,
                    redirect_uri=pending.redirect_uri,
                    error="Too many failed attempts. Try again in a few minutes.",
                ),
                status_code=429,
            )

        if not _verify_password(self._password_hash, password):
            window.record(now)
            self._global.record(now)
            return html_response(
                login_password_page(
                    pending_id=pending.id,
                    client_name=pending.client_name,
                    redirect_uri=pending.redirect_uri,
                    error="Incorrect password.",
                ),
                status_code=401,
            )

        redirect_url = await complete(self.ADMIN_SUBJECT, self._namespaces)
        if redirect_url is None:
            return html_response(
                login_error_page("This login link expired or was already used."),
                status_code=400,
            )
        return RedirectResponse(redirect_url, status_code=302)
