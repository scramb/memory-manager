# SPDX-License-Identifier: AGPL-3.0-only
"""Static bearer tokens and Protected Resource Metadata for the HTTP transport
(ADR-0004, #34, #35).

`tokens.py` owns the `static_tokens` table (create/list/revoke/verify);
`verifier.py` adapts `tokens.verify` to the MCP SDK's `TokenVerifier`
protocol so `http.py` can wire it into `MCPServer`/`streamable_http_app`.
`prm.py` serves RFC 9728 Protected Resource Metadata for `/mcp`.
"""

from __future__ import annotations

from memory_manager.auth.prm import (
    RESOURCE_NAME,
    SCOPE_CHALLENGE,
    WELL_KNOWN_ROOT_PATH,
    build_protected_resource_metadata,
    path_suffixed_well_known_path,
    serve_protected_resource_metadata,
)
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
    "RESOURCE_NAME",
    "SCOPE_CHALLENGE",
    "WELL_KNOWN_ROOT_PATH",
    "StaticTokenVerifier",
    "TokenInfo",
    "build_protected_resource_metadata",
    "create_token",
    "list_tokens",
    "path_suffixed_well_known_path",
    "revoke_token",
    "serve_protected_resource_metadata",
]
