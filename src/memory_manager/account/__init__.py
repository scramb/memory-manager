# SPDX-License-Identifier: AGPL-3.0-only
"""Account-facing state for `/account` (ADR-0008 addendum 2026-10-08, #228/#229).

`account.sessions` is the browser session store; `account.pending` is the matching
pending-login store for `/account/login`; `account.sections` is the page's section
registry; `account.routes`/`account.templates` are the routes and markup
`http.py` mounts on top of all three - see `account.routes`'s own module docstring
for how login is reused unchanged from the three `auth.login*` `Authenticator`s.
"""

from __future__ import annotations

from memory_manager.account.sessions import (
    LOGIN_MODES,
    SessionInfo,
    create,
    csrf_token,
    lookup,
    revoke,
    verify_csrf,
)

__all__ = [
    "LOGIN_MODES",
    "SessionInfo",
    "create",
    "csrf_token",
    "lookup",
    "revoke",
    "verify_csrf",
]
