# SPDX-License-Identifier: AGPL-3.0-only
"""The MCP server exposed over stdio (local) and, later, HTTP (M4, #30+).

`server.build_server` assembles the `mcp.server.mcpserver.MCPServer` from a
`memory_manager.app.Services`; `errors.error_to_dict` is the shared mapping
from vault/queue exceptions to the error shape every tool reports back.
"""

from __future__ import annotations

from memory_manager.mcp.server import INSTRUCTIONS, build_server

__all__ = ["INSTRUCTIONS", "build_server"]
