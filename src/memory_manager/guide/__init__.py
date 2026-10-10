# SPDX-License-Identifier: AGPL-3.0-only
"""Generates `src/memory_manager/mcp/instructions_generated.py` from `docs/memory-guide.md`
(#127) - the single source for the server's `instructions` and the `memory_guide` prompt.

stdlib only, and never imports from `memory_manager.mcp`: this package is what generates
`instructions_generated.py`, so it cannot itself depend on the thing it produces.
"""

from __future__ import annotations

from memory_manager.guide.generate import GuideFormatError, build, is_current, parse_guide

__all__ = ["GuideFormatError", "build", "is_current", "parse_guide"]
