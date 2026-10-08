# SPDX-License-Identifier: AGPL-3.0-only
"""A controllable Starlette mock of Microsoft Entra ID and the Microsoft Graph
endpoints the Entra facade (ADR-0006) calls - OIDC discovery, authorization
code + PKCE, `client_credentials`, `getMemberGroups`, and `users/delta`.
Shapes are pinned against `docs/research/entra-contract.md`; see `app.py`'s
module docstring for what it does and deliberately does not do (no JWKS, no
real signature verification).

`create_app()` builds the ASGI app. `tests/mock_idp_fixtures.py` wires it
into tests either in-process (`httpx.ASGITransport`, used by this package's
own `test_mock_idp.py`) or as a real subprocess via `__main__.py` (same role
as `tests/http_fixtures.py`'s `run_http_server` for the real server) - the
latter is also what `Containerfile`'s image runs, for the kind E2E (WP-30).
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
