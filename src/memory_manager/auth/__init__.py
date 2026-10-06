# SPDX-License-Identifier: AGPL-3.0-only
"""Static bearer tokens for the HTTP transport (ADR-0004, #34).

`tokens.py` owns the `static_tokens` table (create/list/revoke/verify);
`verifier.py` adapts `tokens.verify` to the MCP SDK's `TokenVerifier`
protocol so `http.py` can wire it into `MCPServer`/`streamable_http_app`.
"""

from __future__ import annotations

from memory_manager.auth.tokens import (
    ALL_NAMESPACES,
    TokenInfo,
    create_token,
    list_tokens,
    revoke_token,
)
from memory_manager.auth.verifier import StaticTokenVerifier

__all__ = [
    "ALL_NAMESPACES",
    "StaticTokenVerifier",
    "TokenInfo",
    "create_token",
    "list_tokens",
    "revoke_token",
]
