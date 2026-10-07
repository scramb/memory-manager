# SPDX-License-Identifier: AGPL-3.0-only
"""The two scope strings this server ever grants or requires.

A standalone leaf module on purpose: both `memory_manager.auth` (PRM's
`scopes_supported`, OAuth's default/valid scopes) and
`memory_manager.mcp.authz` (`require_scope` inside each write tool) need
these two constants, but `auth`'s package `__init__` eagerly imports
`auth.prm`, so `auth.prm` importing them straight from `mcp.authz` made
`memory_manager.auth` and `memory_manager.mcp.authz` import each other
(#118). Neither side needs anything else from here, so this module has no
imports of its own and breaks that cycle for both.
"""

from __future__ import annotations

__all__ = ["READ_SCOPE", "WRITE_SCOPE"]

READ_SCOPE = "memory:read"
WRITE_SCOPE = "memory:write"
